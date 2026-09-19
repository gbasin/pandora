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
from commands import classify
from transport import query
from snapshot import names, excluded, entry, encode


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


def main():
    argv = sys.argv[1:]
    action, selectors, message = classify(argv, os.environ.get('PANDORA_TREATMENT', 'normal'))
    if action == 'local':
        os.execv(os.environ['PANDORA_REAL_PNPM'], [os.environ['PANDORA_REAL_PNPM'], *argv])
    if action == 'reject':
        print('[pandora] ' + message, file=sys.stderr)
        return 64
    repo = Path(subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], text=True).strip())
    if Path.cwd().resolve() != repo.resolve():
        print('[pandora] Run this surface command from the repository root. No validation started.', file=sys.stderr)
        return 64
    key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()
    state = Path(os.environ['PANDORA_STATE']) / key
    state.mkdir(parents=True, exist_ok=True)
    lock = (state / 'request.lock').open('a')
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
        if record.get('host') != os.environ['PANDORA_HOST'] or record['command'] != argv:
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
        output = state / uuid.uuid4().hex
        record = {'state': 'active', 'output': str(output), 'command': argv, 'host': os.environ['PANDORA_HOST'], 'session': os.environ['PANDORA_SESSION'], 'attempt': uuid.uuid4().hex}
        write(active, record)
        command = [sys.executable, '-B', str(ROOT.parent / 'warm/warm.py'),
                   '--host', os.environ['PANDORA_HOST'], '--repo', str(repo),
                   '--output', str(output), '--require-warm', '--attempt', record['attempt'], *selectors]
    child = None

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
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
        terminal = json.loads(terminal_path.read_text())
        if terminal.get('cleanup_verified'):
            record.update(state='terminal', terminal=terminal)
            write(active, record)
            if recovering:
                submitted = json.loads((output / 'submission.json').read_text())
                if current_digest(repo) != submitted['source_digest']:
                    print('[pandora] Recovered result applies to earlier source. Local source changed; run again to validate current source.', file=sys.stderr)
                    return 75
    else:
        if not (output / 'submission.json').exists():
            # The preparer has exited and remote launch cannot precede metadata.
            record.update(state='terminal', reason='capture-failed')
            write(active, record)
        print('[pandora] No verified terminal evidence; retry the same command to recover. No replacement was submitted.', file=sys.stderr)
        if status == 0:
            status = 70
    return status if status >= 0 else 128 - status


if __name__ == '__main__':
    raise SystemExit(main())
