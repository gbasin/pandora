"""Explicit Docker argv grammar and external profile, with no local fallback."""
import json
from pathlib import Path, PurePosixPath
import re

EXAMPLE = 'Supported: docker build -t app:test .; docker run --rm app:test; docker image rm app:test'
DEFAULT_QUEUE_TIMEOUT_SECONDS = 900
MAX_QUEUE_TIMEOUT_SECONDS = 86400


def artifact_delivery_limit_bytes(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError('Artifact delivery limit must be a positive integer number of bytes')
    return value


def relative(value):
    if not isinstance(value, str) or any(c in value for c in ('\0', '\n', '\r', ':', ',')):
        raise ValueError('Unsupported profile path')
    p = PurePosixPath(value)
    if not value or p.is_absolute() or any(x in ('', '.', '..', '.git') for x in value.split('/')):
        raise ValueError('Expected a relative path without traversal: ' + value)
    return value


def absolute(value):
    if not value.startswith('/') or value == '/':
        raise ValueError('Expected a non-root absolute container path: ' + value)
    relative(value[1:])
    if value.startswith(('/proc/', '/sys/', '/dev/')) or value in ('/proc', '/sys', '/dev'):
        raise ValueError('Unsupported container path: ' + value)
    return value


def profile(value):
    data = json.loads(value)
    if not isinstance(data, dict) or set(data) - {'dockerfiles', 'mounts', 'outputs', 'network', 'queue_timeout_seconds', 'artifact_delivery_limit_bytes'}:
        raise ValueError('Unsupported Docker profile fields')
    for key in ('dockerfiles', 'mounts', 'outputs'):
        if not isinstance(data.get(key), list):
            raise ValueError('Docker profile requires list: ' + key)
    for item in data['dockerfiles']:
        relative(item)
    for item in data['mounts']:
        absolute(item)
    roots = []
    for item in data['outputs']:
        if set(item) != {'container', 'workspace'}:
            raise ValueError('Each output requires container and workspace paths')
        absolute(item['container'])
        relative(item['workspace'])
        for previous in roots:
            if PurePosixPath(item['workspace']).is_relative_to(previous) or PurePosixPath(previous).is_relative_to(item['workspace']):
                raise ValueError('Output roots must not overlap')
        roots.append(item['workspace'])
    if data.get('network', 'none') not in ('none', 'bridge'):
        raise ValueError('Profile network must be none or bridge')
    if 'queue_timeout_seconds' in data:
        queue_timeout_seconds(data['queue_timeout_seconds'])
    if 'artifact_delivery_limit_bytes' in data:
        artifact_delivery_limit_bytes(data['artifact_delivery_limit_bytes'])
    return data


def queue_timeout_seconds(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_QUEUE_TIMEOUT_SECONDS:
        raise ValueError(f'Queue timeout must be an integer from 1 through {MAX_QUEUE_TIMEOUT_SECONDS} seconds')
    return value


def tag(value):
    if not re.fullmatch(r'[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9_][A-Za-z0-9_.-]*)?', value):
        raise ValueError('Unsupported logical image tag: ' + value)
    return value if ':' in value else value + ':latest'


def classify(argv, repo, config):
    if not argv:
        raise ValueError(EXAMPLE)
    if argv[0] == 'build':
        image = None
        dockerfile = 'Dockerfile'
        seen = set()
        args = iter(argv[1:])
        context = None
        for arg in args:
            if arg in ('-t', '-f'):
                if arg in seen:
                    raise ValueError('Duplicate Docker build option: ' + arg)
                seen.add(arg)
                value = next(args, None)
                if not value:
                    raise ValueError('Missing value for ' + arg)
                if arg == '-t': image = tag(value)
                else: dockerfile = relative(value)
            elif arg == '.' and context is None:
                context = arg
            else:
                raise ValueError('Unsupported Docker build argument: ' + arg + '. ' + EXAMPLE)
        if not image or context != '.':
            raise ValueError('Build requires one -t TAG and context . . ' + EXAMPLE)
        if dockerfile not in config['dockerfiles']:
            raise ValueError('Dockerfile is not declared in the external profile: ' + dockerfile)
        return {'kind': 'build', 'tag': image, 'dockerfile': dockerfile}
    if argv[:2] == ['image', 'rm'] and len(argv) == 3:
        return {'kind': 'remove', 'tag': tag(argv[2])}
    if argv[0] == 'run':
        rm = False
        mount = None
        i = 1
        while i < len(argv) and argv[i].startswith('-'):
            arg = argv[i]
            if arg == '--rm' and not rm:
                rm = True
                i += 1
            elif arg == '-v' and mount is None and i + 1 < len(argv):
                parts = argv[i + 1].split(':')
                if len(parts) not in (2, 3) or (len(parts) == 3 and parts[2] != 'ro'):
                    raise ValueError('Mount must be "$PWD:DECLARED_PATH[:ro]"')
                if not Path(parts[0]).is_absolute() or Path(parts[0]).resolve() != repo.resolve():
                    raise ValueError('Only the current worktree root may be mounted')
                if parts[1] not in config['mounts']:
                    raise ValueError('Mount destination is not declared: ' + parts[1])
                mount = {'target': parts[1], 'readonly': len(parts) == 3}
                i += 2
            else:
                raise ValueError('Unsupported Docker run option: ' + arg + '. ' + EXAMPLE)
        if not rm or i == len(argv):
            raise ValueError('Foreground run requires --rm and a built tag. ' + EXAMPLE)
        return {'kind': 'run', 'tag': tag(argv[i]), 'mount': mount, 'command': argv[i + 1:]}
    raise ValueError('Unsupported Docker command. Nothing ran locally or remotely. ' + EXAMPLE)
