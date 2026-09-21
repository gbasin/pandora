"""Test fixtures: a throwaway state directory, a fake repo, a fake real pnpm.

Nothing here touches the real HOME, the real PATH, ~/.codex, ~/.claude or
launchd.  Every test builds its own tree under a temporary directory and passes
an explicit PATH, which is what makes the whole POC safe to run on a machine
other agents are using.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import protocol
from protocol import dump

REAL_PNPM = '#!/bin/sh\nprintf \'REAL %s\\n\' "$*"\nexit ${PANDORA_FAKE_EXIT:-0}\n'


class Sandbox:
    """A HOME-like sandbox: state dir, repo, shim dir, fake real pnpm."""

    def __init__(self, backend=None, config=None):
        self.root = Path(tempfile.mkdtemp(prefix='pandora-poc-'))
        self.state = self.root / 'state'
        self.state.mkdir(parents=True)
        self.repo = self.root / 'repo'
        (self.repo / '.git').mkdir(parents=True)
        self.realbin = self.root / 'realbin'
        self.realbin.mkdir()
        self.real = self.realbin / 'pnpm'
        self.real.write_text(REAL_PNPM)
        self.real.chmod(0o755)
        settings = {'backend': dict({'mode': 'ok', 'stdout': ['hello\n'], 'stderr': [],
                                     'exit_code': 0}, **(backend or {}))}
        settings.update(config or {})
        (self.state / 'config.json').write_text(json.dumps(settings))
        self.proc = None

    # -- daemon ------------------------------------------------------------

    def start(self, wait=True):
        read_fd, write_fd = os.pipe()
        self.proc = subprocess.Popen(
            [sys.executable, '-B', str(HERE / 'daemon.py'), '--state', str(self.state),
             '--ready-fd', str(write_fd)], pass_fds=(write_fd,))
        os.close(write_fd)
        if wait:
            os.read(read_fd, 1)
        os.close(read_fd)
        return self.proc

    def stop(self, signal_number=None):
        if self.proc and self.proc.poll() is None:
            if signal_number:
                self.proc.send_signal(signal_number)
            else:
                self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)

    def reconfigure(self, backend=None, **top):
        settings = json.loads((self.state / 'config.json').read_text())
        settings.setdefault('backend', {}).update(backend or {})
        settings.update(top)
        (self.state / 'config.json').write_text(json.dumps(settings))

    # -- enrolment ---------------------------------------------------------

    def enrol(self, *, claims=(('test:unit',), ('journeys',), ('journey',)),
              heavy=(('build',), ('lint',)), repo=None, strip=(('run',),)):
        import enrolment
        common = enrolment.common_dir(repo or self.repo)
        enrolment.write(common, enrolment.render(
            socket_path=str(self.state / 'client.sock'), repo='fake',
            claims=[list(c) for c in claims], heavy=[list(h) for h in heavy],
            strip_prefixes=[list(s) for s in strip]))
        # The daemon re-classifies every request, so it must know the same
        # claims the marker advertises.  In production both come from one
        # pandora.toml; here they come from one call.
        self.reconfigure(claims=[list(c) for c in claims])
        return common

    def worktree(self, name='wt'):
        """A linked worktree, whose .git is a FILE pointing into the common dir."""
        path = self.root / name
        path.mkdir()
        target = self.repo / '.git' / 'worktrees' / name
        target.mkdir(parents=True, exist_ok=True)
        (path / '.git').write_text('gitdir: %s\n' % target)
        return path

    # -- invocation --------------------------------------------------------

    def env(self, **extra):
        base = dict(os.environ)
        for key in ('PANDORA_OFF', 'PANDORA_ROUTE_DEPTH', 'PANDORA_STATE', 'PANDORA_TOKEN'):
            base.pop(key, None)
        base['PATH'] = os.pathsep.join([str(HERE / 'bin'), str(self.realbin), '/usr/bin', '/bin'])
        base.update(extra)
        return base

    def pnpm(self, args, cwd=None, timeout=60, shim='pnpm', **extra):
        return subprocess.run([shim, *args], cwd=str(cwd or self.repo), env=self.env(**extra),
                              capture_output=True, timeout=timeout)

    def popen(self, args, cwd=None, **extra):
        return subprocess.Popen(['pnpm', *args], cwd=str(cwd or self.repo), env=self.env(**extra),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    # -- protocol ----------------------------------------------------------

    def ask(self, request, timeout=2.0):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(self.state / 'client.sock'))
        sock.sendall(dump(request))
        reader = protocol.Reader(sock)
        try:
            return reader.line()
        finally:
            sock.close()

    def passthrough(self):
        path = self.state / 'passthrough.jsonl'
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def close(self):
        self.stop()
        shutil.rmtree(self.root, ignore_errors=True)
