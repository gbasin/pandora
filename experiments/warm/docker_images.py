"""Stable worktree-private logical tags. Physical images stay pinned in this pilot."""
import hashlib
import json
from pathlib import Path
import re
import sys


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
    target = path(root, key, tag)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps(record, indent=2) + '\n')
    temp.replace(target)


if __name__ == '__main__':
    try:
        print(json.dumps(resolve(Path.home() / 'pandora-warm', sys.argv[1], sys.argv[2])))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(64)
