"""Validate and publish the single parent proposal from a catalog `--update`."""
import hashlib
import json
from pathlib import Path
import re
import sys

from tracked_outputs import publish
sys.path.append(str(Path(__file__).resolve().parent.parent / 'warm'))
from suite_evidence import validate_plan

FIXTURES = 'packages/scenarios/fixtures/'
ROUTES = FIXTURES + 'write-routes.json'
ATTEMPT = re.compile(r'^[0-9a-f]{32}$')


def _fail(message):
    raise ValueError('Invalid suite update: ' + message)


def _read(path, label):
    if path.is_symlink() or not path.is_file():
        _fail('missing regular ' + label)
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ValueError('Invalid suite update: malformed ' + label) from error


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _artifacts(output):
    value = _read(Path(output) / 'artifacts.json', 'artifact manifest')
    if not isinstance(value, dict) or any(not isinstance(name, str) or not isinstance(digest, str) for name, digest in value.items()):
        _fail('artifact manifest')
    return value


def _verified(output, name, artifacts):
    path = Path(output) / name
    if path.is_symlink() or not path.is_file():
        _fail('missing returned artifact: ' + name)
    value = path.read_bytes()
    if artifacts.get(name) != _digest(value):
        _fail('unverified returned artifact: ' + name)
    return value


def _regular_bytes(root, name, optional=False):
    path = Path(root) / name
    for parent in [path, *path.parents]:
        if parent == root: break
        if parent.is_symlink(): _fail('update path contains a symlink: ' + name)
    if not path.exists() and optional: return None
    if path.is_symlink() or not path.is_file(): _fail('missing regular update: ' + name)
    return path.read_bytes()


def is_update(submitted):
    return submitted.get('workflow') == 'suite-run' and submitted.get('suite', {}).get('update') is True


def _proposal(output, receipt, artifacts, plan, summary):
    required = {'version', 'plan_id', 'source_digest', 'shards', 'routes', 'ledgers'}
    if set(receipt) != required or receipt.get('version') != 1:
        _fail('parent receipt shape')
    if not isinstance(receipt['routes'], dict) or not isinstance(receipt['ledgers'], list) or not isinstance(receipt['shards'], list):
        _fail('parent receipt contents')
    if receipt['plan_id'] != plan['plan_id'] or receipt['source_digest'] != plan['source_digest']:
        _fail('parent receipt differs from frozen plan')
    expected_attempts = summary['shard_attempts']
    expected_ids = {shard['index']: shard['ids'] for shard in plan['shards']}
    if len(receipt['shards']) != len(expected_attempts): _fail('parent shard receipt count')
    seen_shards, seen_ids, changes = set(), set(), {}
    for item in receipt['shards']:
        if not isinstance(item, dict) or set(item) != {'shard', 'attempt'} or type(item['shard']) is not int or not isinstance(item['attempt'], str) or not ATTEMPT.fullmatch(item['attempt']):
            _fail('parent shard receipt')
        if item['shard'] in seen_shards: _fail('duplicate parent shard receipt')
        if item['shard'] not in expected_ids or item['attempt'] != expected_attempts[item['shard'] - 1]:
            _fail('parent shard receipt differs from suite summary')
        seen_shards.add(item['shard'])
        child = Path(output) / 'results' / 'attempts' / item['attempt']
        terminal = _read(child / 'terminal.json', 'child terminal')
        submitted = _read(child / 'submission.json', 'child submission')
        if (terminal.get('attempt') != item['attempt'] or terminal.get('workflow') != 'suite' or
                terminal.get('exit_code') != 0 or terminal.get('cleanup_verified') is not True or
                submitted.get('attempt') != item['attempt'] or submitted.get('workflow') != 'suite' or
                submitted.get('parent_attempt') != summary['parent_attempt'] or submitted.get('suite_update') is not True):
            _fail('child terminal or submission identity')
        child_artifacts = _read(child / 'artifacts.json', 'child artifacts')
        _verified(child, 'results/suite-update.json', child_artifacts)
        proposal = _read(child / 'results/suite-update.json', 'child update receipt')
        if (proposal.get('plan_id') != receipt['plan_id'] or proposal.get('source_digest') != receipt['source_digest'] or
                proposal.get('shard') != item['shard'] or proposal.get('planned_ids') != expected_ids[item['shard']]):
            _fail('child receipt differs from parent receipt')
        routes = proposal.get('routes')
        if not isinstance(routes, dict) or set(routes) != set(expected_ids[item['shard']]):
            _fail('child route ownership')
        if seen_ids & set(routes): _fail('overlapping child route ownership')
        seen_ids.update(routes)
        for identifier, value in routes.items():
            if value is not None and (not isinstance(value, list) or any(not isinstance(route, str) for route in value)):
                _fail('child route value')
            if receipt['routes'].get(identifier, object()) != value:
                _fail('parent route merge differs from child proposal')
        expected = proposal.get('ledger_expected')
        ledgers = proposal.get('ledgers')
        if (not isinstance(expected, list) or any(not isinstance(identifier, str) for identifier in expected) or
                len(expected) != len(set(expected)) or set(expected) - set(expected_ids[item['shard']]) or
                not isinstance(ledgers, list) or len(expected) != len(ledgers)):
            _fail('child ledger receipt')
        for ledger in ledgers:
            if not isinstance(ledger, dict) or set(ledger) != {'id', 'path', 'sha256'}:
                _fail('child ledger receipt')
            identifier, path, digest = ledger['id'], ledger['path'], ledger['sha256']
            if identifier not in expected or path != FIXTURES + identifier + '.ledger.jsonl':
                _fail('child ledger ownership')
            contents = _verified(child, 'results/updates/' + path, child_artifacts)
            if digest != _digest(contents): _fail('child ledger digest')
            if path in changes: _fail('overlapping child ledger ownership')
            changes[path] = contents
    if set(receipt['routes']) != seen_ids:
        _fail('parent routes do not exactly cover children')
    parent_ledgers = receipt['ledgers']
    if sorted(parent_ledgers, key=lambda x: x.get('path', '')) != parent_ledgers:
        _fail('parent ledger order')
    if {item.get('path') for item in parent_ledgers if isinstance(item, dict)} != set(changes):
        _fail('parent ledgers do not exactly cover children')
    for item in parent_ledgers:
        if not isinstance(item, dict) or set(item) != {'id', 'path', 'sha256'} or changes.get(item['path']) is None or item['sha256'] != _digest(changes[item['path']]):
            _fail('parent ledger digest')
        if _verified(output, 'results/updates/' + item['path'], artifacts) != changes[item['path']]:
            _fail('parent ledger differs from child proposal')
    return changes


def declarations(output):
    output = Path(output)
    submitted = _read(output / 'submission.json', 'submission')
    if not is_update(submitted): _fail('submission is not a suite update')
    artifacts = _artifacts(output)
    _verified(output, 'results/suite-updates.json', artifacts)
    _verified(output, 'results/suite-plan.json', artifacts)
    _verified(output, 'results/suite-run.json', artifacts)
    receipt = _read(output / 'results/suite-updates.json', 'parent update receipt')
    plan = validate_plan(_read(output / 'results/suite-plan.json', 'frozen plan'))
    summary = _read(output / 'results/suite-run.json', 'suite summary')
    if (not isinstance(summary, dict) or summary.get('status') != 'pass' or summary.get('exit_code') != 0 or
            summary.get('parent_attempt') != submitted.get('attempt') or summary.get('source_digest') != submitted.get('source_digest') or
            summary.get('plan_id') != plan['plan_id'] or not isinstance(summary.get('shard_attempts'), list) or
            len(summary['shard_attempts']) != len(plan['shards']) or any(not isinstance(item, str) or not ATTEMPT.fullmatch(item) for item in summary['shard_attempts'])):
        _fail('suite summary is not a complete successful frozen plan')
    if receipt.get('source_digest') != submitted.get('source_digest'):
        _fail('parent source differs from submission')
    changes = _proposal(output, receipt, artifacts, plan, summary)
    base_routes = _regular_bytes(output / 'source', ROUTES, optional=True)
    if base_routes is None: _fail('captured route manifest is missing')
    manifest = _read(output / 'manifest.json', 'captured source manifest')
    if not isinstance(manifest, list): _fail('captured source manifest')
    records = {item.get('path'): item for item in manifest if isinstance(item, dict)}
    for name in [ROUTES, *changes]:
        base = _regular_bytes(output / 'source', name, optional=True)
        record = records.get(name)
        if (base is None) != (record is None) or (record is not None and ('link' in record or record.get('sha256') != _digest(base))):
            _fail('captured source differs from its manifest: ' + name)
    base = json.loads(base_routes)
    if not isinstance(base, dict): _fail('captured route manifest')
    for identifier, routes in receipt['routes'].items():
        if routes is None: base.pop(identifier, None)
        else: base[identifier] = routes
    target_routes = (json.dumps(dict(sorted(base.items())), indent=2) + '\n').encode()
    returned = _verified(output, 'results/updates/' + ROUTES, artifacts)
    if returned != target_routes: _fail('merged route target differs from child proposals')
    changes[ROUTES] = returned
    result = {}
    for name, target in changes.items():
        result[name] = {'base': _regular_bytes(output / 'source', name, optional=True), 'target': target}
    return result


def source_is_current(repo, output, submitted, changes):
    from journey_updates import source_is_current as focused_source_is_current
    return focused_source_is_current(repo, output, submitted, changes)


def deliver(repo, output, changes, fault=lambda point: None):
    receipt = publish(repo, output, changes, fault=fault)
    for name in changes:
        print('[pandora] expectation returned: ' + str(Path(repo) / name), flush=True)
    print('[pandora] Review git diff, then validate without --update.', flush=True)
    return receipt
