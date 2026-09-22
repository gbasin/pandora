"""The routed half of the shim: connect, stream, and get the exit code right.

Two rules, and the second one is new.

**Before `accepted`** the command provably has not run, so falling back is
*permitted*. **After `accepted`** the command is on a worker, so falling back
could run it twice; from that point a failure is an infrastructure failure
(exit 70) and never a local run.

Permitted is not the same as wise, and this file used to treat them as the same
thing: every pre-accept error code became `exec pnpm`, which is how a refused
submission turned into 302 browser tests on this Mac. So the second rule is that
**this process never decides to run a claimed command locally while the daemon
is alive**. A daemon that answers has the configuration, the size classes, the
local queue and the host's own pressure; its answer is final, whatever it is. It
falls back by admitting the job into its local lane and streaming it back here,
which arrives as an ordinary `accepted` frame carrying `lane: local`.

That leaves exactly one decision here: what to do when the daemon cannot be
reached at all. There is no local lane to admit into, so the client applies the
same policy from the enrolment marker -- a `refuse` verdict exits 70 with one
line, and a `local` verdict runs under the file-lock slot budget and says that
is what it did.
"""
import argparse
import base64
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from ..exits import CANCELLED, INFRA, STALE, USAGE
from . import enrolment, envfilter, fallback as fallback_module
from .protocol import Reader, VERSION, dump

HANDSHAKE_SECONDS = 20.0     # freeze + ship + submit happen before `accepted`
REATTACH_ATTEMPTS = 20
REATTACH_PAUSE = 0.25


def notice(text):
    sys.stderr.write('pandora: ' + text + '\n')
    sys.stderr.flush()


def die_by(number):
    """Die the way the worker died, so the caller's `$?` and traps are unchanged.

    `signal.signal` refuses SIGKILL and SIGSTOP; re-raising still works, since
    their default disposition can never have been replaced.
    """
    try:
        signal.signal(number, signal.SIG_DFL)
    except (OSError, ValueError, RuntimeError):
        pass
    os.kill(os.getpid(), number)


def run_local(real, argv, *, state=None, claimed=True, reason=''):
    """Run the command here, under the fallback budget when it was claimed."""
    environment = dict(os.environ, PANDORA_ROUTE_DEPTH='1', PANDORA_REAL_PNPM=real)
    slot = None
    started = time.time()
    if claimed and state is not None:
        config = fallback_module.config_for(state)
        count = int(config.get('fallback_slots', 2))
        wait = float(config.get('fallback_wait_seconds', 0))
        try:
            slot = fallback_module.acquire(state, count, wait)
        except fallback_module.Unbounded as error:
            notice('cannot reach the fallback budget at %s (%s); running locally without '
                   'one. Concurrent fallbacks are not limited.' % (state, error))
            slot = None
        else:
            if slot is None:
                notice('%d local fallback slots are all busy; refusing to add a %s run to '
                       'this Mac. Retry, or set PANDORA_OFF=1 to run it anyway.'
                       % (count, argv[0] if argv else 'pnpm'))
                return STALE
    try:
        code = subprocess.call([real, *argv], env=environment)
    finally:
        if slot is not None:
            slot.release()
    if state is not None:
        try:
            fallback_module.record(state, {'ts': started,
                                       'kind': 'fallback' if claimed else 'passthrough',
                                       'argv': argv,
                                           'cwd': os.getcwd(), 'reason': reason,
                                           'duration_ms': int((time.time() - started) * 1000),
                                           'exit': code})
        except OSError:
            pass                        # the log is diagnostics; never fail a run for it
    return code


def connect(path, timeout=HANDSHAKE_SECONDS):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(path)
    return sock


def handshake(sock, request):
    """Send the request; return (reader, accepted-or-error frame, or None)."""
    sock.sendall(dump(request))
    reader = Reader(sock)
    while True:
        frame = reader.line()
        if frame is None:
            return reader, None
        if frame.get('t') in ('accepted', 'error'):
            return reader, frame
        if frame.get('t') == 'notice':
            # Said before anything ran: a fallback, a re-root, a paused lane.
            notice(frame.get('msg') or '')
            continue
        if frame.get('t') == 'queued':
            sock.settimeout(None)       # admitted to a queue: wait as long as it takes
            continue


def marker_policy(command):
    """What the enrolment marker says about this argv: size, fallback, writeback.

    Only ever consulted when the daemon is unreachable. A marker written before
    this rule existed has no `policy` lines, and the caller treats that as
    unknown -- which is decided as `large`, because a client that cannot say how
    big a job is has not earned the right to start it here.
    """
    try:
        _common, marker = enrolment.marker_for(os.getcwd())
    except OSError:
        marker = None
    if not marker:
        return None
    return enrolment.policy_for(command, marker)


def subdirectory_offender(command):
    """The daemon's subdirectory rule, for when there is no daemon to apply it.

    A claimed command typed below the worktree root with an argument that names
    a path is refused, never run -- with or without the daemon. Returns the
    offending token, or None.
    """
    here = os.getcwd()
    root = enrolment.worktree_root(here)
    if root is None or Path(here).resolve() == Path(root).resolve():
        return None
    try:
        _common, marker = enrolment.marker_for(here)
    except OSError:
        marker = None
    if not marker:
        return None
    from ..config.classify import path_like
    declared = enrolment.policy_for(command, marker)
    rest = enrolment.key_of(command, marker.get('strip') or [])
    rest = rest[len(declared['prefix']) if declared else 1:]
    return path_like(rest, exists=lambda token: Path(here, token).exists())


class Stream:
    """Everything after `accepted`. Never falls back."""

    def __init__(self, path, run_id, reader, sock):
        self.path = path
        self.run = run_id
        self.reader = reader
        self.sock = sock
        self.base = 0                    # log-file offset this connection started at
        self.mark = reader.consumed      # reader bytes that were handshake, not log
        self.cancelled = False

    @property
    def offset(self):
        return self.base + (self.reader.consumed - self.mark)

    def cancel(self, *_):
        if not self.cancelled:
            notice('cancelling run %s on the worker; its instance will be destroyed.' % self.run)
        self.cancelled = True
        try:
            self.sock.sendall(dump({'t': 'cancel', 'run': self.run}))
        except OSError:
            pass

    def detach(self, signum, _frame):
        notice('detached; the run keeps going. Re-attach with: pandora wait %s' % self.run)
        die_by(signum)

    def pump(self):
        """Consume frames until exit. Returns the exit code, or None if cut off."""
        out, err = sys.stdout.buffer, sys.stderr.buffer
        while True:
            try:
                frame = self.reader.line()
            except (OSError, ValueError):
                return None
            if frame is None:
                return None
            kind = frame.get('t')
            if kind == 'log':
                data = base64.b64decode(frame['b64'])
                target = err if frame.get('s') == 'err' else out
                target.write(data)
                target.flush()
            elif kind == 'exit':
                out.flush()
                err.flush()
                if frame.get('signal'):
                    die_by(frame['signal'])    # only returns if the signal is ignored
                return int(frame['code'])

    def reattach(self):
        """Re-open the socket and continue the same run from our byte offset."""
        resume = self.offset
        for _ in range(REATTACH_ATTEMPTS):
            time.sleep(REATTACH_PAUSE)
            try:
                sock = connect(self.path, timeout=2.0)
            except OSError:
                continue
            try:
                sock.sendall(dump({'v': VERSION, 'op': 'attach',
                                   'run': self.run, 'from': resume}))
                reader = Reader(sock)
                frame = reader.line()
                if frame is None or frame.get('t') != 'accepted':
                    sock.close()
                    continue
                sock.settimeout(None)
                self.sock, self.reader = sock, reader
                self.base, self.mark = resume, reader.consumed
                return True
            except OSError:
                sock.close()
        return False


def build_request(command, *, cwd=None):
    """The request, with the caller's environment filtered and the drops announced."""
    forwarded, secrets, platform = envfilter.split(os.environ)
    for line in envfilter.notices(secrets, platform):
        notice(line)
    request = {'v': VERSION, 'op': 'run', 'cwd': cwd or os.getcwd(),
               'argv': ['pnpm', *command], 'tty': sys.stdin.isatty(),
               'env': forwarded}
    # Pandora's own control variables never travel to the run -- `envfilter`
    # drops every PANDORA_* name -- so the two that change what Pandora does
    # are lifted out here and carried as fields of the request instead.
    # `--shards` is not read: the shard flag belongs to the repository's runner
    # and Pandora does not parse the repository's command line.
    raw = os.environ.get('PANDORA_SHARDS', '').strip()
    if raw.isdigit() and int(raw) >= 1:
        request['want_shards'] = int(raw)
    if os.environ.get('PANDORA_KEEP_GOING', '') not in ('', '0'):
        request['keep_going'] = True
    return request


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sock', required=True)
    parser.add_argument('--real', required=True)
    parser.add_argument('--state', default=None)
    parser.add_argument('--detach', action='store_true',
                        help='print the run id once accepted and return; the run keeps going')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    state = args.state or str(Path(args.sock).parent)
    updating = '--update' in command

    def no_daemon(cause, message):
        """The daemon is not there to decide, so decide the same way it would.

        The size class and the declared policy come from the enrolment marker,
        which `pandora enrol` writes from the very configuration the daemon
        would have loaded. What cannot be reproduced is the local *queue*: this
        runs under the slot budget instead, and says so.
        """
        if args.detach:
            # Detaching needs a run id, and only the daemon issues them. A
            # local run in the foreground would be the opposite of what was asked.
            notice('%s; nothing was started. Without the daemon there is no run to '
                   'detach from.' % message)
            return INFRA
        if subdirectory_offender(command) is not None:
            notice('run from the repo root to route')
            return USAGE
        declared = marker_policy(command)
        verdict = fallback_module.decide(
            cause=cause,
            size=(declared or {}).get('size', 'large'),
            writeback=updating or bool((declared or {}).get('writeback')),
            declared=None if not declared or declared['fallback'] == 'auto'
                     else {'action': declared['fallback'], 'on': list(fallback_module.CAUSES)})
        if verdict['action'] == 'refuse':
            notice('%s; %s' % (message, verdict['reason']))
            return INFRA
        notice('%s; running it here under the fallback slot budget, because the local '
               'queue needs the daemon that is missing.' % message)
        return run_local(args.real, command, state=state, reason=cause)

    request = build_request(command)
    try:
        sock = connect(args.sock, timeout=2.0)
    except (OSError, socket.timeout) as error:
        return no_daemon('daemon-unreachable', 'daemon socket %s: %s' % (args.sock, error))
    sock.settimeout(HANDSHAKE_SECONDS)
    try:
        reader, frame = handshake(sock, request)
    except (OSError, socket.timeout, ValueError) as error:
        sock.close()
        return no_daemon('handshake-timeout', 'daemon did not answer within %ds (%s)'
                         % (HANDSHAKE_SECONDS, type(error).__name__))
    if frame is None:
        sock.close()
        return no_daemon('daemon-closed', 'daemon closed the connection before accepting')
    if frame.get('t') == 'error':
        sock.close()
        code = frame.get('code')
        if code == 'passthrough' and args.detach:
            notice((frame.get('msg') or 'not routed') + '; nothing was started. '
                   'Only a routed command can be detached; run it directly.')
            return INFRA
        if code == 'passthrough':
            # Pandora has no opinion about this invocation -- not enrolled, not
            # claimed, or typed in a subdirectory with a path in the argv. It is
            # not a fallback, so it takes no slot; it is what would have happened
            # if the shim were not installed.
            notice(frame.get('msg') or 'not routed')
            return run_local(args.real, command, state=state, claimed=False,
                             reason='passthrough')
        # Everything else is the daemon's own verdict, and the daemon is the one
        # thing that knows this machine's queue, this job's size and this repo's
        # policy. It has already decided whether a local run is allowed; there is
        # nothing left here to decide and nothing to second-guess it with.
        message = (frame.get('msg') or ('daemon refused this run (%s)' % code)).rstrip()
        if code == 'invalid-arguments':
            # The repository's own message, reproduced exactly. Pandora adds
            # nothing to it, including its own name.
            sys.stderr.write(message + '\n')
            sys.stderr.flush()
        else:
            notice(message)
        return int(frame.get('exit') or 1)

    remote = frame.get('remote')
    extra = []
    if frame.get('same_input_as'):
        extra.append('same input as ' + frame['same_input_as'])
    if frame.get('source_reused'):
        extra.append('source cache hit')
    if frame.get('lane') == 'local':
        extra.append('%s MiB reserved' % frame.get('reservation_mib'))
        notice('run %s in the local lane%s'
               % (frame['run'], ' (' + '; '.join(extra) + ')' if extra else ''))
    else:
        notice('run %s on the worker as %s%s'
               % (frame['run'], remote, ' (' + '; '.join(extra) + ')' if extra else ''))
    if args.detach:
        # Closing the socket is a detach, never a cancel: the daemon keeps the
        # run and `pandora wait <id>` re-attaches. The id is the only stdout.
        sock.close()
        sys.stdout.write(frame['run'] + '\n')
        sys.stdout.flush()
        return 0
    sock.settimeout(None)
    stream = Stream(args.sock, frame['run'], reader, sock)
    signal.signal(signal.SIGINT, stream.cancel)
    signal.signal(signal.SIGTERM, stream.detach)
    while True:
        code = stream.pump()
        if code is not None:
            return code
        if not stream.reattach():
            notice('lost the daemon after it accepted run %s. Not running locally: it may '
                   'still be executing. Check with: pandora wait %s' % (stream.run, stream.run))
            return INFRA


if __name__ == '__main__':
    raise SystemExit(main())
