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
from commands import classify, selected_surface, suite_request
from workflow_options import surface_outputs
from transport import query, validate_evidence
from delivery import deliver
import journey_updates
import catalog_updates
from tracked_outputs import PublicationConflict
from retention import local as prune_local
from snapshot import names, excluded, entry, encode


def queue_timeout_seconds(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 86400:
        raise ValueError('Queue timeout must be an integer from 1 through 86400 seconds')
    return value


def effective_queue_timeout(default, tool, config):
    value = config.get('queue_timeout_seconds', default) if tool == 'docker' and config else default
    return queue_timeout_seconds(value)


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
                ' before pnpm journeys; routed suites currently support only pnpm journeys [--keep-going]. No validation started.')
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


def main(tool='pnpm'):
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
    key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()
    state = Path(os.environ['PANDORA_STATE']) / key
    state.mkdir(parents=True, exist_ok=True)
    lock = (state / 'request.lock').open('a')
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('[pandora] Validation is already active for this worktree. '
                  'Keep waiting on its existing tool handle. This invocation submitted nothing; '
                  'changed input does not replace the active request.', file=sys.stderr)
            return 75
        active = state / 'active.json'
        record = json.loads(active.read_text()) if active.exists() else None
        recovering = record is not None and record['state'] == 'active'
        if recovering:
            if record.get('host') != os.environ['PANDORA_HOST'] or record['command'] != argv or record.get('tool', 'pnpm') != tool:
                print('[pandora] A different request is active. Retry its original command and worker; no replacement submitted.', file=sys.stderr)
                return 75
            output = Path(record['output'])
            if not (output / 'submission.json').exists():
                terminal = control(output, 'cancel', record.get('attempt'))
                if terminal and terminal.get('cleanup_verified'):
                    record.update(state='terminal', terminal=terminal)
                    write(active, record)
                    print('[pandora] Incomplete capture cancelled. A delayed worker cannot execute it; retry to capture fresh source.', file=sys.stderr)
                else:
                    print('[pandora] Incomplete capture remains unresolved. No new request submitted.', file=sys.stderr)
                return 75
            print(f'[pandora] Recovering existing request, without resubmitting source. Evidence: {output}', flush=True)
            command = [sys.executable, '-B', str(ROOT.parent / 'warm/transport.py'),
                       os.environ['PANDORA_HOST'], str(output)]
        else:
            attempt = uuid.uuid4().hex
            output = state / attempt
            try:
                timeout = effective_queue_timeout(int(os.environ.get('PANDORA_QUEUE_TIMEOUT_SECONDS', '900')), tool, config)
            except ValueError as error:
                print('[pandora] ' + str(error), file=sys.stderr)
                return 64
            record = {'state': 'active', 'tool': tool, 'output': str(output), 'command': argv, 'host': os.environ['PANDORA_HOST'], 'session': os.environ['PANDORA_SESSION'], 'attempt': attempt, 'queue_timeout_seconds': timeout}
            suite_request_path = None
            if suite is not None:
                suite_request_path = state / (attempt + '.suite-request.json')
                write(suite_request_path, suite)
                record['suite_request'] = str(suite_request_path)
            write(active, record)
            if suite is not None:
                policy = ('continue after test failures' if suite['keep_going']
                          else 'stop at the first test failure')
                print(f'[pandora] Running the full suite in {suite["shard_count"]} isolated shards under worker admission; {policy}.',
                      flush=True)
            command = [sys.executable, '-B', str(ROOT.parent / 'warm/warm.py'),
                       '--host', os.environ['PANDORA_HOST'], '--repo', str(repo),
                       '--output', str(output), '--attempt', record['attempt'],
                       '--workflow', action if action in ('journey', 'docker', 'suite-run') else 'surface',
                       '--queue-timeout-seconds', str(record['queue_timeout_seconds']),
                       '--selectors-json=' + json.dumps(selectors)]
            if action == 'remote':
                command += ['--surface-app', selected_surface(argv)]
            if docker_request is not None:
                command += ['--docker-request', json.dumps({'request': docker_request, 'config': config, 'worktree_key': key})]
            if suite_request_path is not None:
                command += ['--suite-request', str(suite_request_path)]
        child = None

        def interrupted(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        try:
            if recovering and (output / 'terminal.json').exists():
                # Verified remote completion can outlive interrupted local delivery.
                # Do not download over publication state or submit another execution.
                status = 75  # Validated below before any publication or state change.
            else:
                child = subprocess.Popen(command, start_new_session=True, pass_fds=(lock.fileno(),))
                status = child.wait()
        except KeyboardInterrupt:
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
            if terminal and terminal.get('cleanup_verified'):
                record.update(state='terminal', terminal=terminal)
                write(active, record)
                print('[pandora] Cancellation verified. No owned test container is running.', file=sys.stderr)
            else:
                print('[pandora] Cleanup unresolved; new requests remain blocked.', file=sys.stderr)
            return 130
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
                print(f'[pandora] Local delivery incomplete: {error}. Retry the same command to recover this run; no new tests will start. Evidence: {output}', file=sys.stderr)
                return 75
        else:
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
        lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
