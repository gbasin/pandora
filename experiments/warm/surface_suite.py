"""Immutable request, plan, and shard evidence for sharded browser surfaces."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from workflow_options import APPS, surface_outputs, surface_selectors


_HEX = re.compile('[0-9a-f]{64}\\Z')
_ID = re.compile('[^\\x00]+\\Z')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _identity(value, name):
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise ValueError(name + ' must be SHA-256 hex')
    return value


def _attempt(value, name):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{32}', value):
        raise ValueError(name + ' must be an attempt identity')
    return value


def _count(value):
    if type(value) is not int or not 1 <= value <= 32:
        raise ValueError('Surface shard count must be an integer from 1 through 32')
    return value


def surface_request(value):
    """Validate the private run/plan/shard metadata carried by surface attempts."""
    if not isinstance(value, dict) or not isinstance(value.get('action'), str):
        raise ValueError('Invalid surface suite request')
    action = value['action']
    if action == 'run':
        expected = {'action', 'app', 'selectors', 'shard_count', 'keep_going'}
        if set(value) != expected:
            raise ValueError('Invalid surface suite run request')
        app = value['app']
        if app not in APPS or type(value['keep_going']) is not bool:
            raise ValueError('Invalid surface suite run request')
        return {'action': action, 'app': app, 'selectors': surface_selectors(value['selectors']),
                'shard_count': _count(value['shard_count']), 'keep_going': value['keep_going']}
    if action == 'plan':
        expected = {'action', 'app', 'selectors', 'shard_count', 'keep_going'}
        if set(value) != expected:
            raise ValueError('Invalid surface suite plan request')
        return surface_request(value | {'action': 'run'}) | {'action': 'plan'}
    if action == 'shard':
        if set(value) != {'action', 'plan', 'shard'} or type(value['shard']) is not int:
            raise ValueError('Invalid surface suite shard request')
        plan = validate_plan(value['plan'])
        if not 1 <= value['shard'] <= plan['shard_count']:
            raise ValueError('Surface shard is outside its plan')
        return {'action': 'shard', 'plan': plan, 'shard': value['shard']}
    raise ValueError('Unsupported surface suite action')


def outputs_manifest(root, app):
    """Hash every regular generated build file, preserving its declared root."""
    root = Path(root)
    files = []
    for output in surface_outputs(app):
        directory = root / output
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError('Missing generated surface output: ' + output)
        for path in sorted(directory.rglob('*')):
            if path.is_symlink():
                raise ValueError('Unsafe generated surface output: ' + str(path))
            if not path.is_file():
                if not path.is_dir():
                    raise ValueError('Unsafe generated surface output: ' + str(path))
                continue
            relative = path.relative_to(root).as_posix()
            files.append({'path': relative, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    if not files:
        raise ValueError('Generated surface output is empty')
    manifest = {'app': app, 'files': files}
    return manifest | {'sha256': _digest(manifest)}


def plan_digest(plan):
    if not isinstance(plan, dict):
        raise ValueError('Surface plan must be an object')
    return _digest({key: value for key, value in plan.items() if key != 'plan_id'})


def _tests(value):
    if not isinstance(value, list):
        raise ValueError('Surface test inventory must be a list')
    result, seen = [], set()
    for row in value:
        if not isinstance(row, dict) or set(row) != {'id', 'project', 'file', 'title'}:
            raise ValueError('Invalid surface test inventory entry')
        if any(not isinstance(row[key], str) or not row[key] for key in row) or not _ID.fullmatch(row['id']):
            raise ValueError('Invalid surface test inventory entry')
        if row['id'] in seen:
            raise ValueError('Surface test inventory has duplicate IDs')
        seen.add(row['id']); result.append(row)
    return result


def _shards(value, tests, count):
    if not isinstance(value, list) or len(value) != count:
        raise ValueError('Surface plan has an invalid shard set')
    known, all_ids, result = {row['id'] for row in tests}, set(), []
    for index, row in enumerate(value, 1):
        if not isinstance(row, dict) or set(row) != {'index', 'test_ids', 'inventory_sha256'} or row['index'] != index:
            raise ValueError('Surface plan has an invalid shard')
        ids = row['test_ids']
        if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids) or len(ids) != len(set(ids)) or not set(ids) <= known:
            raise ValueError('Surface plan has invalid shard membership')
        if _digest(ids) != _identity(row['inventory_sha256'], 'Surface shard inventory'):
            raise ValueError('Surface shard inventory digest mismatch')
        all_ids.update(ids); result.append(row)
    if all_ids != known or sum(len(row['test_ids']) for row in result) != len(known):
        raise ValueError('Surface shards must partition the full inventory')
    return result


def validate_plan(plan, submitted=None):
    expected = {'version', 'parent_attempt', 'source_digest', 'app', 'selectors', 'shard_count',
                'keep_going', 'build', 'tests', 'shards', 'plan_id'}
    if not isinstance(plan, dict) or set(plan) != expected or plan['version'] != 1:
        raise ValueError('Invalid surface plan')
    _attempt(plan['parent_attempt'], 'Surface parent')
    _identity(plan['source_digest'], 'Surface source digest')
    if plan['app'] not in APPS or type(plan['keep_going']) is not bool:
        raise ValueError('Invalid surface plan')
    selectors = surface_selectors(plan['selectors'])
    count = _count(plan['shard_count'])
    build = plan['build']
    if not isinstance(build, dict) or set(build) != {'app', 'files', 'sha256'} or build['app'] != plan['app']:
        raise ValueError('Invalid surface build manifest')
    if build.get('sha256') != _digest({'app': build['app'], 'files': build['files']}):
        raise ValueError('Surface build manifest digest mismatch')
    if not isinstance(build['files'], list) or not build['files']:
        raise ValueError('Surface build manifest is empty')
    paths = set()
    roots = tuple(path + '/' for path in surface_outputs(plan['app']))
    for row in build['files']:
        if not isinstance(row, dict) or set(row) != {'path', 'sha256'} or not isinstance(row['path'], str):
            raise ValueError('Invalid surface build file')
        path = PurePosixPath(row['path'])
        if (path.is_absolute() or '..' in path.parts or '.' in path.parts or str(path) != row['path']
                or not row['path'].startswith(roots) or row['path'] in paths):
            raise ValueError('Invalid surface build file')
        paths.add(row['path'])
        _identity(row['sha256'], 'Surface build file digest')
    tests = _tests(plan['tests'])
    if not tests:
        raise ValueError('Surface selection contains no tests')
    shards = _shards(plan['shards'], tests, count)
    if _identity(plan['plan_id'], 'Surface plan ID') != plan_digest(plan):
        raise ValueError('Surface plan ID mismatch')
    if submitted is not None:
        request = surface_request(submitted['surface_suite'])
        if request['action'] not in ('run', 'plan') or any(plan[key] != request[key] for key in ('app', 'selectors', 'shard_count', 'keep_going')):
            raise ValueError('Surface plan differs from accepted request')
        if plan['parent_attempt'] != submitted.get('parent_attempt', submitted.get('attempt')) or plan['source_digest'] != submitted['source_digest']:
            raise ValueError('Surface plan differs from accepted identity')
    return plan | {'selectors': selectors, 'tests': tests, 'shards': shards}


def shard_request(plan, index):
    return surface_request({'action': 'shard', 'plan': plan, 'shard': index})


def validate_shard(report, plan, index):
    plan = validate_plan(plan)
    expected = {'version', 'plan_id', 'parent_attempt', 'source_digest', 'app', 'shard', 'planned_ids',
                'observed_ids', 'outcomes', 'exit_code', 'detail'}
    if not isinstance(report, dict) or set(report) != expected or report['version'] != 1 or type(report['shard']) is not int:
        raise ValueError('Invalid surface shard report')
    if not 1 <= index <= plan['shard_count']:
        raise ValueError('Surface shard index is outside its plan')
    if (report['plan_id'] != plan['plan_id'] or report['parent_attempt'] != plan['parent_attempt']
            or report['source_digest'] != plan['source_digest'] or report['app'] != plan['app'] or report['shard'] != index):
        raise ValueError('Surface shard report identity mismatch')
    planned = plan['shards'][index - 1]['test_ids']
    if report['planned_ids'] != planned or report['observed_ids'] != planned:
        raise ValueError('Surface shard observed membership mismatch')
    if not isinstance(report['outcomes'], list) or not isinstance(report['detail'], str) or type(report['exit_code']) is not int or report['exit_code'] < 0:
        raise ValueError('Invalid surface shard report')
    outcomes = {row.get('id'): row for row in report['outcomes'] if isinstance(row, dict) and set(row) == {'id', 'status'}}
    statuses = ('passed', 'failed', 'skipped', 'timedOut', 'interrupted', 'expected', 'unexpected')
    if len(outcomes) != len(report['outcomes']) or set(outcomes) != set(planned) or any(row['status'] not in statuses for row in outcomes.values()):
        raise ValueError('Invalid surface shard outcomes')
    if report['exit_code'] == 0 and any(row['status'] in ('failed', 'timedOut', 'interrupted', 'unexpected') for row in outcomes.values()):
        raise ValueError('Successful surface shard has failing outcomes')
    return report


def aggregate(plan, reports):
    plan = validate_plan(plan)
    if not isinstance(reports, list) or len(reports) != plan['shard_count']:
        raise ValueError('Surface aggregation requires every shard report')
    checked = [validate_shard(report, plan, index) for index, report in enumerate(reports, 1)]
    failures = [row for row in checked if row['exit_code'] != 0]
    return {'plan_id': plan['plan_id'], 'source_digest': plan['source_digest'], 'app': plan['app'],
            'status': 'fail' if failures else 'pass', 'exit_code': 1 if failures else 0,
            'shards': checked, 'failed_shards': [row['shard'] for row in failures]}
