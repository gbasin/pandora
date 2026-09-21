"""Authenticated non-pass receipt for a cancelled surface parent."""
import json
from pathlib import Path
import re

from suite_parent_cleanup import validate_registry


_DIGEST = re.compile('[0-9a-f]{64}\\Z')
_RECEIPT = 'results/surface-cancelled.json'


def write_receipt(parent, submitted, cleanup_verified):
    """Record cancellation only after suite cleanup has released every child."""
    parent = Path(parent)
    if submitted.get('workflow') != 'surface-run' or not cleanup_verified:
        return None
    registry = parent / 'children.json'
    if registry.exists():
        children = validate_registry(parent, json.loads(registry.read_text()))
    else:
        children = []
    receipt = {'version': 1, 'parent_attempt': parent.name,
               'source_digest': submitted['source_digest'], 'children': children}
    path = parent / _RECEIPT
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


def validate_receipt(stage, submitted, terminal, manifest):
    """Validate a clean cancellation without inventing a partial test result."""
    stage = Path(stage)
    if (submitted.get('workflow') != 'surface-run' or terminal.get('exit_code') != 130
            or not terminal.get('cleanup_verified')):
        raise ValueError('Surface cancellation has an invalid terminal')
    if _RECEIPT not in manifest:
        raise ValueError('Cancelled surface parent lacks its cancellation receipt')
    value = json.loads((stage / _RECEIPT).read_text())
    expected = {'version', 'parent_attempt', 'source_digest', 'children'}
    if (not isinstance(value, dict) or set(value) != expected or value.get('version') != 1
            or value.get('parent_attempt') != submitted.get('attempt')
            or value.get('source_digest') != submitted.get('source_digest')
            or not isinstance(value.get('children'), list)
            or not isinstance(value.get('source_digest'), str)
            or not _DIGEST.fullmatch(value['source_digest'])):
        raise ValueError('Invalid surface cancellation receipt')
    registry = stage / 'children.json'
    if registry.exists():
        children = validate_registry(Path(submitted['attempt']), json.loads(registry.read_text()))
        if children != value['children']:
            raise ValueError('Surface cancellation registry differs from receipt')
        if 'children.json' not in manifest:
            raise ValueError('Surface cancellation registry is not retained')
    elif value['children']:
        raise ValueError('Surface cancellation claims an absent registry')
    return value
