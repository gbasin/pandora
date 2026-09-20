"""Stable worktree-private logical tags. Resolution reserves images before queueing."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sys


@contextmanager
def locked(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'docker-images.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def reserve(root, key, tag, attempt):
    if not re.fullmatch('[a-f0-9]{32}', attempt):
        raise ValueError('Invalid attempt identity')
    with locked(root):
        target = root / 'docker-pins' / (attempt + '.json')
        if target.exists():
            existing = json.loads(target.read_text())
            if existing['key'] != key or existing['tag'] != tag:
                raise ValueError('Attempt already reserved a different image')
            return existing['image']
        image = resolve(root, key, tag)
        target.parent.mkdir(exist_ok=True)
        temp = target.with_suffix('.tmp')
        temp.write_text(json.dumps({'key': key, 'tag': tag, 'image': image}) + '\n')
        temp.replace(target)
        return image


def remove(root, key, tag):
    with locked(root):
        target = path(root, key, tag)
        if not target.exists():
            return None
        image = resolve(root, key, tag)
        target.unlink()
        return image


def path(root, key, tag):
    if not re.fullmatch('[a-f0-9]{64}', key):
        raise ValueError('Invalid worktree identity')
    return root / 'docker-images' / key / (hashlib.sha256(tag.encode()).hexdigest() + '.json')


def resolve(root, key, tag):
    target = path(root, key, tag)
    if not target.exists():
        raise ValueError('Unknown worktree image ' + tag + '; run docker build -t ' + tag + ' . first')
    return json.loads(target.read_text())


def publish(root, key, tag, record):
    with locked(root):
        _publish(root, key, tag, record)


def _publish(root, key, tag, record):
    target = path(root, key, tag)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps(record, indent=2) + '\n')
    temp.replace(target)


if __name__ == '__main__':
    try:
        print(json.dumps(reserve(Path.home() / 'pandora-warm', sys.argv[1], sys.argv[2], sys.argv[3])))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(64)
