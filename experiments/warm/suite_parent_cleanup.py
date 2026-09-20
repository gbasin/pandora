"""Fail-closed cleanup for a durable suite parent and its reserved children."""
import json
from pathlib import Path
import re

import admission
import docker_cleanup
import service_cleanup


_IDENTITY = re.compile('[0-9a-f]{32}\\Z')


def validate_registry(parent, data):
    """Return the exact child identities owned by ``parent``'s suite run."""
    parent = Path(parent)
    if not _IDENTITY.fullmatch(parent.name):
        raise ValueError('Invalid suite parent identity')
    if (not isinstance(data, dict) or set(data) != {'version', 'parent_attempt', 'children'}
            or type(data['version']) is not int or data['version'] != 1
            or data['parent_attempt'] != parent.name or not isinstance(data['children'], list)):
        raise ValueError('Invalid suite parent registry')
    children = data['children']
    if (any(not isinstance(identity, str) or not _IDENTITY.fullmatch(identity) or identity == parent.name
            for identity in children) or len(children) != len(set(children))):
        raise ValueError('Invalid suite parent child identities')
    return children


def cleanup(parent):
    """Cancel and reconcile the parent-owned child attempts without minting terminals."""
    parent = Path(parent)
    registry = parent / 'children.json'
    if not registry.exists():
        return True
    children = validate_registry(parent, json.loads(registry.read_text()))
    root = parent.parent.parent
    attempts = []
    for identity in children:
        attempt = root / 'runs' / identity
        if not attempt.exists():
            continue  # Reserved but never staged children own no resources.
        if not attempt.is_dir() or attempt.is_symlink():
            return False
        (attempt / 'cancel.request').touch()
        if (attempt / 'children.json').exists():
            return False
        attempts.append(attempt)
    if any(admission.alive(attempt) for attempt in attempts):
        return False
    verified = True
    for attempt in attempts:
        services = service_cleanup.cleanup(attempt)
        containers = docker_cleanup.cleanup(attempt)
        if not admission.record_cleanup(attempt, services and containers):
            verified = False
    if verified:
        (parent / 'suite-cleanup.pending').unlink(missing_ok=True)
    return verified
