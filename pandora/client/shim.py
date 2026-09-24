"""The routed half of the shim: connect, stream, and get the exit code right.

Two rules, and the second one is new.

**Before connecting** no request has been sent, so policy may permit local
execution. Once submission starts, a lost reply leaves execution uncertain.
Only an explicit daemon passthrough permits local execution; connection loss
returns an infrastructure failure (exit 70) without replaying the command.

Permitted is not the same as wise, and this file used to treat them as the same
thing: every pre-accept error code became `exec pnpm`, which is how a refused
submission turned into 302 browser tests on this Mac. So the second rule is that
**this process never decides to run a claimed command locally while the daemon
is alive**. A daemon that answers has the configuration, the size classes, the
local queue and the host's own pressure; its answer is final, whatever it is. It
falls back by admitting the job into its local lane and streaming it back here,
which arrives as an ordinary `accepted` frame carrying `lane: local`.

The shim also lands here, with `--refresh`, when this worktree's claim cache is
missing or older than the config it was derived from, and so does not know
whether the command is claimed. The daemon rewrites the cache and answers with
the shim's own rule; an unclaimed command then runs exactly as the shim would
have run it (exec, or the passthrough logger for a heavy one), and a claimed
one continues below as if the shim had claimed it.

That leaves exactly one decision here: what to do when the daemon cannot be
reached at all. There is no local lane to admit into, so the client applies the
same policy from the claim cache -- a `refuse` verdict exits 70 with one
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

from ..exits import INFRA, STALE, USAGE
from . import enrollment, envfilter, fallback as fallback_module, placement
from .protocol import Reader, VERSION, dump

# Silence tolerated before `accepted`, while freeze, ship and submit run. A
# dead daemon is EOF, not silence, so this guards only a hung one; the daemon
# beats every 5 s, but on 2026-09-24 a Mac at load 25 with 5.6 GiB swapped
# stalled that thread past 20 s and a live 364 MiB upload was withdrawn.
HANDSHAKE_SECONDS = 120.0
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


def run_local(real, argv, *, state=None, claimed=True, reason='', where=None):
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
                       'this Mac. Retry in a minute. As a last resort, PANDORA_OFF=1 runs '
                       'it with no Pandora at all, outside every limit.'
                       % (count, argv[0] if argv else 'pnpm'))
                return STALE
    try:
        code = subprocess.call([real, *argv], env=environment)
    finally:
        if slot is not None:
            slot.release()
    if state is not None:
        entry = {'ts': started, 'kind': 'fallback' if claimed else 'passthrough',
                 'argv': argv, 'cwd': os.getcwd(), 'reason': reason,
                 'duration_ms': int((time.time() - started) * 1000), 'exit': code}
        if where:
            entry['override'] = where   # asked for, and ignored: see `pandora stats`
        try:
            fallback_module.record(state, entry)
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
    """What this worktree's claim cache, or the v0.2 marker, says about this argv.

    Size, fallback, writeback. Only ever consulted when the daemon is
    unreachable. A file written before this rule existed has no `policy` lines,
    and the caller treats that as unknown -- which is decided as `large`,
    because a client that cannot say how big a job is has not earned the right
    to start it here.
    """
    try:
        _common, marker = enrollment.marker_for(os.getcwd())
    except OSError:
        marker = None
    if not marker:
        return None
    return enrollment.policy_for(command, marker)


def subdirectory_offender(command):
    """The daemon's subdirectory rule, for when there is no daemon to apply it.

    A claimed command typed below the worktree root with an argument that names
    a path is refused, never run -- with or without the daemon. Returns the
    offending token, or None.
    """
    here = os.getcwd()
    root = enrollment.worktree_root(here)
    if root is None or Path(here).resolve() == Path(root).resolve():
        return None
    try:
        _common, marker = enrollment.marker_for(here)
    except OSError:
        marker = None
    if not marker:
        return None
    from ..config.classify import path_like
    declared = enrollment.policy_for(command, marker)
    rest = enrollment.key_of(command, marker.get('strip') or [])
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
        self.canceled = False

    @property
    def offset(self):
        return self.base + (self.reader.consumed - self.mark)

    def cancel(self, *_):
        if not self.canceled:
            notice('canceling run %s on the worker; its instance will be destroyed.' % self.run)
        self.canceled = True
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
        """Re-open the socket and continue the same run from our byte offset.

        False when the daemon cannot bring the run to an exit: it has no such
        run, or says nothing here is following it. Retrying either would wait
        forever on a row nothing will finish.
        """
        resume = self.offset
        self.unfollowed = None
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
                if frame is not None and (frame.get('t') == 'error'
                                          or frame.get('owned') is False):
                    sock.close()
                    self.unfollowed = frame.get('msg') or 'the daemon is not following it'
                    return False
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


def ask_claims(sock_path, command):
    """The daemon's verdict on a command the shim could not decide: {claimed, heavy}.

    Raises OSError when there is no daemon to ask, and returns None when the
    daemon predates claim caches, which leaves the decision to its `run`.
    """
    sock = connect(sock_path, timeout=2.0)
    try:
        sock.settimeout(HANDSHAKE_SECONDS)
        sock.sendall(dump({'v': VERSION, 'op': 'claims', 'cwd': os.getcwd(),
                           'argv': list(command)}))
        frame = Reader(sock).line()
    finally:
        sock.close()
    if frame is None:
        raise OSError('the daemon closed the connection without answering')
    if frame.get('t') != 'claims':
        return None
    return frame


def unclaimed(real, command, *, state, repo, heavy):
    """What the POSIX shim does with an unclaimed command, from Python.

    A heavy one, or one with `PANDORA_WHERE` set, goes through the passthrough
    logger so `pandora stats` sees it; anything else is exec'd. Neither says a
    word on stderr: the shim would not have.
    """
    os.environ['PANDORA_ROUTE_DEPTH'] = '1'
    reason = None
    if heavy:
        reason = 'unclaimed'
    elif os.environ.get(placement.ENV):
        reason = 'override-ignored'
    if reason is None:
        os.execv(real, [real, *command])
    from . import passthrough
    return passthrough.main(['--real', real, '--repo', repo or '', '--state', state,
                             '--reason', reason, '--', *command])


def refresh(args, command, state):
    """The slow path: None when the command is claimed, else the exit code it ran to."""
    try:
        answer = ask_claims(args.sock, command)
    except (OSError, ValueError):
        # No daemon: decide by the cache as it stands, stale or not. A claimed
        # command then meets the no-daemon passthrough below, with its notice.
        try:
            _common, marker = enrollment.marker_for(os.getcwd())
        except OSError:
            marker = None
        if (marker and enrollment.claimed(command, marker)
                and not enrollment.claims_nothing_here(os.getcwd(), marker)):
            return None
        return unclaimed(args.real, command, state=state, repo=args.repo,
                         heavy=bool(marker) and enrollment.heavy(command, marker))
    if answer is None or answer.get('claimed'):
        return None
    return unclaimed(args.real, command, state=state, repo=args.repo,
                     heavy=bool(answer.get('heavy')))


# Who submitted a run, in order of preference. `PANDORA_SESSION` is Pandora's
# own and an orchestrator may set it; the other two are what Claude Code and the
# Codex companion export into the shells they start.
SESSION_VARIABLES = ('PANDORA_SESSION', 'CLAUDE_SESSION_ID', 'CODEX_COMPANION_SESSION_ID')
# A parent past which a process chain stops being one person's or one agent's
# session: a terminal's `login`, a remote login, a multiplexer's server.
SESSION_BOUNDARIES = ('login', 'sshd', 'tmux', 'screen', 'launchd', 'init', 'systemd')


def top_interactive(processes, start):
    """The outermost ancestor of `start` still inside one interactive session.

    `processes` maps pid -> (ppid, tty, name). Walks up while the parent has a
    terminal and is not a session boundary, so every command typed under one
    terminal tab, or by one agent in it, names the same process.
    """
    current = start
    for _ in range(64):
        if current not in processes:
            return None
        parent = processes[current][0]
        row = processes.get(parent)
        if (parent <= 1 or row is None or row[1] in ('', '?', '??')
                or row[2] in SESSION_BOUNDARIES):
            break
        current = parent
    name = processes[current][2]
    return '%s:%d' % (name, current)


def process_table(run=subprocess.run):
    """pid -> (ppid, tty, name), from one `ps`; empty when it cannot be read."""
    try:
        out = run(['ps', '-A', '-o', 'pid=,ppid=,tty=,comm='], capture_output=True,
                  text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2],
                                    os.path.basename(parts[3].strip()).lstrip('-'))
    return table


def submitter(environ=None, *, table=process_table):
    """{'via', 'id'}: which session submitted this run, or None.

    A session variable when one is set. Otherwise the top interactive process
    above this one, which costs one `ps` on a claimed command only.
    """
    environ = os.environ if environ is None else environ
    for name in SESSION_VARIABLES:
        value = (environ.get(name) or '').strip()
        if value:
            return {'via': name, 'id': value[:200]}
    found = top_interactive(table(), os.getppid())
    return {'via': 'process', 'id': found} if found else None


def build_request(command, *, cwd=None, where=None):
    """The request, with the caller's environment filtered (`envfilter`, step 1).

    Values travel only for names that passed the filter. The daemon also gets
    two lists of *names*: what was dropped, so it can say which of the
    repository's declared passthrough names did not travel, and every name set
    here, so `reject_if_set` can refuse on `NODE_OPTIONS` or `NPM_TOKEN` -- the
    very names the filter removes.
    """
    forwarded, secrets, platform = envfilter.split(os.environ)
    request = {'v': VERSION, 'op': 'run', 'cwd': cwd or os.getcwd(),
               'argv': ['pnpm', *command], 'tty': sys.stdin.isatty(),
               'env': forwarded,
               'env_present': sorted(name for name, value in os.environ.items() if value),
               'env_dropped': {'secret': secrets, 'platform': platform}}
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
    if where:
        request['where'] = where
    who = submitter()
    if who:
        request['submitter'] = who
    return request


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sock', required=True)
    parser.add_argument('--real', required=True)
    parser.add_argument('--state', default=None)
    parser.add_argument('--detach', action='store_true',
                        help='print the run id once accepted and return; the run keeps going')
    parser.add_argument('--where', default=None,
                        help='`pandora run --local/--remote`; outranks PANDORA_WHERE')
    parser.add_argument('--refresh', action='store_true',
                        help='the claim cache is missing or stale: ask the daemon first')
    parser.add_argument('--repo', default='', help='the git common dir, for the passthrough log')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    state = args.state or str(Path(args.sock).parent)
    if args.refresh:
        code = refresh(args, command, state)
        if code is not None:
            return code
    updating = '--update' in command
    # The flag wins over the variable, and a variable that names neither side is
    # refused before anything is asked of anyone: a typo in a placement is not a
    # request to be read generously.
    try:
        where = (placement.parse(args.where, source='--where') if args.where
                 else placement.parse(os.environ.get(placement.ENV)))
    except ValueError as error:
        notice(str(error))
        return USAGE

    def no_daemon(cause, message):
        """The daemon is not there: run the command as if Pandora were absent.

        Only two requests cannot be honored without a daemon and are refused:
        `--detach` (there is no run id to print) and an explicit remote
        placement (only the daemon reaches the worker). Everything else is a
        passthrough with one line on stderr.
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
        if where == 'remote':
            # Only the daemon can reach the worker, and an explicit `--remote`
            # is never answered with a local run.
            notice('%s; --remote was asked for and only the daemon can send it to the '
                   'worker, so nothing was run. Start the daemon, or drop the override.'
                   % message)
            return INFRA
        # No daemon is the same situation as no Pandora: the command runs here
        # as it would on a machine that never installed the shim. It is a
        # passthrough, not a fallback: no slot, no size class, no policy. The
        # marker's policy lines exist for the daemon's own verdicts; with the
        # daemon gone there is no queue to protect and nothing to decide with,
        # and an engineer whose daemon died must not lose `pnpm journey` (the
        # owner's rule for machines without Pandora: always run directly).
        notice('%s; running it here as if Pandora were not installed (exit codes '
               'are the command\'s own; start the daemon with `pandora daemon '
               '--install` to route again)' % message)
        return run_local(args.real, command, state=state, claimed=False,
                         reason=cause, where=where)

    def pass_through(message, writeback=False):
        """Not claimed here: run it as if the shim were not installed."""
        if args.detach:
            notice(message + '; nothing was started. '
                   'Only a routed command can be detached; run it directly.')
            return INFRA
        if where == 'remote' or updating or writeback:
            notice(message + '; the requested remote or '
                   'write-back run cannot pass through to local execution.')
            return INFRA
        # Pandora has no opinion about this invocation -- not enrolled, not
        # claimed, or typed in a subdirectory with a path in the argv. It is
        # not a fallback, so it takes no slot; it is what would have happened
        # if the shim were not installed.
        notice(message)
        return run_local(args.real, command, state=state, claimed=False,
                         reason='passthrough', where=where)

    # `subdirectory = "passthrough"` below the worktree root: nothing is claimed,
    # so no socket. The POSIX shim decides this itself; `pandora run` lands here.
    here = os.getcwd()
    try:
        _common, marker = enrollment.marker_for(here)
    except OSError:
        marker = None
    if enrollment.claims_nothing_here(here, marker):
        return pass_through('claimed only at the worktree root; not routed')

    request = build_request(command, where=where)
    try:
        sock = connect(args.sock, timeout=2.0)
    except (OSError, socket.timeout) as error:
        return no_daemon('daemon-unreachable', 'daemon socket %s: %s' % (args.sock, error))
    sock.settimeout(HANDSHAKE_SECONDS)
    try:
        reader, frame = handshake(sock, request)
    except (OSError, socket.timeout, ValueError) as error:
        sock.close()
        notice('connection failed after submission (%s); execution is uncertain. '
               'Check pandora ps before retrying. Nothing was replayed locally.'
               % type(error).__name__)
        return INFRA
    if frame is None:
        sock.close()
        notice('daemon closed the connection after submission; execution is uncertain. '
               'Check pandora ps before retrying. Nothing was replayed locally.')
        return INFRA
    if frame.get('t') == 'error':
        sock.close()
        code = frame.get('code')
        if code == 'passthrough':
            return pass_through(frame.get('msg') or 'not routed',
                                writeback=frame.get('writeback'))
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
    # A tree digest, not the command: `journey S0-01 --update` is "the same
    # tree as" a plain `journey S0-01` from the same worktree. A daemon from
    # before the rename says `same_input_as`.
    same = frame.get('same_tree_as') or frame.get('same_input_as')
    if same:
        extra.append('same tree as ' + same)
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
            if getattr(stream, 'unfollowed', None):
                notice('run %s: %s; nothing will finish it. Not running locally. Check '
                       'with: pandora ps' % (stream.run, stream.unfollowed))
                return INFRA
            notice('lost the daemon after it accepted run %s. Not running locally: it may '
                   'still be executing. Check with: pandora wait %s' % (stream.run, stream.run))
            return INFRA


if __name__ == '__main__':
    raise SystemExit(main())
