#!/usr/bin/env python3
"""Per-user Pandora client daemon: owns config, enrolment, runs and the socket.

In production this process would also own the SSH ControlMaster, the snapshot
and the transfer.  Here the backend is faked in-process (see ``backend.py``),
because what this POC measures is client ergonomics and side effects, not remote
execution.

Three invariants the rest of the design leans on:

* One daemon per state directory, enforced by an exclusive lock on
  ``daemon.lock`` -- not by the socket, which is a file that survives a crash.
* Every run's output is appended to a *file* as framed NDJSON, and clients are
  served by copying byte ranges of that file.  Memory stays flat for a 50 MB run
  and re-attach is a byte offset, not a replay buffer.
* A client that disappears detaches; only an explicit ``cancel`` stops a run.
"""
import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import struct
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import protocol
from protocol import dump
import backend
import claims

DEFAULT_CONFIG = {
    'backend': {'mode': 'ok', 'accept_delay_ms': 0, 'delay_ms': 0, 'exit_code': 0,
                'stdout': ['ok\n'], 'stderr': [], 'bytes': 0, 'chunk': 65536, 'signal': None},
    'fallback_slots': 2,
    'fallback_wait_seconds': 0,
    'require_token': False,
    'token': None,
    'repos': [],
}


def now():
    return time.time()


class Run:
    """One routed attempt.  Its log file is the single source of truth."""

    def __init__(self, state, run_id, request):
        self.id = run_id
        self.request = request
        self.dir = state / 'runs' / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log = self.dir / 'log'
        # Create it empty and immediately: a client that attaches before the
        # first frame exists must block on an empty file, not fail to open one.
        self.log.touch()
        self.meta = self.dir / 'meta.json'
        self.lock = threading.Lock()
        self.wake = threading.Condition(self.lock)
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.exit_code = None
        self.state = 'queued'
        self.started = now()

    def save(self):
        payload = {'id': self.id, 'state': self.state, 'exit_code': self.exit_code,
                   'argv': self.request.get('argv'), 'cwd': self.request.get('cwd'),
                   'started': self.started, 'updated': now()}
        temp = self.meta.with_suffix('.tmp')
        temp.write_text(json.dumps(payload) + '\n')
        temp.replace(self.meta)

    def append(self, frame):
        with self.lock:
            with self.log.open('ab') as handle:
                handle.write(frame)
            self.wake.notify_all()

    def finish(self, code, *, state='done'):
        self.exit_code = code
        self.state = state
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
    ``struct xucred``.  Linux has SO_PEERCRED returning ``struct ucred``.
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
    def __init__(self, state, config=None):
        self.state = Path(state)
        self.state.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state, 0o700)
        self.socket_path = self.state / 'client.sock'
        self.config = dict(DEFAULT_CONFIG)
        self.config.update(config or self.load_config())
        self.runs = {}
        self.runs_lock = threading.Lock()
        self.stopping = threading.Event()
        self.server = None
        self.lock_handle = None
        self.passthrough = self.state / 'passthrough.jsonl'
        self.repo_config = None
        self.repo_config_source = None
        self.load_repo_config()

    # -- lifecycle ---------------------------------------------------------

    def load_config(self):
        path = self.state / 'config.json'
        if path.is_file():
            return json.loads(path.read_text())
        return {}

    def refresh_config(self):
        """Re-read config.json per connection: enrolling a repo, changing the
        fallback budget or pointing at a different worker must not need a
        restart, because a restart drops every attached client."""
        try:
            fresh = self.load_config()
        except (OSError, ValueError):
            return
        merged = dict(DEFAULT_CONFIG)
        merged.update(fresh)
        merged['backend'] = dict(DEFAULT_CONFIG['backend'], **fresh.get('backend', {}))
        self.config = merged
        self.load_repo_config()

    def load_repo_config(self):
        """The repo-owned pandora.toml, loaded once and kept.

        ~50 ms to load, which is why the shim gets a derived claim list instead
        of the configuration itself.  Reloaded only when the pointer changes.
        """
        spec = self.config.get('repo_config')
        if not spec:
            self.repo_config, self.repo_config_source = None, None
            return
        source = (spec.get('toml'), spec.get('root'))
        if source == self.repo_config_source and self.repo_config is not None:
            return
        self.repo_config = claims.load_config(spec['toml'], root=spec.get('root'))
        self.repo_config_source = source

    def acquire_lock(self):
        """Exclusive, so a second daemon refuses instead of stealing the socket."""
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
        """Remove a socket file no one is listening on.  Safe: we hold the lock."""
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
        """After a restart, adopt runs that were live when the daemon died.

        The fake backend is deterministic, so it can continue from the number of
        frames already on disk.  A real backend would re-attach to the worker;
        one that cannot must close the run as an infrastructure failure rather
        than pretend it produced test evidence.
        """
        resumed = []
        for meta in sorted((self.state / 'runs').glob('*/meta.json')):
            try:
                payload = json.loads(meta.read_text())
            except (OSError, ValueError):
                continue
            if payload.get('state') not in ('queued', 'running'):
                continue
            run = Run(self.state, payload['id'], {'argv': payload.get('argv'),
                                                  'cwd': payload.get('cwd')})
            run.state = 'running'
            with self.runs_lock:
                self.runs[run.id] = run
            threading.Thread(target=backend.execute, args=(self, run), kwargs={'resume': True},
                             daemon=True).start()
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
            {'pid': os.getpid(), 'version': protocol.VERSION,
             'socket': str(self.socket_path), 'started': now()}) + '\n')
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
        try:
            self.server.close()
        except OSError:
            pass
        try:
            self.socket_path.unlink()
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

    def deny(self, conn, code, message):
        conn.sendall(dump({'v': protocol.VERSION, 't': 'error', 'code': code, 'msg': message}))

    def dispatch(self, conn):
        self.refresh_config()
        uid = peer_uid(conn)
        if uid is not None and uid != os.getuid():
            self.deny(conn, 'unauthorized', 'socket serves uid %d only' % os.getuid())
            return
        reader = protocol.Reader(conn)
        first = reader.line()
        if first is None:
            return
        if first.get('v') != protocol.VERSION:
            self.deny(conn, 'version', 'daemon speaks protocol v%d, client sent v%r'
                      % (protocol.VERSION, first.get('v')))
            return
        if self.config['require_token'] and first.get('token') != self.config['token']:
            self.deny(conn, 'unauthorized', 'token rejected')
            return
        op = first.get('op')
        if op == 'ping':
            conn.sendall(dump({'v': protocol.VERSION, 't': 'pong', 'pid': os.getpid(),
                               'backend': self.config['backend']['mode'],
                               'runs': len(self.runs)}))
        elif op == 'stats':
            conn.sendall(dump({'v': protocol.VERSION, 't': 'stats', 'data': self.stats()}))
        elif op == 'run':
            self.serve_run(conn, reader, first)
        elif op == 'attach':
            self.serve_attach(conn, reader, first)
        elif op == 'cancel':
            run = self.runs.get(first.get('run'))
            if run:
                run.cancelled.set()
            conn.sendall(dump({'t': 'ok'}))
        else:
            self.deny(conn, 'rejected', 'unknown op %r' % op)

    def serve_run(self, conn, reader, request):
        decision = claims.decide(self, request)
        if decision['decision'] != 'remote':
            self.deny(conn, decision.get('code', 'rejected'), decision['message'])
            return
        mode = self.config['backend']['mode']
        if mode in ('unreachable', 'queue-timeout', 'admission-refused'):
            # Decided before acceptance, so the client may still run locally.
            time.sleep(self.config['backend'].get('accept_delay_ms', 0) / 1000)
            self.deny(conn, {'unreachable': 'worker-unreachable'}.get(mode, mode),
                      'fake backend mode ' + mode)
            return
        if mode == 'hang':
            self.stopping.wait(30)
            return
        run = Run(self.state, uuid.uuid4().hex[:12], request)
        with self.runs_lock:
            self.runs[run.id] = run
        run.state = 'running'
        run.save()
        conn.sendall(dump({'v': protocol.VERSION, 't': 'accepted', 'run': run.id,
                           'queue_position': 0}))
        threading.Thread(target=backend.execute, args=(self, run), daemon=True).start()
        if mode == 'accept-then-drop':
            time.sleep(0.02)
            conn.close()
            return
        self.stream(conn, reader, run, 0)

    def serve_attach(self, conn, reader, request):
        run = self.runs.get(request.get('run'))
        if run is None:
            self.deny(conn, 'rejected', 'no such run ' + str(request.get('run')))
            return
        conn.sendall(dump({'v': protocol.VERSION, 't': 'accepted', 'run': run.id,
                           'reattached': True}))
        self.stream(conn, reader, run, int(request.get('from', 0)))

    def stream(self, conn, reader, run, offset):
        """Copy the run log from ``offset`` to the client until the exit frame.

        The control channel is read on a second thread so a ``cancel`` arriving
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

    def stats(self):
        runs = []
        for meta in sorted((self.state / 'runs').glob('*/meta.json')):
            try:
                runs.append(json.loads(meta.read_text()))
            except (OSError, ValueError):
                continue
        rows = []
        if self.passthrough.is_file():
            for line in self.passthrough.read_text().splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        return {'runs': runs, 'passthrough': rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--ready-fd', type=int, default=None,
                        help='write one byte here once the socket is listening')
    args = parser.parse_args(argv)
    daemon = Daemon(args.state).start()
    signal.signal(signal.SIGTERM, lambda *_: daemon.stopping.set())
    if args.ready_fd is not None:
        os.write(args.ready_fd, b'1')
    try:
        daemon.serve()
    except KeyboardInterrupt:
        pass
    finally:
        daemon.stop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
