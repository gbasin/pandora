"""Repository-scoped immutable source reuse, serialized with attempt retention."""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def repository_key(repo):
    common = subprocess.check_output(['git', '-C', str(repo), 'rev-parse',
                                      '--path-format=absolute', '--git-common-dir'], text=True).strip()
    return hashlib.sha256(str(Path(common).resolve()).encode()).hexdigest()


@contextmanager
def locked(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'source-cache.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def pointer(root, key):
    if not re.fullmatch('[a-f0-9]{64}', key):
        raise ValueError('Invalid repository cache key')
    return root / 'source-caches' / key


def prepare(root, key, attempt):
    """Clone hardlinks while GC is excluded; transfer needs no long-lived lease."""
    target = pointer(root, key)
    with locked(root):
        seed = attempt / 'source-seed'
        if target.is_symlink() and target.resolve().is_dir():
            shutil.copytree(target.resolve(), seed, copy_function=os.link,
                            symlinks=True)
            return str(seed)
    return ''


def publish(root, key, attempt):
    target = pointer(root, key)
    with locked(root):
        target.parent.mkdir(exist_ok=True)
        temp = target.with_name(key + '-' + attempt.name)
        temp.symlink_to(attempt / 'source')
        temp.replace(target)
    shutil.rmtree(attempt / 'source-seed', ignore_errors=True)


def protected(root):
    # The legacy pointer protects snapshots still used by older clients.
    return [(root / 'latest').resolve().parent] + [
        item.resolve().parent for item in (root / 'source-caches').glob('*')
        if item.is_symlink()]


if __name__ == '__main__':
    action, key, identity = sys.argv[1:]
    if not re.fullmatch('[a-f0-9]{32}', identity):
        raise ValueError('Invalid attempt identity')
    root = Path.home() / 'pandora-warm'
    attempt = root / 'runs' / identity
    if action == 'prepare':
        print(prepare(root, key, attempt))
    elif action == 'publish':
        publish(root, key, attempt)
    else:
        raise ValueError('Unknown source cache action')
