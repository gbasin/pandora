"""Reconnect to one immutable attempt and verify its returned evidence."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from snapshot import digest
import time
import uuid
import atexit
import tempfile
import hashlib
import math
import stat

# Per-client private socket; persistence covers sequential SSH/scp/rsync calls.
SSH_DIRECTORY = tempfile.mkdtemp(prefix='pandora-ssh-', dir='/tmp')
atexit.register(shutil.rmtree, SSH_DIRECTORY, ignore_errors=True)
SSH_OPTIONS = ['-o', 'ControlMaster=auto', '-o', 'ControlPersist=60',
               '-o', 'ControlPath=' + SSH_DIRECTORY + '/%C','-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
               '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=2']
CLIENT_ONLY_SUBMISSION_FIELDS = frozenset({'total_seconds', 'exit_code'})


def query(host, attempt, action='status', offsets=None):
    script = Path(__file__).resolve().parent.parent / 'routing/control.py'
    args = [attempt, action, *(str(x) for x in (offsets or [0, 0]))]
    result = subprocess.run(['ssh', *SSH_OPTIONS, host, 'python3 - ' + ' '.join(args)],
                            input=script.read_text(), capture_output=True, text=True, timeout=25)
    if result.returncode:
        raise ConnectionError(result.stderr.strip() or 'SSH status unavailable')
    return json.loads(result.stdout)


from evidence import validate_evidence
from artifact_limits import (DEFAULT_ARTIFACT_DELIVERY_LIMIT_BYTES,
                             artifact_delivery_limit,
                             enforce_declared_artifact_limit)


def retrieve(host, output, attempt, artifact_delivery_limit_bytes=DEFAULT_ARTIFACT_DELIVERY_LIMIT_BYTES):
    stage = output / ('.download-' + uuid.uuid4().hex)
    stage.mkdir()
    remote = f'{host}:pandora-warm/runs/{attempt}/'
    try:
        subprocess.run(['rsync', '-rt', '-e', 'ssh ' + ' '.join(SSH_OPTIONS),
                        remote + 'artifacts.json', remote + 'terminal.json', str(stage) + '/'],
                       check=True, timeout=60)
        manifest = json.loads((stage / 'artifacts.json').read_text())
        enforce_declared_artifact_limit(
            manifest, query(host, attempt, 'artifact-stats'), artifact_delivery_limit_bytes)
        subprocess.run(['rsync', '-rt', '--files-from=-', '-e', 'ssh ' + ' '.join(SSH_OPTIONS),
                        remote, str(stage) + '/'], input='\n'.join(manifest) + '\n',
                       text=True, check=True, timeout=120)
        terminal = validate_evidence(stage, attempt, json.loads((output / "submission.json").read_text()))
        # Promote evidence only after complete verification. The terminal is last.
        for child in list(stage.iterdir()):
            if child.name == 'terminal.json':
                continue
            destination = output / child.name
            if destination.is_dir():
                shutil.rmtree(destination)
            os.replace(child, destination)
        os.replace(stage / 'terminal.json', output / 'terminal.json')
        return terminal
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _regular_bytes(path, name):
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(name + ' must be a regular file')
    return path.read_bytes()


def _submission_evidence(output, accepted_submission_path=None):
    local = json.loads(_regular_bytes(output / 'submission.json', 'Submission evidence'))
    accepted_submission_path = accepted_submission_path or output / 'accepted-submission.json'
    if accepted_submission_path.exists() or accepted_submission_path.is_symlink():
        accepted_raw = _regular_bytes(accepted_submission_path, 'Accepted submission evidence')
        accepted = json.loads(accepted_raw)
        if not isinstance(local, dict) or not isinstance(accepted, dict):
            raise ValueError('Invalid submission evidence')
        local_request = {key: value for key, value in local.items() if key not in CLIENT_ONLY_SUBMISSION_FIELDS}
        accepted_request = {key: value for key, value in accepted.items() if key not in CLIENT_ONLY_SUBMISSION_FIELDS}
        if local_request != accepted_request:
            raise ValueError('Accepted submission differs from local request')
        return accepted_raw, accepted
    raw = _regular_bytes(output / 'submission.json', 'Submission evidence')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Invalid submission evidence')
    return raw, value


def validate_operator_result(output, attempt, receipt_path=None, terminal_path=None, accepted_submission_path=None):
    """Validate the immutable operator acknowledgement against local evidence."""
    receipt_path = receipt_path or output / 'operator-result.json'
    raw = _regular_bytes(receipt_path, 'Operator result')
    receipt = json.loads(raw)
    required = {'attempt', 'state', 'reason', 'cleanup_verified', 'acknowledged_at',
                'submission_sha256', 'source_digest', 'workflow'}
    allowed = required | {'terminal_sha256'}
    if not isinstance(receipt, dict) or set(receipt) - allowed or not required <= set(receipt):
        raise ValueError('Invalid operator result fields')
    if receipt['attempt'] != attempt or receipt['state'] != 'infrastructure-failed' or receipt['cleanup_verified'] is not True:
        raise ValueError('Invalid operator result identity or cleanup')
    if not all(isinstance(receipt[name], str) and receipt[name] for name in
               ('reason', 'submission_sha256', 'source_digest', 'workflow')):
        raise ValueError('Invalid operator result values')
    if isinstance(receipt['acknowledged_at'], bool) or not isinstance(receipt['acknowledged_at'], (int, float)) or not math.isfinite(receipt['acknowledged_at']):
        raise ValueError('Invalid operator acknowledgement time')
    if not re.fullmatch('[0-9a-f]{64}', receipt['submission_sha256']):
        raise ValueError('Invalid operator submission digest')
    submission, metadata = _submission_evidence(output, accepted_submission_path)
    if hashlib.sha256(submission).hexdigest() != receipt['submission_sha256']:
        raise ValueError('Operator result submission digest mismatch')
    if metadata.get('attempt') != attempt or metadata.get('source_digest') != receipt['source_digest'] or metadata.get('workflow', 'surface') != receipt['workflow']:
        raise ValueError('Operator result submission identity mismatch')
    terminal = terminal_path or output / 'terminal.json'
    declared_terminal = receipt.get('terminal_sha256')
    if declared_terminal is not None:
        if not isinstance(declared_terminal, str) or not re.fullmatch('[0-9a-f]{64}', declared_terminal):
            raise ValueError('Invalid operator terminal digest')
        if hashlib.sha256(_regular_bytes(terminal, 'Terminal evidence')).hexdigest() != declared_terminal:
            raise ValueError('Operator result terminal digest mismatch')
    elif terminal.exists() or terminal.is_symlink():
        raise ValueError('Operator result omits retained terminal evidence')
    return receipt


def retrieve_operator_result(host, output, attempt):
    """Retrieve an acknowledgement and any terminal it explicitly binds."""
    stage = output / ('.operator-result-' + uuid.uuid4().hex)
    stage.mkdir()
    remote = f'{host}:pandora-warm/runs/{attempt}/'
    try:
        subprocess.run(['rsync', '-rt', '-e', 'ssh ' + ' '.join(SSH_OPTIONS),
                        remote + 'operator-result.json', remote + 'submission.json', str(stage) + '/'], check=True, timeout=60)
        receipt = json.loads(_regular_bytes(stage / 'operator-result.json', 'Operator result'))
        if not isinstance(receipt, dict):
            raise ValueError('Invalid operator result fields')
        if receipt.get('terminal_sha256') is not None:
            subprocess.run(['rsync', '-rt', '-e', 'ssh ' + ' '.join(SSH_OPTIONS),
                            remote + 'terminal.json', str(stage) + '/'], check=True, timeout=60)
        validate_operator_result(output, attempt, stage / 'operator-result.json', stage / 'terminal.json', stage / 'submission.json')
        if receipt.get('terminal_sha256') is not None:
            terminal = _regular_bytes(stage / 'terminal.json', 'Terminal evidence')
            if hashlib.sha256(terminal).hexdigest() != receipt['terminal_sha256']:
                raise ValueError('Operator result terminal digest mismatch')
            os.replace(stage / 'terminal.json', output / 'terminal.json')
        os.replace(stage / 'submission.json', output / 'accepted-submission.json')
        os.replace(stage / 'operator-result.json', output / 'operator-result.json')
        return validate_operator_result(output, attempt)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def follow(host, output, reconnect_seconds=45,
           artifact_delivery_limit_bytes=DEFAULT_ARTIFACT_DELIVERY_LIMIT_BYTES):
    artifact_limit = artifact_delivery_limit(artifact_delivery_limit_bytes)
    metadata = json.loads((output / 'submission.json').read_text())
    attempt = metadata['attempt']
    if not re.fullmatch('[0-9a-f]{32}', attempt):
        raise ValueError('Invalid attempt')
    queue_timeout = metadata.get('queue_timeout_seconds', 900)
    if isinstance(queue_timeout, bool) or not isinstance(queue_timeout, int) or not 0 < queue_timeout <= 86400:
        raise ValueError('Invalid submission queue timeout')
    offsets = [0, 0]
    unavailable = None
    unregistered = time.monotonic()
    following = time.monotonic()
    follow_allowance = metadata.get('worker_config', {}).get('execution_seconds', 1500) + 240
    while True:
        if time.monotonic() - following > queue_timeout + follow_allowance:
            print('[pandora] Follow deadline reached; remote state remains unresolved. No replacement submitted.', flush=True)
            return 75
        try:
            state = query(host, attempt, offsets=offsets)
        except (ConnectionError, subprocess.TimeoutExpired, ValueError) as error:
            unavailable = unavailable or time.monotonic()
            print(f'[pandora] connection interrupted for {attempt}; preserving request: {error}', flush=True)
            if time.monotonic() - unavailable >= reconnect_seconds:
                print('[pandora] Retry the same command to recover this request. No new run was submitted.', flush=True)
                return 75
            time.sleep(3)
            continue
        unavailable = None
        for index, key in enumerate(['stdout', 'stderr']):
            if state.get(key):
                print(state[key], end='', file=sys.stderr if key == 'stderr' else sys.stdout, flush=True)
            offsets[index] = state.get('offsets', offsets)[index]
        if state.get('cleanup_verified'):
            if state.get('more_logs'):
                continue
            try:
                terminal = retrieve(host, output, attempt, artifact_limit)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                print(f'[pandora] Evidence retrieval incomplete: {error}. Retry the same command to recover {attempt}.', flush=True)
                return 75
            print(f'[pandora] attempt={attempt}; exit={terminal["exit_code"]}; evidence={output}', flush=True)
            if (output / 'results/docker.json').exists():
                print(f'[pandora] Docker report: {output / "results/docker.json"}', flush=True)
            if terminal['exit_code'] != 0 and (output / 'results/outputs').exists():
                print(f'[pandora] failed-run outputs retained: {output / "results/outputs"}; workspace outputs were not published', flush=True)
            for suite_report in ('suite-plan.json', 'suite-shard.json', 'suite-run.json', 'suite-error.json'):
                if (output / 'results' / suite_report).exists():
                    print(f'[pandora] suite evidence: {output / "results" / suite_report}', flush=True)
            if (output / 'results/journey.json').exists():
                print(f'[pandora] journey report: {output / "results/journey.json"}', flush=True)
            if (output / 'results/junit.xml').exists():
                print(f'[pandora] test report: {output / "results/junit.xml"}', flush=True)
            if (output / 'results/playwright').exists():
                print(f'[pandora] test diagnostics: {output / "results/playwright"}', flush=True)
            return terminal['exit_code']
        if state.get('operator_result'):
            try:
                receipt = retrieve_operator_result(host, output, attempt)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                print(f'[pandora] Operator acknowledgement could not be verified: {error}. Retry the same command; no replacement was submitted.', flush=True)
                return 75
            print(f'[pandora] attempt={attempt}; acknowledged infrastructure failure; evidence={output}', flush=True)
            return 70
        if not state.get('registered') and time.monotonic() - unregistered > 30:
            print('[pandora] Submission is unresolved before worker registration. No replacement started.', flush=True)
            return 75
        time.sleep(2)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('host')
    parser.add_argument('output', type=Path)
    parser.add_argument('--artifact-delivery-limit-bytes', type=int,
                        default=DEFAULT_ARTIFACT_DELIVERY_LIMIT_BYTES)
    args = parser.parse_args()
    raise SystemExit(follow(args.host, args.output,
                            artifact_delivery_limit_bytes=args.artifact_delivery_limit_bytes))
