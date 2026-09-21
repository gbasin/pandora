"""Canonical aggregate receipt for independently admitted surface shards."""
from surface_suite import validate_plan, validate_shard


def summarize(plan, reports, *, keep_going, stop_reason=None):
    plan = validate_plan(plan)
    if type(keep_going) is not bool or keep_going != plan['keep_going']:
        raise ValueError('Surface continuation policy differs from the plan')
    by_index = {}
    for report in reports:
        checked = validate_shard(report, plan, report['shard'])
        if checked['shard'] in by_index: raise ValueError('Duplicate surface shard receipt')
        by_index[checked['shard']] = checked
    completed = sorted(by_index)
    unrun = [index for index in range(1, plan['shard_count'] + 1) if index not in by_index]
    failures = [index for index in completed if by_index[index]['exit_code']]
    stops = {None, 'test-failure', 'infrastructure', 'deadline', 'queue-timeout', 'cancelled'}
    if stop_reason not in stops: raise ValueError('Invalid surface stop reason')
    if unrun and stop_reason is None: raise ValueError('Missing surface shard receipt has no stop reason')
    if any(by_index[index]['exit_code'] not in (0, 1) for index in failures):
        if stop_reason not in ('infrastructure', 'deadline', 'queue-timeout', 'cancelled'):
            raise ValueError('Infrastructure failure lacks a matching stop reason')
    if failures and stop_reason is None: stop_reason = 'test-failure'
    if stop_reason == 'test-failure' and not failures: raise ValueError('Test stop requires failed shard')
    if stop_reason in ('infrastructure', 'deadline', 'queue-timeout'): code = 75
    elif stop_reason == 'cancelled': code = 130
    else: code = 0 if not failures and not unrun else 1 if failures else 75
    return {'version': 1, 'plan_id': plan['plan_id'], 'parent_attempt': plan['parent_attempt'],
            'source_digest': plan['source_digest'], 'app': plan['app'], 'keep_going': keep_going,
            'stop_reason': stop_reason, 'completed_shards': completed, 'unrun_shards': unrun,
            'reports': [by_index[index] for index in completed], 'exit_code': code,
            'status': 'pass' if code == 0 else 'fail' if code == 1 else 'stopped'}


def validate_summary(plan, result):
    required = {'version', 'plan_id', 'parent_attempt', 'source_digest', 'app', 'keep_going',
                'stop_reason', 'completed_shards', 'unrun_shards', 'reports', 'exit_code', 'status'}
    if not isinstance(result, dict) or set(result) != required: raise ValueError('Invalid surface parent summary')
    expected = summarize(plan, result['reports'], keep_going=result['keep_going'], stop_reason=result['stop_reason'])
    if result != expected: raise ValueError('Surface parent summary differs from receipts')
    return result
