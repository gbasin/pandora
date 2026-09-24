"""Per-user client daemon: owns the config, the enrollments, the runs and the socket.

Three invariants the rest of the design leans on, unchanged from the POC:

* One daemon per state directory, enforced by an exclusive lock on `daemon.lock`
  -- not by the socket, which is a file that survives a crash.
* Every run's output is appended to a *file* as framed NDJSON, and clients are
  served by copying byte ranges of that file. Memory stays flat for a large run
  and re-attach is a byte offset, not a replay buffer.
* A client that disappears detaches; only an explicit `cancel` stops a run.

One that is new, and is the whole point of the slice: `accepted` is sent only
after the worker's engine has admitted the run and named it. Everything before
that -- classification, the repository's own pre-flight validator, freezing the
worktree, shipping it, submitting -- is provably non-executing, so the client
may still go local. Everything after it may not.
"""
import argparse
import base64
import faulthandler
import fcntl
import json
import os
import signal
import socket
import struct
import sys
import threading
import time
import uuid
from pathlib import Path

from ..config import classify as classifier
from ..config import loader
from ..errors import (ConfigError, EngineError, ExecutionUncertain, NotClaimed, PandoraError,
                      Refused, SnapshotError, TransferError, ValidationRejected,
                      WorkerUnreachable, UnknownSchema)
from ..engine import bundle
from ..engine import retry as retries
from ..exits import CANCELED, INFRA, STALE
from . import attribution, runindex
from . import drain as draining
from . import enrollment, envfilter, fallback as policy, hints, placement, progress, settings
from . import stats as statistics
from . import writeback as writebacks
from .health import Monitor
from . import local as local_module
from .local import Budget, Busy, LocalExecutor
from .pressure import Gate, Paused
from .protocol import Reader, VERSION, dump, log_frame
from .status import RunStatus
from .worker import Worker


# The directory this daemon imported `pandora` from, said in `daemon.json` and in
# `pong` so `pandora doctor` can tell a daemon started from one checkout from a
# launcher that resolves to another.
PACKAGE_HOME = str(Path(__file__).resolve().parents[2])
# What the caller is told when a submission may have started on the worker and
# nobody can say. The next action, not a diagnosis: a blind retry could be the
# second copy of a command that is already running.
UNCERTAIN = 'execution is uncertain; check `pandora ps` before retrying'
# This daemon process, as written into every row it saves. A live row whose
# owner is another process is one no thread here is driving.
OWNER = uuid.uuid4().hex


def now():
    return time.time()


def log(text):
    """One line of the daemon's own log, with a UTC time. Its only stderr writer.

    launchd sends stderr to `<state>/logs/daemon.log`; without a time on each
    line, the 2026-09-24 transfers could not be put in order against the
    worker's own log.
    """
    sys.stderr.write('%s %s\n' % (time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), text))
    sys.stderr.flush()


def client_alive(conn):
    """False when the peer has closed its end; a peek, never a read."""
    try:
        return conn.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) != b''
    except (BlockingIOError, InterruptedError):
        return True
    except OSError:
        return False


class Heartbeat:
    """`working` frames every few seconds while the pre-accept work runs.

    The shim's handshake timeout is a silence timeout: every frame restarts it.
    Without these, a freeze that took 21 s on a loaded Mac -- measured, on a
    fresh worktree whose files were out of the page cache -- was indistinguishable
    from a dead daemon, the shim fell back to a local run, and the daemon went on
    to submit the same command to the worker. A send that fails is the other half:
    it is how the daemon learns the caller has gone before it submits anything.
    """

    EVERY = 5.0

    def __init__(self, conn):
        self.conn, self.every = conn, self.EVERY
        self.stopped, self.gone = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self.beat, daemon=True)
        # The beat and `say` share the socket; one frame at a time.
        self.sending = threading.Lock()

    def beat(self):
        while not self.stopped.wait(self.every):
            try:
                with self.sending:
                    self.conn.sendall(dump({'v': VERSION, 't': 'working'}))
            except OSError:
                self.gone.set()
                return

    def say(self, text):
        """One pre-accept progress line, sent between beats rather than across one."""
        if self.stopped.is_set():
            return
        try:
            with self.sending:
                self.conn.sendall(dump({'v': VERSION, 't': 'notice', 'msg': text}))
        except OSError:
            self.gone.set()

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        """Stop beating; True when the caller is known to have left."""
        self.stopped.set()
        self.thread.join()
        return self.gone.is_set()


class Run:
    """One routed attempt. Its log file is the single source of truth."""

    def __init__(self, state, run_id, request, *, on_save=None):
        self.id = run_id
        self.on_save = on_save
        self.request = request
        self.dir = Path(state) / 'runs' / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log = self.dir / 'log'
        # Created empty and immediately: a client that attaches before the first
        # frame exists must block on an empty file, not fail to open one.
        self.log.touch()
        self.meta = self.dir / 'meta.json'
        self.lock = threading.Lock()
        self.wake = threading.Condition(self.lock)
        self.canceled = threading.Event()
        self.done = threading.Event()
        self.exit_code = None
        self.state = 'queued'
        self.remote = None
        self.lane = 'remote'
        # Why this run is in the lane it is in. Empty for the ordinary case;
        # `fallback:<cause>` when a remote submission did not proceed, because a
        # receipt that says `lane: local` and nothing else cannot be told apart
        # from a job that was always local.
        self.reason = request.get('reason') or ''
        self.result = None
        self.started = now()
        # When the client was told `accepted`. The gap between this and
        # `started` is the queue wait -- everything the caller spent not knowing
        # whether its command would run anywhere -- and it is the one number
        # `pandora stats` cannot derive from anything else afterward.
        self.accepted = None
        self.hint = None
        # The local run a remote row handed its request to, when a fallback
        # admitted it. Without it a `fell_back` row is a dead end in `ps`.
        self.fell_back_to = None
        self.shipped = frozenset()      # the snapshot's paths, for the gitignored hint
        # Every earlier attempt at this run, oldest first. Empty unless an
        # infrastructure failure was retried; the caller-visible id stays one.
        self.attempts = list(request.get('attempts') or [])
        # The engine's row state as last seen by `follow`, for `pandora wait`.
        self.phase = None
        # freeze / ship / submit, measured before `accepted`.
        self.pre_accept = request.get('pre_accept') or {}
        # {cause, detail} when the request was refused before reaching the
        # worker, so `pandora result` can say why a row with no result ended.
        self.refusal = request.get('refusal')
        # A local run's process group and when it started, so a daemon that
        # restarts can stop a tree whose supervisor was the process that died.
        self.pgid = request.get('pgid')
        self.pgid_started = request.get('pgid_started')
        # Held across the supervisor's check-then-spawn and across a stop's
        # read-then-kill, so a stop either prevents the spawn or sees the
        # group it produced -- never neither.
        self.spawn_lock = threading.Lock()
        # Around `finish`: two threads closing one row at the same instant --
        # a child exiting as a stop lands -- must produce one exit frame.
        self.close_lock = threading.RLock()
        # Whether any of the command's own output has been streamed. Kept in a
        # file, because a daemon that restarts mid-run must not forget that the
        # caller has already seen half an answer.
        self.output_mark = self.dir / 'command-output'
        self.seen = self.output_mark.exists()
        self.carry = b''
        # A local run still queued when a drain began: it never starts here,
        # and its caller is told to submit it again to the next daemon.
        self.drained = False
        # A local run's CPU time so far, summed over its process tree, and
        # when (wall clock) it last progressed, from the supervisor's
        # once-a-second sample. None until measured, and None when `ps`
        # cannot measure it: such a run is never called idle.
        self.cpu_seconds = None
        self.active_at = None

    def save(self):
        with self.close_lock:
            if self.done.is_set():
                # Closed. A thread still mid-flight when a stop closed the row
                # must not write `running` over the daemon's last word.
                return
            self._save()

    def _save(self):
        payload = {'id': self.id, 'state': self.state, 'exit_code': self.exit_code,
                   'argv': self.request.get('argv'), 'cwd': self.request.get('cwd'),
                   'worktree': self.worktree(),
                   'remote': self.remote, 'repo': self.request.get('repo'),
                   'lane': self.lane, 'reason': self.reason, 'job': self.request.get('job'),
                   'started': self.started, 'accepted': self.accepted,
                   'queue_ms': (None if self.accepted is None
                                else int((self.accepted - self.started) * 1000)),
                   'hint': self.hint, 'attempts': self.attempts, 'phase': self.phase,
                   'pre_accept': self.pre_accept, 'updated': now(),
                   'placement': self.request.get('placement'), 'owner': OWNER,
                   # A write-back run, so a daemon that finds this row before
                   # `accepted` knows its frozen context was never saved.
                   'writeback': bool(self.request.get('writeback'))}
        if self.fell_back_to:
            payload['fell_back_to'] = self.fell_back_to
        if self.refusal:
            payload['refusal'] = self.refusal
        if self.pgid:
            payload['pgid'], payload['pgid_started'] = self.pgid, self.pgid_started
        submitter = submitted_by(self.request)
        if submitter:
            payload['submitter'] = submitter
        if self.request.get('client'):
            payload['client'] = self.request['client']
        temp = self.meta.with_suffix('.tmp')
        temp.write_text(json.dumps(payload) + '\n')
        temp.replace(self.meta)
        if self.on_save is not None:
            self.on_save(payload)

    def worktree(self):
        """Where the run's paths are rooted: the worktree, not where it was typed.

        A re-rooted run was typed in a subdirectory, and its declared outputs
        are worktree-relative. Rooting them at `cwd` would put
        `packages/x/.journeys` under `apps/agent/packages/x/.journeys`.
        """
        return self.request.get('worktree') or self.request.get('cwd')

    def consumed(self):
        """How many bytes of the *remote* log have been copied into this one.

        Kept in its own file rather than in meta.json, because it is written
        once per streamed chunk and meta.json is written once per state change.
        """
        try:
            return int((self.dir / 'remote-offset').read_text().strip() or 0)
        except (OSError, ValueError):
            return 0

    def stream_local(self, which, chunk):
        """One chunk from a local child. No offset: there is no remote to resume.

        Output is progress, dated here rather than at the next once-a-second
        sample, so a drain's idle check never misses it.
        """
        if self.active_at is not None:
            self.active_at = now()
        self.append(log_frame(which, chunk))

    def stream_in(self, chunk):
        """One chunk of the remote log: framed for clients, then acknowledged.

        The remote log is one byte stream holding both the command's output and
        the engine's own `pandora:` lines. They are split here, a whole line at
        a time, so Pandora's lines reach the caller's stderr and the command's
        reach stdout -- the contract `--help` states, which the remote lane did
        not keep before. A trailing fragment that might still become a
        `pandora:` line is held until its newline; anything else is sent at once.

        The offset file counts bytes *framed*, so a held fragment is not
        acknowledged: a crash replays it rather than losing it.
        """
        data = self.carry + chunk
        cut = data.rfind(b'\n') + 1
        whole, tail = data[:cut], data[cut:]
        if tail and not retries.could_be_pandora(tail):
            whole, tail = data, b''
        self.carry = tail
        self.frame_remote(whole)

    def flush_remote(self):
        """The run has ended: whatever fragment was held is a line of its own."""
        tail, self.carry = self.carry, b''
        self.frame_remote(tail)

    def forget_held(self):
        """A reconnect resumes from the acknowledged offset, which re-sends it."""
        self.carry = b''

    def frame_remote(self, data):
        if not data:
            return
        pieces = []
        for line in data.splitlines(keepends=True):
            stream = 'err' if retries.PANDORA_LINE.match(line.decode('utf-8', 'replace')) \
                else 'out'
            if stream == 'out' and line.strip():
                self.saw_output()
            if pieces and pieces[-1][0] == stream:
                pieces[-1][1].append(line)
            else:
                pieces.append((stream, [line]))
        for stream, lines in pieces:
            self.append(log_frame(stream, b''.join(lines)))
        try:
            (self.dir / 'remote-offset').write_text(str(self.consumed() + len(data)))
        except OSError:
            pass

    def saw_output(self):
        if self.seen:
            return
        self.seen = True
        try:
            self.output_mark.touch()
        except OSError:
            pass

    def output_seen(self):
        return self.seen

    def restart_remote(self, remote):
        """Follow a new attempt from its first byte, keeping this run's log."""
        self.remote = remote
        self.carry = b''
        try:
            (self.dir / 'remote-offset').write_text('0')
        except OSError:
            pass

    def append(self, frame):
        with self.lock:
            with self.log.open('ab') as handle:
                handle.write(frame)
            self.wake.notify_all()

    def activity(self, cpu_seconds, active_at):
        """The supervisor's latest reading. Memory only: `ps` asks the Run, not the file."""
        self.cpu_seconds, self.active_at = cpu_seconds, active_at

    def idle_seconds(self, at=None):
        """Seconds since the run last progressed, or None when nobody measured it."""
        if self.active_at is None:
            return None
        return max(0.0, (now() if at is None else at) - self.active_at)

    def activity_fields(self):
        """What `ps --json` and a drain's blockers say about a local run's progress."""
        if self.lane != 'local' or self.state != 'running':
            return {}
        idle = self.idle_seconds()
        return {'cpu_seconds': self.cpu_seconds, 'last_active': self.active_at,
                'idle_seconds': None if idle is None else round(idle, 1)}

    def spawned(self, pid):
        """The local supervisor started the child, as the leader of its own group."""
        self.pgid, self.pgid_started = pid, now()
        self.save()

    def note(self, text):
        self.append(log_frame('err', ('pandora: ' + text + '\n').encode()))

    def said(self, text):
        """A line the caller already had as a pre-accept notice, kept for `logs`.

        Not a `log` frame: the stream after `accepted` starts at offset 0, and a
        second copy of the line would reach the caller then.
        """
        self.append(dump({'t': 'said', 's': 'err', 'b64': base64.b64encode(
            ('pandora: ' + text + '\n').encode()).decode()}))

    def suggest(self, text):
        """The last line the caller sees, when there is one worth saying.

        Said as a note rather than as a frame of its own so that it lands in the
        run log in order, which means `pandora logs` and a re-attach both show
        it exactly where a live caller saw it.
        """
        if not text or text == self.hint:
            return
        self.hint = text
        self.note('hint: ' + text)

    def finish(self, code, *, state='done', result=None):
        with self.close_lock:
            if self.done.is_set():
                # Already closed -- by a stop, while the executor that would
                # have finished it was still on its way. The first word stands.
                return
            self._finish(code, state, result)

    def _finish(self, code, state, result):
        self.exit_code = code
        self.state = state
        self.result = result
        if isinstance(result, dict) and result.get('hint') is None and self.hint:
            result['hint'] = self.hint
        if isinstance(result, dict) and self.request.get('placement'):
            result.setdefault('placement', self.request['placement'])
        if result is not None:
            (self.dir / 'result.json').write_text(json.dumps(result, indent=1, sort_keys=True) + '\n')
        self.save()
        self.append(dump({'t': 'exit', 'code': code, 'run': self.id}))
        self.done.set()
        with self.lock:
            self.wake.notify_all()

    def size(self):
        try:
            return self.log.stat().st_size
        except OSError:
            return 0


def shown(value):
    """A TOML value as a reader would write it, short."""
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= 80 else text[:77] + '...'


def not_understood(error, home):
    """The refusal for a pandora.toml this code cannot read: the key, the value, the fix.

    No version numbers: the file is the contract, and the fix is the same
    whichever side is older.
    """
    what = error.key or 'a key'
    if error.value is not None:
        what = '%s = %s' % (what, shown(error.value))
    from . import install
    return ('%s sets %s, which this daemon does not understand (%s). If the file is right, '
            'the daemon runs older code than the file needs: %s. If it is a mistake, fix '
            'the file. Nothing ran.'
            % (error.path or 'pandora.toml', what, str(error).split(': ', 1)[-1],
               install.update_fix(home)))


def submitted_by(request):
    """The request's `submitter`, as stored: {'via', 'id'} of short strings, or None.

    Whatever the client sent, bounded: it is a label for `pandora ps --json`,
    never an identity anything is decided by.
    """
    given = request.get('submitter')
    if not isinstance(given, dict):
        return None
    via, ident = given.get('via'), given.get('id')
    if not isinstance(via, str) or not isinstance(ident, str) or not ident:
        return None
    return {'via': via[:40], 'id': ident[:200]}


def peer_uid(sock):
    """The connecting process's uid, or None when the platform will not say.

    macOS has no SO_PEERCRED; it has LOCAL_PEERCRED at SOL_LOCAL returning a
    `struct xucred`. Linux has SO_PEERCRED returning a `struct ucred`.
    """
    try:
        if sys.platform == 'darwin':
            raw = sock.getsockopt(0, 0x001, 76)           # SOL_LOCAL, LOCAL_PEERCRED
            version, uid = struct.unpack('=II', raw[:8])
            return uid if version == 0 else None
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _pid, uid, _gid = struct.unpack('=III', raw)
        return uid
    except (OSError, struct.error, AttributeError):
        return None


class Daemon:
    def __init__(self, state=None, config_path=None, stopping=None):
        self.config_path = config_path
        self.config = settings.load(config_path)
        self.state = Path(state or self.config['client']['state']).expanduser()
        self.state.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state, 0o700)
        self.socket_path = self.state / 'client.sock'
        self.runs = {}
        self.status = RunStatus()
        # Run ids by date, and every finished row once read: `stats` reads only
        # its window, and pruning keeps history bounded (`runindex`).
        self.index = runindex.RunIndex(self.state / 'runs')
        self.runs_lock = threading.Lock()
        self.stopping = stopping if stopping is not None else threading.Event()
        # {'since', 'pid', 'daemon'} while a restart drains this daemon, else
        # None. Read and set under `admitting`, with every row a request opens,
        # so a drain's count of what is in flight misses nothing.
        self.draining = None
        self.admitting = threading.Lock()
        # When the last `drain` arrived (monotonic). A drain is a lease: a
        # restarter that dies (SIGKILL, a tool timeout) stops renewing it, and
        # the daemon admits runs again rather than answer `draining` for nobody.
        self.drain_renewed = None
        self.handlers = set()             # live connection threads, drained at stop
        self.server = None
        self.lock_handle = None
        self.repo_configs = {}
        self.repo_stamps = {}
        self.adopting = threading.Lock()   # one takeover per orphaned row
        self.workers = {}
        self.workers_lock = threading.Lock()
        # This daemon's own rows before `accepted`, so a cancel or an attach
        # reaches the Run its connection thread drives rather than a stand-in.
        self.pending = {}
        self.code, self.code_modules = None, []
        self.worker_factory = Worker
        # One local queue per daemon, built once: its learned peaks live in a
        # SQLite file beside the runs, so a restart does not forget what a job
        # costs and re-reserve every class ceiling. The pause gate's counters
        # live beside them, for the same reason.
        self.gate = Gate(self.config['local'].get('pause') or {},
                         store=self.state / 'pause.json')
        self.budget = Budget(self.config['local'],
                             store_path=self.state / 'local-peaks.sqlite3',
                             gate=self.gate)
        self.local = LocalExecutor(self.budget,
                                   drift=self.config['local'].get('drift', 'warn'),
                                   queue_timeout=float(
                                       self.config['local'].get('queue_timeout_seconds') or 0))
        self.budget.estimate = lambda: progress.queue_eta(
            self.state, [run for run in list(self.runs.values())
                         if run.lane == 'local' and run.state == 'running'])
        # The worker, watched rather than discovered. A command that arrives
        # while the worker is known down must not pay the SSH connect timeout
        # again to learn what the last poll already established.
        self.health = Monitor(self.any_worker,
                              interval=self.config['worker'].get('health_interval_s') or 60,
                              notify_enabled=bool(self.config['notify']['enabled']),
                              store=self.state / 'worker-health.json',
                              log=log)

    # -- configuration -----------------------------------------------------

    def refresh(self):
        try:
            self.config = settings.load(self.config_path)
        except ConfigError:
            pass                                   # keep the last good configuration

    def repo_config(self, repo, root):
        """Load the invoking worktree's config, with an explicit external fallback.

        The enrolled root identifies a repository, not the checkout that will
        execute this command. Cache by path so sibling worktrees cannot share a
        config merely because their enrollment name is the same.
        """
        path, origin = loader.resolve(root, repo.get('config') or None)
        # By content, not only by date: a file replaced by one with the same
        # mtime (a checkout, a copy with -p) must not keep its old parse, or a
        # key this code cannot read would route by the previous file's claims.
        stamp = (str(path), path.stat().st_mtime_ns, enrollment.digest_of(path))
        key = str(path.resolve())
        if self.repo_stamps.get(key) != stamp:
            config = loader.load(path)
            config['origin'] = origin
            self.repo_configs[key] = config
            self.repo_stamps[key] = stamp
        return self.repo_configs[key]

    @staticmethod
    def direct_script(spec, root):
        """Check a directly named script; never interpret shell or package commands.

        Return None when the argv does not have the simple interpreter/script
        shape. Such an argv gives no evidence that an external config fits a
        different worktree.
        """
        argv = spec['argv']
        if (len(argv) < 2 or argv[0] not in ('node', 'bun', 'python', 'python3')
                or argv[1].startswith('-') or '{' in argv[1]):
            return None
        script = Path(argv[1])
        if script.is_absolute() or '..' in script.parts:
            return None
        return (Path(root) / spec['cwd'] / script).is_file()

    def compatible_job(self, config, job, root):
        """Decide whether this checkout supports the selected declared runner.

        A repo-owned config is authoritative except for a directly named
        missing script. An external config used by a sibling worktree needs
        positive evidence: declared root markers and directly named scripts
        for both execution and preflight. The enrolled root remains compatible
        with its explicit external config for existing installations.
        """
        for spec in (job['run'], job['validate']):
            if spec is not None and self.direct_script(spec, root) is False:
                return False
        if config['origin'] == 'repo-root':
            return True
        markers = config['repo']['root_markers']
        return (bool(markers) and all(not Path(marker).is_absolute()
                                      and '..' not in Path(marker).parts
                                      and (Path(root) / marker).exists() for marker in markers)
                and self.direct_script(job['run'], root) is True
                and (job['validate'] is None
                     or self.direct_script(job['validate'], root) is True))

    def any_worker(self):
        """A worker to ask about the worker. There is one host in this build."""
        if not self.config['worker']['host']:
            raise WorkerUnreachable('no worker host is configured')
        return self.worker_for(self.config['repos'][0] if self.config['repos'] else {})

    def worker_for(self, repo):
        host = self.config['worker']['host']
        key = (host, self.config['worker']['engine_root'])
        # Startup settles several rows on their own threads; two Workers for one
        # host would be two SSH masters and two engine bundles resolved.
        with self.workers_lock:
            if key not in self.workers:
                self.workers[key] = self.worker_factory(
                    host, state=self.state, engine_root=self.config['worker']['engine_root'],
                    persist=self.config['worker']['ssh_persist'], client=self.client_name())
            # The config is re-read on every connection, so a changed `[client]
            # name` applies to the next submission with no restart.
            self.workers[key].client = self.client_name()
            return self.workers[key]

    def client_name(self):
        """Who this daemon is to a shared worker: `[client] name`, else `user@host`."""
        return settings.client_name(self.config)

    # -- lifecycle ---------------------------------------------------------

    # How long a starting daemon waits for its predecessor to let go of the lock.
    LOCK_WAIT = 120.0

    def acquire_lock(self, wait=None, poll=0.2):
        """Take `daemon.lock`, waiting up to `LOCK_WAIT` for a predecessor still stopping.

        `launchctl kickstart -k` starts the new daemon without waiting for the
        old one to exit. Refusing at once made launchd relaunch it every
        `ThrottleInterval`, and a restart took minutes (2026-09-24).
        """
        handle = (self.state / 'daemon.lock').open('a+')
        deadline = time.monotonic() + (self.LOCK_WAIT if wait is None else wait)
        said = False
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                pass
            if self.stopping.is_set():
                # Told to stop before it ever ran: nothing to report.
                handle.close()
                log('stopped while waiting for %s' % (self.state / 'daemon.lock'))
                raise SystemExit(0)
            if time.monotonic() >= deadline:
                handle.close()
                raise SystemExit('pandora daemon already running for ' + str(self.state))
            if not said:
                handle.seek(0)
                holder = (handle.read().split() or ['?'])[0]
                log('waiting for pid %s to stop: it holds %s' % (holder,
                                                                 self.state / 'daemon.lock'))
                said = True
            time.sleep(poll)
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()) + '\n')
        handle.flush()
        self.lock_handle = handle

    def clear_stale_socket(self):
        """Remove a socket file no one is listening on. Safe: we hold the lock."""
        if not self.socket_path.exists():
            return 'absent'
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            probe.connect(str(self.socket_path))
        except OSError:
            self.socket_path.unlink()
            return 'stale-removed'
        finally:
            probe.close()
        raise SystemExit('a listener already owns ' + str(self.socket_path))

    def resume_interrupted(self):
        """After a restart, settle every row that was live when we died.

        The real backend makes this honest in a way the fake one could not: the
        run is on the worker, the engine's supervisor never stopped, and
        re-attaching is asking the engine for the log from an offset. A run the
        engine no longer knows is closed as an infrastructure failure -- never as
        a pass, because this process has observed no test evidence at all.

        Two kinds of row have no engine run to follow, and used to stay live
        forever: a local run, whose supervisor was this process, and a remote row
        that died before `accepted`. The first is closed and its process group
        killed; the second is looked up on the worker by request id, adopted if
        the engine started it, and closed otherwise. Returns the ids resumed.
        """
        resumed = []
        for meta in sorted((self.state / 'runs').glob('*/meta.json')):
            try:
                payload = json.loads(meta.read_text())
            except (OSError, ValueError):
                continue
            self.status.update(dict(payload, id=meta.parent.name))
            if isinstance(payload, dict) and payload.get('id') == meta.parent.name:
                # Read anyway: `stats` then reads nothing it has seen here.
                self.index.learn(payload)
            if payload.get('state') not in ('queued', 'running'):
                continue
            run = self.resume_row(payload)
            if run is not None and run.remote and not run.done.is_set():
                resumed.append(run.id)
        return resumed

    def hold(self, run):
        """Register a pre-accept row this daemon drives, before its first save."""
        with self.runs_lock:
            for run_id in [key for key, item in self.pending.items()
                           if item.done.is_set() or key in self.runs]:
                del self.pending[run_id]
            self.pending[run.id] = run

    def live(self, run_id):
        """The Run a thread of this daemon drives for `run_id`, accepted or not."""
        with self.runs_lock:
            run = self.runs.get(run_id)
            if run is None:
                held = self.pending.get(run_id)
                run = held if held is not None and not held.done.is_set() else None
            return run

    def orphaned(self, payload):
        """A live row no thread of this daemon drives: its daemon has exited.

        `owner` is the daemon process that last saved the row. One of ours that
        is not in `self.runs` is a submission before `accepted`, still driven by
        its connection thread; anything else live is nobody's.
        """
        return (payload.get('state') in ('queued', 'running')
                and payload.get('id') not in self.runs and payload.get('owner') != OWNER)

    def resume_row(self, payload, *, cancel=False):
        """Take over one live row the last daemon left: follow it, settle it, or close it.

        Returns the row's Run, registered in `self.runs` before anything else can
        find it, so two attaches never start two followers. `cancel` is an
        explicit `pandora cancel` of the row: a local one ends `cancelled`, a
        remote one is followed with its cancel already asked for.
        """
        run = Run(self.state, payload['id'], payload, on_save=self.saved)
        run.lane = payload.get('lane') or 'remote'
        run.remote = payload.get('remote')
        run.accepted = payload.get('accepted')
        run.started = payload.get('started') or run.started
        run.phase = payload.get('phase')
        if cancel:
            run.canceled.set()
        with self.runs_lock:
            self.runs[run.id] = run
        if run.lane == 'local':
            self.close_local(run, payload)
        elif not run.remote:
            log('resume: run %s was not accepted; asking the worker' % run.id)
            threading.Thread(target=self.guarded, args=(self.settle_unaccepted, run, payload),
                             daemon=True).start()
        else:
            log('resume: run %s re-attaching to %s' % (run.id, run.remote))
            run.state = 'running'
            threading.Thread(target=self.guarded, args=(self.reattach, run),
                             daemon=True).start()
        return run

    def guarded(self, body, run, *args):
        """Run a takeover thread's body; whatever it raises, the row still ends.

        As `execute` does for a run this daemon started: an exception nobody
        named (a `TimeoutExpired` from an engine call) must not kill the thread
        and leave the row live with a client waiting on it. A daemon that is
        stopping leaves the row alone for the next one.
        """
        try:
            body(run, *args)
        except Exception as error:                 # noqa: BLE001 - never a silent pass
            if not self.stopping.is_set() and not run.done.is_set():
                run.note('%s: %s' % (type(error).__name__, error))
        finally:
            if not run.done.is_set() and not self.stopping.is_set():
                run.note('the takeover of this run ended without a verdict; %s' % UNCERTAIN)
                log('resume: run %s closed as infra_failed: its takeover thread ended'
                    % run.id)
                run.finish(INFRA, state='infra_failed')

    def close_local(self, run, payload):
        """A local run's supervisor died with the last daemon: end the row, and the tree."""
        killed = local_module.kill_recorded(payload.get('pgid'), payload.get('pgid_started'))
        canceled = run.canceled.is_set()
        log('resume: local run %s closed as %s%s' % (
            run.id, 'cancelled' if canceled else 'infra_failed',
            '; killed process group %s' % payload['pgid'] if killed else
            ('; process group %s not ours any more, left alone' % payload['pgid']
             if payload.get('pgid') else '')))
        run.note('%s: the daemon that supervised it has exited%s%s'
                 % ('canceled' if canceled else 'daemon restarted during the run',
                    '; stopped its process group %s' % payload['pgid'] if killed else '',
                    '' if canceled else '; rerun it'))
        if canceled:
            run.finish(CANCELED, state='cancelled')
        else:
            run.finish(INFRA, state='infra_failed')

    def settle_unaccepted(self, run, payload):
        """A remote row that never reached `accepted`: ask the engine, once, by request id.

        The lookup fences the id, so a submit still in flight cannot start the
        run after this answer. A row still freezing or shipping never reached
        `submit`, so it is closed without asking. Spawned: adopted like any
        accepted run, unless it writes back -- its frozen context was saved only
        at `accepted`, so there is nothing to check a proposal against, and it is
        stopped on the worker instead. Refused or never seen: nothing ran, so
        rerun it. Anything the engine cannot account for: execution is uncertain.
        """
        if payload.get('phase') in ('freeze', 'ship'):
            self.close_unaccepted(run, 'it was still in %s, before anything reached the '
                                  'engine; rerun it' % payload['phase'])
            return
        job = payload.get('job') or ''
        repo = next((item for item in self.config['repos']
                     if item['name'] == (payload.get('repo') or '')), None)
        if repo is None or not self.config['worker']['host']:
            self.close_unaccepted(run, 'no worker or enrollment to ask about it; %s'
                                  % UNCERTAIN)
            return
        worker = self.worker_for(repo)
        try:
            found = worker.lookup(run.id + ':' + job,
                                  plan={'repo': payload.get('repo'), 'job': job}, fence=True)
        except (PandoraError, OSError) as error:
            self.close_unaccepted(run, 'the worker could not be asked (%s); %s'
                                  % (error, UNCERTAIN))
            return
        if found.get('ok') and found.get('spawned') and found.get('run_id'):
            argv = payload.get('argv') or []
            if payload.get('writeback') or '--update' in argv:
                try:
                    worker.cancel(found['run_id'])
                    stopped = 'stopped it there'
                except (PandoraError, OSError) as error:
                    stopped = 'could not stop it there (%s)' % error
                self.close_unaccepted(run, 'the worker had started this write-back run as %s '
                                      'but its frozen context was never saved, so nothing '
                                      'could be written back; %s; rerun it'
                                      % (found['run_id'], stopped))
                return
            run.remote = found['run_id']
            run.state = 'running'
            run.accepted = run.accepted or now()
            run.phase = None
            run.note('daemon restarted before `accepted`; the worker had started it as %s, '
                     'following it' % run.remote)
            log('resume: run %s adopted as %s' % (run.id, run.remote))
            run.save()
            self.reattach(run)
            return
        if found.get('ok') and not found.get('found'):
            why = 'the worker never started it; rerun it'
        elif found.get('ok') and found.get('state') == 'finished':
            why = 'the worker refused it before it ran (%s); rerun it' % (
                found.get('cause') or found.get('outcome') or '?')
        else:
            why = 'the worker has it as %s with no supervisor recorded; %s' % (
                found.get('state') or '?', UNCERTAIN)
        self.close_unaccepted(run, why)

    def close_unaccepted(self, run, why):
        if run.canceled.is_set():
            run.note('canceled; the daemon restarted before `accepted` and %s' % why)
            log('resume: run %s closed as cancelled: %s' % (run.id, why))
            run.finish(CANCELED, state='cancelled')
            return
        run.note('daemon restarted before `accepted`; %s' % why)
        log('resume: run %s closed as infra_failed: %s' % (run.id, why))
        run.finish(INFRA, state='infra_failed')

    def start(self):
        # What this daemon imported, by the time it serves anything: the checkout
        # can change under it, and `pandora doctor` digests these same files
        # there to tell a daemon that needs `--restart`.
        self.code_modules = bundle.loaded_modules()
        self.code = bundle.code_digest(names=self.code_modules)
        self.acquire_lock()
        (self.state / 'runs').mkdir(exist_ok=True)
        self.clear_stale_socket()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        self.server.listen(64)
        self.resume_interrupted()
        # Every row the last daemon left is settled or followed: the restart it
        # drained for is over, and a client in the gap may submit again.
        draining.clear_marker(self.state)
        (self.state / 'daemon.json').write_text(json.dumps(
            {'pid': os.getpid(), 'version': VERSION, 'socket': str(self.socket_path),
             'worker': self.config['worker']['host'], 'started': now(),
             'home': PACKAGE_HOME, 'code': self.code,
             'code_modules': self.code_modules}) + '\n')
        if self.config['worker']['host']:
            self.health.start()
        threading.Thread(target=self.prune_loop, name='prune', daemon=True).start()
        return self

    def prune_loop(self, every=runindex.PRUNE_EVERY):
        """At start, then hourly: remove finished runs older than `[client] keep_runs_days`."""
        keep = runindex.keep_seconds(self.config)
        log('prune: %s' % ('on: finished runs older than %g day(s) are removed at start and '
                           'every %ds ([client] keep_runs_days)' % (keep / 86400.0, every)
                           if keep > 0 else 'off: [client] keep_runs_days = 0 keeps every run'))
        while True:
            self.prune()
            if self.stopping.wait(every):
                return

    def prune(self, now=None):
        keep = runindex.keep_seconds(self.config)
        with self.runs_lock:
            live = {run_id for run_id, run in list(self.runs.items()) + list(self.pending.items())
                    if not run.done.is_set()}
        try:
            removed = runindex.prune(self.state, keep, live=live, now=now, index=self.index)
        except OSError as error:
            log('prune: %s' % error)
            return []
        for run_id in removed:
            self.status.forget(run_id)
        if removed:
            log('prune: removed %d finished run(s) older than %g day(s): %s'
                % (len(removed), keep / 86400.0, ' '.join(removed[:20])
                   + (' ...' if len(removed) > 20 else '')))
        return removed

    def serve(self):
        self.server.settimeout(0.25)
        while not self.stopping.is_set():
            self.check_lease()
            try:
                conn, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(target=self.handle, args=(conn,), daemon=True)
            with self.runs_lock:
                self.handlers.add(thread)
            thread.start()

    def stop(self):
        self.stopping.set()
        # The listener first: a client that connects now finds no socket and
        # waits on the draining marker, where one accepted and then dropped at
        # exit would have read EOF and lost its command.
        for closer in (lambda: self.server.close(), lambda: self.socket_path.unlink()):
            try:
                closer()
            except (OSError, AttributeError):
                pass
        self.close_local_runs()
        self.health.stop()
        for worker in self.workers.values():
            try:
                worker.close()
            except OSError:
                pass
        if self.lock_handle:
            self.lock_handle.close()

    # -- connections -------------------------------------------------------

    def handle(self, conn):
        if RAISED.is_set():
            set_qos(QOS_CLASS_DEFAULT)
        try:
            self._handle(conn)
        finally:
            with self.runs_lock:
                self.handlers.discard(threading.current_thread())

    def _handle(self, conn):
        try:
            self.dispatch(conn)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        except Exception as error:               # noqa: BLE001 - said, never silent
            # A handler thread that dies says so; the row it opened is closed
            # by its own `finally`.
            log('connection handler failed: %s: %s' % (type(error).__name__, error))
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # What each refusal costs the caller. The client no longer invents an exit
    # code for a daemon verdict, so the verdict has to carry one.
    EXITS = {'queue-timeout': STALE, 'busy': STALE, 'version': INFRA,
             'unauthorized': INFRA, 'local-paused': INFRA, 'fallback-refused': INFRA,
             'config-unknown': INFRA}

    def deny(self, conn, code, message, exit=None):
        conn.sendall(dump({'v': VERSION, 't': 'error', 'code': code, 'msg': message,
                           'exit': exit if exit is not None else self.EXITS.get(code, 1)}))

    def dispatch(self, conn):
        uid = peer_uid(conn)
        if uid is not None and uid != os.getuid():
            self.deny(conn, 'unauthorized', 'socket serves uid %d only' % os.getuid())
            return
        reader = Reader(conn)
        first = reader.line()
        if first is None:
            return
        if first.get('v') != VERSION:
            # No version numbers: what the caller can do about it is the same
            # whichever side is older, and it is this.
            from . import install
            self.deny(conn, 'version', 'this client and the daemon run different Pandora '
                      'code: %s, and point the shim and `pandora` on PATH at the same one '
                      '(`pandora doctor` says where they point)' % install.update_fix(PACKAGE_HOME))
            return
        op = first.get('op')
        if op != 'ps':
            self.refresh()
        if op == 'ping':
            conn.sendall(dump({'v': VERSION, 't': 'pong', 'pid': os.getpid(),
                               # What `doctor` reports as the daemon's interpreter.
                               'python': sys.executable,
                               'python_version': '%d.%d.%d' % sys.version_info[:3],
                               'worker': self.config['worker']['host'],
                               'runs': len(self.runs), 'home': PACKAGE_HOME,
                               'code': self.code, 'code_modules': self.code_modules,
                               # The cached reading, never a poll: `pandora doctor`
                               # asks this, and a doctor must not change anything.
                               'health': self.health.state()}))
        elif op == 'stats':
            conn.sendall(dump({'v': VERSION, 't': 'stats',
                               'data': self.stats(first.get('since'))}))
        elif op == 'ps':
            conn.sendall(dump({'v': VERSION, 't': 'ps',
                               'data': self.ps(int(first.get('limit', 20))),
                               'pause': self.gate.state(),
                               'worker': self.health.state(), 'client': self.client_name(),
                               'draining': self.draining}))
        elif op == 'drain':
            if first.get('cancel'):
                self.undrain()
                conn.sendall(dump({'v': VERSION, 't': 'drain', 'draining': False}))
            else:
                conn.sendall(dump(dict({'v': VERSION, 't': 'drain', 'draining': True},
                                       **self.drain(first.get('pid')))))
        elif op == 'claims':
            # The next daemon may derive caches differently; let it write this one.
            if self.draining:
                self.answer_draining(conn)
                return
            conn.sendall(dump(dict({'v': VERSION, 't': 'claims'}, **self.claims(first))))
        elif op == 'run':
            self.serve_run(conn, reader, first)
        elif op == 'attach':
            self.serve_attach(conn, reader, first)
        elif op == 'cancel' and first.get('if_idle') is not None:
            self.cancel_idle(conn, first)
        elif op == 'cancel':
            # A row no thread here drives is taken over with its cancel asked
            # for; answering `ok` and changing nothing left clients hung.
            run = self.live(first.get('run')) or self.adopt(first.get('run'), cancel=True)
            if run:
                run.canceled.set()
            conn.sendall(dump({'t': 'ok', 'run': first.get('run')}))
        else:
            self.deny(conn, 'rejected', 'unknown op %r' % op)

    # -- the routed path ---------------------------------------------------

    def plan_for(self, request, say=None):
        """Classify one request. Raises the pre-accept refusals; returns a plan.

        The worktree, not the enrollment's root: one enrollment covers every
        worktree of a repository, and the command was typed in exactly one of
        them. The *invocation* directory inside that worktree is what decides
        whether the job can be re-rooted -- see `classify.path_like`.
        `say` receives one line for the caller when this worktree's claim cache
        was just rewritten with other claims.
        """
        cwd = request.get('cwd') or ''
        repo = settings.enrollment_for(self.config, cwd)
        if repo is None:
            repo = self.enrollment_by_git(cwd)
        if repo is None:
            raise NotClaimed('cwd is not inside an enrolled repository')
        root = Path(enrollment.worktree_root(cwd) or repo['root'])
        text, refreshed = self.write_claims(root, repo)
        if refreshed and say is not None:
            say('claim cache refreshed from %s'
                % enrollment.refreshed_from(enrollment.parse(text)))
        try:
            config = self.repo_config(repo, root)
        except UnknownSchema as error:
            # Not a passthrough: the file claims this command in words this
            # code cannot read, and running it here unmanaged is how the owner
            # lost 14 forms for two days. Refused, with the key and the fix.
            refusal = Refused(not_understood(error, PACKAGE_HOME))
            refusal.code, refusal.exit = 'config-unknown', INFRA
            raise refusal from None
        except ConfigError as error:
            raise NotClaimed('no usable config in this worktree: %s' % error) from None
        try:
            relative = Path(cwd).resolve().relative_to(root.resolve())
        except ValueError:
            relative = Path('.')
        here = Path(cwd)
        verdict = classifier.classify(config, request.get('argv') or [],
                                      cwd=str(relative), env=request.get('env') or {},
                                      present=request.get('env_present'),
                                      exists=lambda token: (here / token).exists())
        if verdict['decision'] == 'local':
            raise NotClaimed(verdict['reason'])
        job = config['jobs'][verdict['job']]
        if (config['origin'] == 'enrollment'
                and root.resolve() == Path(repo['root']).resolve()):
            # The explicit external config was enrolled for this checkout.
            # Still avoid a known missing direct script before preflight.
            compatible = all(self.direct_script(spec, root) is not False
                             for spec in (job['run'], job['validate']) if spec is not None)
        else:
            compatible = self.compatible_job(config, job, root)
        if not compatible:
            error = NotClaimed('configured runner is unavailable in this worktree')
            error.writeback = any(output['kind'] == 'writeback'
                                  for output in (verdict.get('plan') or {}).get('outputs', []))
            raise error
        if verdict['decision'] == 'reject':
            error = Refused(verdict['message'])
            error.code, error.exit = verdict.get('code') or 'rejected', verdict.get('exit')
            raise error
        # Re-rooting is the run's whole difference from a root invocation, so it
        # is applied once, here, and said out loud rather than inferred later.
        verdict['worktree'] = str(root)
        return repo, config, verdict

    def write_claims(self, root, repo):
        """Derive this worktree's claim cache from the config it routes by.

        Called for every classification, so a changed `pandora.toml` reaches the
        shim at the next claimed command, or at the first command the shim
        finds stale. Rewritten only when the text or its date differs. Never a
        verdict: a cache that cannot be written leaves the shim on the slow
        path, which costs a Python start and routes correctly. Returns (the
        text, whether an existing cache's claims were replaced).
        """
        text, sources = enrollment.derive(
            root, repo, socket_path=str(self.socket_path),
            client=str(settings.path_of(self.config_path)),
            load=lambda where, entry: self.repo_config(entry, Path(where)))
        # Only where the shim already looks: `pandora unenroll` removed the
        # registration and the marker, and a `pandora run` afterward must not
        # leave a cache that makes the shim route this worktree again. And only
        # for the daemon enrollment named: a second daemon (another `--state`)
        # asked about this worktree must not point its cache at itself.
        written, why = enrollment.write_owned(root, text, sources, self.socket_path)
        if written:
            log('claims: wrote %s' % enrollment.cache_path(root))
        elif why and why != 'not enrolled' and why != 'not inside a repository':
            log('claims: not writing %s: %s' % (enrollment.cache_path(root), why))
        return text, written == enrollment.CHANGED

    def claims(self, request):
        """The shim's slow path: refresh this worktree's cache, then classify as the shim would.

        The shim found the cache missing or older than its config and does not
        know whether the command is claimed. The answer is the shim's own rule
        applied to the cache just written, so a command decided here is decided
        exactly as the next, fork-free, invocation will decide it.
        """
        cwd = request.get('cwd') or ''
        argv = list(request.get('argv') or [])
        root = enrollment.worktree_root(cwd) if cwd else None
        if root is None:
            return {'claimed': False, 'heavy': False, 'why': 'not inside a worktree'}
        repo = settings.enrollment_for(self.config, cwd) or self.enrollment_by_git(cwd)
        text, refreshed = self.write_claims(Path(root), repo)
        parsed = enrollment.parse(text)
        claimed = enrollment.claimed(argv, parsed)
        if claimed and enrollment.claims_nothing_here(cwd, parsed):
            claimed = False
        answer = {'claimed': claimed,
                  'heavy': not claimed and enrollment.heavy(argv, parsed),
                  'cache': str(enrollment.cache_path(root))}
        if refreshed:
            answer['refreshed'] = enrollment.refreshed_from(parsed)
        return answer

    def enrollment_by_git(self, cwd):
        """A worktree of an enrolled repository is enrolled.

        Matching on path prefix misses the common case on this machine, where
        every worktree lives beside the repository rather than inside it, so fall
        back to the git common directory -- the same identity the shim's marker
        uses.
        """
        common = enrollment.common_dir(cwd) if cwd else None
        if common is None:
            return None
        for repo in self.config['repos']:
            try:
                enrolled_common = enrollment.common_dir(repo['root'])
                if enrolled_common and Path(enrolled_common).resolve() == Path(common).resolve():
                    return repo
            except OSError:
                continue
        return None

    def tell(self, conn, text):
        """One line of Pandora's own commentary, before `accepted`.

        The client prints it on stderr as it arrives. It is a frame rather than
        a log line because there is no run yet -- and every one of these is said
        at a moment where nothing has executed.
        """
        try:
            conn.sendall(dump({'v': VERSION, 't': 'notice', 'msg': text}))
        except OSError:
            pass

    # -- draining ----------------------------------------------------------

    def drain(self, pid=None):
        """Stop admitting runs for a restart; the rows a restart would still end.

        Idempotent: asked again, it answers again, which is how `pandora daemon
        --restart` polls. A local run still queued is withdrawn and its caller
        told `draining`, so it submits again to the next daemon: nothing ran,
        and a queue position is all it loses. A local run executing and a
        remote row before `accepted` are waited for; an accepted remote run is
        the successor's to follow and blocks nothing.
        """
        with self.admitting:
            if self.draining is None:
                self.draining = {'since': now(), 'pid': pid, 'daemon': os.getpid()}
                try:
                    draining.write_marker(self.state, self.draining)
                except OSError as error:
                    log('drain: could not write %s: %s'
                        % (draining.marker_path(self.state), error))
                log('drain: requested by pid %s; admitting nothing new' % (pid or '?'))
            else:
                # Renewed: dated now, so a client in the gap and `doctor` both
                # see a drain someone is still driving.
                draining.touch_marker(self.state)
            self.drain_renewed = time.monotonic()
            with self.runs_lock:
                live = [run for run in list(self.runs.values()) + list(self.pending.values())
                        if not run.done.is_set()]
            withdrawn = []
            for run in live:
                if run.lane == 'local' and run.state == 'queued' and not run.drained:
                    run.drained = True
                    withdrawn.append(run.id)
            if withdrawn:
                log('drain: withdrew %d queued local run(s) for resubmission: %s'
                    % (len(withdrawn), ' '.join(withdrawn)))
            seen, blocking = set(), []
            for run in live:
                if run.id in seen:
                    continue
                seen.add(run.id)
                if ((run.lane == 'local' and run.state == 'running')
                        or (run.lane != 'local' and run.state == 'queued')):
                    blocking.append(dict({'id': run.id, 'lane': run.lane, 'state': run.state,
                                          'phase': run.phase, 'argv': run.request.get('argv')},
                                         **run.activity_fields()))
            since = self.draining['since']
        return {'since': since, 'blockers': blocking,
                'local': sum(row['lane'] == 'local' for row in blocking),
                'pre_accept': sum(row['lane'] != 'local' for row in blocking)}

    def cancel_idle(self, conn, request):
        """A drain's cancel of a local run with no CPU progress: only if it is still idle.

        The restarter saw the run idle one poll ago; this daemon's own reading
        decides, so a run that woke up in between is never cancelled. The
        reason goes into the run's log, where its caller and `pandora logs`
        see it.
        """
        run = self.live(request.get('run'))
        try:
            limit = float(request['if_idle'])
        except (TypeError, ValueError):
            limit = None
        idle = run.idle_seconds() if run is not None else None
        if (run is None or limit is None or limit <= 0 or run.lane != 'local'
                or run.state != 'running' or idle is None or idle < limit):
            conn.sendall(dump({'v': VERSION, 't': 'error', 'code': 'not-idle', 'exit': 1,
                               'run': request.get('run'),
                               'msg': 'run %s is not an idle local run here (idle %s)'
                                      % (request.get('run'), 'unmeasured' if idle is None
                                         else '%ds' % idle)}))
            return
        why = ('canceled by a restart drain (%s): no CPU progress in its process tree, and '
               'no output, for %s. Rerun it'
               % (request.get('by') or 'pid %s' % (request.get('pid') or '?'),
                  draining.fmt_idle(idle)))
        run.note(why)
        log('drain: cancel run %s: idle for %ds (limit %ds)' % (run.id, idle, limit))
        run.canceled.set()
        conn.sendall(dump({'v': VERSION, 't': 'ok', 'run': run.id, 'idle_seconds': idle}))

    def undrain(self, why='cancelled'):
        with self.admitting:
            was, self.draining = self.draining, None
            draining.clear_marker(self.state)
        if was is not None:
            log('drain: %s; admitting runs again' % why)

    def check_lease(self, clock=time.monotonic):
        """End a drain nobody has renewed within `LEASE_SECONDS`. Not while stopping."""
        renewed = self.drain_renewed
        if (self.draining is not None and renewed is not None and not self.stopping.is_set()
                and clock() - renewed > draining.LEASE_SECONDS):
            self.undrain('no drain request for %ds, so whoever asked for it is gone'
                         % draining.LEASE_SECONDS)

    def withdraw_drained(self, conn, run):
        """A queued local run a drain marked: release its slot, close it, tell the caller to ask again."""
        self.budget.finish(run.id, 0, 'lost')
        if not run.done.is_set():
            run.note('the daemon began a restart while this run was queued; nothing ran, '
                     'and the client submits it again')
            run.finish(INFRA, state='withdrawn')
        self.answer_draining(conn)

    def answer_draining(self, conn):
        """Nothing ran: ask again in a moment, of this daemon or the next."""
        conn.sendall(dump({'v': VERSION, 't': 'draining', 'retry_after': draining.RETRY_AFTER,
                           'reason': 'restarting',
                           'msg': 'the daemon is restarting; nothing ran. Ask again.'}))

    # -- stopping ----------------------------------------------------------

    def close_local_runs(self):
        """A stopping daemon ends the local runs it drives, and says so to each caller.

        Their pipes are this process's and no successor can read a verdict from
        them, so they are not left for the next daemon's sweep to find: the row
        is closed first, as `infra_failed` for one that was running and
        `withdrawn` for one still queued, then the tree is killed, then the
        connection threads get a moment to deliver the exit frame. A remote
        run is left alone: the worker still has it, and the successor settles
        it from the engine's own record.
        """
        with self.runs_lock:
            candidates = list(self.runs.values()) + list(self.pending.values())
        closed = []
        for run in candidates:
            if run.lane != 'local' or run.id in closed:
                continue
            run.canceled.set()
            with run.close_lock:
                # The note and the close under one lock: a run whose child
                # finished at this same instant keeps its verdict and never
                # hears "re-run it".
                if run.done.is_set():
                    continue
                if run.state == 'running':
                    run.note('the daemon stopped while this run was executing here, so its '
                             'verdict is lost. Re-run it.')
                    run._finish(INFRA, 'infra_failed', None)
                else:
                    run.note('the daemon stopped before this run was accepted; nothing ran. '
                             'Re-run it.')
                    run._finish(INFRA, 'withdrawn', None)
            with run.spawn_lock:
                pgid = run.pgid
            if pgid:
                # The leader is this process's own child, not yet reaped, so
                # its group id is nobody else's: no proof needed, and none
                # of the reasons `kill_recorded` may decline apply. The
                # sweep's helper follows, for descendants that left the group.
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                local_module.kill_recorded(pgid, run.pgid_started)
                log('stop: killed the process group of run %s' % run.id)
            closed.append(run.id)
        if not closed:
            return closed
        log('stop: closed %d local run(s): %s' % (len(closed), ' '.join(closed)))
        # The exit frames reach their callers through the connection threads,
        # which die with this process. Give them the moment they need.
        deadline = time.monotonic() + 2.0
        with self.runs_lock:
            handlers = list(self.handlers)
        for thread in handlers:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return closed

    def answer_closed(self, conn, run):
        """The row closed while its caller still waited for `accepted`: say so, in the row's words."""
        if run.state == 'cancelled':
            self.deny(conn, 'canceled', 'run %s was canceled while queued; nothing ran'
                      % run.id, exit=CANCELED)
        else:
            self.deny(conn, 'daemon-stopping',
                      'the daemon stopped before this run was accepted; nothing ran. '
                      'Re-run it.', exit=run.exit_code if run.exit_code is not None else INFRA)

    def serve_run(self, conn, reader, request):
        if self.draining:
            # Before the stop check: a daemon stopping for a drained restart
            # has a successor coming, and the caller should wait for it.
            self.answer_draining(conn)
            return
        if self.stopping.is_set():
            self.deny(conn, 'daemon-stopping', 'the daemon is stopping; nothing ran. Re-run it.',
                      exit=INFRA)
            return
        try:
            repo, config, verdict = self.plan_for(request,
                                                  say=lambda line: self.tell(conn, line))
        except NotClaimed as error:
            # Not a fallback and not a refusal: Pandora has no opinion about
            # this invocation, so the client runs it as if the shim were absent.
            conn.sendall(dump({'v': VERSION, 't': 'error', 'code': 'passthrough',
                               'msg': str(error), 'writeback': bool(
                                   getattr(error, 'writeback', False)), 'exit': 1}))
            return
        except Refused as error:
            self.deny(conn, getattr(error, 'code', 'rejected'), str(error),
                      exit=getattr(error, 'exit', None))
            return
        except ConfigError as error:
            self.deny(conn, 'rejected', str(error))
            return
        plan = verdict['plan']
        job = config['jobs'][verdict['job']]
        worktree = verdict.get('worktree') or request['cwd']
        if not submitted_by(request):
            # No session variable came with it: name the caller's session here,
            # on this thread, rather than make every client run `ps`.
            who = attribution.of_peer(conn)
            if who:
                request = dict(request, submitter=who)
        # Which client daemon this is, for a worker other Macs share: the row,
        # the engine's ledger and the result all carry it.
        request = dict(request, client=self.client_name())
        # Only names the repository asked for: an undeclared variable the shim
        # filtered was never going to travel, so saying so would be noise.
        for line in envfilter.notices(plan['env_passthrough'], request.get('env_dropped')):
            self.tell(conn, line)
        if verdict.get('rerooted'):
            self.tell(conn, 'running from the worktree root; you typed this in %s and no '
                            'argument names a path' % verdict['rerooted'])

        # The job's `where`, or the caller's `--local`/`--remote`. A request the
        # job cannot honor is refused here, before anything is frozen or queued.
        try:
            placed = placement.decide(job, plan, request.get('where'))
        except Refused as error:
            self.deny(conn, error.code, str(error), exit=error.exit)
            return
        plan = placed['plan']
        request = dict(request, placement=placed['record'], reason=placed['reason'])
        if placed['where'] == 'local':
            self.serve_local(conn, reader, request, repo, job, plan, worktree=worktree,
                             reason=placed['reason'])
            return

        # The repository's own opinion of the arguments, before anything queues.
        try:
            checked = classifier.preflight(job, verdict['forwarded'], root=worktree,
                                           extra_env=self.validator_env(request, plan))
        except ValidationRejected as error:
            conn.sendall(dump({'v': VERSION, 't': 'error', 'code': 'invalid-arguments',
                               'msg': error.stderr or str(error), 'exit': error.code}))
            return

        with self.admitting:
            if self.draining:
                self.answer_draining(conn)
                return
            run = Run(self.state, uuid.uuid4().hex[:12],
                      dict(request, repo=repo['name'], job=job['id'], worktree=worktree,
                           writeback=bool(plan.get('writeback'))), on_save=self.saved)
            run.state = 'queued'
            self.hold(run)
            run.save()
        try:
            self.submit_remote(conn, reader, request, repo, job, plan, worktree, checked, run)
        finally:
            # Every way out before `accepted` must close the row it opened. A
            # branch that forgot to -- or an exception nobody anticipated --
            # would otherwise leave a `queued` line in `pandora ps` with no exit,
            # forever, for a request that ended seconds ago (#86). Nothing was
            # accepted, so this is an infrastructure failure, never a pass.
            if run.state == 'queued':
                run.note('the submission ended before `accepted` without a verdict')
                run.finish(INFRA, state='infra_failed')

    def submit_remote(self, conn, reader, request, repo, job, plan, worktree, checked, run):
        """From a saved `queued` row to `accepted`, or to that row's final state.

        `run` is finished on every path that returns before `accepted`: as
        `refused`, `withdrawn`, `infra_failed`, or `fell_back` pointing at the
        local run that took the request over.
        """
        # The health poll's one job. Without it every command typed against a
        # worker that died at lunchtime pays the SSH connect timeout again --
        # 10 s of the 12.2 s the slice measured -- to rediscover the same fact.
        # The decision is identical to `worker-unreachable`; only the price of
        # reaching it differs, and the cause name records which one this was.
        if self.health.known_down():
            self.fall_back(conn, reader, request, repo, job, plan, worktree, 'worker-down',
                           self.health.state().get('reason') or 'the last health poll failed',
                           checked, origin=run)
            return
        # Every way a submission can fail to proceed, through one door. Each of
        # them is provably non-executing -- that is what earns the fallback --
        # and each of them is decided by the same policy rather than by whatever
        # the client happened to do with that error code. The exception is
        # `ExecutionUncertain`: the submit call failed and the engine could not
        # be asked what it did (`Worker.recover`), so it never falls back.
        beat = Heartbeat(conn).start()

        def entered(name):
            # `pandora ps` reads meta.json: a slow ship shows as `shipping`,
            # not as a `queued` row nobody can tell apart from a stuck one.
            run.phase = name
            run.save()

        def said(text):
            # The live caller hears it as a notice; the log keeps it for `logs`.
            run.said(text)
            beat.say(text)
        try:
            try:
                worker = self.worker_for(repo)
                submission = worker.submit(
                    plan=plan, worktree=worktree, request_id=run.id + ':' + plan['job'],
                    control=request, progress=said, phase=entered,
                    log=lambda text: log('run %s (%s): %s' % (run.id, worktree, text)),
                    transfer_stderr=run.dir / 'transfer.stderr')
            except Exception as error:
                # What each step cost up to the failure, the failing one included.
                run.pre_accept = dict(getattr(error, 'pre_accept', None) or run.pre_accept)
                raise
            finally:
                # Stopped before any other frame is written: two threads never
                # share the socket.
                left = beat.stop()
        except ExecutionUncertain as error:
            # The one pre-accept failure that is not provably non-executing: the
            # submit call failed and the engine could not say whether it had
            # already started the run. A fallback here is how one command runs
            # twice, on the worker and on this Mac, so this ends the run instead.
            self.health.recheck()
            run.note(str(error))
            run.finish(INFRA, state='infra_failed')
            self.deny(conn, 'execution-uncertain',
                      '%s; %s' % (error, UNCERTAIN), exit=INFRA)
            return
        except WorkerUnreachable as error:
            # Paid the timeout once; the next command should not. This asks the
            # question immediately rather than answering it: a worker that
            # refuses a submission may still be up, and only the health call is
            # entitled to say otherwise.
            self.health.recheck()
            self.fall_back(conn, reader, request, repo, job, plan, worktree,
                           'worker-unreachable', str(error), checked, origin=run)
            return
        except SnapshotError as error:
            self.fall_back(conn, reader, request, repo, job, plan, worktree,
                           'snapshot-failed', str(error), checked, origin=run)
            return
        except TransferError as error:
            self.fall_back(conn, reader, request, repo, job, plan, worktree,
                           'transfer-failed', str(error), checked, origin=run)
            return
        except EngineError as error:
            cause = 'engine-error'
            try:
                named = json.loads(str(error)).get('code')
            except ValueError:
                named = None
            if named in policy.CAUSES:
                cause = named
            self.fall_back(conn, reader, request, repo, job, plan, worktree,
                           cause, str(error), checked, origin=run)
            return

        if left or not client_alive(conn):
            # The caller left before `accepted`, so it may already be running
            # this command some other way. The worker must not run it too.
            try:
                worker.cancel(submission.run_id)
            except (EngineError, WorkerUnreachable, OSError):
                pass
            run.remote = submission.run_id
            run.finish(INFRA, state='withdrawn')
            return
        run.remote = submission.run_id
        run.shipped = getattr(submission, 'shipped', frozenset())
        run.pre_accept = dict(getattr(submission, 'durations', None) or {})
        if getattr(submission, 'writeback', None) is not None:
            # On disk before `accepted`, so a daemon that adopts this run after
            # a restart checks the proposal against the same frozen hashes.
            writebacks.save(run.dir, submission.writeback)
        run.state = 'running'
        run.accepted = now()
        run.phase = None                 # from here, the engine's row state
        run.save()
        with self.runs_lock:
            self.runs[run.id] = run
        # Only now has the worker acknowledged anything. Past this frame the
        # client will never run the command locally.
        conn.sendall(dump({'v': VERSION, 't': 'accepted', 'run': run.id,
                           'remote': submission.run_id, 'input_id': submission.input_id,
                           'same_tree_as': submission.same_tree_as,
                           'reservation_mib': (submission.admission or {}).get('reservation_mib'),
                           'cpus_hint': (submission.admission or {}).get('cpus_hint'),
                           'source_reused': submission.source.get('reused'),
                           'durations': submission.durations}))
        if checked.get('ran') is False and checked.get('reason') != 'no validator declared':
            run.note(checked['reason'] + '; the arguments were not pre-checked')
        threading.Thread(target=self.execute, args=(run, repo, plan), daemon=True).start()
        self.stream(conn, reader, run, 0)

    # -- the one fallback path ---------------------------------------------

    def fall_back(self, conn, reader, request, repo, job, plan, worktree, cause, detail,
                  checked=None, origin=None):
        """A remote submission did not proceed. Decide once, then admit or refuse.

        There is no third option, and in particular there is no `exec`. A job
        that may fall back is *admitted into the local lane with its size class*,
        so twelve agents whose worker just died queue behind one budget instead
        of starting twelve browser suites on one Mac -- which is the accident
        this whole path exists to make impossible.

        `origin` is the remote row this request opened. It ends here: `refused`
        when nothing will run, or `fell_back` naming the local run once one
        exists. The local run is the request's one counted row from then on.
        """
        def refuse(message, code):
            log('run %s refused: %s: %s' % (origin.id if origin is not None else '-',
                                             cause, detail))
            if origin is not None:
                origin.refusal = {'cause': cause, 'detail': detail}
                origin.reason = cause
                origin.note(message)
                origin.finish(INFRA, state='refused')
            conn.sendall(dump({'v': VERSION, 't': 'error', 'code': code,
                               'msg': message, 'exit': INFRA}))

        if self.draining:
            # Nothing ran, and the local lane admits nothing new: the caller
            # submits it again, to this daemon or the next.
            if origin is not None:
                origin.note('the daemon began a restart before this run was accepted; '
                            'nothing ran, and the client submits it again')
                origin.finish(INFRA, state='withdrawn')
            self.answer_draining(conn)
            return
        if self.stopping.is_set():
            # A worker that failed because this daemon is going down must not
            # hand the job to a local lane that is going down with it.
            refuse('the daemon is stopping; nothing ran. Re-run it.', 'daemon-stopping')
            return
        # Whether an explicit PANDORA_WHERE=local could take this job: what the
        # refusal steers to instead of an unmanaged PANDORA_OFF run.
        local_lane = placement.why_not_local(job) is None
        if (request.get('placement') or {}).get('override') == 'remote':
            # The caller said where. Running it here instead would be the one
            # answer they ruled out, so the fallback lane is not consulted.
            refuse('%s (%s): --remote was asked for, so this is not run on '
                   'this Mac. %s' % (cause, detail,
                                     'Retry, or run it in the local queue with '
                                     'PANDORA_WHERE=local.' if local_lane else 'Retry.'),
                   'placement-unavailable')
            return
        verdict = policy.decide(cause=cause, size=plan['size'],
                                writeback=bool(plan['options'].get('update'))
                                          or plan.get('writeback'),
                                declared=job['fallback'],
                                notice=(job['fallback'] or {}).get('notice'),
                                local_lane=local_lane)
        if verdict['action'] == 'refuse':
            refuse('%s (%s): %s' % (cause, detail, verdict['reason']), 'fallback-refused')
            return
        log('run %s falls back to the local lane: %s: %s'
            % (origin.id if origin is not None else '-', cause, detail))
        try:
            self.tell(conn, '%s (%s); %s' % (cause, detail, verdict['reason']))
            self.serve_local(conn, reader, request, repo, job, plan, worktree=worktree,
                             reason='fallback:' + cause, checked=checked, origin=origin)
        finally:
            if origin is not None and not origin.done.is_set():
                # The local lane turned it away before opening a row of its own
                # (busy, or the caller left): nothing runs anywhere.
                origin.refusal = {'cause': cause, 'detail': '%s; the local lane did not '
                                                           'take it over' % detail}
                origin.reason = cause
                origin.note('%s; the local lane did not take it over' % cause)
                origin.finish(INFRA, state='refused')

    # -- the local path ----------------------------------------------------

    def serve_local(self, conn, reader, request, repo, job, plan, *, worktree=None,
                    reason='', checked=None, origin=None):
        """The same conversation as a routed run, with this Mac as the worker.

        Ordering is the whole contract. The repository's own validator, then the
        exclusivity rules, then the queue, and only then `accepted` -- so every
        way this can end badly before the frame is a way that provably ran
        nothing, exactly as it is for a remote run.
        """
        worktree = worktree or request['cwd']
        if checked is None:
            try:
                checked = classifier.preflight(job, plan['args'], root=worktree,
                                               extra_env=self.validator_env(request, plan))
            except ValidationRejected as error:
                conn.sendall(dump({'v': VERSION, 't': 'error', 'code': 'invalid-arguments',
                                   'msg': error.stderr or str(error), 'exit': error.code}))
                return

        # The id exists before the run does, so an exclusivity refusal leaves no
        # row at all: a `queued` line in `pandora ps` for a job that was told to
        # go away would be a lie the next reader has to un-learn.
        run_id = uuid.uuid4().hex[:12]
        with self.admitting:
            if self.draining:
                if origin is not None:
                    origin.note('the daemon began a restart before this run was accepted; '
                                'nothing ran, and the client submits it again')
                    origin.finish(INFRA, state='withdrawn')
                self.answer_draining(conn)
                return
            try:
                self.budget.reserve(run_id, repo=repo['name'], job=job['id'],
                                    worktree=worktree, singleton=job['singleton'],
                                    size=plan['size'])
            except Busy as error:
                # Not a pre-accept fallback: running it locally anyway is the exact
                # thing the rule exists to prevent.
                conn.sendall(dump({'v': VERSION, 't': 'error', 'code': 'busy',
                                   'msg': str(error), 'exit': STALE}))
                return
            run = Run(self.state, run_id,
                      dict(request, repo=repo['name'], job=job['id'], reason=reason),
                      on_save=self.saved)
            run.lane = 'local'
            self.hold(run)
            run.save()
        if origin is not None:
            # Closed the moment its successor exists, not when that successor
            # ends: for the length of a local run there are otherwise two live
            # rows for one request.
            origin.fell_back_to = run.id
            origin.note('fell back to local run %s (%s)' % (run.id, reason))
            origin.finish(INFRA, state='fell_back')
        try:
            conn.sendall(dump({'v': VERSION, 't': 'queued', 'run': run.id}))
            admission = self.budget.admit(run.id, repo=repo['name'], job=job['id'],
                                          canceled=lambda: (run.canceled.is_set()
                                                            or run.drained),
                                          timeout=self.local.queue_timeout,
                                          note=lambda text: self.tell(conn, text))
        except Busy as error:
            self.budget.finish(run.id, 0, 'lost')
            run.finish(STALE, state='refused')
            conn.sendall(dump({'v': VERSION, 't': 'error', 'code': 'busy',
                               'msg': str(error), 'exit': STALE}))
            return
        except Paused as error:
            # The machine, not the queue. Waiting longer would not have helped
            # and starting anyway is the one thing this gate exists to prevent.
            self.budget.finish(run.id, 0, 'lost')
            run.note(str(error))
            run.finish(INFRA, state='refused')
            conn.sendall(dump({'v': VERSION, 't': 'error', 'code': 'local-paused',
                               'msg': str(error), 'exit': INFRA}))
            return
        except OSError:
            self.budget.finish(run.id, 0, 'lost')      # the client went away while queued
            run.finish(STALE, state='refused')
            return
        if admission is None and run.drained and (not run.canceled.is_set()
                                                  or self.stopping.is_set()):
            # A restart began while it queued: nothing started, and the next
            # daemon queues it again when the caller asks. A stop (`--now`)
            # that closed the row first does not change that answer.
            self.withdraw_drained(conn, run)
            return
        if admission is None and run.canceled.is_set():
            # `pandora cancel` while it queued, or a stop: nothing started.
            self.budget.finish(run.id, 0, 'lost')
            if not run.done.is_set():
                run.note('canceled while queued; nothing ran')
                run.finish(CANCELED, state='cancelled')
            self.answer_closed(conn, run)
            return
        if admission is None:
            self.budget.finish(run.id, 0, 'lost')
            run.finish(STALE, state='refused')
            self.deny(conn, 'queue-timeout',
                      'the local queue did not admit this job within its wait')
            return
        if not client_alive(conn):
            # The caller left while queued, so it may already be running this
            # command some other way. Nothing has started here; release what
            # the queue gave it, exactly as the remote path withdraws.
            self.budget.finish(run.id, 0, 'lost')
            run.finish(INFRA, state='withdrawn')
            return
        with self.admitting:
            # Under the drain's own lock: a drain either saw this run queued
            # and marked it, or sees it running and waits for it.
            if run.drained:
                self.withdraw_drained(conn, run)
                return
            run.state = 'running'
        run.accepted = now()
        run.save()
        with self.runs_lock:
            self.runs[run.id] = run
        try:
            conn.sendall(dump({'v': VERSION, 't': 'accepted', 'run': run.id, 'lane': 'local',
                               'remote': None, 'reason': run.reason,
                               'reservation_mib': admission.get('reservation_mib'),
                               'cpus_hint': admission.get('cpus_hint')}))
        except OSError:
            # The peer closed between the peek and the send. Nobody was told
            # `accepted`, so nothing starts, and the budget and the worktree
            # hold go back now rather than at the next daemon restart.
            with self.runs_lock:
                self.runs.pop(run.id, None)
            self.budget.finish(run.id, 0, 'lost')
            run.finish(INFRA, state='withdrawn')
            return
        if run.reason:
            run.note('lane: local, reason: ' + run.reason)
        if checked.get('ran') is False and checked.get('reason') != 'no validator declared':
            run.note(checked['reason'] + '; the arguments were not pre-checked')
        threading.Thread(target=self.execute_local,
                         args=(run, repo, job, plan, request, admission, worktree),
                         daemon=True).start()
        self.stream(conn, reader, run, 0)

    def execute_local(self, run, repo, job, plan, request, admission, worktree=None):
        try:
            result = self.local.execute(run, plan, repo=repo['name'], job=job['id'],
                                        worktree=worktree or request['cwd'],
                                        request_env=request.get('env') or {},
                                        admission=admission, note=run.note,
                                        started=run.started, reason=run.reason)
        except Exception as error:                     # noqa: BLE001 - never a silent pass
            self.budget.finish(run.id, 0, 'lost')
            run.note('%s: %s' % (type(error).__name__, error))
            run.finish(70, state='infra_failed')
            return
        run.note('%s in %.1fs (local, peak %s MiB of %s reserved)'
                 % (result['outcome'], result['wall_seconds'], result['peak_mib'],
                    result['reservation_mib']))
        run.suggest(self.hint_for(run, result, worktree or request['cwd']))
        run.finish(result['cli_exit'], state=result['outcome'], result=result)

    def validator_env(self, request, plan):
        base = {'PATH': os.environ.get('PATH', ''), 'HOME': os.environ.get('HOME', ''),
                'PANDORA_ROUTE_DEPTH': '1'}
        for name in plan['env_passthrough']:
            if name in (request.get('env') or {}):
                base[name] = request['env'][name]
        return base

    ATTEMPTS = 6
    BACKOFF = 2.0

    def execute(self, run, repo, plan):
        """Follow the remote run, stream it, bring its outputs home.

        The connection is not the run. A broken SSH conversation says nothing
        about what the worker is doing, so it is retried from the byte offset
        already streamed rather than turned into a verdict; only after the
        worker has been unreachable across every attempt does this become an
        infrastructure failure. And when *this daemon* is the thing going away,
        the run is left alone entirely: its row stays `running` so the next
        daemon resumes it, because a shutdown here is not a fact about the run.

        An `infra_failed` verdict is the other kind of failure, and it may earn
        one resubmission of the same input -- see `retry`. A lost connection
        never does: the run may still be executing, and a second submission
        would run it twice.
        """
        offset = run.consumed()
        failures = 0
        while True:
            try:
                worker = self.worker_for(repo)
                result, offset = worker.follow(
                    run.remote, offset=offset,
                    on_log=lambda chunk: run.stream_in(chunk),
                    should_cancel=run.canceled.is_set,
                    on_status=lambda row: self.observe(run, row))
                run.flush_remote()
                if self.retry(run, repo, plan, result):
                    offset, failures = 0, 0
                    continue
                self.deliver(run, repo, plan, result)
                return
            except (WorkerUnreachable, EngineError) as error:
                if self.stopping.is_set():
                    return
                run.forget_held()
                offset = run.consumed()
                failures += 1
                if failures < self.ATTEMPTS:
                    time.sleep(self.BACKOFF)
                    continue
                run.note('lost the worker while run %s was executing, after %d attempts: %s'
                         % (run.remote, self.ATTEMPTS, error))
                run.finish(70, state='infra_failed')
                return
            except Exception as error:             # noqa: BLE001 - never a silent pass
                run.note('%s: %s' % (type(error).__name__, error))
                run.finish(70, state='infra_failed')
                return

    def reattach(self, run):
        """Same as `execute`, for a run this daemon adopted rather than started.

        It resumes from the remote byte offset the previous daemon recorded, so
        a restart costs a few hundred milliseconds of log and never a duplicated
        line. The outputs to collect are taken from what the run actually
        produced, because the plan that asked for them belonged to a process
        that is gone.
        """
        repo = next((item for item in self.config['repos']
                     if item['name'] == (run.request.get('repo') or '')), None)
        if repo is None:
            run.note('this daemon no longer has an enrollment for run %s' % run.id)
            run.finish(70, state='infra_failed')
            return
        collected = None
        try:
            worker = self.worker_for(repo)
            while True:
                result, _ = worker.follow(run.remote, offset=run.consumed(),
                                          on_log=lambda chunk: run.stream_in(chunk),
                                          should_cancel=run.canceled.is_set,
                                          on_status=lambda row: self.observe(run, row))
                run.flush_remote()
                # The plan is gone, so write-back is judged from the argv.
                if not self.retry(run, repo, None, result):
                    break
            collected = list((result.get('evidence') or {}).get('collected') or [])
            plan = {'outputs': [{'kind': 'artifacts', 'paths': collected}] if collected else []}
            self.deliver(run, repo, plan, result)
        except (WorkerUnreachable, EngineError) as error:
            if self.stopping.is_set():
                return
            run.note('could not re-attach to run %s: %s' % (run.remote, error))
            run.finish(70, state='infra_failed')

    def observe(self, run, row):
        """Remember the engine's phase for this run; `pandora wait` reports it."""
        phase = row.get('state')
        if phase and phase != run.phase:
            run.phase = phase

    # -- one retry, remote to remote ---------------------------------------

    def retry_verdict(self, run, plan, result):
        """(retry?, cause, why-not) for one finished attempt.

        The order is the order of the rules in `engine.retry`: only an infra
        failure, only once, never a canceled or stale run, never once the
        caller has seen the command's own output, and then only a cause the
        table names as retryable. Write-back is allowed only because the first
        attempt is never delivered: its outputs are not collected, so nothing
        it wrote can reach the worktree. When this daemon cannot see whether
        write-back was armed, it does not retry.
        """
        if not isinstance(result, dict) or result.get('outcome') != 'infra_failed':
            return False, None, None
        cause = retries.cause_of(result)
        if run.attempts:
            return False, cause, 'this was already the retry'
        if run.canceled.is_set():
            return False, cause, 'the run was canceled'
        if result.get('cli_exit') == STALE:
            return False, cause, 'the run is stale'
        if run.output_seen():
            return False, cause, ("the command's own output had already reached you, and a "
                                  'partly observed run is not repeatable')
        ok, why = retries.retryable(cause)
        if not ok:
            return False, cause, why
        armed = '--update' in (run.request.get('argv') or [])
        if plan is not None:
            options = plan.get('options')
            armed = armed or bool(plan.get('writeback')) or bool(
                isinstance(options, dict) and options.get('update'))
            if armed and not isinstance(options, dict):
                return False, cause, ('write-back is armed and this daemon cannot read how, so '
                                      'it does not repeat it')
        elif armed:
            return False, cause, ('write-back is armed and this daemon no longer holds the '
                                  'plan that armed it')
        return True, cause, None

    def retry(self, run, repo, plan, result):
        """Resubmit the same input once, or annotate why not. True if resubmitted.

        Recorded in three places so no reader has to infer it: a `pandora:`
        line in the run's log when it happens, `attempts` in `meta.json`, and
        `attempts` plus `retry` in the final `result.json`.
        """
        go, cause, why = self.retry_verdict(run, plan, result)
        attempt = {'remote': run.remote, 'outcome': result.get('outcome') if isinstance(
            result, dict) else None, 'cause': cause,
            'cli_exit': result.get('cli_exit') if isinstance(result, dict) else None,
            'wall_seconds': result.get('wall_seconds') if isinstance(result, dict) else None}
        if go:
            run.note('infrastructure failure before output (%s); retrying once' % cause)
            try:
                submission = self.worker_for(repo).resubmit(
                    run.remote, request_id='%s:%s:retry' % (run.id, run.request.get('job')))
            except (WorkerUnreachable, EngineError) as error:
                go, why = False, 'the retry could not be submitted (%s)' % error
                run.note(why)
            else:
                run.attempts.append(attempt)
                run.restart_remote(submission.run_id)
                run.phase = None
                run.save()
                return True
        if run.attempts:
            first = run.attempts[0]
            result['attempts'] = run.attempts + [attempt]
            result['retry'] = {'retried': True, 'of': first['remote'], 'cause': first['cause']}
            if cause is not None:
                result['hint'] = (
                    'infrastructure failed on both attempts: %s (%s), then %s (%s); neither '
                    'reached a verdict, so this is not a test result -- check the worker '
                    'with pandora stats' % (first['remote'], first['cause'],
                                            run.remote, cause))
        elif cause is not None:
            result['retry'] = {'retried': False, 'cause': cause, 'why': why}
            result['hint'] = ('infrastructure failure (%s) was not retried: %s; nothing '
                              'reached a verdict' % (cause, why))
        return False

    def deliver(self, run, repo, plan, result):
        """Bring outputs back, report what is missing, then exit as the run did."""
        worker = self.worker_for(repo)
        try:
            collected = worker.collect(run.remote, plan, worktree=run.worktree())
        except (TransferError, WorkerUnreachable) as error:
            run.note('could not bring outputs back: %s' % error)
            collected = {'fetched': False, 'missing': []}
        for path in collected.get('missing', []):
            # `missing` is a verdict of its own. It is not zero failures.
            run.note('declared output %s is missing from the run' % path)
        result['outputs'] = collected
        code = result.get('cli_exit', 70)
        if result['outcome'] != 'passed' and code == 0:
            # Belt and braces: a zero from a non-passing run would be a
            # fabricated pass, which is the one thing that must never happen.
            code = 70
        written = self.write_back(run, worker, result)
        if written is not None:
            result['writeback'] = written
            if written['exit'] is not None and code == 0:
                code = written['exit']
        run.note('%s in %.1fs (%s, peak %s MiB, %s)' % (
            result['outcome'], result.get('wall_seconds', 0), result.get('layer'),
            result.get('peak_mib'), result.get('run_id')))
        run.suggest(self.hint_for(run, result, run.worktree()))
        run.finish(code, state=result['outcome'], result=result)

    def write_back(self, run, worker, result):
        """Publish a `--update` run's proposal, or say why not. None for other runs.

        Never raises: a write-back that cannot finish is an exit code and a
        sentence, and the proposal stays in the run directory either way.
        """
        try:
            record = writebacks.settle(
                run.dir, result, run_id=run.id,
                fetch=lambda into: worker.fetch_writeback(run.remote, into),
                freeze=writebacks.default_freeze(self.state / 'digests'))
        except (TransferError, WorkerUnreachable, OSError) as error:
            record = {'state': 'incomplete', 'exit': INFRA, 'written': [], 'conflicts': [],
                      'why': 'the proposal could not be brought home: %s' % error}
        if record is None:
            return None
        for line in writebacks.describe(record):
            run.note(line)
        return record

    def hint_for(self, run, result, worktree):
        """One sentence naming the next action, or nothing. Never fatal.

        Computed after the verdict and before the exit frame, so it is the last
        line the caller sees. A failure to produce it is swallowed: a hint is a
        courtesy and must never be the reason a run reports differently.
        """
        if not isinstance(result, dict):
            return None
        if result.get('hint'):
            return result['hint']       # the engine already had the evidence
        if (result.get('outcome') == 'passed' and not result.get('drifted')
                and not result.get('writeback')):
            # Nothing to advise, and reading the log's tail to prove it would be
            # a cost paid on every green run.
            return None
        try:
            return hints.for_run(result, worktree=worktree, log_path=run.log,
                                 shipped=run.shipped)
        except Exception:                        # noqa: BLE001 - a courtesy, never a verdict
            return None

    # -- streaming ---------------------------------------------------------

    def serve_attach(self, conn, reader, request):
        run = self.live(request.get('run'))
        if run is None:
            run = self.adopt(request.get('run'))
        if run is None:
            self.deny(conn, 'rejected', 'no such run ' + str(request.get('run')))
            return
        # Where the run is right now, said once. It rides on the frame rather
        # than in the stream, because the stream is the run's log byte for byte
        # and a client resumes by offset into it.
        try:
            phase = progress.attach_line(self.state, run)
        except Exception:                        # noqa: BLE001 - a courtesy, never a verdict
            phase = None
        owned = run.done.is_set() or self.live(run.id) is run
        conn.sendall(dump({'v': VERSION, 't': 'accepted', 'run': run.id, 'reattached': True,
                           'remote': run.remote, 'phase': phase,
                           # Whether anything here will bring this run to an exit.
                           'owned': owned}))
        if not owned:
            return                       # nothing here will finish it; do not wait on it
        self.stream(conn, reader, run, int(request.get('from', 0)))

    def adopt(self, run_id, *, cancel=False):
        """A run this daemon did not start is still answerable from disk.

        A finished one is replayed. A live one nobody drives -- its daemon has
        exited -- is taken over first (`resume_row`), so an attached client
        always reaches an exit frame instead of waiting on a row nothing will
        ever finish.
        """
        if not run_id:
            return None
        meta = self.state / 'runs' / run_id / 'meta.json'
        if not meta.is_file():
            return None
        try:
            payload = json.loads(meta.read_text())
        except (OSError, ValueError):
            return None
        with self.adopting:
            driven = self.live(run_id)
            if driven is not None:
                return driven
            if self.orphaned(dict(payload, id=run_id)):
                return self.resume_row(dict(payload, id=run_id), cancel=cancel)
        run = Run(self.state, run_id, payload, on_save=self.saved)
        run.state = payload.get('state', 'done')
        run.exit_code = payload.get('exit_code')
        run.remote = payload.get('remote')
        run.lane = payload.get('lane') or 'remote'
        run.accepted = payload.get('accepted')
        run.phase = payload.get('phase')
        if run.state not in ('queued', 'running'):
            run.done.set()
        else:
            # Live and saved by this daemon, but no longer held: a view only.
            # Registered, it would be a stand-in nothing ever finishes.
            return run
        with self.runs_lock:
            self.runs[run_id] = run
        return run

    def stream(self, conn, reader, run, offset):
        """Copy the run log from `offset` to the client until the exit frame.

        The control channel is read on a second thread so a `cancel` arriving
        mid-run is acted on immediately, and so a client disconnect is observed
        as a detach rather than blocking.
        """
        threading.Thread(target=self.control, args=(reader, run), daemon=True).start()
        try:
            handle = run.log.open('rb')
        except OSError:
            return
        handle.seek(offset)
        try:
            while True:
                chunk = handle.read(1 << 20)
                if chunk:
                    conn.sendall(chunk)
                    continue
                if run.done.is_set() and handle.tell() >= run.size():
                    return
                with run.lock:
                    run.wake.wait(0.2)
        except OSError:
            return                       # client vanished: detach, keep running
        finally:
            handle.close()

    def control(self, reader, run):
        while True:
            try:
                frame = reader.line()
            except (OSError, ValueError):
                return
            if frame is None:
                return                   # disconnect == detach, never cancel
            if frame.get('t') == 'cancel':
                run.canceled.set()
            elif frame.get('t') == 'detach':
                return

    # -- reporting ---------------------------------------------------------

    def saved(self, payload):
        """Every save of a row: the published status (`ps`) and the index (`stats`)."""
        self.status.update(payload)
        self.index.learn(payload)

    def ps(self, limit=20):
        """The published status (#110), with each executing local run's CPU progress.

        The progress is the supervisor's in-memory reading, the same one the
        drain's blockers carry (`Run.activity_fields`); it changes every second
        and is never saved, so it is added here, to a copy, rather than
        published.
        """
        rows = self.status.rows(limit)
        with self.runs_lock:
            driven = {run.id: run for run in list(self.runs.values())
                      + list(self.pending.values()) if not run.done.is_set()}
        out = []
        for row in rows:
            run = driven.get(row.get('id'))
            fields = run.activity_fields() if run is not None else {}
            out.append(dict(row, **fields) if fields else row)
        return out

    def stats(self, window=None):
        """The report, from disk plus at most one engine call.

        The worker's half comes from the health monitor's cache when it is
        fresh, and from one poll when it is not, so typing `pandora stats`
        twice in a minute costs one SSH call rather than two. A worker that is
        known down is not polled at all: the whole point of knowing is not
        paying to rediscover.
        """
        worker = self.health.state()
        if self.config['worker']['host'] and worker.get('worker') == 'unknown':
            worker = self.health.poll()
        since = statistics.parse_since(window)
        return statistics.build(self.state, since=since,
                                worker=worker, pause=self.gate.state(),
                                local=self.budget.snapshot(sample=True),
                                window=window or 'all', client=self.client_name(),
                                runs=self.index.history(since),
                                retention={'keep_days': runindex.keep_seconds(self.config)
                                           / 86400.0, 'oldest': self.index.oldest()})


# <pthread/qos.h>. The accept loop runs on the main thread: at user-interactive
# QoS a Mac at load 90 still schedules it, so a client is answered. A thread
# may start at its creator's class, so each handler thread puts itself back at
# the default: the work a connection does is not what a person waits on first.
QOS_CLASS_USER_INTERACTIVE = 0x21
QOS_CLASS_DEFAULT = 0x15
RAISED = threading.Event()


def set_qos(qos_class, platform=None, cdll=None):
    """Put the calling thread at `qos_class` on macOS. True when it took; never raises."""
    if (platform or sys.platform) != 'darwin':
        return False
    try:
        import ctypes
        system = (cdll or ctypes.CDLL)('/usr/lib/libSystem.B.dylib')
        call = system.pthread_set_qos_class_self_np
        call.argtypes, call.restype = [ctypes.c_uint, ctypes.c_int], ctypes.c_int
        return call(qos_class, 0) == 0
    except Exception:                               # noqa: BLE001 - a priority, never a failure
        return False


def raise_accept_qos(platform=None, cdll=None):
    """The accept thread at user-interactive QoS; remembered so handlers step back down."""
    raised = set_qos(QOS_CLASS_USER_INTERACTIVE, platform=platform, cdll=cdll)
    if raised:
        RAISED.set()
    return raised


def main(argv=None):
    # Before anything else: a daemon still starting had no handlers, and
    # SIGUSR1's default action ends a process, which killed one on 2026-09-24.
    # A SIGTERM that lands while it starts is kept and acted on once it serves.
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    # `kill -USR1 <pid>` writes every thread's stack to the daemon log: the
    # one question a stuck run raises that `ps` cannot answer.
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    raised = raise_accept_qos()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', default=None)
    parser.add_argument('--config', default=None)
    parser.add_argument('--ready-fd', type=int, default=None,
                        help='write one byte here once the socket is listening')
    args = parser.parse_args(argv)
    daemon = Daemon(args.state, config_path=args.config, stopping=stopping).start()
    if args.ready_fd is not None:
        os.write(args.ready_fd, b'1')
    if raised:
        log('accept thread QoS user-interactive')
    log('daemon on %s, worker %s, pid %d, code %s'
        % (daemon.socket_path, daemon.config['worker']['host'] or '(none)', os.getpid(),
           daemon.code[:12]))
    try:
        daemon.serve()
    except KeyboardInterrupt:
        pass
    finally:
        daemon.stop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
