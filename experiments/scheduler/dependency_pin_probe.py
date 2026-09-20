#!/usr/bin/env python3
"""Isolated real-Docker retention probe. Uses only uniquely generated alias tags."""
import argparse
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'warm'))
from dependency_images import pin, release, remember


def docker(*args):
    return subprocess.run(['sudo', 'docker', *args], check=True, capture_output=True, text=True, timeout=30)


def exists(tag):
    result = subprocess.run(['sudo', 'docker', 'image', 'inspect', tag], capture_output=True, timeout=30)
    if result.returncode:
        if b'No such image' not in result.stderr:
            raise RuntimeError(result.stderr.decode())
        return False
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=False)
    base = docker('image', 'inspect', args.image, '--format', '{{.Id}}').stdout.strip()
    tags = ['pandora-deps:' + uuid.uuid4().hex + uuid.uuid4().hex for _ in range(5)]
    attempt = args.root / 'runs' / uuid.uuid4().hex
    attempt.mkdir(parents=True)
    evidence = {'tags': tags, 'attempt': attempt.name}
    try:
        with (attempt / 'attempt.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            pin(attempt, tags[0])  # Reserve before the image even exists.
            for tag in tags:
                docker('tag', base, tag)
                remember(args.root, tag)
            assert exists(tags[0]), 'Pinned image was collected before container creation'
            assert not exists(tags[1]), 'Unused oldest image was not collected'
            evidence['protected_before_container'] = True
        # Simulate dead owner with no terminal/cleanup receipt.
        remember(args.root, tags[-1])
        assert exists(tags[0]), 'Owner death incorrectly released the image'
        evidence['dead_owner_protected'] = True
        (attempt / 'admission-cleanup.json').write_text(json.dumps({
            'attempt': attempt.name, 'cleanup_verified': True}))
        assert release(attempt)
        remember(args.root, tags[-1])
        assert not exists(tags[0]), 'Verified cleanup did not permit collection'
        assert not (attempt / 'terminal.json').exists()
        evidence['released_after_cleanup_without_test_result'] = True
        (args.root / 'result.json').write_text(json.dumps(evidence, indent=2) + '\n')
        print(json.dumps(evidence))
    finally:
        for tag in tags:
            if exists(tag):
                docker('image', 'rm', tag)
        assert all(not exists(tag) for tag in tags)
        assert docker('image', 'inspect', args.image, '--format', '{{.Id}}').stdout.strip() == base


if __name__ == '__main__':
    main()
