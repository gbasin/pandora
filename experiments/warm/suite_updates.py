"""Merge complete, isolated suite-update proposals into one parent proposal."""
import hashlib
import json
import re
from pathlib import Path

from suite_evidence import validate_plan, validate_shard

FIXTURES = 'packages/scenarios/fixtures/'
ROUTES = FIXTURES + 'write-routes.json'
ID_SUFFIX = '.ledger.jsonl'
ATTEMPT = re.compile(r'^[0-9a-f]{32}$')


def _fail(message):
    raise ValueError('Invalid suite update: ' + message)


def _json(path, label):
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ValueError('Invalid suite update: missing or malformed ' + label) from error
    return value


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _bytes(path, label):
    if path.is_symlink() or not path.is_file():
        _fail('missing regular ' + label)
    return path.read_bytes()


def _routes(value, label):
    if not isinstance(value, dict):
        _fail(label + ' routes are not an object')
    for identifier, routes in value.items():
        if not isinstance(identifier, str) or not isinstance(routes, list) or any(not isinstance(route, str) for route in routes):
            _fail(label + ' routes are malformed')
    return value


def _artifact(child, name, contents=None):
    artifacts = _json(child / 'artifacts.json', 'child artifact manifest')
    if not isinstance(artifacts, dict) or not isinstance(artifacts.get(name), str):
        _fail('child proposal artifact is not declared: ' + name)
    actual = _bytes(child / name, 'child proposal artifact') if contents is None else contents
    if _digest(actual) != artifacts[name]:
        _fail('child proposal artifact digest differs: ' + name)


def _proposal(parent, child, plan, expected_shard):
    submitted = _json(child / 'submission.json', 'child submission')
    terminal = _json(child / 'terminal.json', 'child terminal')
    if (not ATTEMPT.fullmatch(child.name) or submitted.get('attempt') != child.name or
            submitted.get('workflow') != 'suite' or submitted.get('parent_attempt') != parent.name or
            submitted.get('suite_update') is not True):
        _fail('child identity differs from retained attempt')
    request = submitted.get('suite')
    if not isinstance(request, dict) or request.get('action') != 'shard' or request.get('plan') != plan or request.get('shard') != expected_shard:
        _fail('child request differs from frozen shard')
    if (terminal.get('attempt') != child.name or terminal.get('workflow') != 'suite' or
            submitted.get('source_digest') != plan['source_digest'] or terminal.get('exit_code') != 0 or
            terminal.get('cleanup_verified') is not True):
        _fail('child did not complete successfully')
    report = _json(child / 'results/suite-shard.json', 'child shard report')
    validate_shard(plan, report)
    if report.get('shard') != expected_shard or report.get('exit_code') != 0:
        _fail('child shard report is not a successful expected shard')
    receipt_name = 'results/suite-update.json'
    _artifact(child, receipt_name)
    receipt = _json(child / receipt_name, 'child update receipt')
    required = {'version', 'plan_id', 'source_digest', 'shard', 'planned_ids', 'routes', 'ledger_expected', 'ledgers'}
    if set(receipt) != required or receipt.get('version') != 1:
        _fail('child update receipt has an unexpected shape')
    planned = next(item['ids'] for item in plan['shards'] if item['index'] == expected_shard)
    if (receipt['plan_id'] != plan['plan_id'] or receipt['source_digest'] != plan['source_digest'] or
            receipt['shard'] != expected_shard or receipt['planned_ids'] != planned):
        _fail('child update receipt differs from frozen shard')
    routes = receipt['routes']
    if not isinstance(routes, dict) or set(routes) != set(planned):
        _fail('child update routes do not exactly match shard ownership')
    for value in routes.values():
        if value is not None and (not isinstance(value, list) or any(not isinstance(route, str) for route in value)):
            _fail('child update route value is malformed')
    expected = receipt['ledger_expected']
    if (not isinstance(expected, list) or any(not isinstance(identifier, str) for identifier in expected) or
            len(set(expected)) != len(expected) or set(expected) - set(planned)):
        _fail('child ledger ownership is malformed')
    ledgers = receipt['ledgers']
    if not isinstance(ledgers, list) or [item.get('id') for item in ledgers if isinstance(item, dict)] != sorted(expected) or len(ledgers) != len(expected):
        _fail('child ledger proposals do not exactly match expected ledgers')
    checked = []
    for item in ledgers:
        if not isinstance(item, dict) or set(item) != {'id', 'path', 'sha256'}:
            _fail('child ledger proposal is malformed')
        identifier, path, digest = item['id'], item['path'], item['sha256']
        if identifier not in expected or path != FIXTURES + identifier + ID_SUFFIX or not isinstance(digest, str):
            _fail('child ledger proposal is outside its ownership')
        artifact = 'results/updates/' + path
        contents = _bytes(child / artifact, 'child ledger proposal')
        _artifact(child, artifact, contents)
        if _digest(contents) != digest:
            _fail('child ledger proposal digest differs')
        checked.append((identifier, path, contents, digest))
    return routes, checked


def merge(parent, plan, shard_paths):
    """Validate every complete child and write the one parent update proposal."""
    plan = validate_plan(plan)
    parent = Path(parent)
    paths = [Path(path) for path in shard_paths]
    expected = [item['index'] for item in plan['shards']]
    if len(paths) != len(expected) or len({path.name for path in paths}) != len(paths):
        _fail('child paths do not exactly cover frozen shards')
    baseline = _routes(_json(parent / 'source' / ROUTES, 'captured route manifest'), 'captured')
    merged = dict(baseline)
    seen_ids, seen_paths, ledgers, shard_receipts = set(), set(), [], []
    for index, child in zip(expected, paths, strict=True):
        routes, proposal_ledgers = _proposal(parent, child, plan, index)
        if seen_ids & set(routes):
            _fail('route ownership overlaps across child proposals')
        seen_ids.update(routes)
        for identifier, value in routes.items():
            if value is None:
                merged.pop(identifier, None)
            else:
                merged[identifier] = value
        for identifier, path, contents, digest in proposal_ledgers:
            if path in seen_paths:
                _fail('ledger ownership overlaps across child proposals')
            seen_paths.add(path)
            ledgers.append({'id': identifier, 'path': path, 'sha256': digest})
            target = parent / 'results' / 'updates' / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(contents)
        shard_receipts.append({'shard': index, 'attempt': child.name})
    if seen_ids != {identifier for shard in plan['shards'] for identifier in shard['ids']}:
        _fail('route ownership does not cover frozen catalog')
    route_bytes = (json.dumps(dict(sorted(merged.items())), indent=2) + '\n').encode()
    route_target = parent / 'results' / 'updates' / ROUTES
    route_target.parent.mkdir(parents=True, exist_ok=True)
    route_target.write_bytes(route_bytes)
    receipt = {'version': 1, 'plan_id': plan['plan_id'], 'source_digest': plan['source_digest'],
               'shards': shard_receipts, 'routes': {identifier: merged.get(identifier) for identifier in sorted(seen_ids)},
               'ledgers': sorted(ledgers, key=lambda item: item['path'])}
    destination = parent / 'results' / 'suite-updates.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt
