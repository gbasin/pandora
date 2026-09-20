"""Stop one registered attempt at its deadline without trusting a unit name."""
import json
import os
from pathlib import Path
import re
import signal
import sys
import time


def start_ticks(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().split()[21]
    except (FileNotFoundError, IndexError):
        return None


def stop(attempt):
    attempt = Path(attempt)
    if not re.fullmatch('[0-9a-f]{32}', attempt.name):
        raise ValueError('Invalid attempt identity')
    marker = attempt / 'deadline.request'
    marker.write_text(json.dumps({'requested_at': time.time()}) + '\n')
    try:
        registered = json.loads((attempt / 'worker.json').read_text())
        pid, expected = registered['pid'], registered['start_ticks']
        if type(pid) is not int or pid <= 0 or not isinstance(expected, str) or not expected.isdecimal():
            raise ValueError('Invalid worker registration')
    except FileNotFoundError:
        return False
    if start_ticks(pid) != expected:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    return True


if __name__ == '__main__':
    stop(sys.argv[1])
