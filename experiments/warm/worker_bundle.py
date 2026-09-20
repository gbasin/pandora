"""Content-addressed worker helpers, verified before each attempt uses them."""
import base64
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile

NAMES = ['suite.py', 'suite_evidence.py', 'suite.mjs', 'workflow_options.py', 'admission.py', 'snapshot.py', 'worker.py', 'dependencies.py', 'retention.py', 'source_cache.py',
         'in-container.sh', 'journey.py', 'service_cleanup.py', 'journey.mjs',
         'docker_workflow.py', 'docker_cleanup.py', 'docker_images.py', 'image_gc.py']


def bundle(scripts):
    files = {name: base64.b64encode((scripts / name).read_bytes()).decode() for name in NAMES}
    files['runtime.Dockerfile'] = base64.b64encode((scripts.parent / 'surface/Dockerfile').read_bytes()).decode()
    payload = json.dumps(files, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode()).hexdigest(), payload


def verified(path, identity):
    try:
        payload = (path / 'bundle.json').read_text()
        if hashlib.sha256(payload.encode()).hexdigest() != identity:
            return False
        return all((path / name).is_file() and not (path / name).is_symlink()
                   and (path / name).read_bytes() == base64.b64decode(data)
                   for name, data in json.loads(payload).items())
    except (OSError, ValueError):
        return False


def prepare(root, identity, attempt_id, repo_key, payload=None):
    if not re.fullmatch('[0-9a-f]{64}', identity) or not re.fullmatch('[0-9a-f]{32}', attempt_id):
        raise ValueError('Invalid bundle or attempt identity')
    cache = root / 'worker-bundles' / identity
    if not verified(cache, identity):
        if payload is None:
            return {'missing': True}
        if hashlib.sha256(payload.encode()).hexdigest() != identity:
            raise ValueError('Worker bundle checksum mismatch')
        files = json.loads(payload)
        if set(files) != set(NAMES + ['runtime.Dockerfile']):
            raise ValueError('Unexpected worker bundle files')
        cache.parent.mkdir(parents=True, exist_ok=True)
        # Separate staging and a lock avoid partial bundles and concurrent repair.
        import fcntl
        with (cache.parent / (identity + '.lock')).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not verified(cache, identity):
                stage = Path(tempfile.mkdtemp(dir=cache.parent))
                try:
                    for name, data in files.items():
                        (stage / name).write_bytes(base64.b64decode(data, validate=True))
                    (stage / 'bundle.json').write_text(payload)
                    if cache.exists():
                        shutil.rmtree(cache)
                    stage.rename(cache)
                finally:
                    if stage.exists():
                        shutil.rmtree(stage)
    attempt = root / 'runs' / attempt_id
    attempt.mkdir(parents=True, exist_ok=False)
    (attempt / 'source').mkdir()
    for name in NAMES + ['runtime.Dockerfile']:
        shutil.copyfile(cache / name, attempt / name)
    sys.path.insert(0, str(attempt))
    from source_cache import prepare as source_seed
    cached = source_seed(root, repo_key, attempt) if repo_key else ''
    return {'missing': False, 'home': str(root.parent), 'cached': cached}


if __name__ == '__main__':
    request = json.loads(sys.argv[1])
    print(json.dumps(prepare(Path.home() / 'pandora-warm', **request)))
