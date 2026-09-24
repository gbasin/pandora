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

from ..exits import STALE
from .protocol import Reader, VERSION, dump

MARKER = 'draining'
# What the daemon tells a client to wait between asks, in seconds.
RETRY_AFTER = 2.0
# How long a client waits for a restart before it runs the command unmanaged.
WAIT_ENV = 'PANDORA_DRAIN_WAIT'
DEFAULT_CLIENT_WAIT = 180.0
# How long `pandora daemon --restart` waits for the runs a restart would end.
DEFAULT_RESTART_WAIT = 300.0
# A marker older than this is a restart that never finished; nobody waits on it.
STALE_SECONDS = 15 * 60
# Between asks while no socket exists: the gap is one or two seconds, so a
# two-second poll would double what a command pays for it.
ABSENT_POLL = 0.5
# How long a restart waits for the successor to clear the marker.
SUCCESSOR_SECONDS = 20.0
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
        if self.budget <= 0:
            self.exhausted = True        # PANDORA_DRAIN_WAIT=0: never wait, never say so
            return False
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


class Unanswered(Exception):
    """The daemon is there, or may be, and did not answer the drain."""


def begin(sock_path, *, ask=ask):
    """Drain, or ask again: the rows still blocking a restart, or None for a daemon without drain.

    Raises FileNotFoundError or ConnectionRefusedError when no daemon listens,
    and Unanswered when one may and said nothing usable.
    """
    try:
        answer = ask(sock_path, {'op': 'drain', 'pid': os.getpid()})
    except (FileNotFoundError, ConnectionRefusedError):
        raise
    except (OSError, ValueError) as error:
        raise Unanswered(str(error) or type(error).__name__) from None
    if isinstance(answer, dict) and answer.get('t') == 'drain':
        return answer.get('blockers') or []
    if isinstance(answer, dict) and answer.get('t') == 'error' and 'unknown op' in (
            answer.get('msg') or ''):
        return None
    raise Unanswered('it answered %s' % ((answer or {}).get('msg') if isinstance(answer, dict)
                                          else answer))


def end(sock_path, *, ask=ask):
    """Leave draining. True when the daemon said so; never raises."""
    try:
        answer = ask(sock_path, {'op': 'drain', 'cancel': True})
    except (OSError, ValueError):
        return False
    return isinstance(answer, dict) and answer.get('t') == 'drain' and not answer.get('draining')


def rows_from_ps(sock_path, *, ask=ask):
    """A daemon from before drain: the same question, asked of `ps`.

    Its queued local runs block too: nothing withdraws them for resubmission,
    and its stop would end them with exit 70.
    """
    try:
        answer = ask(sock_path, {'op': 'ps'})
    except (OSError, ValueError) as error:
        raise Unanswered(str(error) or type(error).__name__) from None
    if not isinstance(answer, dict) or answer.get('t') != 'ps':
        raise Unanswered('it answered %s' % answer)
    rows = answer.get('data') or []
    return [row for row in rows if row in blockers(rows) or (
        (row.get('lane') == 'local') and row.get('state') == 'queued')]


NOW_NOTE = ('restarting now (--now): a local run still executing ends with exit 70; a remote '
            'run still freezing or shipping ends with exit 70, one submitting is looked up '
            'on the worker. Rerun what ended. Accepted remote runs continue')


def drain_and_restart(state, *, restart, wait=DEFAULT_RESTART_WAIT, now=False, say=notice,
                      before_restart=None, ask=ask, clock=time.monotonic, sleep=time.sleep,
                      interval=1.0, successor_seconds=SUCCESSOR_SECONDS,
                      again='`pandora daemon --restart --now`'):
    """Drain the daemon, wait for what a restart would end, restart it. Returns an exit code.

    `restart()` restarts the daemon (launchd's kickstart for `pandora daemon
    --restart`); `before_restart()`, when given, runs once nothing blocks and
    before the restart -- `pandora upgrade` moves `current` there. Either may
    raise: the daemon leaves draining and the error propagates. `again` is the
    command the timeout and silence messages name for going ahead anyway.

    0 when the successor cleared the marker; 75 (`STALE`) when the wait ran out
    without `now`, after the daemon left draining; 1 when the successor did not
    come back. A daemon from before drain is waited on through `ps`, with no
    submissions held. Nothing listening: restarted at once, nothing to wait for.
    One that does not answer is restarted only with `now`.
    """
    state = Path(state)
    sock = state / 'client.sock'
    deadline = clock() + max(0.0, float(wait))
    held = True
    try:
        blocking = begin(sock, ask=ask)
    except (FileNotFoundError, ConnectionRefusedError) as error:
        say('no daemon answers on %s (%s); restarting without a drain' % (sock, error))
        held, blocking = False, []
    except Unanswered as error:
        if not now:
            end(sock, ask=ask)        # in case it heard the drain and the answer was lost
            say('the daemon did not answer the drain (%s); not restarting. Look at '
                '%s, or go ahead anyway with %s' % (error, state / 'logs' / 'daemon.log', again))
            return STALE
        say('the daemon did not answer the drain (%s); restarting anyway (--now)' % error)
        held, blocking = False, []
    if blocking is None:
        held = False
        say('the daemon predates drain: new submissions are not held, and the restart '
            'waits for a moment when nothing it would end is running')
        try:
            blocking = rows_from_ps(sock, ask=ask)
        except Unanswered as error:
            blocking = [{'id': '-', 'lane': 'daemon', 'state': 'silent',
                         'argv': ['(did not answer: %s)' % error]}]

    def poll():
        return begin(sock, ask=ask) if held else rows_from_ps(sock, ask=ask)

    try:
        said = None
        while blocking:
            lines = [blocker_line(row) for row in blocking]
            if lines != said:
                say('draining: waiting up to %ds for %d run%s a restart would end:'
                    % (max(0, round(deadline - clock())), len(lines),
                       '' if len(lines) == 1 else 's'))
                for line in lines:
                    say('  ' + line)
                said = lines
            remaining = deadline - clock()
            if remaining <= 0:
                break
            sleep(min(interval, remaining))
            try:
                blocking = poll()
            except Unanswered as error:
                blocking = [{'id': '-', 'lane': 'daemon', 'state': 'silent',
                             'argv': ['(did not answer: %s)' % error]}]
            except (FileNotFoundError, ConnectionRefusedError):
                say('the daemon went away while draining; restarting')
                blocking = []
        if blocking:
            if not now:
                if held:
                    end(sock, ask=ask)
                say('gave up after %ds; the daemon is admitting runs again and was not '
                    'restarted. Still running:' % round(wait))
                for row in blocking:
                    say('  ' + blocker_line(row))
                say('retry later, or %s to end them' % again)
                return STALE
            say(NOW_NOTE)
        elif held:
            say('drained: nothing a restart would end is running')
        if before_restart is not None:
            before_restart()
        # The gap starts now, and a client trusts the marker only for its own
        # wait from this date, not from when the drain began.
        touch_marker(state)
        restart()
    except BaseException:
        # Ctrl-C, a refused kickstart, a failed `before_restart`: the daemon
        # must not be left refusing every submission for nobody.
        if held:
            end(sock, ask=ask)
        raise
    if not held:
        return 0
    limit = clock() + successor_seconds
    while marker_path(state).exists():
        if clock() >= limit:
            say('the draining marker is still there %ds after the restart: no new daemon has '
                'settled its runs. Read %s. Commands wait up to %s (%gs) each, then run '
                'here unmanaged' % (successor_seconds, state / 'logs' / 'daemon.log',
                                    WAIT_ENV, DEFAULT_CLIENT_WAIT))
            return 1
        sleep(0.25)
    say('the new daemon is up and admitting runs')
    return 0
