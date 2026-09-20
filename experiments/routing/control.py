"""Read/cancel exactly one experiment attempt. Sent to the worker over SSH."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys


def main():
    attempt, action = sys.argv[1:3]
    offsets = [int(x) for x in sys.argv[3:5]] or [0, 0]
    if len(offsets) != 2 or any(x < 0 for x in offsets):
        raise ValueError('Invalid log offsets')
    if not re.fullmatch('[0-9a-f]{32}', attempt) or action not in {'status', 'cancel', 'release'}:
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
        result = json.loads(terminal.read_text())
        if action == 'release' and result.get('cleanup_verified'):
            (path / 'released').touch()
            if result.get('workflow') == 'suite-run':
                sys.path.insert(0, str(path))
                from suite_parent_cleanup import validate_registry
                for identity in validate_registry(path, json.loads((path / 'children.json').read_text())):
                    child = path.parent / identity
                    if (child / 'terminal.json').exists():
                        receipt = json.loads((child / 'terminal.json').read_text())
                        if receipt.get('attempt') == identity and receipt.get('cleanup_verified'):
                            (child / 'released').touch()
    elif not (path / 'worker.json').exists() and (path / 'cancel.request').exists():
        # Registration precedes the worker's cancel-marker check. A late worker
        # therefore exits before preparation/execution even if no PID exists yet.
        result = {'exit_code': 130, 'cleanup_verified': True,
                  'state': 'cancelled-before-start'}
    else:
        result = {'state': 'active-or-unresolved'}
    result['registered'] = (path / 'worker.json').exists()
    result['offsets'] = offsets
    result['more_logs'] = False
    for index, name in enumerate(['stdout', 'stderr']):
        log = path / (name + '.log')
        if log.exists():
            with log.open('rb') as stream:
                stream.seek(offsets[index])
                result[name] = stream.read(65536).decode('utf-8', errors='replace')
                result['offsets'][index] = stream.tell()
                result['more_logs'] = result['more_logs'] or stream.tell() < log.stat().st_size
    print(json.dumps(result))


if __name__ == '__main__':
    main()
