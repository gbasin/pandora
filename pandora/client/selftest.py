"""`pandora selftest`: one real submission through the whole routed path.

What it proves, end to end and against the real worker: the pnpm shim claims a
command in a repository enrolled for the test, a client daemon running from
this checkout freezes and ships the worktree over SSH, the engine admits a
run, Incus clones a golden, the command executes, and the receipt comes home.
Nothing is simulated, which is what makes this `selftest` and not a unit test:
it costs one small incus run on the worker, attributed to a client named
`e2e-<host>`.

Everything it owns lives under one `mkdtemp`: a scratch client configuration
pointing at the real worker, a scratch state directory the test daemon alone
holds, and a scratch git repository whose `pandora.toml` claims a `selftest`
job that echoes a marker -- or, with `--update`, writes one file back through
the write-back path. The live daemon, the live configuration and the live
state directory are read, never written, and the test refuses to run with a
state directory that resolves to either of them.

The scratch repository declares the `[worker]` toolchain of a repository the
caller already enrolled -- minus `prepare_command`, which is not fingerprinted
and would run the enrolled repository's install against the wrong source. The
fingerprint is therefore the golden the worker already has, and the run costs
a clone rather than a build. That is also why the check is made before the
submission: asking the fingerprint of a golden that is absent would trigger a
build of the borrowed toolchain against the selftest source, which cannot
succeed, and the next real run would pay for the rebuild. With no warm
candidate the test declares a minimal toolchain of its own and lets the run
build it.

Exit: 0 the path worked; 1 a run failed or its receipt did not arrive; 70 the
path could not be exercised (no worker host configured, the worker
unreachable, the test daemon never answered, a live state directory named).
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

from ..config import loader
from ..engine.runner import toolchain_of
from ..errors import ConfigError, PandoraError, UnknownSchema
from ..executor import incus
from ..exits import INFRA
from . import doctor, settings
from .worker import Worker

PACKAGE_HOME = str(Path(__file__).resolve().parents[2])
REPO_NAME = 'pandora-selftest'
MARKER = 'pandora selftest ran on the worker'
WRITEBACK_PATH = 'selftest-out.txt'
WRITEBACK_TEXT = 'written by pandora selftest --update on the worker'

# The toolchain the scratch repository declares when no enrolled repository
# lends a warm golden. Every field is the fingerprint's, so a selftest golden
# is rebuilt only when this table changes. `docker.io` is required rather than
# minimal: `prepare` starts dockerd in every golden it builds.
MINIMAL_WORKER = {'base_image': 'images:ubuntu/26.04',
                  'packages': ['docker.io', 'ca-certificates', 'git', 'rsync'],
                  'node_version': '', 'pnpm_version': '', 'service_images': [],
                  'install_command': '', 'prepare_command': '',
                  'source_id': REPO_NAME, 'env': {}, 'workdir': '/work'}

PANDORA_TOML = '''version = 1

[repo]
name = "{repo}"
entrypoints = ["pnpm"]

{worker}

[[jobs]]
id = "selftest"
summary = "The end-to-end smoke run"
size = "small"
args = "optional"
timeout_minutes = 5
forms = [{{ prefix = ["selftest"] }}]
options = [{{ name = "--update", sets = "update", forward = true, writeback = true }}]
run = {{ argv = ["sh", "selftest.sh", "{{args}}"] }}
outputs = [{{ kind = "writeback", requires_option = "update", paths = ["{writeback}"] }}]
'''

SELFTEST_SH = '''#!/bin/sh
# The scratch repository's runner. The marker on stdout proves the command
# executed on the worker; the file proves the write-back path when the job's
# --update option armed it.
echo "{marker}"
if [ "${{1:-}}" = "--update" ]; then
    echo "{writeback_text}" > "{writeback}"
fi
'''

CLIENT_TOML = '''[worker]
host = {host}
engine_root = {engine_root}

[client]
state = {state}
name = {name}

[notify]
enabled = false
'''


class SelftestError(Exception):
    """The path could not be exercised or asserted. `exit` is the verb's code."""

    def __init__(self, message, exit=INFRA):
        super().__init__(message)
        self.exit = exit
        self.report = None


def notice(text):
    sys.stderr.write('pandora: selftest: ' + text + '\n')
    sys.stderr.flush()


def read_real_config(path):
    """The caller's client configuration, read tolerantly.

    `settings.load` is the strict path every verb uses, and a configuration
    written by a newer Pandora may name keys this checkout does not know --
    this verb borrows only `[worker]` and `[[repos]]`, so it reads the file as
    TOML rather than refuse a configuration it does not have to write.
    """
    path = settings.path_of(path)
    try:
        raw = tomllib.loads(path.read_text())
    except OSError as error:
        raise SelftestError('cannot read the client configuration %s: %s' % (path, error))
    except tomllib.TOMLDecodeError as error:
        raise SelftestError('%s is not valid TOML: %s' % (path, error))
    worker = raw.get('worker') or {}
    return {'path': str(path), 'worker': worker, 'repos': list(raw.get('repos') or []),
            'client': raw.get('client') or {}}


def check_state_dir(state, real):
    """Resolve the test daemon's state directory, or refuse a live one.

    The default live directory and the one the caller's configuration names
    are the two that hold the live daemon's lock; a test daemon must never
    take either. Anything else is honored, so `--state` can pin the scratch
    for debugging. `mkdtemp` when `state` is None.
    """
    forbidden = {settings.DEFAULT_STATE.expanduser().resolve()}
    live_state = (real.get('client') or {}).get('state')
    if live_state:
        forbidden.add(Path(live_state).expanduser().resolve())
    if state is None:
        return None
    resolved = Path(state).expanduser().resolve()
    if resolved in forbidden:
        raise SelftestError('%s is the live state directory; the test daemon gets '
                            'a scratch one' % resolved)
    return resolved


def e2e_name(hostname=None):
    """The client name the engine's ledger records for the test runs."""
    host = re.sub(r'[^A-Za-z0-9._@+-]', '-',
                  (hostname or socket.gethostname()).split('.')[0] or 'host')
    name = ('e2e-' + host)[:64]
    return name if settings.CLIENT_NAME.fullmatch(name) else 'e2e-selftest'


def render_worker(spec):
    """The `[worker]` table of a toolchain dict, as TOML.

    String values go through `json.dumps`: a JSON string is a TOML basic
    string, which keeps an `install_command` holding newlines valid.
    """
    lines = ['[worker]']
    for key in ('base_image', 'node_version', 'pnpm_version', 'install_command',
                'prepare_command', 'source_id', 'workdir'):
        value = spec.get(key)
        if value:
            lines.append('%s = %s' % (key, json.dumps(value)))
    for key in ('packages', 'service_images'):
        lines.append('%s = [%s]' % (key, ', '.join(json.dumps(item)
                                                  for item in spec.get(key) or [])))
    env = spec.get('env') or {}
    if env:
        lines += ['', '[worker.env]']
        lines += ['%s = %s' % (key, json.dumps(value)) for key, value in sorted(env.items())]
    return '\n'.join(lines)


def repo_toml(worker_spec):
    """The scratch repository's `pandora.toml`: one job, one claimed form."""
    return PANDORA_TOML.format(repo=REPO_NAME, worker=render_worker(worker_spec),
                               writeback=WRITEBACK_PATH)


def write_repo(root, worker_spec):
    """A git repository claiming `pnpm selftest`. Returns its pandora.toml's path."""
    root.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(['git', 'init', '-q', '-b', 'main', str(root)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise SelftestError('git init in the scratch repository failed: %s'
                            % (proc.stderr or '').strip()[:200])
    (root / 'selftest.sh').write_text(SELFTEST_SH.format(marker=MARKER,
                                                       writeback=WRITEBACK_PATH,
                                                       writeback_text=WRITEBACK_TEXT))
    (root / 'selftest.sh').chmod(0o755)
    toml = root / loader.FILENAME
    toml.write_text(repo_toml(worker_spec))
    return toml


def write_client_config(path, *, host, engine_root, state, name):
    """The scratch `config.toml`: the real worker, the scratch state, the e2e name."""
    path.write_text(CLIENT_TOML.format(host=json.dumps(host),
                                       engine_root=json.dumps(engine_root),
                                       state=json.dumps(str(state)),
                                       name=json.dumps(name)))
    return path


def borrowed_toolchains(repos, *, say=notice):
    """[(label, spec)] the enrolled repositories' `[worker]` tables, loadable ones.

    `prepare_command` is dropped: it is not part of the golden's fingerprint,
    so dropping it keeps the golden name, and it is the enrolled repository's
    own build step, which must never run against the selftest source.
    """
    found = []
    for repo in repos:
        try:
            config = loader.load_for(repo['root'], repo.get('config') or None)
        except (ConfigError, UnknownSchema, OSError) as error:
            say('enrolled repository %s unreadable (%s); trying the next'
                % (repo.get('name') or repo.get('root'), error))
            continue
        spec = dict(config['worker'])
        spec['prepare_command'] = ''
        found.append((repo.get('name') or repo['root'], spec))
    return found


def golden_warm(link, fingerprint):
    """True when `golden-<fingerprint>` exists with its `warm` snapshot.

    The same test `IncusDriver.prepare` makes, over the same SSH link the
    daemon uses: a golden that is absent or has no `warm` snapshot is not
    reusable, and a submission naming it would trigger a build against the
    selftest source.
    """
    driver = incus.IncusDriver()
    code, out, _ = link.run(driver.base + ['snapshot', 'list', 'golden-' + fingerprint,
                                         '--format', 'csv'],
                            timeout=60, check=False)
    return code == 0 and any(line.split(',')[0] == 'warm' for line in out.splitlines())


def choose_toolchain(candidates, link, *, say=notice):
    """(spec, label, warm) for the scratch repository's `[worker]` table.

    The first enrolled toolchain whose golden is already built wins: the run
    then costs a clone. With none warm, the minimal toolchain is declared and
    the run builds it -- slow the first time, and afterward warm like any
    other. A borrowed fingerprint is never submitted cold, because its
    `install_command` is the enrolled repository's own and cannot run against
    this source.
    """
    for label, spec in candidates:
        fingerprint = toolchain_of(spec).fingerprint()
        if golden_warm(link, fingerprint):
            return spec, 'borrowed from %s (golden-%s)' % (label, fingerprint[:12]), True
        say('golden-%s for %s is not on the worker; trying the next'
            % (fingerprint[:12], label))
    spec = dict(MINIMAL_WORKER)
    fingerprint = toolchain_of(spec).fingerprint()
    return spec, 'minimal (golden-%s)' % fingerprint[:12], golden_warm(link, fingerprint)


# -- the run -----------------------------------------------------------------


def clean_env(environ):
    """The caller's environment minus every `PANDORA_*` name.

    `PANDORA_CONFIG` is set back per subprocess; the rest -- `PANDORA_OFF`,
    `PANDORA_WHERE`, `PANDORA_ROUTE_DEPTH`, `PANDORA_HOME` -- would change what
    the test measures, so none of it may leak in.
    """
    return {key: value for key, value in environ.items()
            if not key.startswith('PANDORA_')}


def wait_socket(sock_path, proc, *, timeout=30.0):
    """The test daemon's socket answering `ping`, or SelftestError."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SelftestError('the test daemon exited %d before it answered; its log is '
                                'in the scratch state directory' % proc.returncode)
        try:
            doctor.ping(sock_path, timeout=2.0)
            return
        except OSError:
            time.sleep(0.1)
    raise SelftestError('the test daemon never answered on %s' % sock_path)


def start_daemon(bin_dir, state, config, log_path, env):
    """`bin/pandora --state S --config C daemon` as a subprocess. Returns the Popen."""
    handle = open(log_path, 'wb')
    proc = subprocess.Popen([str(bin_dir / 'pandora'), '--state', str(state),
                             '--config', str(config), 'daemon'],
                            stdout=subprocess.DEVNULL, stderr=handle, env=env)
    # The handle is the child's; closing ours leaks nothing and leaves the file
    # complete for a later reader.
    handle.close()
    return proc


def stop_daemon(proc, *, timeout=15.0):
    """SIGTERM, then SIGKILL if the drain took too long. Returns the exit code."""
    if proc.poll() is not None:
        return proc.returncode
    proc.terminate()
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait(timeout=10)


def submit(repo, argv, env, *, timeout):
    """One `pnpm <argv>` through the real shim. Returns (exit, stdout, stderr, seconds)."""
    started = time.monotonic()
    try:
        proc = subprocess.run(['pnpm', *argv], cwd=str(repo), env=env,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SelftestError('`pnpm %s` did not exit within %ds; the run was not '
                            'canceled and may still be on the worker'
                            % (' '.join(argv), timeout))
    return proc.returncode, proc.stdout, proc.stderr, round(time.monotonic() - started, 2)


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def run_rows(state):
    """Every `<state>/runs/*/meta.json`, newest first."""
    rows = []
    for meta in sorted((Path(state) / 'runs').glob('*/meta.json')):
        row = read_json(meta)
        if row is not None:
            row['_dir'] = str(meta.parent)
            rows.append(row)
    rows.sort(key=lambda row: row.get('started') or 0, reverse=True)
    return rows


def receipt(state, argv, *, wait=10.0):
    """(meta, result) for the run whose argv is `pnpm <argv>`, waiting briefly for it."""
    want = ['pnpm', *argv]
    deadline = time.monotonic() + wait
    while True:
        for meta in run_rows(state):
            if meta.get('argv') == want:
                result = read_json(Path(meta['_dir']) / 'result.json')
                if result is not None:
                    return meta, result
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    metas = run_rows(state)
    for meta in metas:
        if meta.get('argv') == want:
            return meta, read_json(Path(meta['_dir']) / 'result.json')
    raise SelftestError('no run of `%s` in the test daemon\'s rows (%d found)'
                        % (' '.join(want), len(metas)), exit=1)


def run_report(meta, result, wall):
    """The timings one submission produced, for the summary and --json."""
    durations = (result or {}).get('durations') or {}
    return {'id': meta.get('id'), 'argv': meta.get('argv'), 'wall_seconds': wall,
            'queue_ms': meta.get('queue_ms'), 'exit': result.get('cli_exit'),
            'outcome': result.get('outcome'), 'lane': result.get('lane'),
            'pre_accept': meta.get('pre_accept') or {}, 'engine': durations}


def phases_line(record):
    """The compact timing line: pre-accept on the caller, then the engine's phases."""
    pre = record['pre_accept']
    engine = record['engine']
    parts = ['%s %gs' % (name, pre[name]) for name in ('freeze', 'ship', 'submit')
             if pre.get(name) is not None]
    if record.get('queue_ms') is not None:
        parts.append('(submit to accepted %d ms)' % record['queue_ms'])
    order = ('queue', 'prepare', 'clone', 'start', 'inject', 'graft', 'git',
             'harden', 'prepare_command', 'boot', 'execute', 'collect', 'destroy')
    engine_parts = ['%s %gs' % (name, engine[name]) for name in order
                    if engine.get(name) is not None]
    return 'caller: %s\n  engine: %s' % (', '.join(parts), ', '.join(engine_parts))


def render(report):
    """The human summary, one line per fact."""
    lines = ['worker %s, engine root %s' % (report['host'], report['engine_root']),
             'client %s' % report['client'],
             'toolchain %s' % report['toolchain']]
    for record in report['runs']:
        lines.append('run %s `pnpm %s`: %s, exit %s, %.1fs caller wall'
                     % (record['id'], ' '.join((record['argv'] or [])[1:]),
                        record['outcome'], record['exit'], record['wall_seconds']))
        lines.append('  ' + phases_line(record))
    if report.get('writeback'):
        lines.append('write-back: %s landed in the scratch worktree' % report['writeback'])
    lines.append('%s in %.1fs%s' % ('OK' if report['ok'] else 'FAILED', report['seconds'],
                                    '' if report['kept'] else ' (scratch removed)'))
    if report['kept']:
        lines.append('scratch kept at %s' % report['kept'])
    return '\n'.join(lines)


# -- the verb ------------------------------------------------------------------

def run(*, state=None, config_path=None, host=None, update=False, keep=False,
        timeout=900.0, say=notice, environ=None):
    """Drive the whole path once. Returns (report, exit code)."""
    environ = os.environ if environ is None else environ
    real = read_real_config(config_path)
    host = host or (real['worker'].get('host') or '')
    if not host:
        raise SelftestError('no worker host: %s has no [worker] host, and no --host '
                            'was given' % real['path'])
    engine_root = real['worker'].get('engine_root') or 'pandora-engine'
    state = check_state_dir(state, real)
    root = Path(tempfile.mkdtemp(prefix='pandora-selftest-')).resolve()
    if state is None:
        state = root / 'state'
    state = Path(state)
    report = {'host': host, 'engine_root': engine_root, 'client': e2e_name(),
              'state': str(state), 'runs': [], 'ok': False, 'kept': None,
              'seconds': 0.0}
    daemon_proc, worker, started = None, None, time.monotonic()
    try:
        state.mkdir(parents=True, exist_ok=True)
        config = write_client_config(root / 'config.toml', host=host,
                                     engine_root=engine_root, state=state,
                                     name=report['client'])
        daemon_env = dict(clean_env(environ), PANDORA_CONFIG=str(config))
        try:
            worker = Worker(host, state=state, engine_root=engine_root,
                            client=report['client'])
            candidates = borrowed_toolchains(real['repos'], say=say)
            spec, report['toolchain'], warm = choose_toolchain(candidates, worker.link,
                                                             say=say)
        except PandoraError as error:
            raise SelftestError('the worker could not be asked about its goldens: %s'
                                % error)
        if not warm:
            say('the chosen toolchain has no warm golden; this run builds it, which '
                'is minutes, not seconds')
        repo = root / 'repo'
        write_repo(repo, spec)
        say('scratch %s: repo %s, state %s' % (root, repo, state))

        daemon_started = time.monotonic()
        daemon_proc = start_daemon(Path(PACKAGE_HOME) / 'bin', state, config,
                                   root / 'daemon.log', daemon_env)
        wait_socket(state / 'client.sock', daemon_proc)
        report['daemon_start_seconds'] = round(time.monotonic() - daemon_started, 2)
        say('test daemon pid %d on %s (%.1fs)'
            % (daemon_proc.pid, state / 'client.sock', report['daemon_start_seconds']))

        enrolled = subprocess.run([str(Path(PACKAGE_HOME) / 'bin' / 'pandora'),
                                   '--state', str(state), '--config', str(config),
                                   'enroll', str(repo)],
                                  capture_output=True, text=True, env=daemon_env,
                                  timeout=60)
        if enrolled.returncode != 0:
            raise SelftestError('enroll of the scratch repository failed (%d): %s'
                                % (enrolled.returncode,
                                   (enrolled.stderr or enrolled.stdout).strip()[:400]))

        shim_env = dict(daemon_env, PANDORA_SESSION=REPO_NAME)
        # The real shim first on PATH, then the caller's PATH, then a stub
        # `pnpm` so the walk never lacks a "real" one on a pnpm-less machine.
        stub = root / 'realbin'
        stub.mkdir()
        stub_pnpm = stub / 'pnpm'
        stub_pnpm.write_text('#!/bin/sh\n'
                             'echo "pandora selftest: unclaimed command reached the stub" '
                             '>&2\nexit 127\n')
        stub_pnpm.chmod(0o755)
        shim_env['PATH'] = '%s:%s:%s' % (Path(PACKAGE_HOME) / 'bin',
                                         environ.get('PATH') or '', stub)

        submissions = [['selftest']]
        if update:
            submissions.append(['selftest', '--update'])
        for argv in submissions:
            code, out, err, wall = submit(repo, argv, shim_env, timeout=timeout)
            meta, result = receipt(state, argv)
            record = run_report(meta, result or {}, wall)
            report['runs'].append(record)
            if MARKER not in (out or ''):
                say('the run\'s marker is missing from its stdout; stdout tail: %s'
                    % (out or '')[-300:])
            if code != 0 or (result or {}).get('outcome') != 'passed':
                say('run %s failed: exit %s, outcome %s; stderr tail: %s'
                    % (record['id'], code, (result or {}).get('outcome'), (err or '')[-400:]))
                raise SelftestError('run %s failed: exit %s, outcome %s'
                                    % (record['id'], code, (result or {}).get('outcome')),
                                    exit=code if code else 1)
        if update:
            written = repo / WRITEBACK_PATH
            if not written.is_file() or WRITEBACK_TEXT not in written.read_text():
                raise SelftestError('the write-back never landed: %s is missing or '
                                    'differs' % written, exit=1)
            report['writeback'] = WRITEBACK_PATH
        report['ok'] = True
        return report, 0
    except SelftestError as error:
        error.report = report
        raise
    finally:
        report['seconds'] = round(time.monotonic() - started, 2)
        if daemon_proc is not None:
            code = stop_daemon(daemon_proc)
            say('test daemon stopped (%s)' % ('exit %d' % code if isinstance(code, int)
                                              else code))
        if worker is not None:
            try:
                worker.close()
            except OSError:
                pass
        if keep:
            report['kept'] = str(root)
        else:
            shutil.rmtree(root, ignore_errors=True)
