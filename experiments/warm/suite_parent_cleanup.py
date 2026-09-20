"""Fail-closed cleanup for a durable suite parent and its reserved children."""
import json
from pathlib import Path
import re

import admission
import docker_cleanup
import dependencies
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


def terminal_verified(attempt):
    """A cleanup receipt releases resources; only this receipt closes suite work."""
    terminal = attempt / 'terminal.json'
    if terminal.is_symlink() or not terminal.is_file():
        return False
    try:
        value = json.loads(terminal.read_text())
    except (OSError, ValueError):
        return False
    return value.get('attempt') == attempt.name and value.get('cleanup_verified') is True


def operator_result_verified(attempt):
    """Accept a bounded operator resolution only when it matches child evidence."""
    result = attempt / 'operator-result.json'
    submission = attempt / 'submission.json'
    if (result.is_symlink() or not result.is_file() or submission.is_symlink()
            or not submission.is_file()):
        return False
    try:
        receipt = json.loads(result.read_text())
        content = submission.read_bytes()
        submitted = json.loads(content)
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(receipt, dict) or not isinstance(submitted, dict):
        return False
    import hashlib
    expected = {
        'attempt': attempt.name,
        'state': 'infrastructure-failed',
        'cleanup_verified': True,
        'submission_sha256': hashlib.sha256(content).hexdigest(),
        'source_digest': submitted.get('source_digest'),
        'workflow': submitted.get('workflow'),
    }
    allowed = set(expected) | {'reason', 'acknowledged_at'}
    terminal = attempt / 'terminal.json'
    if terminal.exists():
        if terminal.is_symlink() or not terminal.is_file():
            return False
        expected['terminal_sha256'] = hashlib.sha256(terminal.read_bytes()).hexdigest()
        allowed.add('terminal_sha256')
    return (set(receipt) == allowed and all(receipt.get(key) == value for key, value in expected.items())
            and isinstance(receipt.get('reason'), str) and bool(receipt['reason'])
            and isinstance(receipt.get('acknowledged_at'), (int, float))
            and not isinstance(receipt.get('acknowledged_at'), bool)
            and isinstance(submitted.get('source_digest'), str) and bool(submitted['source_digest'])
            and isinstance(submitted.get('workflow'), str) and bool(submitted['workflow']))


def cleanup(parent):
    """Release dead child resources, but close the parent only with child terminals.

    A child may be staged before it registers or writes its terminal record. Its
    cleanup receipt can safely release the FIFO barrier, but cannot prove that
    the suite parent completed. Such a parent remains pending for recovery.
    """
    parent = Path(parent)
    registry = parent / 'children.json'
    if not registry.exists():
        return not (parent / 'suite-cleanup.pending').exists()
    children = validate_registry(parent, json.loads(registry.read_text()))
    root = parent.parent.parent
    attempts = []
    malformed = False
    for identity in children:
        attempt = root / 'runs' / identity
        if not attempt.exists():
            continue  # Reserved but never staged children own no resources.
        if not attempt.is_dir() or attempt.is_symlink():
            malformed = True
            continue
        (attempt / 'cancel.request').touch()
        if (attempt / 'children.json').exists():
            malformed = True
            continue
        attempts.append(attempt)
    dead = [attempt for attempt in attempts if not admission.alive(attempt)]
    verified = not malformed and len(dead) == len(attempts)
    for attempt in dead:
        services = service_cleanup.cleanup(attempt)
        containers = docker_cleanup.cleanup(attempt)
        prepared = dependencies.cleanup(attempt)
        if not admission.record_cleanup(attempt, services and containers and prepared):
            verified = False
        if not (terminal_verified(attempt) or operator_result_verified(attempt)):
            verified = False
    if verified:
        (parent / 'suite-cleanup.pending').unlink(missing_ok=True)
    return verified
