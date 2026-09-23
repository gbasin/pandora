"""Per-user client daemon: owns the config, the enrolments, the runs and the socket.

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
import errno
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
from ..errors import (ConfigError, EngineError, NotClaimed, PandoraError, Refused,
                      SnapshotError, TransferError, ValidationRejected, WorkerUnreachable)
from ..engine import retry as retries
from ..exits import INFRA, STALE
from . import enrolment, fallback as policy, hints, placement, progress, settings
from . import stats as statistics
from . import writeback as writebacks
from .health import Monitor
from .local import Budget, Busy, LocalExecutor
from .pressure import Gate, Paused
from .protocol import Reader, VERSION, dump, log_frame
from .worker import Worker


# The directory this daemon imported `pandora` from, said in `daemon.json` and in
# `pong` so `pandora doctor` can tell a daemon started from one checkout from a
# launcher that resolves to another.
PACKAGE_HOME = str(Path(__file__).resolve().parents[2])


def now():
    return time.time()


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

    def __init__(self, state, run_id, request):
        self.id = run_id
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
        self.cancelled = threading.Event()
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
        # `pandora stats` cannot derive from anything else afterwards.
        self.accepted = None
        self.hint = None
        self.shipped = frozenset()      # the snapshot's paths, for the gitignored hint
        # Every earlier attempt at this run, oldest first. Empty unless an
        # infrastructure failure was retried; the caller-visible id stays one.
        self.attempts = list(request.get('attempts') or [])
        # The engine's row state as last seen by `follow`, for `pandora wait`.
        self.phase = None
        # freeze / ship / submit, measured before `accepted`.
        self.pre_accept = request.get('pre_accept') or {}
        # Whether any of the command's own output has been streamed. Kept in a
        # file, because a daemon that restarts mid-run must not forget that the
        # caller has already seen half an answer.
        self.output_mark = self.dir / 'command-output'
        self.seen = self.output_mark.exists()
        self.carry = b''

    def save(self):
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
                   'placement': self.request.get('placement')}
        temp = self.meta.with_suffix('.tmp')
        temp.write_text(json.dumps(payload) + '\n')
        temp.replace(self.meta)

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
        """One chunk from a local child. No offset: there is no remote to resume."""
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

    def note(self, text):
        self.append(log_frame('err', ('pandora: ' + text + '\n').encode()))

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
    def __init__(self, state=None, config_path=None):
        self.config_path = config_path
        self.config = settings.load(config_path)
        self.state = Path(state or self.config['client']['state']).expanduser()
        self.state.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state, 0o700)
        self.socket_path = self.state / 'client.sock'
        self.runs = {}
        self.runs_lock = threading.Lock()
        self.stopping = threading.Event()
        self.server = None
        self.lock_handle = None
        self.repo_configs = {}
        self.repo_stamps = {}
        self.workers = {}
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
                              log=lambda text: sys.stderr.write(text + '\n'))

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
        config merely because their enrolment name is the same.
        """
        path, origin = loader.resolve(root, repo.get('config') or None)
        stamp = (str(path), path.stat().st_mtime_ns)
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
        if key not in self.workers:
            self.workers[key] = self.worker_factory(
                host, state=self.state, engine_root=self.config['worker']['engine_root'],
                persist=self.config['worker']['ssh_persist'])
        return self.workers[key]

    # -- lifecycle ---------------------------------------------------------

    def acquire_lock(self):
        handle = (self.state / 'daemon.lock').open('a+')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise SystemExit('pandora daemon already running for ' + str(self.state))
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
        """After a restart, re-attach to runs that were live when we died.

        The real backend makes this honest in a way the fake one could not: the
        run is on the worker, the engine's supervisor never stopped, and
        re-attaching is asking the engine for the log from an offset. A run the
        engine no longer knows is closed as an infrastructure failure -- never as
        a pass, because this process has observed no test evidence at all.
        """
        resumed = []
        for meta in sorted((self.state / 'runs').glob('*/meta.json')):
            try:
                payload = json.loads(meta.read_text())
            except (OSError, ValueError):
                continue
            if payload.get('state') not in ('queued', 'running') or not payload.get('remote'):
                continue
            run = Run(self.state, payload['id'], payload)
            run.state = 'running'
            run.remote = payload['remote']
            run.accepted = payload.get('accepted')
            run.started = payload.get('started') or run.started
            with self.runs_lock:
                self.runs[run.id] = run
            threading.Thread(target=self.reattach, args=(run,), daemon=True).start()
            resumed.append(run.id)
        return resumed

    def start(self):
        self.acquire_lock()
        (self.state / 'runs').mkdir(exist_ok=True)
        self.clear_stale_socket()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        self.server.listen(64)
        self.resume_interrupted()
        (self.state / 'daemon.json').write_text(json.dumps(
            {'pid': os.getpid(), 'version': VERSION, 'socket': str(self.socket_path),
             'worker': self.config['worker']['host'], 'started': now(),
             'home': PACKAGE_HOME}) + '\n')
        if self.config['worker']['host']:
            self.health.start()
        return self

    def serve(self):
        self.server.settimeout(0.25)
        while not self.stopping.is_set():
            try:
                conn, _ = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def stop(self):
        self.stopping.set()
        self.health.stop()
        for worker in self.workers.values():
            try:
                worker.close()
            except OSError:
                pass
        for closer in (lambda: self.server.close(), lambda: self.socket_path.unlink()):
            try:
                closer()
            except OSError:
                pass
        if self.lock_handle:
            self.lock_handle.close()

    # -- connections -------------------------------------------------------

    def handle(self, conn):
        try:
            self.dispatch(conn)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # What each refusal costs the caller. The client no longer invents an exit
    # code for a daemon verdict, so the verdict has to carry one.
    EXITS = {'queue-timeout': STALE, 'busy': STALE, 'version': INFRA,
             'unauthorized': INFRA, 'local-paused': INFRA, 'fallback-refused': INFRA}

    def deny(self, conn, code, message, exit=None):
        conn.sendall(dump({'v': VERSION, 't': 'error', 'code': code, 'msg': message,
                           'exit': exit if exit is not None else self.EXITS.get(code, 1)}))

    def dispatch(self, conn):
        self.refresh()
        uid = peer_uid(conn)
        if uid is not None and uid != os.getuid():
            self.deny(conn, 'unauthorized', 'socket serves uid %d only' % os.getuid())
            return
        reader = Reader(conn)
        first = reader.line()
        if first is None:
            return
        if first.get('v') != VERSION:
            self.deny(conn, 'version', 'daemon speaks protocol v%d, client sent v%r'
                      % (VERSION, first.get('v')))
            return
        op = first.get('op')
        if op == 'ping':
            conn.sendall(dump({'v': VERSION, 't': 'pong', 'pid': os.getpid(),
                               'worker': self.config['worker']['host'],
                               'runs': len(self.runs), 'home': PACKAGE_HOME,
                               # The cached reading, never a poll: `pandora doctor`
                               # asks this, and a doctor must not change anything.
                               'health': self.health.state()}))
        elif op == 'stats':
            conn.sendall(dump({'v': VERSION, 't': 'stats',
                               'data': self.stats(first.get('since'))}))
        elif op == 'ps':
            self.gate.sample()          # a person asked; answer about now, not about then
            conn.sendall(dump({'v': VERSION, 't': 'ps', 'data': self.ps(),
                               'pause': self.gate.state(),
                               'worker': self.health.state()}))
        elif op == 'run':
            self.serve_run(conn, reader, first)
        elif op == 'attach':
            self.serve_attach(conn, reader, first)
        elif op == 'cancel':
            run = self.runs.get(first.get('run'))
            if run:
                run.cancelled.set()
            conn.sendall(dump({'t': 'ok', 'run': first.get('run')}))
        else:
            self.deny(conn, 'rejected', 'unknown op %r' % op)

    # -- the routed path ---------------------------------------------------

    def plan_for(self, request):
        """Classify one request. Raises the pre-accept refusals; returns a plan.

        The worktree, not the enrolment's root: one enrolment covers every
        worktree of a repository, and the command was typed in exactly one of
        them. The *invocation* directory inside that worktree is what decides
        whether the job can be re-rooted -- see `classify.path_like`.
        """
        cwd = request.get('cwd') or ''
        repo = settings.enrolment_for(self.config, cwd)
        if repo is None:
            repo = self.enrolment_by_git(cwd)
        if repo is None:
            raise NotClaimed('cwd is not inside an enrolled repository')
        root = Path(enrolment.worktree_root(cwd) or repo['root'])
        try:
            config = self.repo_config(repo, root)
        except ConfigError as error:
            raise NotClaimed('no usable config in this worktree: %s' % error) from None
        try:
            relative = Path(cwd).resolve().relative_to(root.resolve())
        except ValueError:
            relative = Path('.')
        here = Path(cwd)
        verdict = classifier.classify(config, request.get('argv') or [],
                                      cwd=str(relative), env=request.get('env') or {},
                                      exists=lambda token: (here / token).exists())
        if verdict['decision'] == 'local':
            raise NotClaimed(verdict['reason'])
        job = config['jobs'][verdict['job']]
        if (config['origin'] == 'enrolment'
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

    def enrolment_by_git(self, cwd):
        """A worktree of an enrolled repository is enrolled.

        Matching on path prefix misses the common case on this machine, where
        every worktree lives beside the repository rather than inside it, so fall
        back to the git common directory -- the same identity the shim's marker
        uses.
        """
        common = enrolment.common_dir(cwd) if cwd else None
        if common is None:
            return None
        for repo in self.config['repos']:
            try:
                enrolled_common = enrolment.common_dir(repo['root'])
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

    def serve_run(self, conn, reader, request):
        try:
            repo, config, verdict = self.plan_for(request)
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
        if verdict.get('rerooted'):
            self.tell(conn, 'running from the worktree root; you typed this in %s and no '
                            'argument names a path' % verdict['rerooted'])

        # The job's `where`, or the caller's `--local`/`--remote`. A request the
        # job cannot honour is refused here, before anything is frozen or queued.
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

        run = Run(self.state, uuid.uuid4().hex[:12],
                  dict(request, repo=repo['name'], job=job['id'], worktree=worktree))
        run.state = 'queued'
        run.save()
        # The health poll's one job. Without it every command typed against a
        # worker that died at lunchtime pays the SSH connect timeout again --
        # 10 s of the 12.2 s the slice measured -- to rediscover the same fact.
        # The decision is identical to `worker-unreachable`; only the price of
        # reaching it differs, and the cause name records which one this was.
        if self.health.known_down():
            self.fall_back(conn, reader, request, repo, job, plan, worktree, 'worker-down',
                           self.health.state().get('reason') or 'the last health poll failed',
                           checked)
            return
        # Every way a submission can fail to proceed, through one door. Each of
        # them is provably non-executing -- that is what earns the fallback --
        # and each of them is decided by the same policy rather than by whatever
        # the client happened to do with that error code.
        beat = Heartbeat(conn).start()
        try:
            try:
                worker = self.worker_for(repo)
                submission = worker.submit(plan=plan, worktree=worktree,
                                           request_id=run.id + ':' + plan['job'],
                                           control=request, progress=beat.say)
            finally:
                # Stopped before any other frame is written: two threads never
                # share the socket.
                left = beat.stop()
        except WorkerUnreachable as error:
            # Paid the timeout once; the next command should not. This asks the
            # question immediately rather than answering it: a worker that
            # refuses a submission may still be up, and only the health call is
            # entitled to say otherwise.
            self.health.recheck()
            self.fall_back(conn, reader, request, repo, job, plan, worktree,
                           'worker-unreachable', str(error), checked)
            return
        except SnapshotError as error:
            self.fall_back(conn, reader, request, repo, job, plan, worktree,
                           'snapshot-failed', str(error), checked)
            return
        except TransferError as error:
            self.fall_back(conn, reader, request, repo, job, plan, worktree,
                           'transfer-failed', str(error), checked)
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
                           cause, str(error), checked)
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
                  checked=None):
        """A remote submission did not proceed. Decide once, then admit or refuse.

        There is no third option, and in particular there is no `exec`. A job
        that may fall back is *admitted into the local lane with its size class*,
        so twelve agents whose worker just died queue behind one budget instead
        of starting twelve browser suites on one Mac -- which is the accident
        this whole path exists to make impossible.
        """
        if (request.get('placement') or {}).get('override') == 'remote':
            # The caller said where. Running it here instead would be the one
            # answer they ruled out, so the fallback lane is not consulted.
            conn.sendall(dump({'v': VERSION, 't': 'error', 'code': 'placement-unavailable',
                               'msg': '%s (%s): --remote was asked for, so this is not run on '
                                      'this Mac. Retry, or drop the override.' % (cause, detail),
                               'exit': INFRA}))
            return
        verdict = policy.decide(cause=cause, size=plan['size'],
                                writeback=bool(plan['options'].get('update'))
                                          or plan.get('writeback'),
                                declared=job['fallback'],
                                notice=(job['fallback'] or {}).get('notice'))
        if verdict['action'] == 'refuse':
            conn.sendall(dump({'v': VERSION, 't': 'error', 'code': 'fallback-refused',
                               'msg': '%s (%s): %s' % (cause, detail, verdict['reason']),
                               'exit': INFRA}))
            return
        self.tell(conn, '%s (%s); %s' % (cause, detail, verdict['reason']))
        self.serve_local(conn, reader, request, repo, job, plan, worktree=worktree,
                         reason='fallback:' + cause, checked=checked)

    # -- the local path ----------------------------------------------------

    def serve_local(self, conn, reader, request, repo, job, plan, *, worktree=None,
                    reason='', checked=None):
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
                  dict(request, repo=repo['name'], job=job['id'], reason=reason))
        run.lane = 'local'
        run.save()
        try:
            conn.sendall(dump({'v': VERSION, 't': 'queued', 'run': run.id}))
            admission = self.budget.admit(run.id, repo=repo['name'], job=job['id'],
                                          cancelled=run.cancelled.is_set,
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
            run.finish(INFRA, state='refused')
            conn.sendall(dump({'v': VERSION, 't': 'error', 'code': 'local-paused',
                               'msg': str(error), 'exit': INFRA}))
            return
        except OSError:
            self.budget.finish(run.id, 0, 'lost')      # the client went away while queued
            run.finish(STALE, state='refused')
            return
        if admission is None:
            self.budget.finish(run.id, 0, 'lost')
            run.finish(STALE, state='refused')
            self.deny(conn, 'queue-timeout',
                      'the local queue did not admit this job within its wait')
            return
        run.state = 'running'
        run.accepted = now()
        run.save()
        with self.runs_lock:
            self.runs[run.id] = run
        conn.sendall(dump({'v': VERSION, 't': 'accepted', 'run': run.id, 'lane': 'local',
                           'remote': None, 'reason': run.reason,
                           'reservation_mib': admission.get('reservation_mib'),
                           'cpus_hint': admission.get('cpus_hint')}))
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
                    should_cancel=run.cancelled.is_set,
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
            run.note('this daemon no longer has an enrolment for run %s' % run.id)
            run.finish(70, state='infra_failed')
            return
        collected = None
        try:
            worker = self.worker_for(repo)
            while True:
                result, _ = worker.follow(run.remote, offset=run.consumed(),
                                          on_log=lambda chunk: run.stream_in(chunk),
                                          should_cancel=run.cancelled.is_set,
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
        failure, only once, never a cancelled or stale run, never once the
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
        if run.cancelled.is_set():
            return False, cause, 'the run was cancelled'
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
        run = self.runs.get(request.get('run'))
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
        conn.sendall(dump({'v': VERSION, 't': 'accepted', 'run': run.id, 'reattached': True,
                           'remote': run.remote, 'phase': phase}))
        self.stream(conn, reader, run, int(request.get('from', 0)))

    def adopt(self, run_id):
        """A finished run this daemon did not start is still answerable from disk."""
        if not run_id:
            return None
        meta = self.state / 'runs' / run_id / 'meta.json'
        if not meta.is_file():
            return None
        try:
            payload = json.loads(meta.read_text())
        except (OSError, ValueError):
            return None
        run = Run(self.state, run_id, payload)
        run.state = payload.get('state', 'done')
        run.exit_code = payload.get('exit_code')
        run.remote = payload.get('remote')
        run.lane = payload.get('lane') or 'remote'
        run.accepted = payload.get('accepted')
        run.phase = payload.get('phase')
        if run.state not in ('queued', 'running'):
            run.done.set()
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
                run.cancelled.set()
            elif frame.get('t') == 'detach':
                return

    # -- reporting ---------------------------------------------------------

    def ps(self):
        rows = []
        for meta in sorted((self.state / 'runs').glob('*/meta.json')):
            try:
                rows.append(json.loads(meta.read_text()))
            except (OSError, ValueError):
                continue
        rows.sort(key=lambda row: row.get('started', 0), reverse=True)
        return rows

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
        return statistics.build(self.state, since=statistics.parse_since(window),
                                worker=worker, pause=self.gate.state(),
                                local=self.budget.snapshot(sample=True),
                                window=window or 'all')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', default=None)
    parser.add_argument('--config', default=None)
    parser.add_argument('--ready-fd', type=int, default=None,
                        help='write one byte here once the socket is listening')
    args = parser.parse_args(argv)
    daemon = Daemon(args.state, config_path=args.config).start()
    signal.signal(signal.SIGTERM, lambda *_: daemon.stopping.set())
    if args.ready_fd is not None:
        os.write(args.ready_fd, b'1')
    sys.stderr.write('pandora: daemon on %s, worker %s\n'
                     % (daemon.socket_path, daemon.config['worker']['host'] or '(none)'))
    sys.stderr.flush()
    try:
        daemon.serve()
    except KeyboardInterrupt:
        pass
    finally:
        daemon.stop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
