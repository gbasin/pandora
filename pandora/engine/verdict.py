"""A signed statement that a whole run passed over one exact git tree.

A passing remote run on a ready worker produces a verdict: a canonical JSON
payload naming the tree the run saw, the job, its argv, environment and working
directory, and the golden it ran on, signed with an Ed25519 key that only this engine root holds. The client
publishes it as a git ref; a CI job for the same tree verifies the signature
against an allowed-signers file and may skip the work.

The key lives at `<engine_root>/keys/verdict` (0600, directory 0700) and is
generated on the first run that needs it. It never leaves the engine root and
is never injected into an instance: the instance sees `/work`, the attempt's
plan output and nothing else of the engine root. `submit` refuses a source
outside `<engine_root>/src`, and the gateway refuses rsync shapes that reach
`keys`, so neither an instance nor a teammate's client can name the key.

Signing is a courtesy, never a verdict. Every failure here, a missing
`ssh-keygen` included, leaves `verdict` null with `verdict_skipped`
`sign_failed:<reason>`, and the run's own result is unchanged.

Pure standard library plus the `ssh-keygen` binary.
"""
import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

KIND = 'pandora-verdict'
NAMESPACE = 'pandora-verdict'
COMMENT = 'pandora-verdict'
VERSION = 1
TIMEOUT = 30

# The conditions, in the order they are checked. The first one that fails names
# the skip.
NOT_PASSED = 'not_passed'
NOT_WHOLE = 'not_whole'
WORKER_NOT_READY = 'worker_not_ready'
NO_SYNTHETIC_GIT = 'no_synthetic_git'


class SignFailed(Exception):
    """Signing could not complete. The message is the short reason."""


def key_path(engine_root):
    return Path(engine_root).expanduser() / 'keys' / 'verdict'


def signer(engine_root):
    """The public key line, or None when no key exists yet. Never creates one."""
    try:
        line = key_path(engine_root).with_suffix('.pub').read_text().strip()
    except OSError:
        return None
    return line or None


def ensure_key(engine_root):
    """(private key path, public key line), generating the pair on first need.

    Two supervisors can finish at once on a fresh engine root, so generation is
    under a lock and lands by rename: the public half first, then the private
    half, so a private key on disk always has its public line beside it.
    """
    key = key_path(engine_root)
    pub = key.with_suffix('.pub')
    folder = key.parent
    if key.is_file() and pub.is_file():
        return key, pub.read_text().strip()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    with open(folder / '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not (key.is_file() and pub.is_file()):
            stage = Path(tempfile.mkdtemp(dir=str(folder), prefix='.new-'))
            try:
                fresh = stage / 'verdict'
                keygen(['-q', '-t', 'ed25519', '-N', '', '-C', COMMENT, '-f', str(fresh)])
                os.chmod(fresh, 0o600)
                os.replace(fresh.with_suffix('.pub'), pub)
                os.replace(fresh, key)
            finally:
                for leftover in (stage / 'verdict', stage / 'verdict.pub'):
                    try:
                        leftover.unlink()
                    except OSError:
                        pass
                try:
                    stage.rmdir()
                except OSError:
                    pass
    return key, pub.read_text().strip()


def keygen(argv, *, stdin=b''):
    """One `ssh-keygen` call. stdin is always given: a bare `ssh-keygen` prompts."""
    try:
        proc = subprocess.run(['ssh-keygen', *argv], input=stdin, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=TIMEOUT)
    except FileNotFoundError:
        raise SignFailed('ssh-keygen not found') from None
    except subprocess.TimeoutExpired:
        raise SignFailed('ssh-keygen timed out') from None
    if proc.returncode != 0:
        err = proc.stderr.decode('utf-8', 'replace').strip().splitlines()
        raise SignFailed('ssh-keygen exit %d%s' % (proc.returncode,
                                                   ': ' + err[-1][:120] if err else ''))
    return proc.stdout


def canonical(value):
    """`json.dumps(value, sort_keys=True, separators=(',', ':'))` as UTF-8 bytes."""
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')


def env_digest(env):
    """sha256 hex of the canonical JSON of the run's environment mapping, as the
    plan carries it. The payload binds the digest, not the values, so a secret
    in the environment is never published."""
    return hashlib.sha256(canonical(dict(env or {}))).hexdigest()


def payload(*, argv, cwd, engine, env_digest, finished, golden, input_id, job, outcome,
            repo, run_id, tree):
    """The canonical payload bytes: sorted keys, no spaces, UTF-8, no newline."""
    return canonical({'argv': list(argv), 'cwd': cwd, 'engine': engine,
                      'env_digest': env_digest, 'finished': finished, 'golden': golden,
                      'input_id': input_id, 'job': job, 'kind': KIND, 'outcome': outcome,
                      'repo': repo, 'run_id': run_id, 'tree': tree, 'v': VERSION})


def sign(engine_root, data):
    """{payload, signature, signer} over `data`; raises SignFailed."""
    try:
        key, line = ensure_key(engine_root)
    except OSError as error:
        raise SignFailed('key: %s' % (error.strerror or error)) from None
    signature = keygen(['-Y', 'sign', '-f', str(key), '-n', NAMESPACE], stdin=data)
    return {'payload': data.decode('utf-8'),
            'signature': signature.decode('ascii'),
            'signer': line}


def verify(data, signature, signer_line, *, identity=COMMENT):
    """True when `signature` is `signer_line`'s over `data`. For tests and tooling."""
    if isinstance(data, str):
        data = data.encode('utf-8')
    with tempfile.TemporaryDirectory() as folder:
        allowed = Path(folder) / 'allowed_signers'
        allowed.write_text('%s namespaces="%s" %s\n' % (identity, NAMESPACE, signer_line))
        sig = Path(folder) / 'verdict.sig'
        sig.write_text(signature)
        try:
            keygen(['-Y', 'verify', '-f', str(allowed), '-I', identity, '-n', NAMESPACE,
                    '-s', str(sig)], stdin=data)
        except SignFailed:
            return False
    return True


def engine_id():
    """The installed engine version: the digest of the bundle this code runs from.

    A bundle carries its digest in `pandora/.bundle` (`bundle.BOOTSTRAP`), and
    `<engine_root>/bundles/<digest>` is how the worker names engine versions.
    Code run from a checkout has no marker and reads the digest its bundle
    would have.
    """
    package = Path(__file__).resolve().parents[1]
    try:
        marker = (package / '.bundle').read_text().strip()
    except OSError:
        marker = ''
    if marker:
        return marker
    try:
        from . import bundle
        return bundle.payload(package)[0]
    except Exception:                               # noqa: BLE001 - a label, never a verdict
        return 'unknown'


def worker_root():
    """Where `pandora worker canary --mark` wrote the ready state: the worker
    service's own default, `PANDORA_WORKER_ROOT` or `~/pandora`."""
    return Path(os.environ.get('PANDORA_WORKER_ROOT') or Path.home() / 'pandora')


def ready_state(root=None):
    """`ready`, or what the ready state is instead.

    The state file is the one `canary --mark` wrote. A kernel other than the one
    the canary passed on reads `drifted`, as `pandora worker status` says it.
    Package drift needs a survey of the host and is left to `status`: drift
    after `canary --mark` does not stop signing until the next canary.
    """
    try:
        state = json.loads((Path(root or worker_root()) / 'worker' / 'state.json').read_text())
    except (OSError, ValueError):
        return 'unprovisioned'
    current = state.get('state') or 'unprovisioned'
    if current == 'ready' and state.get('kernel'):
        try:
            kernel = Path('/proc/sys/kernel/osrelease').read_text().strip()
        except OSError:
            kernel = ''
        if kernel and kernel != state['kernel']:
            return 'drifted'
    return current


def skip_reason(*, outcome, role, ready, tree):
    """The first signing condition that fails, or None when all hold."""
    if outcome != 'passed':
        return NOT_PASSED
    if (role or 'single') != 'single':
        return NOT_WHOLE
    if ready != 'ready':
        return WORKER_NOT_READY
    if not tree:
        return NO_SYNTHETIC_GIT
    return None


def decide(engine_root, row, *, outcome, tree, finished, golden, ready=None):
    """The three result fields: `tree`, `verdict`, `verdict_skipped`.

    `row` is the attempt's ledger row as a dictionary. `ready` is the worker's
    ready state, read from the state file when not given. Never raises.
    """
    answer = {'tree': tree, 'verdict': None, 'verdict_skipped': None}
    try:
        role = row.get('role') or 'single'
        if ready is None and outcome == 'passed' and role == 'single':
            ready = ready_state()
        reason = skip_reason(outcome=outcome, role=role, ready=ready, tree=tree)
        if reason:
            answer['verdict_skipped'] = reason
            return answer
        if not golden:
            raise SignFailed('golden fingerprint unknown')
        data = payload(argv=row['argv'], cwd=str(row.get('cwd') or ''), engine=engine_id(),
                       env_digest=env_digest(row.get('env')), finished=finished,
                       golden=golden, input_id=row['input_id'], job=row['job'],
                       outcome=outcome, repo=row['repo'], run_id=row['run_id'], tree=tree)
        answer['verdict'] = sign(engine_root, data)
    except SignFailed as error:
        answer['verdict_skipped'] = 'sign_failed:%s' % error
    except Exception as error:                      # noqa: BLE001 - never fatal to a run
        answer['verdict_skipped'] = 'sign_failed:%s' % type(error).__name__
    return answer
