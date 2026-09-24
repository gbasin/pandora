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
# How long a client waits for a restart before it runs the command unmanaged.
WAIT_ENV = 'PANDORA_DRAIN_WAIT'
DEFAULT_CLIENT_WAIT = 180.0
# A marker older than this is a restart that never finished; nobody waits on it.
STALE_SECONDS = 15 * 60
# Between asks while no socket exists: the gap is one or two seconds, so a
# two-second poll would double what a command pays for it.
ABSENT_POLL = 0.5
NOTICE = 'daemon is restarting; waiting'


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


def client_wait(environ=None):
    """PANDORA_DRAIN_WAIT in seconds, or the default; never negative."""
    raw = (os.environ if environ is None else environ).get(WAIT_ENV, '').strip()
    try:
        return max(0.0, float(raw)) if raw else DEFAULT_CLIENT_WAIT
    except ValueError:
        return DEFAULT_CLIENT_WAIT


class Waiter:
    """One command's wait for a restart: one notice, one budget, shared by every ask.

    `draining` is for a daemon that answered `draining`: it is alive and said
    so, and the marker does not matter. `absent` is for no socket: only a
    marker younger than the budget (and never a stale one) says a daemon is on
    its way. Each returns True after sleeping, when the caller should ask
    again, and False when the budget is spent or there is nothing to wait for.
    """

    def __init__(self, state, *, budget=None, clock=time.monotonic, wall=time.time,
                 sleep=time.sleep, say=notice):
        self.state = state
        self.budget = client_wait() if budget is None else budget
        self.clock, self.wall, self.sleep, self.say = clock, wall, sleep, say
        self.began = None
        self.exhausted = False

    def spent(self):
        return 0.0 if self.began is None else self.clock() - self.began

    def waited(self):
        """Whether this command has waited on a restart at all."""
        return self.began is not None

    def pause(self, seconds):
        if self.began is None:
            self.began = self.clock()
            self.say(NOTICE)
        remaining = self.budget - self.spent()
        if remaining <= 0:
            self.exhausted = True
            return False
        self.sleep(max(0.0, min(seconds, remaining)))
        return True

    def draining(self, retry_after=None):
        try:
            every = float(retry_after) if retry_after is not None else RETRY_AFTER
        except (TypeError, ValueError):
            every = RETRY_AFTER
        return self.pause(every if every > 0 else RETRY_AFTER)

    def absent(self):
        marker = read_marker(self.state, clock=self.wall)
        if marker is None or marker['age'] >= min(self.budget, STALE_SECONDS):
            return False
        return self.pause(ABSENT_POLL)

    def gave_up(self):
        """The one line said when the budget ran out, before the no-daemon path."""
        return 'the daemon did not come back within %gs (%s)' % (self.budget, WAIT_ENV)


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
