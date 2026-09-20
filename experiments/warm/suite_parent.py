"""One remotely owned invocation, with sequential independently admitted shards."""
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

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


def stage_child(parent, identity, submitted, request, remaining):
    child = parent.parent / identity
    child.mkdir()  # Existing work is never replaced or restarted.
    from worker_bundle import NAMES
    for name in NAMES + ['runtime.Dockerfile', 'manifest.json']:
        shutil.copyfile(parent / name, child / name)
    shutil.copytree(parent / 'source', child / 'source', copy_function=os.link, symlinks=True)
    metadata = submitted | {'attempt': identity, 'workflow': 'suite', 'suite': request,
                            'parent_attempt': parent.name, 'queue_timeout_seconds': remaining}
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
        if index == 0:
            if terminal['exit_code'] != 0:
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
                          ('stopping new dispatch' if reason else 'continuing because --keep-going was requested'), flush=True)
        if reason:
            break
    if plan is None:
        return 75
    result = summarize(plan, reports, parent_attempt=parent.name, plan_attempt=children[0],
                       shard_attempts=children[1:], keep_going=request['keep_going'], stop_reason=reason)
    state['stop_reason'] = result['stop_reason']
    write(parent / 'suite-state.json', state)
    write(parent / 'results/suite-run.json', result)
    (parent / 'results/exit-code').write_text(str(result['exit_code']) + '\n')
    print(f'[pandora] suite {result["status"]}; {len(reports)}/{request["shard_count"]} shard reports; '
          f'not run: {", ".join(result["unrun_journeys"]) or "none"}; queue used {waited:.1f}s', flush=True)
    return result['exit_code']


def validate_result(stage, submitted, terminal, manifest):
    """Bind every child receipt to the parent's reserved attempt identities."""
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
                metadata.get('workflow') != 'suite'):
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
                      'plan_attempt': identities[0], 'reason': 'planning-failed', 'exit_code': terminal['exit_code']}
    state = json.loads((stage / 'suite-state.json').read_text())
    if error != expected_error or state['completed'] != identities[:1] or state['stop_reason'] != 'planning-failed':
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
    if receipt['exit_code'] != terminal['exit_code']:
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
