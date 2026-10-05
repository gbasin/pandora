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
the write-back path. The job declares `git = "synthetic"`, so the receipt
carries the run's git tree and, from a ready worker, a verdict signed by the
worker's key, which the test verifies with `ssh-keygen -Y verify`. The scratch
repository's `origin` is a bare repository in the same scratch directory and
its `pandora.toml` opts in to `[verdicts] publish = true`, so a signed verdict
also goes through the daemon's real publication (parentless commit,
`ls-remote`, push), and the test reads the ref back from that bare origin and
verifies it the way a CI job would. Nothing reaches GitHub. The live
daemon, the live configuration and the live state directory are read, never
written, and the test refuses to run with a state directory that resolves to
either of them.

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

Exit: 0 the path worked; 1 a run failed, its receipt did not arrive, or its
signed verdict did not reach the scratch origin intact; 70 the
path could not be exercised (no worker host configured, the worker
unreachable, the test daemon never answered, a live state directory named).
"""
import base64
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
from ..engine import pinning
from ..errors import ConfigError, PandoraError, UnknownSchema, WorkerUnreachable
from ..exits import INFRA
from . import doctor, settings, verdicts
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

[verdicts]
publish = true

[[jobs]]
id = "selftest"
summary = "The end-to-end smoke run"
size = "small"
args = "optional"
git = "synthetic"
timeout_minutes = 5
forms = [{{ prefix = ["selftest"] }}]
options = [{{ name = "--update", sets = "update", forward = true, writeback = true }}]
run = {{ argv = ["sh", "selftest.sh", "{{args}}"] }}
outputs = [{{ kind = "writeback", requires_option = "update", paths = ["{writeback}"] }}]
'''

QUEUE_TOML = '''

[[jobs]]
id = "qtest"
summary = "The queue-fed fan-out smoke run"
size = "small"
args = "none"
timeout_minutes = 10
forms = [{{ prefix = ["qtest"] }}]
run = {{ argv = ["sh", "qbatch.sh"] }}

[jobs.shards]
strategy = "queue"
default = 2
max = 4
plan = ["sh", "qplan.sh", "{{n}}", "{{plan}}"]
batch_size = 4
batch_attempts = 2

[[jobs.outputs]]
kind = "artifacts"
paths = ["test-results"]
'''

# The scratch plan: twelve ids in a fixed two-way split. The queue flattens it
# into three batches of four, so on two shards whichever finishes first steals
# the third -- the queue's whole point, in miniature.
QPLAN_SH = '''#!/bin/sh
mkdir -p "$(dirname "$2")"
cat > "$2" <<'EOF'
{"inventory": [{"shard": 1, "testIds": ["t01","t02","t03","t04","t05","t06"]},
               {"shard": 2, "testIds": ["t07","t08","t09","t10","t11","t12"]}]}
EOF
'''

# The batch half of the runner contract, in POSIX sh so it holds on a minimal
# toolchain: read the pushed spec, "run" each id, write the report to
# PANDORA_BATCH_REPORT, leave one artifact per batch.
QBATCH_SH = '''#!/bin/sh
ids=$(sed -n 's/.*"testIds" *: *\\[//; s/\\].*//p' "$PANDORA_BATCH_FILE" \\
      | tr ',' '\\n' | tr -d ' "')
seq=$(sed -n 's/.*"batch" *: *//; s/[^0-9].*//p' "$PANDORA_BATCH_FILE" | head -1)
report='{"observed":['
first=1
for id in $ids; do
    echo "ran $id"
    if [ "$first" = 1 ]; then first=0; else report="$report,"; fi
    report="$report{\\"id\\":\\"$id\\"}"
done
report="$report]}"
mkdir -p "$(dirname "$PANDORA_BATCH_REPORT")" test-results
printf '%s\\n' "$report" > "$PANDORA_BATCH_REPORT"
printf '%s\\n' "$ids" > "test-results/batch-$seq.txt"
'''

SELFTEST_SH = '''#!/bin/sh
# The scratch repository's runner. The marker on stdout proves the command
# executed on the worker; the file proves the write-back path when the job's
# --update option armed it. `cpus` is what the instance can see, `threads` is
# the width the run was told it was pinned to: the pin makes them the same
# number. `env` is PANDORA_CPUS, the whole physical cores in that pin. A
# worker that predates PANDORA_CPU_THREADS says `none` and pins by count.
echo "{marker}"
echo "cpus=$(nproc) env=${{PANDORA_CPUS:-0}} threads=${{PANDORA_CPU_THREADS:-none}}"
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


# The one key a selftest verdict may be skipped for: the e2e worker need not be
# marked ready, and a worker that is not ready signs nothing.
SKIP_ALLOWED = 'worker_not_ready'
HEX40 = re.compile(r'[0-9a-f]{40}\Z')
VERDICT_NAMESPACE = 'pandora-verdict'
# The scratch repository's `origin`: a bare repository beside it, inside the
# scratch directory, so publication is real git and never leaves this machine.
ORIGIN_DIR = 'origin.git'
VERDICT_AUTHOR = 'pandora <pandora@localhost>'
VERDICT_FILES = ('payload.json', 'signer', 'verdict.sig')
# How long the daemon's background push may take to record itself after the
# caller has its exit. A push to a local bare repository takes milliseconds.
PUBLISH_WAIT = 10.0


class SelftestError(Exception):
    """The path could not be exercised or asserted. `exit` is the verb's code."""

    def __init__(self, message, exit=INFRA):
        super().__init__(message)
        self.exit = exit
        self.report = None


def pin_agrees(cpus, env, threads):
    """Whether a run's `nproc`, `PANDORA_CPUS` and `PANDORA_CPU_THREADS` agree.

    A worker that sets `PANDORA_CPU_THREADS` pins whole cores: `nproc` is that
    width, and `PANDORA_CPUS` is its cores, from all of them (no SMT) down to
    half (two threads per core). A worker that predates it (`threads` None)
    pins by count, and `nproc` is `PANDORA_CPUS` itself."""
    if threads is None:
        return cpus == env
    # Assumes at most two threads per core; a 4-way SMT host would fail here.
    return cpus == threads and threads <= 2 * env and env <= threads


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


def repo_toml(worker_spec, *, queue=False):
    """The scratch repository's `pandora.toml`: one job, one claimed form."""
    text = PANDORA_TOML.format(repo=REPO_NAME, worker=render_worker(worker_spec),
                               writeback=WRITEBACK_PATH)
    return text + (QUEUE_TOML.format() if queue else '')


def write_repo(root, worker_spec, *, queue=False):
    """A git repository claiming `pnpm selftest` (and `pnpm qtest` when asked).

    Returns its pandora.toml's path."""
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
    if queue:
        (root / 'qplan.sh').write_text(QPLAN_SH)
        (root / 'qplan.sh').chmod(0o755)
        (root / 'qbatch.sh').write_text(QBATCH_SH)
        (root / 'qbatch.sh').chmod(0o755)
    toml = root / loader.FILENAME
    toml.write_text(repo_toml(worker_spec, queue=queue))
    return toml


def write_origin(root, repo):
    """A bare repository at `<root>/origin.git`, added as `repo`'s `origin`."""
    bare = root / ORIGIN_DIR
    for argv in (['git', 'init', '-q', '--bare', str(bare)],
                 ['git', '-C', str(repo), 'remote', 'add', 'origin', str(bare)]):
        proc = subprocess.run(argv, capture_output=True, text=True)
        if proc.returncode != 0:
            raise SelftestError('the scratch origin could not be made (%s): %s'
                                % (' '.join(argv[:3]), (proc.stderr or '').strip()[:200]))
    return bare


def write_client_config(path, *, host, engine_root, state, name):
    """The scratch `config.toml`: the real worker, the scratch state, the e2e name."""
    path.write_text(CLIENT_TOML.format(host=json.dumps(host),
                                       engine_root=json.dumps(engine_root),
                                       state=json.dumps(str(state)),
                                       name=json.dumps(name)))
    return path


def borrowed_toolchains(repos, *, say=notice):
    """[(label, spec, root)] the enrolled repositories' `[worker]` tables, loadable ones.

    `prepare_command` is dropped: it is not part of the golden's fingerprint,
    so dropping it keeps the golden name, and it is the enrolled repository's
    own build step, which must never run against the selftest source. `root`
    is where the repository's lockfiles are read from: the worker folds their
    digests into the golden's name, so the scratch tree carries copies of them.
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
        found.append((repo.get('name') or repo['root'], spec, repo['root']))
    return found


def golden_asker(worker):
    """`ask(spec, lockfiles)` over the worker's own `golden` verb.

    The worker names a golden, not the client: it folds in the base image's
    fingerprint, which only it can resolve. So the warm check asks it, with
    the lockfile digests the scratch tree will carry.
    """
    def ask(spec, lockfiles):
        return worker.engine(['golden'], stdin=json.dumps({'worker': spec,
                                                           'lockfiles': lockfiles}),
                             timeout=90)
    return ask


def asked(ask, spec, lockfiles, say):
    """The `golden` answer, or {} when the worker cannot give one.

    A gateway or engine older than the `golden` verb refuses it, and so does a
    request the worker finds malformed. Either way the selftest cannot know
    which golden is warm, so it is treated as not warm: the minimal toolchain
    builds, which is slow but never wrong. An unreachable worker still raises.
    """
    try:
        answer = ask(spec, lockfiles)
    except WorkerUnreachable:
        raise
    except PandoraError as error:
        say('the worker did not answer `golden` (%s); treating it as not warm'
            % str(error)[:200])
        return {}
    if not isinstance(answer, dict):
        answer = {'ok': False}
    if not answer.get('ok', True):
        say('the worker refused `golden` (%s); treating it as not warm'
            % (answer.get('detail') or answer.get('code') or 'no answer'))
        return {}
    return answer


def choose_toolchain(candidates, ask, *, say=notice):
    """(spec, label, warm, lockfile root or None) for the scratch repository.

    The first enrolled toolchain whose golden is already built wins: the run
    then costs a clone. With none warm, the minimal toolchain is declared and
    the run builds it -- slow the first time, and afterward warm like any
    other. A borrowed toolchain is never submitted cold, because its
    `install_command` is the enrolled repository's own and cannot run against
    this source. `ask` is `golden_asker`'s: the golden name the worker would
    pin for a recipe and lockfile digests, and whether it is warm.

    The lockfiles are read from the enrolled repository's checkout on this
    Mac, which may be behind or ahead of `<engine_root>/src/<repo>/latest`,
    the tree its routed runs last shipped. When they differ, the name asked
    about is not the warm golden, and the selftest falls through to the next
    candidate rather than borrowing the wrong one.
    """
    for label, spec, root in candidates:
        answer = asked(ask, spec, pinning.lockfiles(root), say)
        golden = answer.get('golden') or '?'
        if answer.get('warm'):
            return spec, 'borrowed from %s (%s)' % (label, golden[:19]), True, root
        say('%s for %s is not warm on the worker (lockfiles from %s, which may be '
            'stale against the worker\'s src/<repo>/latest); trying the next'
            % (golden[:19], label, root))
    spec = dict(MINIMAL_WORKER)
    answer = asked(ask, spec, {}, say)
    return (spec, 'minimal (%s)' % (answer.get('golden') or '?')[:19],
            bool(answer.get('warm')), None)


def copy_lockfiles(source, repo):
    """Copy `source`'s root lockfiles into the scratch `repo`, by name."""
    for name in pinning.lockfiles(source):
        shutil.copyfile(Path(source) / name, Path(repo) / name)


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


def verify_signature(payload, signature, signer):
    """None when `ssh-keygen -Y verify` accepts the signature for `signer`, else why not."""
    with tempfile.TemporaryDirectory(prefix='pandora-verdict-') as scratch:
        signers = Path(scratch) / 'allowed_signers'
        signers.write_text('%s namespaces="%s" %s\n'
                           % (VERDICT_NAMESPACE, VERDICT_NAMESPACE, signer.strip()))
        sig = Path(scratch) / 'verdict.sig'
        sig.write_text(signature)
        try:
            proc = subprocess.run(['ssh-keygen', '-Y', 'verify', '-f', str(signers),
                                   '-I', VERDICT_NAMESPACE, '-n', VERDICT_NAMESPACE,
                                   '-s', str(sig)],
                                  input=payload.encode(), capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as error:
            return 'ssh-keygen could not run: %s' % error
    if proc.returncode != 0:
        return ((proc.stderr or proc.stdout).decode('utf-8', 'replace').strip()[:300]
                or 'ssh-keygen exited %d' % proc.returncode)
    return None


def check_verdict(result, job, *, say=notice):
    """What the run's signed verdict says, or SelftestError(exit=1) when it is wrong.

    The run declares `git = "synthetic"`, so the worker knows its tree. A
    passing whole run on a ready worker must come home signed by the key the
    result names; on a worker that is not marked ready it must say so, and
    nothing else. A result with no `tree` key at all is from an engine that
    predates verdicts: noted, not failed, so the selftest still proves the
    rest of the path against it.
    """
    if 'tree' not in result:
        say('the engine wrote no tree to result.json; it predates signed verdicts, '
            'so the verdict check is skipped')
        return 'not checked (engine predates verdicts)'
    tree = result.get('tree')
    if not isinstance(tree, str) or not HEX40.fullmatch(tree):
        raise SelftestError('the run declares git = "synthetic" but its result names tree %r, '
                            'not a 40-hex git tree id' % (tree,), exit=1)
    verdict = result.get('verdict')
    if not verdict:
        skipped = result.get('verdict_skipped')
        if skipped == SKIP_ALLOWED:
            say('verdict skipped: the worker is not marked ready, so it signs nothing')
            return 'none (%s)' % skipped
        raise SelftestError('a passing whole run over tree %s was not signed: '
                            'verdict_skipped is %r; only %r is expected here'
                            % (tree, skipped, SKIP_ALLOWED), exit=1)
    parts = [verdict.get(key) if isinstance(verdict, dict) else None
             for key in ('payload', 'signature', 'signer')]
    if not all(isinstance(item, str) and item for item in parts):
        raise SelftestError('the verdict lacks a payload, signature or signer', exit=1)
    payload, signature, signer = parts
    why = verify_signature(payload, signature, signer)
    if why is not None:
        raise SelftestError('the verdict signature does not verify against its signer: %s'
                            % why, exit=1)
    try:
        body = json.loads(payload)
    except ValueError:
        raise SelftestError('the signed verdict payload is not JSON', exit=1) from None
    expected = {'kind': 'pandora-verdict', 'v': 1, 'tree': tree, 'job': job,
                'outcome': 'passed'}
    wrong = sorted(key for key, value in expected.items()
                   if not isinstance(body, dict) or body.get(key) != value)
    if wrong:
        raise SelftestError('the signed verdict payload disagrees with the run on %s'
                            % ', '.join(wrong), exit=1)
    return 'signed, tree %s' % tree[:12]


def bare_git(origin, *args):
    """(exit, stdout bytes) of one git command against the bare origin."""
    try:
        proc = subprocess.run(['git', '--git-dir', str(origin), *args],
                              capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return 1, b''
    return proc.returncode, proc.stdout


def run_log_text(run_dir):
    """The decoded text of every frame in a run's `log`, in order."""
    try:
        raw = (Path(run_dir) / 'log').read_bytes()
    except OSError:
        return ''
    text = []
    for line in raw.splitlines():
        try:
            frame = json.loads(line)
            if isinstance(frame, dict) and frame.get('b64'):
                text.append(base64.b64decode(frame['b64']).decode('utf-8', 'replace'))
        except (ValueError, TypeError):
            continue
    return ''.join(text)


def wait_publication(run_dir, line, *, wait=PUBLISH_WAIT):
    """The daemon's publish record once it and its log line landed, else what did.

    The daemon writes `verdict-publish.json` and then appends the line, from a
    thread that starts after the exit frame, so both are polled for.
    """
    path = Path(run_dir) / verdicts.RECORD
    deadline = time.monotonic() + wait
    while True:
        record = read_json(path)
        if record is not None and (line is None or line in run_log_text(run_dir)):
            return record
        if time.monotonic() >= deadline:
            return record
        time.sleep(0.1)


def published_refs(origin):
    code, out = bare_git(origin, 'for-each-ref', '--format=%(refname)', 'refs/pandora/')
    return out.decode('utf-8', 'replace').split() if code == 0 else ['(unreadable)']


def check_publication(result, run_dir, origin, *, seen, wait=PUBLISH_WAIT, say=notice):
    """What the daemon's verdict publication did, or SelftestError(exit=1).

    Called after `check_verdict` accepted the receipt. A signed verdict must
    reach the bare origin as `refs/pandora/verdicts/<tree>/<job>`: a parentless
    commit by `pandora <pandora@localhost>` holding exactly payload.json,
    verdict.sig and signer, whose signature `ssh-keygen -Y verify` accepts
    against the signer it carries. A ref this selftest already published
    (`seen`) must take the "already on the remote" path instead. An unsigned
    run on a worker that is not ready must publish nothing. `seen` gains the
    ref. None when there is nothing to check (an engine before verdicts).
    """
    if 'tree' not in result:
        return None
    verdict = result.get('verdict')
    if not verdict:
        if result.get('verdict_skipped') != SKIP_ALLOWED:
            return None
        refs = published_refs(origin)
        if refs:
            raise SelftestError('the run was not signed (%s) but the scratch origin holds %s'
                                % (SKIP_ALLOWED, ', '.join(refs)), exit=1)
        if 'verdict published' in run_log_text(run_dir):
            raise SelftestError('the run was not signed (%s) but its log says a verdict '
                                'was published' % SKIP_ALLOWED, exit=1)
        say('verdict not signed (%s); publication not exercised' % SKIP_ALLOWED)
        return 'not exercised (%s)' % SKIP_ALLOWED
    try:
        tree, job, payload, _, _ = verdicts.parts(result)
    except verdicts.Failed as error:
        raise SelftestError('the verdict cannot be published: %s' % error, exit=1) from None
    ref = verdicts.ref_for(tree, job)
    state = 'present' if ref in seen else 'published'
    line = 'pandora: ' + verdicts.line({'state': state, 'ref': ref, 'reason': None})
    record = wait_publication(run_dir, line, wait=wait)
    if record is None:
        raise SelftestError('no %s beside result.json within %gs: the daemon never '
                            'recorded publishing the verdict for %s'
                            % (verdicts.RECORD, wait, ref), exit=1)
    if record.get('state') != state or record.get('ref') != ref:
        raise SelftestError('the daemon recorded verdict publication %s %s (%s); expected '
                            '%s %s' % (record.get('state'), record.get('ref'),
                                       record.get('reason') or 'no reason', state, ref),
                            exit=1)
    if line not in run_log_text(run_dir):
        raise SelftestError('the run log lacks the line `%s`' % line, exit=1)
    code, out = bare_git(origin, 'rev-parse', '--verify', '--quiet', ref + '^{commit}')
    if code != 0:
        raise SelftestError('the scratch origin has no %s after the daemon said %s'
                            % (ref, state), exit=1)
    commit = out.decode().strip()
    if state == 'published' and record.get('commit') != commit:
        raise SelftestError('the scratch origin\'s %s is %s, not the pushed commit %s'
                            % (ref, commit, record.get('commit')), exit=1)
    _, raw = bare_git(origin, 'cat-file', 'commit', commit)
    headers = raw.split(b'\n\n', 1)[0].decode('utf-8', 'replace').splitlines()
    if any(header.startswith('parent ') for header in headers):
        raise SelftestError('the verdict commit %s on %s has a parent' % (commit, ref), exit=1)
    if not any(header.startswith('author %s ' % VERDICT_AUTHOR) for header in headers):
        raise SelftestError('the verdict commit %s is not authored by %s'
                            % (commit, VERDICT_AUTHOR), exit=1)
    _, listing = bare_git(origin, 'ls-tree', '--name-only', commit)
    names = tuple(sorted(listing.decode('utf-8', 'replace').split()))
    if names != VERDICT_FILES:
        raise SelftestError('the verdict commit %s holds %s, not exactly %s'
                            % (commit, ', '.join(names) or 'nothing',
                               ', '.join(VERDICT_FILES)), exit=1)
    blobs = {}
    for name in VERDICT_FILES:
        code, blobs[name] = bare_git(origin, 'cat-file', 'blob', '%s:%s' % (commit, name))
        if code != 0:
            raise SelftestError('the verdict commit %s has no readable %s' % (commit, name),
                                exit=1)
    if state == 'published' and blobs['payload.json'] != payload.encode():
        raise SelftestError('payload.json on %s is not the receipt\'s signed payload' % ref,
                            exit=1)
    why = verify_signature(blobs['payload.json'].decode('utf-8', 'replace'),
                           blobs['verdict.sig'].decode('utf-8', 'replace'),
                           blobs['signer'].decode('utf-8', 'replace'))
    if why is not None:
        raise SelftestError('the published verdict on %s does not verify against its '
                            'signer: %s' % (ref, why), exit=1)
    seen.add(ref)
    return ('published %s' % ref if state == 'published'
            else 'already on the remote %s' % ref)


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
        if record.get('verdict'):
            lines.append('  verdict: ' + record['verdict'])
        if record.get('publication'):
            lines.append('  publication: ' + record['publication'])
    if report.get('writeback'):
        lines.append('write-back: %s landed in the scratch worktree' % report['writeback'])
    lines.append('%s in %.1fs%s' % ('OK' if report['ok'] else 'FAILED', report['seconds'],
                                    '' if report['kept'] else ' (scratch removed)'))
    if report['kept']:
        lines.append('scratch kept at %s' % report['kept'])
    return '\n'.join(lines)


# -- the verb ------------------------------------------------------------------

def run(*, state=None, config_path=None, host=None, update=False, queue=False,
        keep=False, timeout=900.0, say=notice, environ=None):
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
            spec, report['toolchain'], warm, lockroot = choose_toolchain(
                candidates, golden_asker(worker), say=say)
        except PandoraError as error:
            raise SelftestError('the worker could not be asked about its goldens: %s'
                                % error)
        if not warm:
            say('the chosen toolchain has no warm golden; this run builds it, which '
                'is minutes, not seconds')
        repo = root / 'repo'
        write_repo(repo, spec, queue=queue)
        if lockroot:
            copy_lockfiles(lockroot, repo)
        origin = write_origin(root, repo)
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
        if queue:
            submissions.append(['qtest'])
        seen_refs = set()
        for argv in submissions:
            code, out, err, wall = submit(repo, argv, shim_env, timeout=timeout)
            meta, result = receipt(state, argv)
            record = run_report(meta, result or {}, wall)
            report['runs'].append(record)
            if argv[0] == 'selftest' and MARKER not in (out or ''):
                say('the run\'s marker is missing from its stdout; stdout tail: %s'
                    % (out or '')[-300:])
            if argv[0] == 'selftest':
                seen = re.search(r'cpus=(\d+) env=(\d+) threads=(\d+|none)\b', out or '')
                if not seen or not pin_agrees(
                        int(seen.group(1)), int(seen.group(2)),
                        None if seen.group(3) == 'none' else int(seen.group(3))):
                    raise SelftestError(
                        'the run saw %s thread(s) but was told PANDORA_CPUS=%s, '
                        'PANDORA_CPU_THREADS=%s: the cpuset and the pin disagree'
                        % (seen.groups() if seen else ('?', '?', '?')), exit=1)
            if code != 0 or (result or {}).get('outcome') != 'passed':
                say('run %s failed: exit %s, outcome %s; stderr tail: %s'
                    % (record['id'], code, (result or {}).get('outcome'), (err or '')[-400:]))
                raise SelftestError('run %s failed: exit %s, outcome %s'
                                    % (record['id'], code, (result or {}).get('outcome')),
                                    exit=code if code else 1)
            if argv[0] == 'selftest':
                record['verdict'] = check_verdict(result or {}, 'selftest', say=say)
                publication = check_publication(result or {}, meta['_dir'], origin,
                                                seen=seen_refs, say=say)
                if publication:
                    record['publication'] = publication
            if argv == ['qtest']:
                # The queue's receipt is its verification: every planned id
                # observed exactly once, no batch left dead or dangling.
                verification = (((result or {}).get('evidence') or {})
                                .get('verification') or {})
                record['verification'] = verification
                if not verification.get('verified'):
                    raise SelftestError(
                        'the queue fan-out passed but did not verify: %s'
                        % verification.get('reason'), exit=1)
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
