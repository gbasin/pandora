"""Live Docker retention check in a disposable namespace on the worker."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid
from retention import remote, remember_image, PROFILE

p = argparse.ArgumentParser()
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
a.output.mkdir(parents=True, exist_ok=False)
containers, tags = [], []


def docker(*args, check=True):
    return subprocess.run(['sudo', 'docker', *args], check=check, capture_output=True, text=True)


try:
    with tempfile.TemporaryDirectory(prefix='pandora-retention-probe-') as temp:
        root = Path(temp)
        (root / 'runs').mkdir()
        paths = []
        for index in range(13):
            path = root / 'runs' / uuid.uuid4().hex
            path.mkdir()
            (path / 'submission.json').write_text(json.dumps({'profile': PROFILE}))
            (path / 'terminal.json').write_text(json.dumps({'attempt': path.name, 'cleanup_verified': True}))
            (path / 'released').touch()
            os.utime(path / 'released', (1000 + index, 1000 + index))
            paths.append(path)
        for path in paths[:3]:
            name = 'pandora-warm-' + path.name
            containers.append(name)
            attempt = path.name if path != paths[2] else '0' * 32
            docker('run', '-d', '--name', name, '--label', 'pandora.experiment=warm-surface',
                   '--label', 'pandora.workflow=surface', '--label', 'pandora.attempt=' + attempt,
                   '--memory=32m', '--memory-swap=32m',
                   '--cpus=.1', 'node:24-bookworm-slim', 'sleep', '120')
        docker('stop', containers[1], containers[2])
        remote(root)
        assert paths[0].exists()  # Live process wins over an inconsistent receipt.
        assert not paths[1].exists()
        assert paths[2].exists()  # Exact name alone does not authorize foreign-resource removal.
        assert all(path.exists() for path in paths[3:])
        assert docker('inspect', containers[1], check=False).returncode != 0
        assert docker('inspect', containers[2], check=False).returncode == 0
        for index in range(5):
            tag = 'pandora-deps:' + hashlib.sha256((temp + str(index)).encode()).hexdigest()
            tags.append(tag)
            docker('tag', 'node:24-bookworm-slim', tag)
            remember_image(root, tag)
        assert json.loads((root / 'integrated-images.json').read_text()) == tags[-3:]
        assert all(docker('image', 'inspect', tag, check=False).returncode != 0 for tag in tags[:2])
        (a.output / 'result.json').write_text(json.dumps({
            'assertions': 'passed', 'released_runs_kept': 10, 'inconsistent_live_run_preserved': True,
            'foreign_stopped_container_preserved': True, 'old_stopped_container_removed': True,
            'managed_image_tags_kept': 3}, indent=2) + '\n')
finally:
    for name in containers:
        docker('rm', '-f', name, check=False)
    for tag in tags:
        docker('image', 'rm', tag, check=False)
