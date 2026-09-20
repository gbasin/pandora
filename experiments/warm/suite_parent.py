"""One remotely owned invocation with independently admitted, bounded shards."""
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Event

from evidence import validate_evidence
from suite import suite_request
from suite_parent_evidence import summarize, validate_summary
from suite_parent_cleanup import validate_registry


def write(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def reserve(parent, count):
    registry = {'version': 1, 'parent_attempt': parent.name,
                'children': [uuid.uuid4().hex for _ in range(count + 1)]}
    # No child can exist before the owned identity registry is durable.
    write(parent / 'children.json', registry)
    (parent / 'suite-cleanup.pending').touch()
    return registry['children']


def stage_child(parent, identity, submitted, request, remaining=None):
    child = parent.parent / identity
    child.mkdir()  # Existing work is never replaced or restarted.
    from worker_bundle import NAMES
    for name in NAMES + ['runtime.Dockerfile', 'manifest.json']:
        shutil.copyfile(parent / name, child / name)
    shutil.copytree(parent / 'source', child / 'source', copy_function=os.link, symlinks=True)
    # A configured invocation owns one immutable queue budget.  Every child
    # carries the submitted value; it is never a remaining per-child budget.
    timeout = submitted['queue_timeout_seconds'] if submitted.get('worker_config') else remaining
    metadata = submitted | {'attempt': identity, 'workflow': 'suite', 'suite': request,
                            'parent_attempt': parent.name, 'queue_timeout_seconds': timeout,
                            'suite_update': submitted['suite'].get('update', False)}
    write(child / 'submission.json', metadata)
    return child


def queue_wait(child):
    path = child / 'queue.json'
    if path.exists():
        value = json.loads(path.read_text())['waited']
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError('Invalid child queue accounting')
        return value
    path = child / 'queue-start.json'
    if path.exists():
        started = json.loads(path.read_text())['monotonic']
        if isinstance(started, bool) or not isinstance(started, (float, int)) or not math.isfinite(started) or started < 0:
            raise ValueError('Invalid child queue start')
        return max(0, time.monotonic() - started)
    return 0


def run_child(parent, child, started, waited):
    """Stream output while the remote parent, not the SSH client, owns execution."""
    stopped = None
    offsets = [0, 0]
    with (child / 'stdout.log').open('wb') as out, (child / 'stderr.log').open('wb') as err:
        process = subprocess.Popen([sys.executable, '-u', 'worker.py'], cwd=child, stdout=out, stderr=err)
        def stream():
            for index, name in enumerate(('stdout.log', 'stderr.log')):
                with (child / name).open('rb') as log:
                    log.seek(offsets[index])
                    chunk = log.read()
                    offsets[index] = log.tell()
                if chunk:
                    target = sys.stdout if index == 0 else sys.stderr
                    target.write(chunk.decode('utf-8', errors='replace'))
                    target.flush()
        try:
            while process.poll() is None:
                stream()
                if time.monotonic() - started - waited - queue_wait(child) >= 1500:
                    stopped = 'deadline'
                    break
                time.sleep(0.25)
        finally:
            if process.poll() is None:
                (child / 'cancel.request').touch()
                process.terminate()
                try:
                    process.wait(timeout=35)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            stream()
    return stopped


def retain(parent, child):
    """Copy verified receipts into the parent artifact namespace before release."""
    metadata = json.loads((child / 'submission.json').read_text())
    terminal = validate_evidence(child, child.name, metadata)
    target = parent / 'results/attempts' / child.name
    target.mkdir(parents=True)
    artifacts = json.loads((child / 'artifacts.json').read_text())
    for name in [*artifacts, 'artifacts.json', 'terminal.json', 'submission.json']:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(child / name, destination)
    return terminal


def execute(parent, submitted):
    if submitted.get('worker_config'):
        return execute_configured(parent, submitted)
    request = suite_request(submitted['suite'])
    if request['action'] != 'run':
        raise ValueError('Parent requires a suite run request')
    children = reserve(parent, request['shard_count'])
    started, waited = time.monotonic(), 0.0
    reports, plan, reason = [], None, None
    state = {'version': 1, 'completed': [], 'queue_seconds': 0.0, 'stop_reason': None}
    write(parent / 'suite-state.json', state)
    for index, identity in enumerate(children):
        remaining = submitted['queue_timeout_seconds'] - waited
        if remaining <= 0:
            reason = 'queue-timeout'
            break
        if time.monotonic() - started - waited >= 1500:
            reason = 'deadline'
            break
        if (parent / 'cancel.request').exists():
            raise KeyboardInterrupt
        task = ({'action': 'plan', 'shard_count': request['shard_count'], 'selection': request['selection']}
                if index == 0 else {'action': 'shard', 'plan': plan, 'shard': index})
        print(f'[pandora] suite {parent.name}: ' + ('planning frozen source' if index == 0 else
              f'starting shard {index}/{request["shard_count"]}') +
              f'; cumulative queue remaining {remaining:.1f}s', flush=True)
        child = stage_child(parent, identity, submitted, task, remaining)
        reason = run_child(parent, child, started, waited)
        # Missing or unverified receipts are unresolved, never intentionally skipped.
        terminal = retain(parent, child)
        waited += queue_wait(child)
        state.update(queue_seconds=waited, completed=[*state['completed'], identity])
        write(parent / 'suite-state.json', state)
        if terminal['exit_code'] == 124:
            reason = 'deadline'
        if index == 0:
            if terminal['exit_code'] != 0:
                if reason == 'deadline':
                    state['stop_reason'] = reason
                    write(parent / 'suite-state.json', state)
                    write(parent / 'results/suite-error.json', {
                        'version': 1, 'parent_attempt': parent.name, 'source_digest': submitted['source_digest'],
                        'plan_attempt': identity, 'reason': reason, 'exit_code': terminal['exit_code']})
                    (parent / 'results/exit-code').write_text('75\n')
                    return 75
                state['stop_reason'] = 'planning-failed'
                write(parent / 'suite-state.json', state)
                write(parent / 'results/suite-error.json', {
                    'version': 1, 'parent_attempt': parent.name, 'source_digest': submitted['source_digest'],
                    'plan_attempt': identity, 'reason': 'planning-failed', 'exit_code': terminal['exit_code']})
                (parent / 'results/exit-code').write_text(str(terminal['exit_code']) + '\n')
                print('[pandora] Suite planning failed; no shards dispatched. See results/attempts/' + identity, flush=True)
                return terminal['exit_code']
            plan = json.loads((child / 'results/suite-plan.json').read_text())
            write(parent / 'results/suite-plan.json', plan)
        else:
            report_path = child / 'results/suite-shard.json'
            if not report_path.exists():
                reason = reason or ('queue-timeout' if terminal['exit_code'] == 75 else 'infrastructure')
            else:
                report = json.loads(report_path.read_text())
                reports.append(report)
                if terminal['exit_code'] != 0:
                    infra = (report['errors']['infrastructureFailures'] or report['errors']['unrunJourneys']
                             or report['exit_code'] not in (0, 1))
                    reason = reason or ('infrastructure' if infra else
                                        None if request['keep_going'] else 'test-failure')
                    print(f'[pandora] shard {index} failed; ' +
                          ('stopping new dispatch' if reason else
                           'no remaining shards' if index == request['shard_count'] else
                           'continuing because --keep-going was requested'), flush=True)
        if reason:
            break
    if plan is None:
        return 75
    result = summarize(plan, reports, parent_attempt=parent.name, plan_attempt=children[0],
                       shard_attempts=children[1:], keep_going=request['keep_going'], stop_reason=reason)
    state['stop_reason'] = result['stop_reason']
    write(parent / 'suite-state.json', state)
    write(parent / 'results/suite-run.json', result)
    if result['exit_code'] == 0 and request.get('update'):
        from suite_updates import merge
        merge(parent, plan, [parent / 'results/attempts' / identity for identity in children[1:]])
    (parent / 'results/exit-code').write_text(str(result['exit_code']) + '\n')
    print(f'[pandora] suite {result["status"]}; {len(reports)}/{request["shard_count"]} shard reports; '
          f'not run: {", ".join(result["unrun_journeys"]) or "none"}; queue used {waited:.1f}s', flush=True)
    return result['exit_code']


def _configured_waited(queue, invocation):
    return next(row['waited'] for row in queue.snapshot()['invocations'] if row['identity'] == invocation)


def _configured_child(parent, child, started, queue, invocation, execution_seconds, cancelled=None):
    """Run one child without turning the parent into an admitted resource user."""
    stopped, offsets = None, [0, 0]
    with (child / 'stdout.log').open('wb') as out, (child / 'stderr.log').open('wb') as err:
        process = subprocess.Popen([sys.executable, '-u', 'worker.py'], cwd=child, stdout=out, stderr=err)
        def stream():
            for index, name in enumerate(('stdout.log', 'stderr.log')):
                with (child / name).open('rb') as log:
                    log.seek(offsets[index]); chunk = log.read(); offsets[index] = log.tell()
                if chunk:
                    target = sys.stdout if index == 0 else sys.stderr
                    target.write(chunk.decode('utf-8', errors='replace')); target.flush()
        try:
            while process.poll() is None:
                stream()
                if cancelled is not None and cancelled.is_set():
                    stopped = 'cancelled'
                    break
                # Queue time is invocation wall time, and is subtracted once.
                if time.monotonic() - started - _configured_waited(queue, invocation) >= execution_seconds:
                    stopped = 'deadline'
                    queue.stop(invocation, 'deadline')
                    break
                time.sleep(.25)
        finally:
            if process.poll() is None:
                # The worker maps this marker to exit 124 and updates a partial
                # shard receipt before publishing its terminal evidence.
                if stopped == 'deadline':
                    (child / 'deadline.request').touch()
                else:
                    (child / 'cancel.request').touch()
                process.terminate()
                try: process.wait(timeout=35)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
            stream()
    return stopped


def execute_configured(parent, submitted):
    """Configured suite parent: register once, then fan out admitted children."""
    from worker_runtime import register
    request = suite_request(submitted['suite'])
    if request['action'] != 'run':
        raise ValueError('Parent requires a suite run request')
    queue = register(parent.parent.parent, submitted, parent.name)
    children = reserve(parent, request['shard_count'])
    state = {'version': 2, 'reserved': children, 'dispatched': [], 'completed': [],
             'queue_seconds': 0.0, 'stop_reason': None}
    write(parent / 'suite-state.json', state)
    started, plan, reports, reason = time.monotonic(), None, {}, None

    def stop_reason(value):
        nonlocal reason
        priority = {None: 0, 'test-failure': 1, 'planning-failed': 2,
                    'infrastructure': 2, 'queue-timeout': 3, 'deadline': 4}
        if priority[value] > priority[reason]:
            reason = value
            state['stop_reason'] = value
            write(parent / 'suite-state.json', state)

    def finish(identity, child, stopped):
        nonlocal reason
        terminal = retain(parent, child)
        state['completed'].append(identity)
        write(parent / 'suite-state.json', state)
        if stopped == 'deadline' or terminal['exit_code'] == 124:
            stop_reason('deadline')
        return terminal

    # Planning must finish before frozen shards can be defined, but it is not a
    # parent resource lease.  Its child joins the same invocation.
    planner = stage_child(parent, children[0], submitted,
                         {'action': 'plan', 'shard_count': request['shard_count'], 'selection': request['selection']})
    state['dispatched'].append(children[0]); write(parent / 'suite-state.json', state)
    terminal = finish(children[0], planner, _configured_child(
        parent, planner, started, queue, parent.name, submitted['worker_config']['execution_seconds']))
    if terminal['exit_code'] != 0:
        reason = reason or ('deadline' if terminal['exit_code'] == 124 else 'planning-failed')
        state['stop_reason'] = reason; state['queue_seconds'] = _configured_waited(queue, parent.name)
        write(parent / 'suite-state.json', state)
        write(parent / 'queue.json', {'mode': 'resource', 'invocation': parent.name,
              'waited': state['queue_seconds'], 'config_digest': __import__('worker_config').identity(submitted['worker_config'])})
        write(parent / 'results/suite-error.json', {'version': 1, 'parent_attempt': parent.name,
              'source_digest': submitted['source_digest'], 'plan_attempt': children[0], 'reason': reason,
              'exit_code': terminal['exit_code']})
        code = 75 if reason == 'deadline' else terminal['exit_code']
        (parent / 'results/exit-code').write_text(str(code) + '\n'); return code
    plan = json.loads((planner / 'results/suite-plan.json').read_text())
    write(parent / 'results/suite-plan.json', plan)

    next_shard, futures, cancelled = 1, {}, Event()
    pool = ThreadPoolExecutor(max_workers=submitted['worker_config']['max_parallel'])
    try:
        while (next_shard <= request['shard_count'] and reason is None) or futures:
            while reason is None and next_shard <= request['shard_count'] and len(futures) < submitted['worker_config']['max_parallel']:
                identity = children[next_shard]
                child = stage_child(parent, identity, submitted, {'action': 'shard', 'plan': plan, 'shard': next_shard})
                state['dispatched'].append(identity); write(parent / 'suite-state.json', state)
                futures[pool.submit(_configured_child, parent, child, started, queue, parent.name,
                                     submitted['worker_config']['execution_seconds'], cancelled)] = (identity, child, next_shard)
                next_shard += 1
            if not futures: break
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                identity, child, index = futures.pop(future)
                terminal = finish(identity, child, future.result())
                report_path = child / 'results/suite-shard.json'
                if not report_path.exists():
                    stopped = next(row['stopped'] for row in queue.snapshot()['invocations'] if row['identity'] == parent.name)
                    if stopped == 'test-failure' and terminal['exit_code'] == 75:
                        stop_reason('test-failure')  # A withdrawn waiter never tested a shard.
                    else:
                        stop_reason('queue-timeout' if stopped == 'queue-timeout' else 'infrastructure')
                else:
                    report = json.loads(report_path.read_text()); reports[index] = report
                    infra = (report['errors']['infrastructureFailures'] or report['errors']['unrunJourneys']
                             or report['exit_code'] not in (0, 1))
                    if infra: stop_reason('infrastructure')
                    elif terminal['exit_code'] and not request['keep_going']:
                        stop_reason('test-failure')
                if reason:
                    state['stop_reason'] = reason; write(parent / 'suite-state.json', state)
                    if reason != 'queue-timeout' and reason != 'deadline': queue.stop(parent.name, reason)
            if reason and not futures: break
    except BaseException as error:
        cancelled.set()
        failed_reason = 'cancelled' if isinstance(error, KeyboardInterrupt) else 'infrastructure'
        state['stop_reason'] = failed_reason
        # Preserve the triggering exception even when the scheduler is already
        # stopped or unavailable.  No synthetic child receipt is written here.
        try:
            queue.stop(parent.name, failed_reason)
        except BaseException:
            pass
        try:
            write(parent / 'suite-state.json', state)
        except BaseException:
            pass
        # Futures observe the event and reap their children; do not leave the
        # executor waiting for an execution deadline.
        try:
            pool.shutdown(wait=True, cancel_futures=True)
        except BaseException:
            pass
        raise
    else:
        pool.shutdown(wait=True)
    state['queue_seconds'] = _configured_waited(queue, parent.name)
    write(parent / 'queue.json', {'mode': 'resource', 'invocation': parent.name,
          'waited': state['queue_seconds'], 'config_digest': __import__('worker_config').identity(submitted['worker_config'])})
    result = summarize(plan, list(reports.values()), parent_attempt=parent.name, plan_attempt=children[0],
                       shard_attempts=children[1:], keep_going=request['keep_going'], stop_reason=reason)
    state['stop_reason'] = result['stop_reason']; write(parent / 'suite-state.json', state)
    write(parent / 'results/suite-run.json', result)
    if result['exit_code'] == 0 and request.get('update'):
        from suite_updates import merge
        merge(parent, plan, [parent / 'results/attempts' / identity for identity in children[1:]])
    (parent / 'results/exit-code').write_text(str(result['exit_code']) + '\n')
    print(f'[pandora] suite {result["status"]}; {len(reports)}/{request["shard_count"]} shard reports; '
          f'not run: {", ".join(result["unrun_journeys"]) or "none"}; '
          f'invocation queue used {state["queue_seconds"]:.1f}s', flush=True)
    return result['exit_code']


def validate_result(stage, submitted, terminal, manifest):
    """Bind every child receipt to the parent's reserved attempt identities."""
    if submitted.get('worker_config'):
        return validate_configured_result(stage, submitted, terminal, manifest)
    if 'results/suite-run.json' not in manifest:
        return validate_planning_failure(stage, submitted, terminal, manifest)
    for name in ('children.json', 'results/suite-plan.json', 'suite-state.json'):
        if name not in manifest:
            raise ValueError('Suite invocation lacks its identity registry or plan')
    registry = json.loads((stage / 'children.json').read_text())
    # Downloads use a temporary staging directory, whose name is not the attempt.
    identities = validate_registry(Path(submitted['attempt']), registry)
    request = suite_request(submitted['suite'])
    result = json.loads((stage / 'results/suite-run.json').read_text())
    plan = json.loads((stage / 'results/suite-plan.json').read_text())
    if (len(identities) != request['shard_count'] + 1 or
            plan['source_digest'] != submitted['source_digest'] or plan['selection'] != request['selection'] or
            len(plan['shards']) != request['shard_count'] or
            result['parent_attempt'] != submitted['attempt'] or result['plan_attempt'] != identities[0] or
            result['shard_attempts'] != identities[1:] or result['keep_going'] != request['keep_going'] or
            result['exit_code'] != terminal['exit_code']):
        raise ValueError('Suite invocation differs from submitted work or reserved identities')
    state = json.loads((stage / 'suite-state.json').read_text())
    if (state['completed'] != identities[:len(state['completed'])] or not state['completed'] or
            state['stop_reason'] != result['stop_reason']):
        raise ValueError('Suite completion journal does not match reserved work')
    reports = []
    for index, identity in enumerate(state['completed']):
        child = stage / 'results/attempts' / identity
        for name in ('submission.json', 'terminal.json', 'artifacts.json'):
            if str((child / name).relative_to(stage)) not in manifest:
                raise ValueError('Missing child receipt in invocation artifacts')
        metadata = json.loads((child / 'submission.json').read_text())
        expected = ({'action': 'plan', 'shard_count': request['shard_count'], 'selection': request['selection']}
                    if index == 0 else {'action': 'shard', 'plan': plan, 'shard': index})
        if (metadata.get('parent_attempt') != submitted['attempt'] or metadata['suite'] != expected or
                metadata['source_digest'] != submitted['source_digest'] or metadata['attempt'] != identity or
                metadata.get('workflow') != 'suite' or metadata.get('suite_update', False) != request.get('update', False)):
            raise ValueError('Child receipt does not match reserved task')
        receipt = validate_evidence(child, identity, metadata)
        path = child / ('results/suite-plan.json' if index == 0 else 'results/suite-shard.json')
        if index == 0:
            if receipt['exit_code'] or json.loads(path.read_text()) != plan:
                raise ValueError('Parent plan differs from verified planning attempt')
        elif path.exists():
            reports.append(json.loads(path.read_text()))
        elif receipt['exit_code'] == 0 or result['stop_reason'] not in ('infrastructure', 'queue-timeout', 'deadline'):
            raise ValueError('Missing shard evidence is not explained by a failed attempt')
    validate_queue_accounting(stage, submitted, manifest, state, identities)
    validate_summary(plan, reports, result)


def validate_configured_result(stage, submitted, terminal, manifest):
    """Validate unordered configured child completion and one invocation queue receipt."""
    if 'results/suite-run.json' not in manifest:
        return validate_configured_planning_failure(stage, submitted, terminal, manifest)
    required = {'children.json', 'suite-state.json', 'results/suite-plan.json',
                'results/suite-run.json', 'queue.json'}
    if not required <= set(manifest):
        raise ValueError('Configured suite invocation lacks parent evidence')
    request = suite_request(submitted['suite'])
    identities = validate_registry(Path(submitted['attempt']), json.loads((stage / 'children.json').read_text()))
    state = json.loads((stage / 'suite-state.json').read_text())
    if (state.get('version') != 2 or state.get('reserved') != identities or not isinstance(state.get('dispatched'), list)
            or not isinstance(state.get('completed'), list) or len(set(state['dispatched'])) != len(state['dispatched'])
            or len(set(state['completed'])) != len(state['completed']) or not set(state['completed']) <= set(state['dispatched'])
            or not set(state['dispatched']) <= set(identities) or identities[0] not in state['completed']):
        raise ValueError('Configured suite journal does not preserve reserved, dispatched, and completed identities')
    result = json.loads((stage / 'results/suite-run.json').read_text())
    plan = json.loads((stage / 'results/suite-plan.json').read_text())
    if (len(identities) != request['shard_count'] + 1 or result.get('parent_attempt') != submitted['attempt']
            or result.get('plan_attempt') != identities[0] or result.get('shard_attempts') != identities[1:]
            or result.get('keep_going') != request['keep_going'] or result.get('exit_code') != terminal['exit_code']):
        raise ValueError('Configured suite result differs from reserved invocation')
    queue = json.loads((stage / 'queue.json').read_text())
    from worker_config import identity as config_identity, validate
    validate(submitted['worker_config'])
    if (queue.get('mode') != 'resource' or queue.get('invocation') != submitted['attempt']
            or queue.get('config_digest') != config_identity(submitted['worker_config'])
            or type(queue.get('waited')) not in (int, float) or not math.isfinite(queue['waited']) or queue['waited'] < 0
            or state.get('queue_seconds') != queue['waited'] or state.get('stop_reason') != result.get('stop_reason')):
        raise ValueError('Configured suite queue receipt does not bind the invocation ledger')
    reports = []
    for identity in state['completed']:
        child = stage / 'results/attempts' / identity
        for name in ('submission.json', 'terminal.json', 'artifacts.json'):
            if str((child / name).relative_to(stage)) not in manifest:
                raise ValueError('Missing configured child receipt')
        metadata = json.loads((child / 'submission.json').read_text())
        index = identities.index(identity)
        expected = ({'action': 'plan', 'shard_count': request['shard_count'], 'selection': request['selection']}
                    if index == 0 else {'action': 'shard', 'plan': plan, 'shard': index})
        if (metadata.get('parent_attempt') != submitted['attempt'] or metadata.get('attempt') != identity
                or metadata.get('workflow') != 'suite' or metadata.get('suite') != expected
                or metadata.get('queue_timeout_seconds') != submitted['queue_timeout_seconds']
                or metadata.get('source_digest') != submitted['source_digest']
                or metadata.get('worker_config') != submitted['worker_config']
                or metadata.get('suite_update', False) != request.get('update', False)):
            raise ValueError('Configured child changed immutable invocation metadata')
        receipt = validate_evidence(child, identity, metadata)
        output = child / ('results/suite-plan.json' if index == 0 else 'results/suite-shard.json')
        if index == 0:
            if receipt['exit_code'] or not output.exists() or json.loads(output.read_text()) != plan:
                raise ValueError('Configured plan receipt is not verified')
        elif output.exists():
            reports.append(json.loads(output.read_text()))
        elif receipt['exit_code'] == 0:
            raise ValueError('Successful configured shard lacks result evidence')
    validate_summary(plan, reports, result)


def validate_configured_planning_failure(stage, submitted, terminal, manifest):
    required = {'children.json', 'suite-state.json', 'results/suite-error.json', 'results/exit-code', 'queue.json'}
    if not required <= set(manifest) or terminal['exit_code'] == 0:
        raise ValueError('Configured failed planning invocation lacks receipt evidence')
    request = suite_request(submitted['suite'])
    identities = validate_registry(Path(submitted['attempt']), json.loads((stage / 'children.json').read_text()))
    state = json.loads((stage / 'suite-state.json').read_text())
    error = json.loads((stage / 'results/suite-error.json').read_text())
    queue = json.loads((stage / 'queue.json').read_text())
    from worker_config import identity as config_identity
    if (len(identities) != request['shard_count'] + 1 or state.get('version') != 2
            or state.get('reserved') != identities or state.get('dispatched') != [identities[0]]
            or state.get('completed') != [identities[0]] or state.get('stop_reason') not in ('planning-failed', 'deadline')
            or error != {'version': 1, 'parent_attempt': submitted['attempt'], 'source_digest': submitted['source_digest'],
                         'plan_attempt': identities[0], 'reason': state['stop_reason'],
                         'exit_code': 124 if state['stop_reason'] == 'deadline' else terminal['exit_code']}):
        raise ValueError('Configured failed planning receipt differs from reserved work')
    if (queue.get('mode') != 'resource' or queue.get('invocation') != submitted['attempt']
            or queue.get('config_digest') != config_identity(submitted['worker_config'])
            or type(queue.get('waited')) not in (int, float) or not math.isfinite(queue['waited'])
            or queue['waited'] < 0 or state.get('queue_seconds') != queue['waited']):
        raise ValueError('Configured failed planning queue receipt differs from invocation')
    if state['stop_reason'] == 'deadline' and terminal['exit_code'] != 75:
        raise ValueError('Configured planning deadline must exit 75')
    child = stage / 'results/attempts' / identities[0]
    for name in ('submission.json', 'terminal.json', 'artifacts.json'):
        if str((child / name).relative_to(stage)) not in manifest:
            raise ValueError('Configured planning child receipt is missing')
    metadata = json.loads((child / 'submission.json').read_text())
    expected = {'action': 'plan', 'shard_count': request['shard_count'], 'selection': request['selection']}
    if (metadata.get('attempt') != identities[0] or metadata.get('parent_attempt') != submitted['attempt']
            or metadata.get('suite') != expected or metadata.get('queue_timeout_seconds') != submitted['queue_timeout_seconds']
            or metadata.get('source_digest') != submitted['source_digest']
            or metadata.get('worker_config') != submitted['worker_config']):
        raise ValueError('Configured planning child changed immutable metadata')
    receipt = validate_evidence(child, identities[0], metadata)
    if receipt['exit_code'] != error['exit_code']:
        raise ValueError('Configured planning terminal differs from parent error')


def validate_planning_failure(stage, submitted, terminal, manifest):
    required = {'results/suite-error.json', 'children.json', 'suite-state.json', 'results/exit-code'}
    if not required <= set(manifest):
        raise ValueError('Suite invocation lacks completion evidence; existing work remains unresolved')
    request = suite_request(submitted['suite'])
    identities = validate_registry(Path(submitted['attempt']), json.loads((stage / 'children.json').read_text()))
    if len(identities) != request['shard_count'] + 1 or terminal['exit_code'] == 0:
        raise ValueError('Invalid failed planning invocation')
    error = json.loads((stage / 'results/suite-error.json').read_text())
    expected_error = {'version': 1, 'parent_attempt': submitted['attempt'], 'source_digest': submitted['source_digest'],
                      'plan_attempt': identities[0], 'reason': None, 'exit_code': None}
    state = json.loads((stage / 'suite-state.json').read_text())
    if error.get('reason') not in ('planning-failed', 'deadline'):
        raise ValueError('Failed plan has an invalid stop reason')
    if error['reason'] == 'deadline' and terminal['exit_code'] != 75:
        raise ValueError('Deadline planning stop must exit 75')
    expected_error['reason'] = error['reason']
    expected_error['exit_code'] = 124 if error['reason'] == 'deadline' else terminal['exit_code']
    if error != expected_error or state['completed'] != identities[:1] or state['stop_reason'] != error['reason']:
        raise ValueError('Failed plan receipt does not match reserved work')
    child = stage / 'results/attempts' / identities[0]
    for name in ('submission.json', 'terminal.json', 'artifacts.json'):
        if str((child / name).relative_to(stage)) not in manifest:
            raise ValueError('Failed planning receipt is missing')
    metadata = json.loads((child / 'submission.json').read_text())
    expected_request = {'action': 'plan', 'shard_count': request['shard_count'], 'selection': request['selection']}
    if (metadata.get('parent_attempt') != submitted['attempt'] or metadata.get('suite') != expected_request or
            metadata.get('source_digest') != submitted['source_digest'] or metadata.get('workflow') != 'suite'):
        raise ValueError('Failed plan receipt belongs to different work')
    receipt = validate_evidence(child, identities[0], metadata)
    expected_child_exit = 124 if error['reason'] == 'deadline' else terminal['exit_code']
    if receipt['exit_code'] != expected_child_exit:
        raise ValueError('Failed plan disagrees with invocation exit')
    validate_queue_accounting(stage, submitted, manifest, state, identities)


def validate_queue_accounting(stage, submitted, manifest, state, identities):
    waited = 0.0
    for identity in state['completed']:
        child = stage / 'results/attempts' / identity
        metadata = json.loads((child / 'submission.json').read_text())
        remaining = submitted['queue_timeout_seconds'] - waited
        if remaining <= 0 or metadata['queue_timeout_seconds'] != remaining:
            raise ValueError('Child queue limit reset the cumulative invocation budget')
        artifacts = json.loads((child / 'artifacts.json').read_text())
        if 'queue.json' not in artifacts:
            # Failure before admission consumed no queue time.
            if (child / 'queue.json').exists():
                raise ValueError('Unauthenticated child queue receipt')
            continue
        waited += queue_wait(child)
    if state['queue_seconds'] != waited:
        raise ValueError('Parent queue accounting differs from child receipts')
