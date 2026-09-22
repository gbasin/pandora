"""One run, start to receipt, supervised by a process that outlives its caller.

The sequence is the executor interface's, in order, with the ledger written
between every pair of steps so that a supervisor killed anywhere leaves a row
that says what had already happened:

    prepare -> clone -> inject -> harden -> execute -> collect -> destroy

Two properties this file exists to hold:

* **Nothing fabricates a pass.** A run is `passed` only when the command itself
  exited 0 *and* the executor's outcome was `ok` *and* destroy returned a clean
  receipt. Every other path lands on a named non-passing outcome. A missing
  report is `missing`, which is not the same as zero failures.
* **The supervisor is not the caller.** It is started detached, writes its log to
  a file, and the SSH call that started it returns as soon as the run is
  admitted. Killing the client, the daemon or the SSH connection does not touch
  it; only `cancel` does.
"""
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from ..executor.incus import IncusDriver
from ..executor.interface import (CloneFailed, DestroyIncomplete, ExecutionFailed,
                                  InstanceLost, Limits, PrepareFailed, Toolchain)
from .ledger import Ledger, row_to_dict
from .scheduler import Scheduler, gate
from . import admission

RESULT_VERSION = 2
# Which layer reached the verdict. A reader who only trusts `passed` still wants
# to know whether a failure came from the code under test or from us.
LAYERS = ('command', 'watchdog', 'executor', 'engine', 'client')


def toolchain_of(spec):
    return Toolchain(base_image=spec['base_image'],
                     packages=tuple(spec['packages']),
                     node_version=spec['node_version'],
                     pnpm_version=spec['pnpm_version'],
                     service_images=tuple(spec['service_images']),
                     install_command=spec['install_command'],
                     source_id=spec['source_id'],
                     env=tuple(sorted(spec['env'].items())))


class Paths:
    """Everything the engine owns on the worker, derived from one root."""

    def __init__(self, root):
        self.root = Path(root)
        self.runs = self.root / 'runs'
        self.src = self.root / 'src'
        self.ledger = self.root / 'ledger.db'
        self.peaks = self.root / 'peaks.db'

    def attempt(self, run_id):
        return self.runs / run_id

    def log(self, run_id):
        return self.attempt(run_id) / 'log'

    def result(self, run_id):
        return self.attempt(run_id) / 'result.json'

    def outputs(self, run_id):
        return self.attempt(run_id) / 'outputs'

    def ensure(self):
        for path in (self.runs, self.src):
            path.mkdir(parents=True, exist_ok=True)
        return self


def supervise(root, run_id, *, driver=None):
    """Run one admitted attempt to a receipt. Returns the result dictionary."""
    paths = Paths(root).ensure()
    ledger = Ledger(paths.ledger)
    row = ledger.get(run_id)
    if row is None:
        raise SystemExit('no attempt %s' % run_id)
    if row['state'] == 'finished':
        return json.loads(paths.result(run_id).read_text())

    attempt = paths.attempt(run_id)
    attempt.mkdir(parents=True, exist_ok=True)
    plan = row_to_dict(row)
    driver = driver or IncusDriver(root=paths.root)
    limits = Limits(memory_mib=row['reservation_mib'] or 2048,
                    ceiling_mib=row['ceiling_mib'] or 4096,
                    cpu_weight=100,
                    cpus_hint=row['cpus_hint'] or 1,
                    wall_seconds=int(plan['env'].get('PANDORA_WALL_SECONDS', 1800)))
    durations, marks = {}, time.monotonic()
    log_handle = paths.log(run_id).open('a', buffering=1)
    instance = None
    outcome, layer, exit_code, evidence = 'infra_failed', 'engine', None, {}
    peak_mib, receipt_dict = 0, None

    def mark(name):
        nonlocal marks
        durations[name] = round(time.monotonic() - marks, 2)
        marks = time.monotonic()

    def note(text):
        log_handle.write('pandora: ' + text + '\n')

    try:
        # The toolchain was written beside the attempt at submission time, so the
        # golden's identity is fixed by the request rather than by whatever the
        # engine happens to be configured with when the supervisor starts.
        toolchain = toolchain_of(json.loads((attempt / 'toolchain.json').read_text()))
        # `source` is used only on a cold build, to bake `pnpm install` and the
        # service images into the golden. A warm golden ignores it.
        golden = driver.prepare(toolchain, source=row['source_path'], log=note)
        mark('prepare')
        ledger.update(run_id, state='running')
        instance = driver.clone(golden, run_id, limits=limits)
        durations['clone'] = round(instance.clone_seconds, 2)
        durations['start'] = round(instance.start_seconds, 2)
        marks = time.monotonic()
        ledger.update(run_id, instance=instance.name)
        durations['inject'] = round(
            driver.inject(instance.name, row['source_path'], '/work', method='device-rsync'), 2)
        marks = time.monotonic()
        evidence['cgroup'] = driver.harden(instance, limits)
        mark('harden')

        cancelled = {'yes': False}

        def tick():
            """One host-side check per sample: has anyone asked us to stop?"""
            if cancelled['yes']:
                return 'cancel'
            fresh = ledger.get(run_id)
            if fresh is not None and fresh['cancel_requested']:
                cancelled['yes'] = True
                return 'cancel'
            return None

        env = dict(plan['env'])
        env.pop('__toolchain__', None)
        result = driver.execute(instance, plan['argv'], env=env, cwd='/work',
                                limits=limits, on_log=log_handle.write, on_tick=tick)
        durations['execute'] = round(result.seconds, 2)
        marks = time.monotonic()
        peak_mib = (result.usage.memory_peak or 0) // 1048576
        evidence.update({key: value for key, value in result.evidence.items()
                         if key != 'samples'})
        evidence['samples'] = result.evidence.get('samples', [])[-8:]
        exit_code = result.exit_code
        outcome, layer = {
            'ok': ('passed', 'command'),
            'failed': ('command_failed', 'command'),
            'oom': ('oom', 'watchdog'),
            'timeout': ('timed_out', 'watchdog'),
            'cancelled': ('cancelled', 'engine'),
            'lost': ('infra_failed', 'executor'),
        }[result.outcome]

        ledger.update(run_id, state='collecting', peak_mib=peak_mib)
        collected = collect(driver, instance, plan['outputs'], paths.outputs(run_id))
        evidence['collected'] = collected
        mark('collect')
    except (PrepareFailed, CloneFailed, ExecutionFailed, InstanceLost) as error:
        outcome, layer = 'infra_failed', 'executor'
        evidence['error'] = '%s: %s' % (type(error).__name__, error)
        note(evidence['error'])
    except Exception as error:                      # noqa: BLE001 - recorded, never swallowed
        outcome, layer = 'infra_failed', 'engine'
        evidence['error'] = '%s: %s' % (type(error).__name__, error)
        note(evidence['error'])
    finally:
        if instance is not None:
            try:
                receipt = driver.destroy(instance)
                receipt_dict = dict(receipt.__dict__)
                receipt_dict['clean'] = receipt.clean
                durations['destroy'] = round(receipt.seconds, 2)
            except DestroyIncomplete as error:
                receipt_dict = dict(error.receipt)
                receipt_dict['clean'] = False
                # A run whose machine is still on the box has not finished, and
                # the operator must be told even if the tests passed.
                if outcome == 'passed':
                    outcome, layer = 'infra_failed', 'engine'
                    evidence['destroy_error'] = str(error)
            except Exception as error:              # noqa: BLE001
                receipt_dict = {'clean': False, 'error': str(error)}
                if outcome == 'passed':
                    outcome, layer = 'infra_failed', 'engine'
        log_handle.close()

    if outcome == 'passed' and exit_code != 0:
        outcome, layer = 'command_failed', 'command'
    result_json = write_result(paths, ledger, run_id, outcome=outcome, layer=layer,
                               exit_code=exit_code, peak_mib=peak_mib,
                               durations=durations, evidence=evidence, receipt=receipt_dict)
    with gate(paths.root):
        store = admission.Store(str(paths.peaks))
        try:
            scheduler = Scheduler(ledger, store, budget_mib=budget_of(paths))
            if peak_mib > 0:
                result_json['learned'] = scheduler.learn(ledger.get(run_id), peak_mib, outcome)
                paths.result(run_id).write_text(
                    json.dumps(result_json, indent=1, sort_keys=True) + '\n')
        finally:
            store.close()
    ledger.close()
    return result_json


def collect(driver, instance, outputs, into):
    """Pull every declared artifact path out of the instance.

    A declared path that produced nothing is reported as `missing`. It is never
    reported as an empty success, because "the report is not there" and "the
    report says zero failures" are different facts and only one of them is good.
    """
    into = Path(into)
    into.mkdir(parents=True, exist_ok=True)
    got = {}
    for output in outputs:
        if output['kind'] != 'artifacts':
            continue
        for path in output['paths']:
            guest = '/work/' + path.lstrip('/')
            target = into / path
            target.parent.mkdir(parents=True, exist_ok=True)
            code, _, err = driver.incus('file', 'pull', '-r', instance.name + guest,
                                        str(target.parent), check=False, timeout=900)
            got[path] = 'present' if code == 0 and target.exists() else 'missing'
    return got


def write_result(paths, ledger, run_id, *, outcome, layer, exit_code, peak_mib,
                 durations, evidence, receipt):
    row = ledger.finish(run_id, outcome=outcome, exit_code=exit_code, peak_mib=peak_mib,
                        durations=durations, evidence=evidence, receipt=receipt)
    item = row_to_dict(row)
    result = {
        'version': RESULT_VERSION,
        'run_id': run_id,
        'request_id': item['request_id'],
        'repo': item['repo'],
        'job': item['job'],
        'input_id': item['input_id'],
        'same_input_as': item['same_input_as'],
        'argv': item['argv'],
        # The command's own exit, exactly as the instance reported it. -9 means
        # the watchdog or a cancel killed it; it is not the code a caller gets.
        'observed_exit': exit_code,
        # What the client exits with. The command's own code when the command
        # reached a verdict; a named Pandora code otherwise.
        'cli_exit': cli_exit(outcome, exit_code),
        'outcome': outcome,
        'layer': layer,
        'size_class': item['size_class'],
        'reservation_mib': item['reservation_mib'],
        'ceiling_mib': item['ceiling_mib'],
        'cpus_hint': item['cpus_hint'],
        'peak_mib': peak_mib,
        'instance': item['instance'],
        'receipt': receipt,
        'durations': durations,
        'evidence': evidence,
        'created': item['created'],
        'finished': item['finished'],
        'wall_seconds': round((item['finished'] or 0) - item['created'], 2),
    }
    paths.attempt(run_id).mkdir(parents=True, exist_ok=True)
    paths.result(run_id).write_text(json.dumps(result, indent=1, sort_keys=True) + '\n')
    return result


def cli_exit(outcome, exit_code):
    from ..exits import CANCELLED, INFRA
    if outcome in ('passed', 'command_failed'):
        return exit_code if exit_code is not None else INFRA
    if outcome == 'cancelled':
        return CANCELLED
    if outcome in ('oom', 'timed_out'):
        # The run did not produce a verdict. Reporting the command's -9 as if it
        # were its own exit would let a caller mistake a killed run for a test
        # failure, so these get the infrastructure code instead.
        return INFRA
    return INFRA


def budget_of(paths):
    """Host memory the scheduler may hand out, less a floor for the host itself."""
    override = os.environ.get('PANDORA_BUDGET_MIB')
    if override and override.isdigit():
        return int(override)
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemTotal:'):
                total = int(line.split()[1]) // 1024
                return max(admission.FLOOR_MIB, total - 1536)
    except OSError:
        pass
    return 4096


def spawn(root, run_id, *, python=None):
    """Start the supervisor detached, so the SSH call that asked for it can end."""
    paths = Paths(root).ensure()
    attempt = paths.attempt(run_id)
    attempt.mkdir(parents=True, exist_ok=True)
    stderr = (attempt / 'supervisor.err').open('a')
    proc = subprocess.Popen(
        # `--root` is a parser-level option, so it must precede the subcommand;
        # after it, argparse hands the whole thing to the subparser and refuses.
        [python or 'python3', '-m', 'pandora.engine.service',
         '--root', str(paths.root), 'supervise', '--run', run_id],
        stdin=subprocess.DEVNULL, stdout=stderr, stderr=stderr,
        start_new_session=True, cwd=str(Path(__file__).resolve().parents[2]))
    stderr.close()
    return proc.pid


def reconcile(root, *, driver=None):
    """After an engine restart, say honestly what happened to every live row.

    A supervisor whose pid is still alive is re-adopted and left alone. One whose
    pid is gone cannot be resumed -- its `execute` loop held the only handle on
    the instance -- so the row becomes `infra_failed` and its instance is
    destroyed. What it must never become is `passed`.
    """
    paths = Paths(root).ensure()
    ledger = Ledger(paths.ledger)
    driver = driver or IncusDriver(root=paths.root)
    adopted, failed = [], []
    for row in ledger.live():
        pid = row['supervisor_pid']
        if pid and alive(pid):
            adopted.append(row['run_id'])
            continue
        evidence = {'reason': 'supervisor %s gone at engine restart' % (pid or 'never recorded')}
        receipt = None
        if row['instance']:
            try:
                from ..executor.interface import Instance
                gone = driver.destroy(Instance(name=row['instance'], run_id=row['run_id'],
                                               golden=''))
                receipt = dict(gone.__dict__)
                receipt['clean'] = gone.clean
            except Exception as error:               # noqa: BLE001
                receipt = {'clean': False, 'error': str(error)}
        write_result(paths, ledger, row['run_id'], outcome='infra_failed', layer='engine',
                     exit_code=None, peak_mib=row['peak_mib'] or 0,
                     durations=row_to_dict(row)['durations'], evidence=evidence,
                     receipt=receipt)
        failed.append(row['run_id'])
    ledger.close()
    return {'adopted': adopted, 'infra_failed': failed}


def alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def retain(root, *, keep_seconds=86400, keep_failed_seconds=86400):
    """Delete attempt directories older than the retention window.

    A stub on purpose, and named as one: it deletes directories and leaves the
    ledger rows, because the rows are what `same_input_as` and the learned
    reservations are built on and they cost bytes rather than gigabytes.
    """
    import shutil
    paths = Paths(root).ensure()
    ledger = Ledger(paths.ledger)
    removed, kept = [], []
    cutoff = time.time() - keep_seconds
    failed_cutoff = time.time() - keep_failed_seconds
    for row in ledger.recent(limit=10000):
        directory = paths.attempt(row['run_id'])
        if not directory.is_dir() or row['state'] != 'finished':
            continue
        finished = row['finished'] or row['created']
        limit = failed_cutoff if row['outcome'] != 'passed' else cutoff
        if finished < limit:
            shutil.rmtree(directory, ignore_errors=True)
            removed.append(row['run_id'])
        else:
            kept.append(row['run_id'])
    ledger.close()
    return {'removed': removed, 'kept': len(kept)}


def stop_group(pid):
    try:
        os.killpg(int(pid), signal.SIGTERM)
        return True
    except (OSError, ValueError, TypeError):
        return False
