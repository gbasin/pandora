"""Read/cancel exactly one experiment attempt. Sent to the worker over SSH."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys


def main():
    attempt, action = sys.argv[1:]
    if not re.fullmatch('[0-9a-f]{32}', attempt) or action not in {'status', 'cancel'}:
        raise ValueError('Invalid attempt/action')
    path = Path.home() / 'pandora-warm/runs' / attempt
    if action == 'cancel':
        path.mkdir(parents=True, exist_ok=True)
        (path / 'cancel.request').touch()
        registration = path / 'worker.json'
        if registration.exists():
            worker = json.loads(registration.read_text())
            pid = worker['pid']
            proc = Path(f'/proc/{pid}/stat')
            if proc.exists() and proc.read_text().split()[21] == worker['start_ticks']:
                os.kill(pid, signal.SIGTERM)
    terminal = path / 'terminal.json'
    if terminal.exists():
        print(terminal.read_text())
    elif not (path / 'worker.json').exists() and (path / 'cancel.request').exists():
        # Registration precedes the worker's cancel-marker check. A late worker
        # therefore exits before preparation/execution even if no PID exists yet.
        print(json.dumps({'exit_code': 130, 'cleanup_verified': True,
                          'state': 'cancelled-before-start'}))
    else:
        print(json.dumps({'state': 'active-or-unresolved'}))


if __name__ == '__main__':
    main()
