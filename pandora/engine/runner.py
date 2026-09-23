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
import dataclasses
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
from .result import facts_from_result, hint_for
from .scheduler import Scheduler, gate
from . import admission, history, retry, turbocache, writeback

RESULT_VERSION = 2
# Which layer reached the verdict. A reader who only trusts `passed` still wants
# to know whether a failure came from the code under test or from us.
LAYERS = ('command', 'watchdog', 'executor', 'engine', 'client')

def submitted_request(paths, run_id):
    """The request as the client sent it, from the attempt's own `request.json`."""
    try:
        return json.loads((paths.attempt(run_id) / 'request.json').read_text())
    except (OSError, ValueError):
        return None


def submitted_plan(paths, run_id):
    """The plan as the client sent it, from the attempt's own `request.json`."""
    return (submitted_request(paths, run_id) or {}).get('plan')


def toolchain_of(spec):
    return Toolchain(base_image=spec['base_image'],
                     packages=tuple(spec['packages']),
                     node_version=spec['node_version'],
                     pnpm_version=spec['pnpm_version'],
                     service_images=tuple(spec['service_images']),
                     install_command=spec['install_command'],
                     source_id=spec['source_id'],
                     env=tuple(sorted(spec['env'].items())),
                     # Absent on every toolchain written before pinning existed,
                     # and an empty `pins` fingerprints as if the field were not
                     # there, so an old attempt still names its own golden.
                     pins=tuple(sorted((spec.get('pins') or {}).items())))


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

    def planout(self, run_id):
        """What a tier-2 plan attempt produced, for its shards to mount."""
        return self.attempt(run_id) / 'planout'

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
    if (row['role'] or 'single') == 'parent':
        # A parent owns instances only through its children. Imported here
        # rather than at the top because the fan-out is written in terms of
        # this module's own steps.
        from .fanout import supervise_parent
        ledger.close()
        return supervise_parent(root, run_id, driver=driver)

    attempt = paths.attempt(run_id)
    attempt.mkdir(parents=True, exist_ok=True)
    plan = row_to_dict(row)
    driver = driver or IncusDriver(root=paths.root)
    # The ledger stores the fields it schedules on; the submitted plan is kept
    # whole beside the attempt, which is where the parts the *executor* needs but
    # the scheduler does not -- the cancel contract -- are read from. No column,
    # no migration, and an attempt written before this existed reads as the
    # default, which is the old behaviour exactly.
    cancel = (submitted_plan(paths, run_id) or {}).get('cancel') or {}
    limits = Limits(memory_mib=row['reservation_mib'] or 2048,
                    ceiling_mib=row['ceiling_mib'] or 4096,
                    cpu_weight=100,
                    cpus_hint=row['cpus_hint'] or 1,
                    wall_seconds=int(plan['env'].get('PANDORA_WALL_SECONDS', 1800)),
                    cancel_signal=cancel.get('signal') or 'SIGKILL',
                    cancel_grace_ms=int(cancel.get('grace_ms') or 0))
    durations, marks = {}, time.monotonic()
    log_handle = paths.log(run_id).open('a', buffering=1)
    instance = None
    outcome, layer, exit_code, evidence = 'infra_failed', 'engine', None, {}
    peak_mib, receipt_dict = 0, None
    # None when the plan arms no write-back; otherwise always a record, so a
    # client can tell "proposed nothing" from "was never asked to".
    proposal = (writeback.incomplete('the run did not pass, so it proposes nothing', None)
                if writeback.patterns_of(plan['outputs']) else None)

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
        # A shard mounts what its parent's plan built, on top of the source it
        # shares with its siblings. It is injected after the source rsync, not
        # before, because that rsync runs with --delete.
        graft = attempt / 'planout'
        if graft.is_dir():
            durations['graft'] = round(
                driver.inject(instance.name, graft, '/work', method='device-rsync-over'), 2)
            marks = time.monotonic()
        # Before the cache and before the command: the repository is part of
        # the source, and a job that declared it must never start without it.
        submitted = submitted_request(paths, run_id) or {}
        if (submitted.get('plan') or {}).get('git') == 'synthetic':
            durations['git'] = round(driver.synthetic_git(
                instance.name, '/work', submitted.get('git_marks') or {},
                'pandora %s' % row['input_id']), 2)
            marks = time.monotonic()
            note('synthetic git repository in %.1fs' % durations['git'])
        # turbo's remote cache, served by this worker on the runs' bridge
        # (`turbocache`). Probed, never required: a run the cache cannot serve
        # is slower, not wrong, so the reason goes in the log and the evidence.
        cache_env, why = turbocache.env_for(paths.root / 'turbo-cache', row['repo'])
        evidence['turbo_cache'] = ({'api': cache_env['TURBO_API'], 'team': cache_env['TURBO_TEAM']}
                                   if cache_env else {'error': why})
        note('turbo cache %s' % (cache_env['TURBO_API'] if cache_env else 'off: ' + why))
        evidence['cgroup'] = driver.harden(instance, limits)
        mark('harden')
        # One line for everything between admission and the command, and the
        # sum is recorded as a phase of its own so the next estimate can use it.
        durations['boot'] = round(sum(durations.get(name, 0) for name in (
            'prepare', 'clone', 'start', 'inject', 'graft', 'git', 'harden')), 2)
        note('instance ready in %.1f s' % durations['boot'])

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
        # The repository's own `[env] set` wins: a job that states its cache
        # meant it, and the engine's default is only a default.
        for key, value in cache_env.items():
            env.setdefault(key, value)
        # PANDORA_CPUS is decided here, not at admission. The hint is a *share*
        # -- host cores divided by the runs actually admitted -- and admission
        # happens before the instance exists, so a run admitted while it was
        # alone would otherwise start believing it owns four cores while three
        # siblings started beside it. The environment of a started process
        # cannot be rewritten, so the only moment this can be right is the last
        # one before the command starts.
        limits = dataclasses.replace(limits, cpus_hint=cpus_now(paths, ledger))
        ledger.update(run_id, cpus_hint=limits.cpus_hint)
        note(running_line(ledger, row, limits.cpus_hint))
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
        if result.outcome == 'lost':
            evidence['cause'] = 'instance-lost'

        ledger.update(run_id, state='collecting', peak_mib=peak_mib)
        collected = collect(driver, instance, plan['outputs'], paths.outputs(run_id))
        evidence['collected'] = collected
        if outcome == 'passed' and exit_code == 0:
            # Only a passing run proposes anything: a failed `--update` wrote
            # fixtures for a run that did not reach its verdict.
            # A proposal that cannot be made is a fact about the write-back,
            # not about the tests, so it never turns the verdict into a failure.
            try:
                proposal = writeback.collect(
                    lambda root, into: pull(driver, instance, root, into),
                    row['source_path'], attempt, plan['outputs']) or proposal
            except OSError as error:
                proposal = writeback.incomplete('the proposal could not be collected: %s'
                                                % error, writeback.INFRA_EXIT)
        mark('collect')
    except (PrepareFailed, CloneFailed, ExecutionFailed, InstanceLost) as error:
        outcome, layer = 'infra_failed', 'executor'
        evidence['error'] = '%s: %s' % (type(error).__name__, error)
        evidence['cause'] = retry.cause_of_exception(error)
        note(evidence['error'])
    except Exception as error:                      # noqa: BLE001 - recorded, never swallowed
        outcome, layer = 'infra_failed', 'engine'
        evidence['error'] = '%s: %s' % (type(error).__name__, error)
        evidence['cause'] = 'engine-error'
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
                    evidence['cause'] = 'destroy-incomplete'
            except Exception as error:              # noqa: BLE001
                receipt_dict = {'clean': False, 'error': str(error)}
                if outcome == 'passed':
                    outcome, layer = 'infra_failed', 'engine'
                    evidence['cause'] = 'destroy-incomplete'
        log_handle.close()

    if outcome == 'passed' and exit_code != 0:
        outcome, layer = 'command_failed', 'command'
    if proposal is not None and outcome != 'passed':
        # A destroy that did not come back clean turned a pass into a failure
        # after the proposal was made. The proposal goes with the pass.
        proposal = writeback.incomplete('the run did not pass, so it proposes nothing', None)
    result_json = write_result(paths, ledger, run_id, outcome=outcome, layer=layer,
                               exit_code=exit_code, peak_mib=peak_mib,
                               durations=durations, evidence=evidence, receipt=receipt_dict,
                               extra={'writeback': proposal} if proposal is not None else None)
    with gate(paths.root):
        store = admission.Store(str(paths.peaks))
        try:
            scheduler = Scheduler(ledger, store, budget_mib=budget_of(paths))
            if peak_mib > 0:
                result_json['learned'] = scheduler.learn(ledger.get(run_id), peak_mib, outcome)
                write_json(paths.result(run_id), result_json)
        finally:
            store.close()
    ledger.close()
    return result_json


def running_line(ledger, row, cpus_hint):
    """`running (typical 4m10s for check; cpus hint 2)`, from the ledger alone.

    The typical time is the median of this job's last few verdicts in the same
    role -- a shard is compared with shards, a whole run with whole runs -- and
    is left out entirely when there are too few of them to mean anything.
    """
    role = row['role'] or 'single'
    try:
        expected = history.typical(ledger, row['repo'], row['job'], role=role)
    except Exception:                               # noqa: BLE001 - a courtesy, never a verdict
        expected = None
    parts = []
    if expected is not None:
        parts.append('typical %s %s %s' % (history.fmt_seconds(expected),
                                           'per shard of' if role == 'shard' else 'for',
                                           row['job']))
    parts.append('cpus hint %d' % cpus_hint)
    return 'running (%s)' % '; '.join(parts)


def cpus_now(paths, ledger):
    """Host cores over the runs admitted *at this instant*, never below one.

    Read under the admission lock so it cannot land between a sibling's
    admission and that sibling's ledger row, which is precisely the window
    that made the slice's proof 7 report 4 and 2 on a four-core box.
    """
    with gate(paths.root):
        store = admission.Store(str(paths.peaks))
        try:
            scheduler = Scheduler(ledger, store, budget_mib=budget_of(paths))
            return scheduler.cpus_hint(scheduler.lanes())
        finally:
            store.close()


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
    # `incus file pull` runs under sudo, so what lands here is owned by root with
    # the instance's own modes. The engine runs as an ordinary user and has to be
    # able to hand these to rsync, so take ownership of what was just pulled.
    if any(state == 'present' for state in got.values()):
        subprocess.run(['sudo', 'chown', '-R', '%d:%d' % (os.getuid(), os.getgid()), str(into)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        subprocess.run(['chmod', '-R', 'u+rwX', str(into)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return got


def pull(driver, instance, relative, into):
    """`/work/<relative>` out of an instance into `into`, owned by this user.

    False when the instance had nothing there. The ownership fix is the same one
    `collect` needs, for the same reason: `incus file pull` runs under sudo.
    """
    guest = '/work/' + relative.lstrip('/') if relative else '/work'
    code, _, _ = driver.incus('file', 'pull', '-r', instance.name + guest, str(into),
                              check=False, timeout=900)
    if code == 0:
        subprocess.run(['sudo', 'chown', '-R', '%d:%d' % (os.getuid(), os.getgid()), str(into)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        subprocess.run(['chmod', '-R', 'u+rwX', str(into)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return code == 0


def write_result(paths, ledger, run_id, *, outcome, layer, exit_code, peak_mib,
                 durations, evidence, receipt, extra=None):
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
        # The earlier whole attempt of this job over the same source tree. The
        # tree digest only: argv, environment and cwd are not compared, so
        # `journey S0-01` and `journey S0-01 --update` share it. Named for that.
        'same_tree_as': item['same_input_as'],
        # The pre-rename key, kept for one release so a script reading
        # `pandora result --json` does not break. Remove after v0.2.
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
        'role': item.get('role') or 'single',
        'shard': ('%s/%s' % (item['shard_index'], item['shard_total'])
                  if item.get('shard_index') else None),
        'parent': item.get('parent'),
        'retry_of': item.get('retry_of'),
    }
    result.update(extra or {})
    # Evidence of non-determinism, recorded where both attempts can be seen.
    # Never a reason to run anything again; only a reason to say so.
    try:
        pair = history.flaky(ledger, run_id, outcome)
    except Exception:                               # noqa: BLE001 - a courtesy, never a verdict
        pair = None
    if pair is not None:
        result['flaky'] = pair
    # Attached at collect time, from evidence already in hand. The two rules
    # that need the Mac -- a gitignored path and worktree drift -- come back as
    # None here and are filled in by the client, which has the worktree.
    result['hint'] = hint_for(facts_from_result(result))
    paths.attempt(run_id).mkdir(parents=True, exist_ok=True)
    write_json(paths.result(run_id), result)
    if pair is not None:
        # After the file, not before: a reader that sees `finished` looks for it.
        ledger.update(run_id, flaky_with=pair['with'])
    return result


def write_json(path, payload):
    """Replace a result file whole. A fan-out parent polls these while they are
    written, and a truncated file read mid-write is a JSON error it reports as
    an engine failure of a child that passed."""
    temp = Path(str(path) + '.tmp')
    temp.write_text(json.dumps(payload, indent=1, sort_keys=True) + '\n')
    temp.replace(path)


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


def disk_headroom(paths, driver=None):
    """Whether the pool has room for another run. See `IncusDriver.capacity`.

    Memory admission has a ledger to reason with; disk has none, so this is a
    floor and nothing cleverer. The floor is the worker's, read from the
    manifest `pandora worker provision` wrote, with an environment override for
    a person who needs to open the gate by hand.
    """
    floor = os.environ.get('PANDORA_DISK_FLOOR_GIB') or ''
    if not floor.isdigit():
        floor = read_text(Path(paths.root) / 'disk_floor') or '4'
    return (driver or IncusDriver(root=paths.root)).capacity(
        floor_gib=int(floor) if floor.isdigit() else 4)


def read_text(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ''


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
        evidence = {'reason': 'supervisor %s gone at engine restart' % (pid or 'never recorded'),
                    'cause': 'supervisor-gone'}
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
    ledger rows, because the rows are what `same_tree_as` and the learned
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
