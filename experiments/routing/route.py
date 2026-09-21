#!/usr/bin/env python3
"""Route supported commands and recover the same request after client loss."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'warm'))
from commands import classify, selected_surface, suite_request, surface_suite_request
from workflow_options import surface_outputs
from transport import query, validate_evidence, validate_operator_result
from delivery import deliver
import journey_updates
import catalog_updates
from tracked_outputs import PublicationConflict
from retention import local as prune_local
from snapshot import names, excluded, entry, encode
from artifact_limits import artifact_delivery_limit


def queue_timeout_seconds(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 86400:
        raise ValueError('Queue timeout must be an integer from 1 through 86400 seconds')
    return value


def effective_queue_timeout(default, tool, config):
    value = config.get('queue_timeout_seconds', default) if tool == 'docker' and config else default
    return queue_timeout_seconds(value)


def effective_artifact_delivery_limit(default, tool, config):
    value = config.get('artifact_delivery_limit_bytes', default) if tool == 'docker' and config else default
    return artifact_delivery_limit(value)


def suite_shard_count(value):
    if isinstance(value, bool):
        raise ValueError('Suite shards must be an integer from 1 through 32')
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise ValueError('Suite shards must be an integer from 1 through 32') from None
    if not 1 <= count <= 32:
        raise ValueError('Suite shards must be an integer from 1 through 32')
    return count


def suite_environment_error(environment):
    names = ('JOURNEY_FILTER', 'JOURNEY_SHARD', 'JOURNEY_CONCURRENCY',
             'JOURNEY_REPLAY', 'JOURNEY_TEMPLATE', 'IKE_WORLD')
    active = [name for name in names if environment.get(name)]
    if active:
        return ('Unset ' + ', '.join(active) +
                ' before pnpm journeys; routed suites support pnpm journeys [--update] [--keep-going]. No validation started.')
    return None


def write(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.replace(path)


def control(output, action, attempt=None):
    source = output / 'submission.json'
    if source.exists():
        attempt = json.loads(source.read_text())['attempt']
    if not attempt:
        return None
    try:
        return query(os.environ['PANDORA_HOST'], attempt, action)
    except (ConnectionError, ValueError, subprocess.SubprocessError):
        return None


def current_digest(repo):
    current = [e for name in names(repo) if not excluded(name)
               if (e := entry(repo, name)) is not None]
    return hashlib.sha256(encode(current)).hexdigest()


def complete(active, record, output, terminal):
    record.update(state='terminal', terminal=terminal)
    write(active, record)
    try:
        write(output / 'completed.json', {'attempt': record['attempt']})
        # Retention acknowledgement is best effort; losing it preserves data.
        if control(output, 'release') is None:
            print('[pandora] Remote retention acknowledgement unavailable; evidence remains pinned.', flush=True)
        prune_local(active.parent, output)
    except (OSError, ValueError) as error:
        print(f'[pandora] Retention sweep deferred: {error}', file=sys.stderr)


def complete_infrastructure_failure(active, record, output, receipt):
    """Close a dead-worker request without representing it as test evidence."""
    record.update(state='infrastructurefailure', operator_result=receipt)
    write(active, record)
    try:
        write(output / 'completed.json', {'attempt': record['attempt'],
                                          'outcome': 'infrastructure-failed'})
    except (OSError, ValueError) as error:
        print(f'[pandora] Infrastructure outcome archived; completion marker deferred: {error}', file=sys.stderr)


def locked(path, nonblocking=False):
    handle = path.open('a')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
    except:
        handle.close()
        raise
    return handle


def owner_path(output):
    return output.parent / (output.name + '.owner.lock')


def owner_is_busy(output):
    try:
        handle = locked(owner_path(output), nonblocking=True)
    except BlockingIOError:
        return True
    handle.close()
    return False


def legacy_owner_is_busy(state):
    try:
        handle = locked(state / 'request.lock', nonblocking=True)
    except BlockingIOError:
        return True
    handle.close()
    return False


def evidence_path(state, record):
    output = Path(record['output'])
    if output.parent != state or output.name != record['attempt'] or output.is_symlink():
        raise ValueError('Active Pandora request has an invalid evidence path')
    return output


def completed_result(repo, output, record):
    """Report an existing outcome without publishing or creating another request."""
    if record['state'] == 'infrastructurefailure':
        validate_operator_result(output, record['attempt'])
        print('[pandora] This request ended with acknowledged infrastructure failure; it has no test result.', flush=True)
        return 70
    terminal = validate_evidence(output, record['attempt'])
    submitted = json.loads((output / 'submission.json').read_text())
    request = submitted.get('docker', {}).get('request', {})
    checks_source = submitted.get('workflow') != 'docker' or request.get('kind') == 'build' or request.get('mount')
    workflow = catalog_updates if catalog_updates.is_update(submitted) else journey_updates
    if terminal['exit_code'] == 0 and workflow.is_update(submitted):
        updates = workflow.declarations(output)
        source_matches = journey_updates.source_is_current(repo, output, submitted, updates)
        # Returned expectations are an intentional input change. A subsequent
        # edit or manual resolution is not evidence for the current bytes.
        source_matches = source_matches and all(
            journey_updates.regular_bytes(repo, name, optional=True) == change['target']
            for name, change in updates.items())
    else:
        source_matches = not checks_source or current_digest(repo) == submitted['source_digest']
    if not source_matches:
        print('[pandora] Result applies to earlier source. Run the original command to validate current source. Evidence: ' + str(output), file=sys.stderr)
        return 75
    print(f'[pandora] attempt={record["attempt"]}; exit={terminal["exit_code"]}; evidence={output}', flush=True)
    return terminal['exit_code']


def main(tool='pnpm', expected_attempt=None, observer=False):
    argv = sys.argv[1:]
    docker_request = None
    config = None
    if tool == 'docker':
        action, selectors, message = 'docker', [], ''
    else:
        action, selectors, message = classify(argv, os.environ.get('PANDORA_TREATMENT', 'normal'))
    if action == 'local':
        os.execv(os.environ['PANDORA_REAL_PNPM'], [os.environ['PANDORA_REAL_PNPM'], *argv])
    if action == 'reject':
        print('[pandora] ' + message, file=sys.stderr)
        return 64
    suite = None
    surface_suite = None
    if action == 'suite-run':
        error = suite_environment_error(os.environ)
        if error:
            print('[pandora] ' + error, file=sys.stderr)
            return 64
        try:
            suite = suite_request(argv, suite_shard_count(os.environ.get('PANDORA_SUITE_SHARDS', '4')))
        except ValueError as error:
            print('[pandora] ' + str(error), file=sys.stderr)
            return 64
    if action == 'remote':
        try:
            surface_suite = surface_suite_request(
                argv, suite_shard_count(os.environ.get('PANDORA_SUITE_SHARDS', '4')))
        except ValueError as error:
            if str(error) != 'Not a surface command':
                print('[pandora] ' + str(error), file=sys.stderr)
                return 64
    repo = Path(subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], text=True).strip())
    if Path.cwd().resolve() != repo.resolve():
        print('[pandora] Run this validation command from the repository root. No validation started.', file=sys.stderr)
        return 64
    if tool == 'docker':
        try:
            from docker_commands import profile, classify as classify_docker
            if 'PANDORA_DOCKER_PROFILE_JSON' not in os.environ:
                raise ValueError('Docker routing needs a human-selected external profile via launch.py --docker-profile. No local command ran.')
            config = profile(os.environ['PANDORA_DOCKER_PROFILE_JSON'])
            if argv[:1] == ['run'] and config['outputs']:
                paths = ', '.join(x['container'] + ' -> ' + str(repo / x['workspace']) for x in config['outputs'])
                print('[pandora] After a successful run, declared outputs return automatically: ' + paths +
                      '. No output-directory mount is needed; use docker run --rm TAG for an image-only test.', flush=True)
            docker_request = classify_docker(argv, repo, config)
        except (ValueError, TypeError, KeyError) as error:
            print('[pandora] ' + str(error), file=sys.stderr)
            return 64
    try:
        delivery_limit = effective_artifact_delivery_limit(
            int(os.environ.get('PANDORA_ARTIFACT_DELIVERY_LIMIT_BYTES', '2147483648')), tool, config)
    except ValueError as error:
        print('[pandora] ' + str(error), file=sys.stderr)
        return 64
    key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()
    state = Path(os.environ['PANDORA_STATE']) / key
    state.mkdir(parents=True, exist_ok=True)
    lock = locked(state / 'state.lock')
    owner = None
    try:
        active = state / 'active.json'
        record = json.loads(active.read_text()) if active.exists() else None
        expected = expected_attempt
        if expected and (not record or record.get('attempt') != expected):
            print('[pandora] The observed request is no longer active. No replacement submitted.', file=sys.stderr)
            return 75
        if expected and record.get('state') in ('terminal', 'infrastructurefailure'):
            return completed_result(repo, evidence_path(state, record), record)
        if record is not None and record.get('state') == 'infrastructurefailure':
            # A crash can follow the durable active-state update but precede the
            # local completion marker. Repair that marker before allowing the
            # next explicit command to create a new attempt.
            try:
                output = evidence_path(state, record)
                receipt = validate_operator_result(output, record['attempt'])
                write(output / 'completed.json', {'attempt': record['attempt'],
                                                  'outcome': 'infrastructure-failed'})
                record['operator_result'] = receipt
                write(active, record)
            except (KeyError, OSError, ValueError) as error:
                print('[pandora] Acknowledged infrastructure outcome is incomplete locally: ' + str(error) +
                      '. Retry this command after restoring its evidence; no replacement submitted.', file=sys.stderr)
                return 75
            record = None
        recovering = record is not None and record['state'] == 'active'
        if recovering:
            if record.get('protocol') != 2 and legacy_owner_is_busy(state):
                print('[pandora] A legacy active request remains protected by its original client. Keep waiting on that client; no replacement submitted.', file=sys.stderr)
                return 75
            if record.get('host') != os.environ['PANDORA_HOST'] or record['command'] != argv or record.get('tool', 'pnpm') != tool:
                print('[pandora] A different request is active. Retry its original command and worker; no replacement submitted.', file=sys.stderr)
                return 75
            output = evidence_path(state, record)
            if record.get('protocol') == 2 and owner_is_busy(output) and not observer:
                print(f'[pandora] Validation is already active for this worktree. Use `pandora wait {record["attempt"]}` for feedback; this invocation submitted nothing.', file=sys.stderr)
                return 75
            if record.get('protocol') == 2 and not observer:
                try:
                    owner = locked(owner_path(output), nonblocking=True)
                except BlockingIOError:
                    print(f'[pandora] Validation is already active for this worktree. Use `pandora wait {record["attempt"]}` for feedback; this invocation submitted nothing.', file=sys.stderr)
                    return 75
            if not (output / 'submission.json').exists():
                terminal = control(output, 'cancel', record.get('attempt'))
                if terminal and terminal.get('cleanup_verified'):
                    record.update(state='terminal', terminal=terminal)
                    write(active, record)
                    print('[pandora] Incomplete capture cancelled. Retry to capture fresh source.', file=sys.stderr)
                else:
                    print('[pandora] Incomplete capture remains unresolved. No new request submitted.', file=sys.stderr)
                return 75
            if not observer and record.get('protocol') == 2:
                abandoned = control(output, 'abandon-unregistered', record.get('attempt'))
                if abandoned and abandoned.get('state') == 'abandoned-unregistered':
                    record.update(state='capture-aborted', remote=abandoned)
                    write(active, record)
                    write(output / 'completed.json', {'attempt': record['attempt'],
                                                      'outcome': 'capture-aborted'})
                    print('[pandora] Remote worker never registered. This captured request was aborted before work; run the command again to start a fresh request.', file=sys.stderr)
                    return 75
            print(f'[pandora] Recovering existing request, without resubmitting source. Evidence: {output}', flush=True)
            command = [sys.executable, '-B', str(ROOT.parent / 'warm/transport.py'),
                       os.environ['PANDORA_HOST'], str(output),
                       '--artifact-delivery-limit-bytes', str(delivery_limit)]
        else:
            if expected:
                print('[pandora] The observed request cannot be resumed. No replacement submitted.', file=sys.stderr)
                return 75
            attempt = uuid.uuid4().hex
            output = state / attempt
            try:
                timeout = effective_queue_timeout(int(os.environ.get('PANDORA_QUEUE_TIMEOUT_SECONDS', '900')), tool, config)
            except ValueError as error:
                print('[pandora] ' + str(error), file=sys.stderr)
                return 64
            record = {'state': 'active', 'protocol': 2, 'tool': tool, 'output': str(output), 'command': argv, 'host': os.environ['PANDORA_HOST'], 'session': os.environ['PANDORA_SESSION'], 'attempt': attempt, 'queue_timeout_seconds': timeout, 'artifact_delivery_limit_bytes': delivery_limit}
            suite_request_path = None
            surface_suite_request_path = None
            if suite is not None:
                suite_request_path = state / (attempt + '.suite-request.json')
                write(suite_request_path, suite)
                record['suite_request'] = str(suite_request_path)
            if surface_suite is not None:
                surface_suite_request_path = state / (attempt + '.surface-suite-request.json')
                write(surface_suite_request_path, surface_suite)
                record['surface_suite_request'] = str(surface_suite_request_path)
            write(active, record)
            owner = locked(owner_path(output))
            print(f'[pandora] accepted {attempt}; recover feedback with `pandora wait {attempt}`. Evidence: {output}', flush=True)
            if suite is not None:
                policy = ('continue after test failures' if suite['keep_going']
                          else 'stop at the first test failure')
                print(f'[pandora] Running the full suite in {suite["shard_count"]} isolated shards under worker admission; {policy}.',
                      flush=True)
            command = [sys.executable, '-B', str(ROOT.parent / 'warm/warm.py'),
                       '--host', os.environ['PANDORA_HOST'], '--repo', str(repo),
                       '--output', str(output), '--attempt', record['attempt'],
                       '--workflow', ('surface-run' if surface_suite is not None
                                      else action if action in ('journey', 'docker', 'suite-run') else 'surface'),
                       '--queue-timeout-seconds', str(record['queue_timeout_seconds']),
                       '--artifact-delivery-limit-bytes', str(delivery_limit),
                       '--selectors-json=' + json.dumps(selectors)]
            if action == 'remote':
                if surface_suite is not None:
                    command += ['--surface-suite-request', str(surface_suite_request_path)]
                else:
                    command += ['--surface-app', selected_surface(argv)]
            if docker_request is not None:
                command += ['--docker-request', json.dumps({'request': docker_request, 'config': config, 'worktree_key': key})]
            if suite_request_path is not None:
                command += ['--suite-request', str(suite_request_path)]
        # Allocation is complete. A waiter may now inspect immutable evidence
        # while the attempt owner retains the per-attempt lock.
        lock.close()
        lock = None
        child = None

        def interrupted(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        try:
            if recovering and ((output / 'terminal.json').exists() or (output / 'operator-result.json').exists()):
                # Verified remote completion can outlive interrupted local delivery.
                # Do not download over publication state or submit another execution.
                status = 75  # Validated below before any publication or state change.
            else:
                child = subprocess.Popen(command, start_new_session=True,
                                         pass_fds=((owner.fileno(),) if owner else ()))
                status = child.wait()
        except KeyboardInterrupt:
            if observer:
                print('[pandora] Detached from this observer. The original request continues unchanged.', file=sys.stderr)
                return 130
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            if child and child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
            print('[pandora] Cancelling this request; verifying remote cleanup.', file=sys.stderr)
            terminal = control(output, 'cancel', record.get('attempt'))
            for _ in range(15):
                if terminal and terminal.get('cleanup_verified'):
                    break
                time.sleep(1)
                terminal = control(output, 'status', record.get('attempt'))
            lock = locked(state / 'state.lock')
            current = json.loads(active.read_text()) if active.exists() else None
            if not current or current.get('state') != 'active' or current.get('attempt') != record['attempt']:
                print('[pandora] Request ownership changed while cancelling; no state was replaced.', file=sys.stderr)
                return 130
            record = current
            if terminal and terminal.get('cleanup_verified'):
                record.update(state='terminal', terminal=terminal)
                write(active, record)
                print('[pandora] Cancellation verified. No owned test container is running.', file=sys.stderr)
            else:
                print('[pandora] Cleanup unresolved; new requests remain blocked.', file=sys.stderr)
            return 130
        lock = locked(state / 'state.lock')
        current = json.loads(active.read_text()) if active.exists() else None
        if current and current.get('attempt') == record['attempt'] and current.get('state') in ('terminal', 'infrastructurefailure'):
            return completed_result(repo, output, current)
        if not current or current.get('state') != 'active' or current.get('attempt') != record['attempt']:
            print('[pandora] This client no longer owns the active request. No state or output changed.', file=sys.stderr)
            return 75
        record = current
        terminal_path = output / 'terminal.json'
        if terminal_path.exists():
            try:
                terminal = validate_evidence(output, record['attempt'])
                submitted = json.loads((output / 'submission.json').read_text())
                request = submitted.get('docker', {}).get('request', {})
                checks_source = submitted.get('workflow') != 'docker' or request.get('kind') == 'build' or request.get('mount')
                updates = None
                update_workflow = catalog_updates if catalog_updates.is_update(submitted) else journey_updates
                if terminal['exit_code'] == 0 and update_workflow.is_update(submitted):
                    updates = update_workflow.declarations(output)
                source_matches = (journey_updates.source_is_current(repo, output, submitted, updates)
                                  if updates is not None else
                                  not checks_source or current_digest(repo) == submitted['source_digest'])
                if not source_matches:
                    complete(active, record, output, terminal)
                    print('[pandora] Result applies to earlier source. Run again to validate current source; inspect retained outputs before using them. Evidence: ' + str(output), file=sys.stderr)
                    return 75
                if terminal['exit_code'] == 0 and submitted.get('workflow', 'surface') == 'surface':
                    deliver(repo, output, outputs=surface_outputs(submitted.get("surface_app", "borrower-web")))
                if terminal['exit_code'] == 0 and submitted.get('workflow') == 'surface-run':
                    from surface_delivery import deliver_surface
                    deliver_surface(repo, output, submitted)
                if terminal['exit_code'] == 0 and request.get('kind') == 'run':
                    deliver(repo, output, outputs=tuple(x['workspace'] for x in submitted['docker']['config']['outputs']))
                if updates is not None:
                    update_workflow.deliver(repo, output, updates)
                complete(active, record, output, terminal)
                status = terminal['exit_code']
            except PublicationConflict as error:
                print(f'[pandora] Local expectation publication conflicted: {error}. '
                      f'Manually merge the declared expectation files, then run '\
                      f'`pandora resolve-expectations {record["attempt"]} --keep-local`. '
                      'That accepts current local contents without rerunning or validating them; '
                      'review git diff, then run ordinary validation without --update.', file=sys.stderr)
                return 75
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                try:
                    receipt = validate_operator_result(output, record['attempt'])
                except (OSError, ValueError):
                    receipt = None
                if receipt is not None:
                    complete_infrastructure_failure(active, record, output, receipt)
                    print('[pandora] Remote worker loss was acknowledged after verified cleanup. This request has no test result; run the command again to start a new request. Evidence: ' + str(output), file=sys.stderr)
                    return 70
                print(f'[pandora] Local delivery incomplete: {error}. Retry the same command to recover this run; no new tests will start. Evidence: {output}', file=sys.stderr)
                return 75
        else:
            try:
                receipt = validate_operator_result(output, record['attempt'])
            except (OSError, ValueError):
                receipt = None
            if receipt is not None:
                complete_infrastructure_failure(active, record, output, receipt)
                print('[pandora] Remote worker loss was acknowledged after verified cleanup. This request has no test result; run the command again to start a new request. Evidence: ' + str(output), file=sys.stderr)
                return 70
            if not (output / 'submission.json').exists():
                # The preparer has exited and remote launch cannot precede metadata.
                record.update(state='terminal', reason='capture-failed')
                write(active, record)
                print('[pandora] Capture failed before submission. Correct the reported problem and retry to capture source.', file=sys.stderr)
                return status if status != 0 else 70
            print('[pandora] No verified terminal evidence; retry the same command to recover. No replacement was submitted.', file=sys.stderr)
            if status == 0:
                status = 70
        return status if status >= 0 else 128 - status
    finally:
        if lock:
            lock.close()
        if owner:
            owner.close()


if __name__ == '__main__':
    raise SystemExit(main())
