"""Focused journey expectation return; no general source synchronization."""
import hashlib
import json
from pathlib import Path
from snapshot import names, excluded, entry, encode
from tracked_outputs import publish
from journey import journey_config

FIXTURES = 'packages/scenarios/fixtures/'
PATHS = (FIXTURES + 'S0-01.ledger.jsonl', FIXTURES + 'write-routes.json')


def is_update(submitted):
    return submitted.get('workflow') == 'journey' and journey_config(submitted)['update']


def regular_bytes(root, name, optional=False):
    path = root / name
    for parent in [path, *path.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError('Journey update path contains a symlink: ' + name)
    if not path.exists() and optional:
        return None
    if not path.is_file():
        raise ValueError('Missing or nonregular journey update: ' + name)
    return path.read_bytes()


def declarations(output):
    config = journey_config(json.loads((output / 'submission.json').read_text()))
    journey_id = config['id']
    paths = (FIXTURES + journey_id + '.ledger.jsonl', FIXTURES + 'write-routes.json')
    manifest = {x['path']: x for x in json.loads((output / 'manifest.json').read_text())}
    artifacts = json.loads((output / 'artifacts.json').read_text())
    report = json.loads((output / 'results/journey.json').read_text())
    if report.get('update') is not True or report.get('journey') != journey_id or report.get('status') != 'pass':
        raise ValueError('Journey update lacks matching successful update evidence')
    prefix = 'results/updates/'
    proposed = {p[len(prefix):] for p in artifacts if p.startswith(prefix)}
    if proposed != set(paths):
        raise ValueError('Unexpected or missing declared journey expectation outputs')
    result = {}
    for name in paths:
        base = regular_bytes(output / 'source', name, optional=True)
        record = manifest.get(name)
        if (base is None) != (record is None) or (record is not None and (
                'link' in record or hashlib.sha256(base).hexdigest() != record.get('sha256'))):
            raise ValueError('Captured journey expectation differs from the submitted manifest: ' + name)
        target = regular_bytes(output / 'results/updates', name)
        if hashlib.sha256(target).hexdigest() != artifacts[prefix + name]:
            raise ValueError('Journey expectation checksum mismatch: ' + name)
        result[name] = {'base': base, 'target': target}
    route = result[paths[1]]
    before = json.loads(route['base']) if route['base'] is not None else {}
    after = json.loads(route['target'])
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise ValueError('Invalid journey route manifest')
    if {k: v for k, v in before.items() if k != journey_id} != {k: v for k, v in after.items() if k != journey_id}:
        raise ValueError('Focused journey update modified unrelated route entries')
    if not isinstance(after.get(journey_id), list) or any(not isinstance(x, str) for x in after[journey_id]):
        raise ValueError('Focused journey update lacks its route entry')
    return result


def source_is_current(repo, output, submitted, changes):
    """Check non-output source; publisher validates declared destinations separately."""
    original = json.loads((output / 'manifest.json').read_text())
    current = {e['path']: e for n in names(repo) if not excluded(n)
               if (e := entry(repo, n)) is not None}
    captured = {e['path']: e for e in original}
    # These exact destinations get stronger per-path base/target checks in publish.
    # Compare all OTHER input first so unrelated source changes never get applied.
    # Do not clear an active attempt merely because one destination conflicts.
    for name in changes:
        if name in captured:
            current[name] = captured[name]
        else:
            current.pop(name, None)
    return hashlib.sha256(encode([current[k] for k in sorted(current)])).hexdigest() == submitted['source_digest']


def deliver(repo, output, changes, fault=lambda point: None):
    receipt = publish(repo, output, changes, fault=fault)
    for name in changes:
        print('[pandora] expectation returned: ' + str(repo / name), flush=True)
    print('[pandora] Review git diff, then validate without --update.', flush=True)
    return receipt
