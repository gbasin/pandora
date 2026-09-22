"""One worker, over SSH, without the client daemon's machinery.

`pandora.client.worker.Worker` is the daemon's conversation with a worker: it
freezes trees, ships sources and streams runs. Provisioning needs none of that
and must work on a machine that has never had a run on it, so it takes the two
pieces it does need -- the control-master link and the content-addressed engine
bundle -- and nothing else.
"""
import json
import shlex
import subprocess

from ..engine import bundle
from ..errors import EngineError, WorkerUnreachable
from ..snapshot import transfer


class Remote:
    def __init__(self, host, *, control_dir, engine_root='pandora-engine', persist='10m'):
        if not host:
            raise WorkerUnreachable('no worker host was given; pass --host or set [worker] host')
        self.host = host
        self.link = transfer.Link(host, control_dir, persist=persist)
        self.engine_root = engine_root
        self._root = engine_root if engine_root.startswith('/') else None
        self._bundle = None

    def home(self):
        _, out, _ = self.link.run(['sh', '-c', 'cd "$HOME" && pwd'], timeout=60)
        return out.strip().rstrip('/')

    def root(self):
        if self._root is None:
            self._root = self.home() + '/' + self.engine_root.lstrip('./')
        return self._root

    def expand(self, path):
        """`~/pandora` as the worker sees it, resolved once, on the worker."""
        return self.home() + path[1:] if path.startswith('~/') else path

    def bundle_path(self):
        if self._bundle is None:
            self._bundle = bundle.ensure(self.link, self.root())
        return self._bundle['path']

    def call(self, module, argv, *, stdin=None, timeout=300, check=True):
        """One JSON-answering subcommand, inside the shipped bundle."""
        path = self.bundle_path()
        command = 'cd %s && PYTHONPATH=%s python3 -m %s %s' % (
            shlex.quote(path), shlex.quote(path), module,
            ' '.join(shlex.quote(str(item)) for item in argv))
        proc = subprocess.run(['ssh', *self.link.options, self.host, command],
                              input=stdin.encode() if isinstance(stdin, str) else stdin,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        err = (proc.stderr or b'').decode('utf-8', 'replace')
        if proc.returncode == 255:
            raise WorkerUnreachable('ssh %s: %s' % (self.host, err.strip()[:400] or 'no route'))
        out = (proc.stdout or b'').decode('utf-8', 'replace').strip()
        if check and proc.returncode != 0 and not out:
            raise EngineError('%s %s failed (%d): %s'
                              % (module, argv[0], proc.returncode, err.strip()[:600]))
        if not out:
            raise EngineError('%s %s said nothing; stderr: %s' % (module, argv[0], err.strip()[:400]))
        try:
            return json.loads(out.splitlines()[-1])
        except ValueError:
            raise EngineError('%s %s returned non-JSON: %s'
                              % (module, argv[0], out[:400])) from None

    def worker(self, argv, **kwargs):
        return self.call('pandora.worker.service', argv, **kwargs)

    def engine(self, argv, **kwargs):
        return self.call('pandora.engine.service', argv, **kwargs)

    def put(self, path, text):
        """Write one small file on the worker, without an rsync."""
        self.link.run(['sh', '-c', 'mkdir -p "$(dirname %s)" && cat > %s'
                       % (shlex.quote(path), shlex.quote(path))],
                      stdin=text.encode(), timeout=120)
        return path

    def close(self):
        self.link.close()
