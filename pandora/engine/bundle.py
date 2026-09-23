"""Ship the engine to the worker as a content-addressed payload over SSH stdin.

The v0.1.1 idea from `experiments/warm/worker_bundle.py`, narrowed. There is no
install step on the worker and nothing to keep in sync by hand: the client sends
the engine's own source, the worker verifies the digest it was told to expect
before anything is imported, and a bundle that is already there is not sent
again.

Why a digest rather than an rsync: an engine that half-updated while a run was
in flight would be a supervisor from one version writing rows another version
reads. A bundle is either entirely present under its own digest or it is not
present at all, and a run names the digest it started under.

The payload is the `pandora` package minus the client half -- the worker has no
use for the daemon, the shim or the fallback budget, and shipping them would put
the Mac's control plane on a machine that should never make those decisions.
"""
import base64
import hashlib
import json
from pathlib import Path

PACKAGE = 'pandora'
# Everything the worker needs and nothing it does not.
MEMBERS = (
    '__init__.py',
    'errors.py',
    'exits.py',
    'engine/__init__.py',
    'engine/admission.py',
    'engine/ledger.py',
    'engine/scheduler.py',
    'engine/result.py',
    'engine/runner.py',
    'engine/fanout.py',
    'engine/shards.py',
    'engine/service.py',
    'engine/turbocache.py',
    'engine/writeback.py',
    'executor/__init__.py',
    'executor/interface.py',
    'executor/incus.py',
    'executor/memtest.py',
    # The worker half: provisioning runs from the control machine, but the
    # canary, the sweeps and the status survey all read cgroups, btrfs qgroups
    # and the Incus socket, so they run here for the same reason the driver does.
    'worker/__init__.py',
    'worker/service.py',
    'worker/facts.py',
    'worker/canary.py',
    'worker/gc.py',
    'worker/goldens.py',
    'worker/pins.py',
    'worker/versions.py',
)

# Unpacks a verified bundle under ~/pandora-engine/bundles/<digest>/ and prints
# where it went. Deliberately tiny and dependency-free: it is the one piece of
# Pandora that has to run before Pandora is on the worker.
BOOTSTRAP = r'''
import base64, hashlib, json, os, sys, tempfile
from pathlib import Path

payload = sys.stdin.read()
digest = sys.argv[1]
root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path.home() / 'pandora-engine'

if hashlib.sha256(payload.encode()).hexdigest() != digest:
    raise SystemExit('bundle digest mismatch')
target = root / 'bundles' / digest
marker = target / 'pandora' / '.bundle'
if not marker.is_file() or marker.read_text().strip() != digest:
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(dir=str(target.parent)))
    for name, data in json.loads(payload).items():
        path = stage / 'pandora' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data, validate=True))
    (stage / 'pandora' / '.bundle').write_text(digest + '\n')
    try:
        os.rename(stage, target)
    except OSError:
        import shutil
        shutil.rmtree(stage, ignore_errors=True)
print(json.dumps({'ok': True, 'path': str(target), 'digest': digest}))
'''


def payload(source_root=None):
    """(digest, payload) for the engine half of this checkout."""
    root = Path(source_root or Path(__file__).resolve().parents[1])
    files = {}
    for name in MEMBERS:
        path = root / name
        if not path.is_file():
            raise FileNotFoundError('engine bundle is missing %s' % path)
        files[name] = base64.b64encode(path.read_bytes()).decode()
    text = json.dumps(files, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(text.encode()).hexdigest(), text


def ensure(link, root, *, source_root=None):
    """Make sure the worker has this engine. Returns the directory to import from."""
    digest, text = payload(source_root)
    code, out, _ = link.run(['sh', '-c', 'cat %s/bundles/%s/pandora/.bundle 2>/dev/null || true'
                             % (root, digest)], timeout=60)
    if out.strip() == digest:
        return {'digest': digest, 'path': '%s/bundles/%s' % (root, digest), 'sent': False}
    _, out, _ = link.feed(BOOTSTRAP, [digest, root], stdin=text.encode(), timeout=300)
    answer = json.loads(out.strip().splitlines()[-1])
    answer['sent'] = True
    return answer


def call(link, bundle_path, engine_root, argv, *, stdin=None, timeout=120, check=True,
         binary=False):
    """Run one engine subcommand on the worker, inside the shipped bundle."""
    import shlex
    import subprocess
    command = 'cd %s && PYTHONPATH=%s python3 -m pandora.engine.service --root %s %s' % (
        shlex.quote(bundle_path), shlex.quote(bundle_path), shlex.quote(engine_root),
        ' '.join(shlex.quote(str(item)) for item in argv))
    proc = subprocess.run(['ssh', *link.options, link.host, command],
                          input=stdin.encode() if isinstance(stdin, str) else stdin,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    from ..errors import EngineError, WorkerUnreachable
    err = (proc.stderr or b'').decode('utf-8', 'replace')
    if proc.returncode == 255:
        raise WorkerUnreachable('ssh %s: %s' % (link.host, err.strip()[:400] or 'no route'))
    if check and proc.returncode != 0:
        raise EngineError('engine %s failed (%d): %s'
                          % (argv[0], proc.returncode, err.strip()[:600]))
    if binary:
        return proc.stdout or b''
    out = (proc.stdout or b'').decode('utf-8', 'replace').strip()
    if not out:
        raise EngineError('engine %s said nothing; stderr: %s' % (argv[0], err.strip()[:400]))
    try:
        return json.loads(out.splitlines()[-1])
    except ValueError:
        raise EngineError('engine %s returned non-JSON: %s' % (argv[0], out[:400])) from None
