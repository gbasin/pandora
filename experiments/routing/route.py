#!/usr/bin/env python3
"""Route supported surface commands; preserve one unresolved request per worktree/session."""
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


def write(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2) + '\n')
    temp.replace(path)


def control(output, action):
    source = output / 'submission.json'
    if not source.exists():
        return {'exit_code': 130, 'cleanup_verified': True, 'state': 'cancelled-during-capture'} if action == 'cancel' else None
    attempt = json.loads(source.read_text())['attempt']
    r = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                        os.environ['PANDORA_HOST'], f'python3 - {attempt} {action}'],
                       input=(ROOT / 'control.py').read_text(), text=True,
                       capture_output=True, timeout=20)
    return json.loads(r.stdout) if r.returncode == 0 else None


def main():
    argv = sys.argv[1:]
    normalized = argv[1:] if argv[:1] == ['run'] else argv
    if normalized[:2] == ['test:surface', 'borrower-web']:
        selectors = normalized[2:]
    elif normalized[:3] == ['validate', 'surface', 'borrower-web']:
        selectors = normalized[3:]
    else:
        os.execv(os.environ['PANDORA_REAL_PNPM'], [os.environ['PANDORA_REAL_PNPM'], *argv])
    if any(x.startswith('-') for x in selectors):
        print('[pandora] This trial supports file selectors only. No validation started.', file=sys.stderr)
        return 64
    repo = subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], text=True).strip()
    if Path.cwd().resolve() != Path(repo).resolve():
        print('[pandora] Run this surface command from the repository root. No validation started.', file=sys.stderr)
        return 64
    key = hashlib.sha256((os.environ['PANDORA_SESSION'] + '\0' + repo).encode()).hexdigest()
    state = Path(os.environ['PANDORA_STATE']) / key
    state.mkdir(parents=True, exist_ok=True)
    lock = (state / 'request.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('[pandora] Validation is already active for this session/worktree. '
              'Keep waiting on its existing tool handle. This invocation submitted nothing; '
              'changed input does not replace the active request.', file=sys.stderr)
        return 75
    active = state / 'active.json'
    if active.exists():
        previous = json.loads(active.read_text())
        if previous['state'] == 'active':
            terminal = control(Path(previous['output']), 'status')
            if terminal and terminal.get('cleanup_verified'):
                previous['state'] = 'terminal'
                previous['terminal'] = terminal
                write(active, previous)
                print('[pandora] The previous request finished after its client disconnected. '
                      f'Remote exit={terminal["exit_code"]}. Local evidence: {previous["output"]}. '
                      'No new request submitted. Result/artifact recovery needs operator review.', file=sys.stderr)
                return 75  # Do not claim tests passed without recovered artifacts.
            print('[pandora] A previous request is unresolved. No new request submitted. '
                  'Its client may have disconnected; do not launch a local replacement. '
                  f'Evidence: {previous["output"]}', file=sys.stderr)
            return 75
    output = state / uuid.uuid4().hex
    record = {'state': 'active', 'output': str(output), 'command': argv}
    write(active, record)
    child = None

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        child = subprocess.Popen([sys.executable, '-B', str(ROOT.parent / 'warm/warm.py'),
                                  '--host', os.environ['PANDORA_HOST'], '--repo', repo,
                                  '--output', str(output), '--require-warm', *selectors],
                                 start_new_session=True)
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
        terminal = control(output, 'cancel')
        for _ in range(15):
            if terminal and terminal.get('cleanup_verified'):
                break
            time.sleep(1)
            terminal = control(output, 'status')
        if terminal and terminal.get('cleanup_verified'):
            record.update(state='terminal', terminal=terminal)
            write(active, record)
            print('[pandora] Cancellation verified. No owned test container is running.', file=sys.stderr)
        else:
            print('[pandora] Cleanup remains unresolved; new requests remain blocked.', file=sys.stderr)
        return 130
    terminal_path = output / 'terminal.json'
    if terminal_path.exists():
        terminal = json.loads(terminal_path.read_text())
        if terminal.get('cleanup_verified'):
            record.update(state='terminal', terminal=terminal)
            write(active, record)
    else:
        print('[pandora] No verified terminal record; the request remains unresolved.', file=sys.stderr)
        if status == 0:
            status = 70
    return status if status >= 0 else 128 - status


if __name__ == '__main__':
    raise SystemExit(main())
