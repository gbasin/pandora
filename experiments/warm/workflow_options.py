"""Bounded surface options shared by routing, capture and the worker."""
from pathlib import PurePosixPath

APPS = ('web', 'desk')


def surface_selectors(values):
    if not isinstance(values, list) or any(not isinstance(v, str) or '\x00' in v for v in values):
        raise ValueError('Surface selectors must be strings')
    grep_seen = False
    index = 0
    while index < len(values):
        value = values[index]
        if value == '--grep':
            if grep_seen or index + 1 >= len(values) or not values[index + 1]:
                raise ValueError('Provide one nonempty --grep pattern')
            grep_seen = True
            index += 2
            continue
        if not value or value.startswith('-') or '\x00' in value:
            raise ValueError('Surface supports file selectors and --grep PATTERN only')
        path = PurePosixPath(value)
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('Surface file selectors must stay within the test directory')
        index += 1
    return list(values)


def surface_outputs(app):
    if app not in APPS:
        raise ValueError('Unsupported surface app')
    return (f'apps/{app}/dist', f'apps/{app}/e2e/dist')
