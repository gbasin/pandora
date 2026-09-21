"""Configured parent for one built-once, immutable browser surface plan."""
import json
from pathlib import Path
import shutil
from threading import Event

from configured_dispatch import dispatch
from evidence import validate_evidence
from surface_parent_evidence import summarize
from surface_suite import surface_request, validate_plan, validate_shard
from suite_parent import _configured_child, reserve, retain, write
from suite_parent_cleanup import validate_registry


def _stage(parent, identity, submitted, request, children):
    """Create a child only from frozen parent inputs and planner-owned outputs."""
    child = parent.parent / identity
    child.mkdir()
    from worker_bundle import NAMES
    for name in NAMES + ['runtime.Dockerfile', 'manifest.json']:
        shutil.copyfile(parent / name, child / name)
    shutil.copytree(parent / 'source', child / 'source', copy_function=__import__('os').link, symlinks=True)
    metadata = submitted | {'attempt': identity, 'workflow': 'surface', 'parent_attempt': parent.name,
                            'surface_suite': request, 'queue_timeout_seconds': submitted['queue_timeout_seconds']}
    write(child / 'submission.json', metadata)
    return child


def execute(parent, submitted):
    """Run a configured surface suite; worker routing invokes this before admission."""
    from worker_runtime import register
    request = surface_request(submitted['surface_suite'])
    if request['action'] != 'run': raise ValueError('Surface parent requires a run request')
    queue = register(parent.parent.parent, submitted, parent.name)
    children = reserve(parent, request['shard_count'])
    state = {'version': 2, 'reserved': children, 'dispatched': [], 'completed': [], 'queue_seconds': 0.0, 'stop_reason': None}
    write(parent / 'suite-state.json', state)
    started = __import__('time').monotonic()
    planner_request = request | {'action': 'plan'}
    planner = _stage(parent, children[0], submitted, planner_request, children)
    state['dispatched'].append(children[0]); write(parent / 'suite-state.json', state)
    planner_stopped = _configured_child(parent, planner, started, queue, parent.name,
                                        submitted['worker_config']['execution_seconds'])
    terminal = retain(parent, planner); state['completed'].append(children[0]); write(parent / 'suite-state.json', state)
    if terminal['exit_code']:
        reason = 'deadline' if terminal['exit_code'] == 124 else 'planning-failed'
        waited = next(row['waited'] for row in queue.snapshot()['invocations'] if row['identity'] == parent.name)
        state['stop_reason'] = reason; state['queue_seconds'] = waited; write(parent / 'suite-state.json', state)
        from worker_config import identity as config_identity
        write(parent / 'queue.json', {'mode': 'resource', 'invocation': parent.name, 'waited': waited,
              'config_digest': config_identity(submitted['worker_config'])})
        write(parent / 'results' / 'surface-error.json', {'version': 1, 'parent_attempt': parent.name,
              'source_digest': submitted['source_digest'], 'plan_attempt': children[0], 'reason': reason,
              'exit_code': terminal['exit_code']})
        code = 75 if reason == 'deadline' else terminal['exit_code']
        (parent / 'results' / 'exit-code').write_text(str(code) + '\n'); return code
    plan = validate_plan(json.loads((planner / 'results' / 'surface-plan.json').read_text()), submitted)
    write(parent / 'results' / 'surface-plan.json', plan)
    reports, cancelled = {}, Event()
    def stage(index): return children[index], _stage(parent, children[index], submitted, {'action': 'shard', 'plan': plan, 'shard': index}, children)
    def run(identity, child, event): return _configured_child(parent, child, started, queue, parent.name, submitted['worker_config']['execution_seconds'], event)
    def finish(identity, child, stopped):
        terminal = retain(parent, child); state['completed'].append(identity); write(parent / 'suite-state.json', state); return terminal, stopped
    def classify(index, child, terminal, stopped):
        path = child / 'results' / 'surface-shard.json'
        if not path.exists(): return 'infrastructure'
        reports[index] = json.loads(path.read_text())
        if terminal['exit_code'] not in (0, 1): return 'infrastructure'
        return 'test-failure' if terminal['exit_code'] and not request['keep_going'] else None
    def stop(current, observed):
        if observed and observed not in ('deadline', 'queue-timeout'):
            queue.stop(parent.name, observed)
        return observed or current
    def persist(kind, value):
        if kind == 'dispatched': state['dispatched'].append(value)
        else: state['stop_reason'] = value
        write(parent / 'suite-state.json', state)
    reason = dispatch(count=request['shard_count'], parallel=submitted['worker_config']['max_parallel'], stage=stage, run=run,
                      finish=finish, classify=classify, stop=stop, persist=persist, cancelled=cancelled)
    result = summarize(plan, list(reports.values()), keep_going=request['keep_going'], stop_reason=reason)
    waited = next(row['waited'] for row in queue.snapshot()['invocations'] if row['identity'] == parent.name)
    state['queue_seconds'] = waited
    from worker_config import identity as config_identity
    write(parent / 'queue.json', {'mode': 'resource', 'invocation': parent.name,
          'waited': waited, 'config_digest': config_identity(submitted['worker_config'])})
    state['stop_reason'] = result['stop_reason']; write(parent / 'suite-state.json', state)
    write(parent / 'results' / 'surface-run.json', result); (parent / 'results' / 'exit-code').write_text(str(result['exit_code']) + '\n')
    return result['exit_code']


def validate_result(stage, submitted, terminal, manifest):
    """Bind retained surface plan/shards to the one accepted configured invocation."""
    required = {'children.json', 'suite-state.json', 'results/surface-plan.json',
                'results/surface-run.json', 'queue.json'}
    if not required <= set(manifest):
        raise ValueError('Surface parent lacks invocation evidence')
    request = surface_request(submitted['surface_suite'])
    children = validate_registry(Path(submitted['attempt']), json.loads((stage / 'children.json').read_text()))
    if len(children) != request['shard_count'] + 1: raise ValueError('Surface reserved child count differs')
    state = json.loads((stage / 'suite-state.json').read_text())
    if (state.get('version') != 2 or state.get('reserved') != children or not set(state.get('completed', [])) <= set(state.get('dispatched', []))
            or not set(state.get('dispatched', [])) <= set(children) or children[0] not in state.get('completed', [])):
        raise ValueError('Surface dispatch journal differs from reserved work')
    plan = validate_plan(json.loads((stage / 'results/surface-plan.json').read_text()), submitted)
    result = json.loads((stage / 'results/surface-run.json').read_text())
    from surface_parent_evidence import validate_summary
    validate_summary(plan, result)
    if result['exit_code'] != terminal['exit_code'] or state.get('stop_reason') != result['stop_reason']:
        raise ValueError('Surface parent terminal differs from aggregate')
    queue = json.loads((stage / 'queue.json').read_text())
    from worker_config import identity as config_identity
    if (queue.get('mode') != 'resource' or queue.get('invocation') != submitted['attempt']
            or queue.get('config_digest') != config_identity(submitted['worker_config'])
            or queue.get('waited') != state.get('queue_seconds')):
        raise ValueError('Surface queue receipt differs from configured invocation')
    for index, identity in enumerate(children):
        if identity not in state['completed']: continue
        child = stage / 'results' / 'attempts' / identity
        for name in ('submission.json', 'terminal.json', 'artifacts.json'):
            if str((child / name).relative_to(stage)) not in manifest: raise ValueError('Surface child receipt missing')
        metadata = json.loads((child / 'submission.json').read_text())
        expected = request | {'action': 'plan'} if index == 0 else {'action': 'shard', 'plan': plan, 'shard': index}
        if (metadata.get('attempt') != identity or metadata.get('parent_attempt') != submitted['attempt']
                or metadata.get('workflow') != 'surface' or metadata.get('surface_suite') != expected
                or metadata.get('source_digest') != submitted['source_digest'] or metadata.get('worker_config') != submitted['worker_config']):
            raise ValueError('Surface child metadata differs from parent')
        receipt = validate_evidence(child, identity, metadata)
        if index == 0:
            if receipt['exit_code'] or json.loads((child / 'results/surface-plan.json').read_text()) != plan: raise ValueError('Surface planner receipt differs')
        else:
            path = child / 'results/surface-shard.json'
            if path.exists(): validate_shard(json.loads(path.read_text()), plan, index)
            elif receipt['exit_code'] == 0: raise ValueError('Successful surface shard lacks report')
