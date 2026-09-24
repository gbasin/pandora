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

The drain is a lease. The restarter asks again every second; each ask renews
it and dates the marker. A daemon that hears nothing for `LEASE_SECONDS` ends
the drain itself, so a restarter killed mid-wait (SIGKILL, a tool timeout)
leaves the daemon admitting runs within half a minute rather than answering
`draining` to everyone.

The marker is `<state>/draining`. It is how a client that finds no socket at
all -- the restart gap -- tells a restart from a daemon that is gone. A client
trusts it only while it is younger than its own wait, so a daemon that never
comes back costs each command that wait and no more. One older than
`STALE_SECONDS` is ignored by everyone and reported by `pandora doctor`.

A client told `draining` by a live daemon never runs the command unmanaged:
when its wait runs out it exits 75, retry. Only a daemon that is gone -- no
socket once the marker is no longer fresh -- gets the no-daemon path.

A blocker that is doing nothing is canceled. On 2026-09-24 a local run sat
at 0% CPU for 20 minutes, its pnpm processes idle, and held a 25-minute
drain while every new command waited and then exited 75. The supervisor
samples each local run's process tree every second (`local.Supervisor`), and
a blocker with no CPU progress and no output for `--idle-cancel` seconds
(default 600; 0 never cancels) is canceled by the restarter, with the
reason in its run log. The daemon checks its own reading before it cancels,
so a run that progressed since the last poll is left alone, and a run it
cannot measure is never called idle.
"""
import json
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

from ..exits import STALE
from .protocol import Reader, VERSION, dump

MARKER = 'draining'
# What the daemon tells a client to wait between asks, in seconds.
RETRY_AFTER = 2.0
# How long `pandora daemon --restart` waits for the runs a restart would end.
# `pandora upgrade` waits up to 600 s (`install.WAIT_SECONDS`).
DEFAULT_RESTART_WAIT = 300.0
LONGEST_RESTART_WAIT = 600.0
# How long a blocker may go without CPU progress before a drain cancels it.
DEFAULT_IDLE_CANCEL = 600.0
# A blocker idle for less than this is not called idle in the blocker lines.
IDLE_SHOWN = 60.0
# How long a daemon keeps draining with no `drain` request renewing it.
LEASE_SECONDS = 30
# How long a restart waits for the successor to clear the marker.
SUCCESSOR_SECONDS = 20.0
# How long a client waits for a restart: the longest restart wait, plus the
# restart itself. Shorter, and a client gives up on a drain that is working.
WAIT_ENV = 'PANDORA_DRAIN_WAIT'
DEFAULT_CLIENT_WAIT = LONGEST_RESTART_WAIT + 60.0
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


# What a client does when no daemon answers the socket: the one decision.
WAIT_FOR_RESTART = 'wait-for-restart'     # a drain says a daemon is coming
GRACE_THEN_REFUSE = 'grace-then-refuse'   # installed here: a short grace, then exit 70
PASS_THROUGH = 'pass-through'             # never installed here: no daemon, no Pandora


def when_unanswered(marker, installed, *, waited=False, budget=DEFAULT_CLIENT_WAIT):
    """Which of the three a command takes when the socket is absent or refuses.

    A fresh draining marker (younger than the client's wait, and never a stale
    one), or a wait this command already began, is a restart: wait for it on the
    client's budget, then exit 75. Otherwise an installed daemon that does not
    answer gets a few seconds, then exit 70 with nothing run. Only a Mac where
    the daemon was never installed runs the command as if Pandora were absent.
    """
    if waited or (marker is not None and marker['age'] < min(budget, STALE_SECONDS)):
        return WAIT_FOR_RESTART
    return GRACE_THEN_REFUSE if installed else PASS_THROUGH


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
        # What the last ask found: 'draining' (a live daemon said so) or
        # 'absent'. A wait that ends on a live daemon's `draining` is exit 75,
        # never an unmanaged run.
        self.last = None

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
        self.last = 'draining'
        try:
            every = float(retry_after) if retry_after is not None else RETRY_AFTER
        except (TypeError, ValueError):
            every = RETRY_AFTER
        return self.pause(every if every > 0 else RETRY_AFTER)

    def absent(self):
        self.last = 'absent'
        marker = read_marker(self.state, clock=self.wall)
        if marker is None or marker['age'] >= min(self.budget, STALE_SECONDS):
            return False
        return self.pause(ABSENT_POLL)

    def fresh_since(self, sent):
        """A fresh marker from a drain that began before `sent` (wall clock): EOF is a retry.

        A request sent after the drain began was never admitted, so a
        connection the stopping daemon dropped unanswered ran nothing.
        """
        marker = read_marker(self.state, clock=self.wall)
        since = (marker or {}).get('since')
        return (marker is not None and marker['age'] < min(self.budget, STALE_SECONDS)
                and isinstance(since, (int, float)) and since <= sent)

    def decide(self, installed):
        """`when_unanswered` for this command, from the marker as it is now."""
        return when_unanswered(read_marker(self.state, clock=self.wall), installed,
                               waited=self.waited(), budget=self.budget)

    def gave_up(self):
        """The one line said when the budget ran out with no daemon answering."""
        return ('the daemon was draining for a restart and did not come back within %gs (%s); '
                'nothing ran. Retry, or run `pandora doctor`' % (self.budget, WAIT_ENV))

    def still_draining(self):
        """The one line said when the budget ran out on a daemon that still says `draining`."""
        return ('the daemon is still draining for a restart after %gs (%s); nothing ran. '
                'Retry in a minute' % (self.budget, WAIT_ENV))


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


def fmt_idle(seconds):
    """`45s`, `20m`, `2h05m`."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return '%ds' % seconds
    if seconds < 3600:
        return '%dm' % (seconds // 60)
    return '%dh%02dm' % (seconds // 3600, seconds % 3600 // 60)


def idle_of(row):
    """The blocker's seconds without progress, or None when unmeasured."""
    value = row.get('idle_seconds')
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        else None


def blocker_line(row):
    state = row.get('state') or '?'
    if state == 'queued' and row.get('phase') in PRE_ACCEPT:
        state = PRE_ACCEPT[row['phase']]
    idle = idle_of(row)
    if idle is not None and idle >= IDLE_SHOWN:
        state += ', idle %s' % fmt_idle(idle)
    return '%s %s %s: %s' % (row.get('id', '?'), row.get('lane') or 'remote', state,
                             ' '.join(row.get('argv') or [])[:60])


def shape(rows):
    """What a blocker list must change in before it is printed again: who, and how idle.

    Idle time in five-minute steps, so an idle run is said again every five
    minutes rather than every second.
    """
    return [(row.get('id'), None if (idle_of(row) or 0) < IDLE_SHOWN
             else int(idle_of(row) // 300)) for row in rows]


def cancel_idle(sock_path, row, limit, *, ask=ask, say=notice):
    """Ask the daemon to cancel one blocker idle for `limit` seconds. True when it did."""
    say('canceling %s: no CPU progress and no output for %s (--idle-cancel %gs): %s'
        % (row.get('id'), fmt_idle(idle_of(row) or 0), limit,
           ' '.join(row.get('argv') or [])[:60]))
    try:
        answer = ask(sock_path, {'op': 'cancel', 'run': row.get('id'), 'if_idle': limit,
                                 'pid': os.getpid(), 'by': 'pid %d' % os.getpid()})
    except (OSError, ValueError) as error:
        say('  the daemon did not answer the cancel (%s); still waiting for it' % error)
        return False
    if isinstance(answer, dict) and answer.get('t') == 'ok':
        return True
    say('  not canceled: %s' % ((answer or {}).get('msg') if isinstance(answer, dict)
                                 else answer))
    return False


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


def end_loudly(sock_path, *, ask=ask, say=notice):
    """`end`, and a line that says so when it did not take. True when it took."""
    if end(sock_path, ask=ask):
        return True
    say('could not end the drain: the daemon did not answer. It answers `draining` until '
        'it ends the drain itself, within %ds of the last request' % LEASE_SECONDS)
    return False


def lock_held(state):
    """Whether a daemon holds `daemon.lock`: one may be alive behind a refusing socket."""
    from .launchd import lock_holder
    return bool(lock_holder(state))


class Interrupted(BaseException):
    """SIGHUP or SIGTERM reached the restarter: end the drain on the way out."""


class _Signals:
    """SIGHUP and SIGTERM raise `Interrupted` for the length of a drain, on the main thread."""

    def __enter__(self):
        self.saved = {}
        if threading.current_thread() is not threading.main_thread():
            return self

        def interrupt(number, _frame):
            raise Interrupted(signal.Signals(number).name)
        for number in (signal.SIGHUP, signal.SIGTERM):
            try:
                self.saved[number] = signal.signal(number, interrupt)
            except (ValueError, OSError):
                pass
        return self

    def __exit__(self, *_):
        for number, handler in self.saved.items():
            try:
                signal.signal(number, handler)
            except (ValueError, OSError):
                pass
        return False


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
                      before_restart=None, undo_before_restart=None, ask=ask,
                      clock=time.monotonic, sleep=time.sleep, interval=1.0,
                      successor_seconds=SUCCESSOR_SECONDS,
                      again='`pandora daemon --restart --now`', report=None, held_lock=None,
                      idle_cancel=DEFAULT_IDLE_CANCEL):
    """Drain the daemon, wait for what a restart would end, restart it. Returns an exit code.

    `restart()` restarts the daemon (launchd's kickstart for `pandora daemon
    --restart`); `before_restart()`, when given, runs once nothing blocks and
    before the restart -- `pandora upgrade` moves `current` there, and
    `undo_before_restart()` moves it back when a daemon from before drain
    admitted a run meanwhile. Either may raise: the daemon leaves draining and
    the error propagates. So do SIGHUP and SIGTERM, as `Interrupted`. `again`
    is the command the timeout and silence messages name for going ahead
    anyway. `report`, a dict, gets `undrained`: False when the drain could not
    be ended and lingers until its lease runs out. A local blocker with no CPU
    progress for `idle_cancel` seconds is canceled (0 or None: never).

    0 when the successor cleared the marker; 75 (`STALE`) when the wait ran out
    without `now`, after the daemon left draining; 1 when the successor did not
    come back. A daemon from before drain is waited on through `ps`, with no
    submissions held. Nothing listening and no lock held: restarted at once.
    A lock held behind a refusing socket, or a daemon that does not answer, is
    restarted only with `now`.
    """
    state = Path(state)
    sock = state / 'client.sock'
    report = {} if report is None else report
    report['undrained'] = True
    held_lock = held_lock or (lambda: lock_held(state))
    deadline = clock() + max(0.0, float(wait))
    held = True

    def silent(why):
        return [{'id': '-', 'lane': 'daemon', 'state': 'silent',
                 'argv': ['(did not answer: %s)' % why]}]

    def undrain():
        if held:
            report['undrained'] = end_loudly(sock, ask=ask, say=say)

    with _Signals():
        unanswered = None
        try:
            blocking = begin(sock, ask=ask)
        except (FileNotFoundError, ConnectionRefusedError) as error:
            if held_lock():
                # macOS refuses a connection when the listen backlog is full:
                # a daemon that holds the lock may be alive and driving runs.
                unanswered = '%s, but a daemon holds %s' % (error, state / 'daemon.lock')
            else:
                say('no daemon answers on %s (%s); restarting without a drain'
                    % (sock, error))
                held, blocking = False, []
        except Unanswered as error:
            unanswered = str(error)
        if unanswered is not None:
            if not now:
                undrain()     # in case it heard the drain and the answer was lost
                say('the daemon did not answer the drain (%s); not restarting. Look at '
                    '%s, or go ahead anyway with %s'
                    % (unanswered, state / 'logs' / 'daemon.log', again))
                return STALE
            say('the daemon did not answer the drain (%s); restarting anyway (--now)'
                % unanswered)
            held, blocking = False, []
        predates = held and blocking is None
        if predates:
            held = False
            say('the daemon predates drain: new submissions are not held, and the restart '
                'waits for a moment when nothing it would end is running')
            try:
                blocking = rows_from_ps(sock, ask=ask)
            except Unanswered as error:
                blocking = silent(error)

        def poll():
            try:
                return begin(sock, ask=ask) if held else rows_from_ps(sock, ask=ask)
            except Unanswered as caught:
                return silent(caught)
            except (FileNotFoundError, ConnectionRefusedError) as caught:
                if held_lock():
                    return silent('%s, but a daemon holds the lock' % caught)
                say('the daemon went away while draining; restarting')
                return []

        try:
            canceled = set()
            while True:
                said = None
                while blocking:
                    if held and idle_cancel and idle_cancel > 0:
                        for row in blocking:
                            idle = idle_of(row)
                            if (row.get('id') not in canceled and row.get('lane') == 'local'
                                    and idle is not None and idle >= idle_cancel
                                    and cancel_idle(sock, row, idle_cancel, ask=ask, say=say)):
                                canceled.add(row.get('id'))
                    lines = [blocker_line(row) for row in blocking]
                    if shape(blocking) != said:
                        say('draining: waiting up to %ds for %d run%s a restart would end:'
                            % (max(0, round(deadline - clock())), len(lines),
                               '' if len(lines) == 1 else 's'))
                        for line in lines:
                            say('  ' + line)
                        said = shape(blocking)
                    remaining = deadline - clock()
                    if remaining <= 0:
                        break
                    sleep(min(interval, remaining))
                    blocking = poll()
                if blocking and not now:
                    undrain()
                    say('gave up after %ds; the daemon was not restarted%s. Still running:'
                        % (round(wait), ' and is admitting runs again'
                           if report['undrained'] else ''))
                    for row in blocking:
                        say('  ' + blocker_line(row))
                    say('retry later, or %s to end them' % again)
                    return STALE
                if blocking:
                    say(NOW_NOTE)
                elif held:
                    say('drained: nothing a restart would end is running')
                if before_restart is not None:
                    before_restart()
                if not predates or now:
                    break
                # A daemon from before drain admits runs while we flip: look
                # once more, and put the flip back if one arrived.
                late = poll()
                if not late:
                    break
                if undo_before_restart is not None:
                    undo_before_restart()
                say('a run started as the restart was prepared; waiting again:')
                for row in late:
                    say('  ' + blocker_line(row))
                blocking = late
                if clock() >= deadline:
                    say('gave up after %ds; the daemon was not restarted. Retry later, or '
                        '%s' % (round(wait), again))
                    return STALE
            # The gap starts now, and a client trusts the marker only for its own
            # wait from this date, not from when the drain began.
            touch_marker(state)
            restart()
        except BaseException:
            # Ctrl-C, SIGHUP, SIGTERM, a refused kickstart, a failed
            # `before_restart`: the daemon must not be left refusing every
            # submission for nobody.
            undrain()
            raise
    if not held:
        return 0
    limit = clock() + successor_seconds
    while marker_path(state).exists():
        if clock() >= limit:
            marker = read_marker(state) or {}
            try:
                pong = ask(sock, {'op': 'ping'}, timeout=5.0)
            except (OSError, ValueError):
                pong = None
            if (isinstance(pong, dict) and pong.get('t') == 'pong'
                    and pong.get('pid') == marker.get('daemon')):
                # The kickstart did not stop it: the old daemon still answers.
                undrain()
                say('the daemon (pid %s) is still the one that drained, %ds after the '
                    'restart; launchd did not restart it. Read %s'
                    % (pong.get('pid'), successor_seconds, state / 'logs' / 'daemon.log'))
                return 1
            say('the draining marker is still there %ds after the restart: no new daemon has '
                'settled its runs. Read %s. Commands wait up to %s (%gs) each, then run '
                'here unmanaged' % (successor_seconds, state / 'logs' / 'daemon.log',
                                    WAIT_ENV, DEFAULT_CLIENT_WAIT))
            return 1
        sleep(0.25)
    say('the new daemon is up and admitting runs')
    return 0
