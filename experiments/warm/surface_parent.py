"""Configured parent for one built-once, immutable browser surface plan."""
import json
from pathlib import Path
import shutil
from threading import Event

from configured_dispatch import dispatch
from evidence import validate_evidence
from surface_parent_evidence import summarize
from surface_suite import outputs_manifest, surface_request, validate_plan
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
    if request['action'] == 'shard':
        # The planner is position zero in the durable registry, never caller input.
        planner = parent.parent / children[0] / 'results' / 'outputs'
        plan = validate_plan(request['plan'])
        if outputs_manifest(planner, plan['app']) != plan['build']:
            raise ValueError('Planner outputs differ from frozen surface build')
        for output in ('apps/' + plan['app'] + '/dist', 'apps/' + plan['app'] + '/e2e/dist'):
            source, target = planner / output, child / 'source' / output
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target, copy_function=shutil.copy2)
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
    write(parent / 'surface-state.json', state)
    started = __import__('time').monotonic()
    planner_request = request | {'action': 'plan'}
    planner = _stage(parent, children[0], submitted, planner_request, children)
    state['dispatched'].append(children[0]); write(parent / 'surface-state.json', state)
    planner_stopped = _configured_child(parent, planner, started, queue, parent.name,
                                        submitted['worker_config']['execution_seconds'])
    terminal = retain(parent, planner); state['completed'].append(children[0]); write(parent / 'surface-state.json', state)
    if terminal['exit_code']:
        state['stop_reason'] = 'planning-failed'; write(parent / 'surface-state.json', state); return terminal['exit_code']
    plan = validate_plan(json.loads((planner / 'results' / 'surface-plan.json').read_text()), submitted)
    write(parent / 'results' / 'surface-plan.json', plan)
    reports, cancelled = {}, Event()
    def stage(index): return children[index], _stage(parent, children[index], submitted, {'action': 'shard', 'plan': plan, 'shard': index}, children)
    def run(identity, child, event): return _configured_child(parent, child, started, queue, parent.name, submitted['worker_config']['execution_seconds'], event)
    def finish(identity, child, stopped):
        terminal = retain(parent, child); state['completed'].append(identity); write(parent / 'surface-state.json', state); return terminal, stopped
    def classify(index, child, terminal, stopped):
        path = child / 'results' / 'surface-shard.json'
        if not path.exists(): return 'infrastructure'
        reports[index] = json.loads(path.read_text())
        return 'test-failure' if terminal['exit_code'] and not request['keep_going'] else None
    def stop(current, observed):
        if observed and observed not in ('deadline', 'queue-timeout'):
            queue.stop(parent.name, observed)
        return observed or current
    def persist(kind, value):
        if kind == 'dispatched': state['dispatched'].append(value)
        else: state['stop_reason'] = value
        write(parent / 'surface-state.json', state)
    reason = dispatch(count=request['shard_count'], parallel=submitted['worker_config']['max_parallel'], stage=stage, run=run,
                      finish=finish, classify=classify, stop=stop, persist=persist, cancelled=cancelled)
    result = summarize(plan, list(reports.values()), keep_going=request['keep_going'], stop_reason=reason)
    state['stop_reason'] = result['stop_reason']; write(parent / 'surface-state.json', state)
    write(parent / 'results' / 'surface-run.json', result); (parent / 'results' / 'exit-code').write_text(str(result['exit_code']) + '\n')
    return result['exit_code']
