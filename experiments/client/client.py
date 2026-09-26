#!/usr/bin/env python3
"""The routed half of the shim: connect, stream, and get the exit code right.

The whole file is organised around one rule.  Before the daemon says
``accepted`` the command provably has not run, so any problem at all means
"print one line and run it locally".  After ``accepted`` the command may be
running on a worker, so falling back could run it twice; from that point a
failure is an infrastructure failure (exit 70) and never a local run.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import protocol
from protocol import dump
import fallback as fallback_module

HANDSHAKE_SECONDS = 0.3
REATTACH_ATTEMPTS = 10
REATTACH_PAUSE = 0.25

# Environment the worker is allowed to see.  An allowlist, not a filter: the
# daemon must never receive an agent's whole environment.
ENV_KEYS = ('CI', 'TZ', 'NODE_ENV', 'PANDORA_TREATMENT', 'PANDORA_SESSION',
            'JOURNEY_FILTER', 'JOURNEY_SHARD', 'JOURNEY_CONCURRENCY',
            'JOURNEY_REPLAY', 'JOURNEY_TEMPLATE', 'APP_WORLD')


def notice(text):
    sys.stderr.write('[pandora] ' + text + '\n')
    sys.stderr.flush()


def die_by(number):
    """Die the way the worker died, so the caller's `$?` and traps are unchanged.

    ``signal.signal`` refuses SIGKILL and SIGSTOP; re-raising still works, since
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
                       % (count, argv[0]))
                return protocol.FALLBACK_REFUSED
    try:
        code = subprocess.call([real, *argv], env=environment)
    finally:
        if slot is not None:
            slot.release()
    if state is not None:
        try:
            fallback_module.record(state, {'ts': started, 'kind': 'fallback', 'argv': argv,
                                           'cwd': os.getcwd(), 'reason': reason,
                                           'duration_ms': int((time.time() - started) * 1000),
                                           'exit': code})
        except OSError:
            pass                        # the log is diagnostics; never fail a run for it
    return code


def connect(path):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(HANDSHAKE_SECONDS)
    sock.connect(path)
    return sock


def handshake(sock, request):
    """Send the request; return (reader, accepted-frame) or (reader, None)."""
    sock.sendall(dump(request))
    reader = protocol.Reader(sock)
    while True:
        frame = reader.line()
        if frame is None:
            return reader, None
        if frame.get('t') == 'accepted':
            return reader, frame
        if frame.get('t') == 'error':
            return reader, frame
        if frame.get('t') == 'queued':
            sock.settimeout(None)       # admitted to a queue: wait as long as it takes
            continue


class Stream:
    """Everything after ``accepted``.  Never falls back."""

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
        self.cancelled = True
        try:
            self.sock.sendall(dump({'t': 'cancel', 'run': self.run}))
        except OSError:
            pass

    def detach(self, signum, _frame):
        notice('detached; the run keeps going. Re-attach with: pandora wait %s' % self.run)
        die_by(signum)

    def pump(self):
        """Consume frames until exit.  Returns the exit code, or None if cut off."""
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
                (err if frame.get('s') == 'err' else out).write(data)
                (err if frame.get('s') == 'err' else out).flush()
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
                sock = connect(self.path)
            except OSError:
                continue
            try:
                sock.sendall(dump({'v': protocol.VERSION, 'op': 'attach',
                                   'run': self.run, 'from': resume}))
                reader = protocol.Reader(sock)
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sock', required=True)
    parser.add_argument('--real', required=True)
    parser.add_argument('--state', default=None)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    state = args.state or str(Path(args.sock).parent)
    updating = '--update' in command

    def give_up(reason, message):
        """Pre-acceptance exit.  Local, unless the run would write back."""
        if updating:
            notice('%s; --update runs are never run locally, because a local run would '
                   'write files the worker should have written.' % message)
            return protocol.INFRA_FAILURE
        notice('%s; running locally instead.' % message)
        return run_local(args.real, command, state=state, reason=reason)

    request = {'v': protocol.VERSION, 'op': 'run', 'cwd': os.getcwd(),
               'argv': ['pnpm', *command], 'tty': sys.stdin.isatty(),
               'token': os.environ.get('PANDORA_TOKEN'),
               'env': {key: os.environ[key] for key in ENV_KEYS if key in os.environ}}
    try:
        sock = connect(args.sock)
    except (OSError, socket.timeout) as error:
        return give_up('daemon-unreachable', 'daemon socket %s: %s' % (args.sock, error))
    try:
        reader, frame = handshake(sock, request)
    except (OSError, socket.timeout, ValueError) as error:
        sock.close()
        return give_up('handshake-timeout',
                       'daemon did not answer within %dms (%s)'
                       % (HANDSHAKE_SECONDS * 1000, type(error).__name__))
    if frame is None:
        sock.close()
        return give_up('daemon-closed', 'daemon closed the connection before accepting')
    if frame.get('t') == 'error':
        sock.close()
        return give_up(frame.get('code', 'refused'),
                       'daemon refused this run (%s: %s)'
                       % (frame.get('code'), frame.get('msg')))

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
            return protocol.INFRA_FAILURE


if __name__ == '__main__':
    raise SystemExit(main())
