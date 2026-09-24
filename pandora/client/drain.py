"""Draining: a restart that waits for the runs it would end, while submissions wait for it.

Eight agents submit all day, so `pandora ps` is never empty and "restart at a
quiet moment" never comes. A restart under load was safe after #97 and #98 but
lossy: every local run executing at that instant ended with exit 70, and every
command typed in the one or two seconds without a socket ran unmanaged.

So a restart drains first. The daemon stops admitting runs and answers each new
`run` or `claims` request with a `draining` frame; the client waits and asks
again. Accepted remote runs are left alone (the successor follows them), and a
restart waits for what it would otherwise end: a local run executing here, and
a remote row still before `accepted`. Then launchd restarts the daemon, and the
successor removes the marker once it has settled every row.

The marker is `<state>/draining`. It is how a client that finds no socket at
all -- the restart gap -- tells a restart from a daemon that is gone. Its date
is refreshed just before the restart, and a client trusts it only while it is
younger than its own wait, so a daemon that never comes back costs each
command that wait and no more. One older than `STALE_SECONDS` is ignored by
everyone and reported by `pandora doctor`.
"""
import json
import os
import socket
import sys
import time
from pathlib import Path

from .protocol import Reader, VERSION, dump

MARKER = 'draining'
# What the daemon tells a client to wait between asks, in seconds.
RETRY_AFTER = 2.0
# A marker older than this is a restart that never finished; nobody waits on it.
STALE_SECONDS = 15 * 60


def notice(text):
    sys.stderr.write('pandora: ' + text + '\n')
    sys.stderr.flush()


# -- the marker ------------------------------------------------------------------

def marker_path(state):
    return Path(state) / MARKER


def write_marker(state, record):
    path = marker_path(state)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(record) + '\n')
    temp.replace(path)


def clear_marker(state):
    try:
        marker_path(state).unlink()
    except FileNotFoundError:
        pass


def touch_marker(state):
    """Date the marker now: the restart is about to happen, not when the drain began."""
    try:
        os.utime(marker_path(state))
    except OSError:
        pass


def read_marker(state, clock=time.time):
    """The marker's record plus its `age` in seconds (by its date), or None."""
    path = marker_path(state)
    try:
        age = clock() - path.stat().st_mtime
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        record = {}
    record['age'] = max(0.0, age)
    return record


# -- asking the daemon -----------------------------------------------------------

def ask(sock_path, request, timeout=30.0):
    """One request, one frame. Raises OSError when nothing answers."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(sock_path))
        sock.sendall(dump(dict({'v': VERSION}, **request)))
        return Reader(sock).line()
    finally:
        sock.close()


def blockers(rows):
    """The rows a restart would end: a local run executing, a remote row before `accepted`."""
    out = []
    for row in rows:
        state, lane = row.get('state'), row.get('lane') or 'remote'
        if (lane == 'local' and state == 'running') or (lane != 'local' and state == 'queued'):
            out.append(row)
    return out


PRE_ACCEPT = {'freeze': 'freezing', 'ship': 'shipping', 'submit': 'submitting'}


def blocker_line(row):
    state = row.get('state') or '?'
    if state == 'queued' and row.get('phase') in PRE_ACCEPT:
        state = PRE_ACCEPT[row['phase']]
    return '%s %s %s: %s' % (row.get('id', '?'), row.get('lane') or 'remote', state,
                             ' '.join(row.get('argv') or [])[:60])
