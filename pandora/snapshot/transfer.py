"""Put a frozen worktree into the worker's per-repo source cache.

One rsync, from the live worktree, with the manifest's names on stdin so that
nothing outside the manifest can travel -- not `.env`, not `.git`, not a
worktree nested inside this one. `--link-dest` against the repository's previous
input makes an unchanged file a hard link rather than a copy, so the second run
of a repository ships only its diff and the cache costs one tree plus deltas.

The cache is content-addressed by `input_id`, which is the manifest digest, so:

* two worktrees with identical tracked content share one cache entry and the
  engine can say `same_tree_as`;
* a run's source is immutable for the life of the run, which is what lets a
  second run start while the first is still reading;
* each transfer stages in its own `.partial.*` directory; publishing is a
  rename, so an interrupted transfer cannot expose an incomplete tree.

SSH connection reuse belongs here rather than in the caller: rsync and the
engine calls are the same conversation with the same host, and a ControlMaster
turns the second and later round trips from ~90 ms into ~1 ms.
"""
import os
import shlex
import subprocess
from pathlib import Path

from ..errors import TransferError, WorkerUnreachable

CONNECT_TIMEOUT = 10


def ssh_options(control_path, *, persist='10m'):
    """The ControlMaster arrangement every SSH call in a run shares."""
    return ['-o', 'BatchMode=yes',
            '-o', 'ConnectTimeout=%d' % CONNECT_TIMEOUT,
            '-o', 'ControlMaster=auto',
            '-o', 'ControlPath=' + str(control_path) + '/ssh-%C',
            '-o', 'ControlPersist=' + persist,
            '-o', 'ServerAliveInterval=15',
            '-o', 'ServerAliveCountMax=3']


def control_dir_for(anchor):
    """A short, private directory for the control socket.

    A unix socket path is capped at ~104 bytes and ssh appends a temporary
    suffix of its own, so `<state>/ssh/ssh-%C` overflows for any state directory
    inside a home directory of normal length -- and so does macOS's own TMPDIR,
    which is a 49-character path under /var/folders. `/tmp` is used when it is
    writable, which is the only place short enough on this platform, with a name
    taken from a digest of the state directory so that two daemons never share a
    socket.
    """
    import hashlib
    import tempfile
    tag = hashlib.sha256(str(anchor).encode()).hexdigest()[:8]
    base = Path('/tmp') if os.path.isdir('/tmp') and os.access('/tmp', os.W_OK) \
        else Path(tempfile.gettempdir())
    path = base / ('pandora-%d-%s' % (os.getuid(), tag))
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


class Link:
    """One host, one control socket, every call in one conversation."""

    def __init__(self, host, control_dir, *, persist='10m'):
        self.host = host
        self.anchor = Path(control_dir)
        self.control_dir = control_dir_for(self.anchor)
        self.options = ssh_options(self.control_dir, persist=persist)

    @property
    def rsh(self):
        return 'ssh ' + ' '.join(shlex.quote(item) for item in self.options)

    def run(self, argv, *, stdin=None, timeout=120, check=True):
        """One remote command. `argv` is a list; it is quoted, never a shell line."""
        command = ' '.join(shlex.quote(item) for item in argv)
        proc = subprocess.run(['ssh', *self.options, self.host, command],
                              input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
        out = (proc.stdout or b'').decode('utf-8', 'replace')
        err = (proc.stderr or b'').decode('utf-8', 'replace')
        if proc.returncode == 255:
            raise WorkerUnreachable('ssh %s: %s' % (self.host, err.strip()[:400] or 'no route'))
        if check and proc.returncode != 0:
            raise TransferError('remote %s failed (%d): %s'
                                % (argv[0], proc.returncode, err.strip()[:600]))
        return proc.returncode, out, err

    def feed(self, script, args=(), *, stdin=b'', timeout=600, check=True):
        """Run a Python program on the worker with nothing installed there.

        The program travels on the *command line* (`python3 -c`) rather than on
        stdin, because stdin is where its input goes: handing the interpreter
        its own source over stdin consumes the channel the program then wants to
        read from, which is a mistake that shows up as a checksum that never
        matches.
        """
        command = ' '.join(['python3', '-c', shlex.quote(script)]
                           + [shlex.quote(str(item)) for item in args])
        proc = subprocess.run(['ssh', *self.options, self.host, command],
                              input=stdin, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout)
        out = (proc.stdout or b'').decode('utf-8', 'replace')
        err = (proc.stderr or b'').decode('utf-8', 'replace')
        if proc.returncode == 255:
            raise WorkerUnreachable('ssh %s: %s' % (self.host, err.strip()[:400] or 'no route'))
        if check and proc.returncode != 0:
            raise TransferError('remote python failed (%d): %s'
                                % (proc.returncode, err.strip()[:800]))
        return proc.returncode, out, err

    def close(self):
        subprocess.run(['ssh', *self.options, '-O', 'exit', self.host],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def cache_paths(root, repo, input_id):
    base = '%s/src/%s' % (root, repo)
    return {'base': base, 'final': '%s/%s' % (base, input_id),
            'partial': '%s/%s.partial' % (base, input_id),
            'latest': '%s/latest' % base}


def send(link, manifest, *, worktree, root, repo, input_id, timeout=1800, on_send=None):
    """Place this input in the worker's source cache. Idempotent.

    `on_send` is called once, just before rsync starts, and only when there is
    something to send: a cache hit is silent here because the accepted line
    already says so.

    Returns {'path', 'reused', 'link_dest', 'seconds', 'files'}.
    """
    paths = cache_paths(root, repo, input_id)
    code, out, _ = link.run(['sh', '-c',
                             'test -e %s && echo present || echo absent' % shlex.quote(paths['final'])],
                            timeout=60)
    if out.strip() == 'present':
        return {'path': paths['final'], 'reused': True, 'link_dest': None,
                'seconds': 0.0, 'files': len(manifest)}
    _, previous, _ = link.run(['sh', '-c', 'readlink %s 2>/dev/null || true'
                               % shlex.quote(paths['latest'])], timeout=60)
    link_dest = previous.strip() or None
    # Fresh worktrees have new mtimes for identical content. Match by checksum
    # and omit timestamp preservation so link-dest can reuse immutable files.
    # Checksums also catch equal-size edits whose mtime did not change.
    argv = ['rsync', '-a', '--no-times', '--checksum', '--delete',
            '--files-from=-', '--from0', '-e', link.rsh]
    if link_dest and link_dest != paths['final']:
        argv += ['--link-dest=' + link_dest]
    names = b'\0'.join(record['path'].encode() for record in manifest) + b'\0'
    _, staged, _ = link.feed('''
import os, sys, tempfile
base, input_id = sys.argv[1:]
os.makedirs(base, exist_ok=True)
stage = tempfile.mkdtemp(prefix=input_id + '.partial.', dir=base)
# Isolated container UIDs must be able to read the mounted source tree.
os.chmod(stage, 0o755)
print(stage)
''', (paths['base'], input_id), timeout=120)
    stage = staged.rstrip('\n')
    argv += [str(worktree) + '/', '%s:%s/' % (link.host, stage)]
    import time
    try:
        if on_send is not None:
            on_send()
        started = time.monotonic()
        proc = subprocess.run(argv, input=names, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout)
        if proc.returncode != 0:
            raise TransferError('rsync to %s failed (%d): %s'
                                % (link.host, proc.returncode,
                                   (proc.stderr or b'').decode('utf-8', 'replace').strip()[:600]))
        # A concurrent attempt may already have published this input. Keep that
        # completed tree, then atomically point latest at it. Each attempt uses
        # a separate temporary symlink, including across different inputs.
        link.feed('''
import errno, os, sys, uuid
stage, final, latest = sys.argv[1:]
if not os.path.lexists(final):
    try:
        os.rename(stage, final)
    except OSError as exc:
        if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
            raise
        if not os.path.isdir(final):
            raise
temporary = latest + '.tmp.' + uuid.uuid4().hex
try:
    os.symlink(final, temporary)
    os.replace(temporary, latest)
finally:
    if os.path.lexists(temporary):
        os.unlink(temporary)
''', (stage, paths['final'], paths['latest']), timeout=120)
    finally:
        link.feed('''
import shutil, sys
shutil.rmtree(sys.argv[1], ignore_errors=True)
''', (stage,), timeout=120)
    return {'path': paths['final'], 'reused': False, 'link_dest': link_dest,
            'seconds': round(time.monotonic() - started, 2), 'files': len(manifest)}


def fetch(link, remote_dir, into, *, paths=None, timeout=600):
    """Bring a run's declared outputs back into the worktree-relative location."""
    into = Path(into)
    into.mkdir(parents=True, exist_ok=True)
    argv = ['rsync', '-a', '-e', link.rsh]
    stdin = None
    if paths:
        argv += ['--files-from=-', '--from0']
        stdin = b'\0'.join(item.encode() for item in paths) + b'\0'
    argv += ['%s:%s/' % (link.host, remote_dir), str(into) + '/']
    proc = subprocess.run(argv, input=stdin, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout)
    if proc.returncode != 0:
        raise TransferError('rsync from %s failed (%d): %s'
                            % (link.host, proc.returncode,
                               (proc.stderr or b'').decode('utf-8', 'replace').strip()[:600]))
    return (proc.stdout or b'').decode('utf-8', 'replace')
