"""A signed statement that a whole run passed over one exact git tree.

A passing remote run on a ready worker produces a verdict: a canonical JSON
payload naming the tree the run saw, the job, its argv, environment and working
directory, and the golden it ran on (its pinned fingerprint and what it was
pinned to), signed with an Ed25519 key that only this engine root holds. The client
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
import time
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
# A job with synthetic git whose tree could not be computed, for example a
# failed Git LFS pointer step: `tree_failed:<reason>`. The run is unchanged.
TREE_FAILED = 'tree_failed'
WORKER_DRIFTED = 'worker_drifted'

# How long one drift answer stands. Every supervisor is its own process, so the
# answer is kept in a file under the engine root rather than in memory.
DRIFT_TTL = 60.0
DRIFT_DETAIL_MAX = 600


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
            repo, run_id, tree, golden_pins=None, test_evidence=None):
    """The canonical payload bytes: sorted keys, no spaces, UTF-8, no newline.

    `golden` is the pinned fingerprint in `golden-<fingerprint>`; `golden_pins`
    is what it was pinned to, `{image, lockfiles: {name: sha256}}`, or null for
    a toolchain resolved before pinning existed (`pinning.golden_pins`).
    """
    body = {'argv': list(argv), 'cwd': cwd, 'engine': engine,
                      'env_digest': env_digest, 'finished': finished, 'golden': golden,
                      'golden_pins': golden_pins,
                      'input_id': input_id, 'job': job, 'kind': KIND, 'outcome': outcome,
                      'repo': repo, 'run_id': run_id, 'tree': tree, 'v': VERSION}
    if test_evidence is not None:
        body['test_evidence'] = test_evidence
    return canonical(body)


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
    """`ready`, or what the ready state is instead: the state file that
    `canary --mark` wrote. Drift is a separate condition (`worker_drift`)."""
    try:
        state = json.loads((Path(root or worker_root()) / 'worker' / 'state.json').read_text())
    except (OSError, ValueError):
        return 'unprovisioned'
    return state.get('state') or 'unprovisioned'


def drift_cache_path(engine_root):
    """Beside the key, where the gateway lets no client write."""
    return Path(engine_root).expanduser() / 'keys' / 'drift.json'


def describe_drift(items):
    """One line naming each difference, short enough for a log."""
    parts = []
    for item in items:
        parts.append('%s %s: want %s, have %s (%s)' % (
            item.get('kind'), item.get('name'), item.get('want'),
            item.get('have'), item.get('detail')))
    text = '; '.join(parts)
    return text if len(text) <= DRIFT_DETAIL_MAX else text[:DRIFT_DETAIL_MAX - 3] + '...'


def survey_drift(root=None):
    """'' when the host matches its stored manifest, else what differs.

    The comparison is `facts.drift`, the one `pandora worker status` uses, over
    `facts.quick_survey`: the manifest's packages, its settings and the kernel.
    A manifest or dpkg that cannot be read is drift: nothing proves the host is
    what the canary passed on.
    """
    from pandora.errors import ConfigError
    from pandora.worker import facts, versions
    folder = Path(root or worker_root())
    path = facts.manifest_path(folder)
    if not path.is_file():
        return 'manifest unreadable: no versions manifest at %s' % path
    try:
        manifest = versions.load(path)
    except (ConfigError, OSError, ValueError) as error:
        return 'manifest unreadable: %s' % error
    try:
        state = json.loads((folder / 'worker' / 'state.json').read_text())
    except (OSError, ValueError):
        state = {}
    try:
        observed = facts.quick_survey(manifest)
    except facts.Unreadable as error:
        return 'dpkg unreadable: %s' % error
    items = facts.drift(manifest, observed, state if isinstance(state, dict) else {})
    return describe_drift(items) if items else ''


def drift_key(root=None):
    """What a cached answer depends on besides time: the worker root itself,
    the manifest and the state file. Re-provisioning or a new `canary --mark`
    starts afresh, and an engine root shared by two worker roots never reads
    one root's answer for the other."""
    top = Path(root or worker_root()).expanduser()
    folder = top / 'worker'
    key = [['root', str(top.resolve()), None]]
    for name in ('versions.toml', 'state.json'):
        try:
            info = (folder / name).stat()
            key.append([name, info.st_mtime_ns, info.st_size])
        except OSError:
            key.append([name, None, None])
    return key


def worker_drift(engine_root, root=None, *, clock=time.time, survey=None):
    """'' when the worker has not drifted, else the detail. Never raises.

    The answer is cached for DRIFT_TTL seconds in `drift_cache_path`, shared by
    every supervisor on the engine root, so a burst of finishing runs pays for
    one `dpkg-query`. A failed survey is an answer too, and cached the same
    way: caching "drifted" fails closed, and a hung tool costs one run its
    timeout rather than every run for a minute. A cache that cannot be read or
    written only costs a fresh survey.
    """
    try:
        now = clock()
        key = drift_key(root)
        cache = drift_cache_path(engine_root)
        try:
            held = json.loads(cache.read_text())
            if (isinstance(held, dict) and held.get('key') == key
                    and isinstance(held.get('detail'), str)
                    and 0 <= now - float(held.get('at')) < DRIFT_TTL):
                return held['detail']
        except (OSError, ValueError, TypeError):
            pass
        try:
            detail = (survey or survey_drift)(root)
        except Exception as error:                  # noqa: BLE001 - fail closed, and cache it
            detail = 'drift check failed: %s' % type(error).__name__
        temp = None
        try:
            cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            sweep_temps(cache.parent)
            handle, temp = tempfile.mkstemp(dir=str(cache.parent), prefix='.drift-')
            with os.fdopen(handle, 'w') as out:
                json.dump({'at': now, 'key': key, 'detail': detail}, out)
            os.replace(temp, cache)
        except OSError:
            if temp is not None:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
        return detail
    except Exception as error:                      # noqa: BLE001 - fail closed, never fatal
        return 'drift check failed: %s' % type(error).__name__


def sweep_temps(folder, *, age=DRIFT_TTL):
    """Remove `.drift-*` files a killed writer left in `folder`. Only ones older
    than `age` seconds: a younger one may be another supervisor's write in
    flight."""
    cutoff = time.time() - age
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if not name.startswith('.drift-'):
            continue
        path = os.path.join(folder, name)
        try:
            if os.lstat(path).st_mtime < cutoff:
                os.unlink(path)
        except OSError:
            pass


def skip_reason(*, outcome, role, ready, tree, drift='', tree_failed=None):
    """The first signing condition that fails, or None when all hold.
    `drift` is `worker_drift`'s answer: '' for none. `tree_failed` is why a
    job with synthetic git has no tree; None when it has no synthetic git."""
    if outcome != 'passed':
        return NOT_PASSED
    if (role or 'single') != 'single':
        return NOT_WHOLE
    if ready != 'ready':
        return WORKER_NOT_READY
    if not tree:
        return '%s:%s' % (TREE_FAILED, tree_failed) if tree_failed else NO_SYNTHETIC_GIT
    if drift:
        return WORKER_DRIFTED
    return None


def decide(engine_root, row, *, outcome, tree, finished, golden, ready=None, drift=None,
           note=None, golden_pins=None, tree_failed=None, test_evidence=None):
    """The three result fields: `tree`, `verdict`, `verdict_skipped`.

    `row` is the attempt's ledger row as a dictionary. `ready` is the worker's
    ready state, read from the state file when not given; `drift` is
    `worker_drift`'s answer, surveyed when not given and only when every
    earlier condition holds, so a job without synthetic git never pays for
    the survey. `note` takes one log
    line: a drift skip writes its detail there. Never raises.
    """
    answer = {'tree': tree, 'verdict': None, 'verdict_skipped': None}
    try:
        role = row.get('role') or 'single'
        if ready is None and outcome == 'passed' and role == 'single':
            ready = ready_state()
        if (drift is None and outcome == 'passed' and role == 'single' and ready == 'ready'
                and tree):
            drift = worker_drift(engine_root)
        reason = skip_reason(outcome=outcome, role=role, ready=ready, tree=tree,
                             drift=drift or '', tree_failed=tree_failed)
        if reason:
            answer['verdict_skipped'] = reason
            if reason == WORKER_DRIFTED and note is not None:
                try:
                    note('verdict not signed: %s: %s' % (reason, drift))
                except Exception:                   # noqa: BLE001 - a log line, never fatal
                    pass
            return answer
        if not golden:
            raise SignFailed('golden fingerprint unknown')
        data = payload(argv=row['argv'], cwd=str(row.get('cwd') or ''), engine=engine_id(),
                       env_digest=env_digest(row.get('env')), finished=finished,
                       golden=golden, golden_pins=golden_pins,
                       input_id=row['input_id'], job=row['job'],
                       outcome=outcome, repo=row['repo'], run_id=row['run_id'], tree=tree,
                       test_evidence=test_evidence)
        answer['verdict'] = sign(engine_root, data)
    except SignFailed as error:
        answer['verdict_skipped'] = 'sign_failed:%s' % error
    except Exception as error:                      # noqa: BLE001 - never fatal to a run
        answer['verdict_skipped'] = 'sign_failed:%s' % type(error).__name__
    return answer
