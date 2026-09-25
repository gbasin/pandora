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
import sys
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
        # None until the first call asks whether a master already listens.
        self.owns_master = None

    def _probe(self):
        """Before the first call: is a master already on this control path?

        If one is, another process started it and its sessions ride on it, so
        `close()` must leave it alone. If none is, this Link's first call starts
        it. `-O check` talks only to the local socket, never to the host.
        """
        if self.owns_master is not None:
            return
        try:
            proc = subprocess.run(['ssh', *self.options, '-O', 'check', self.host],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=CONNECT_TIMEOUT, check=False)
            self.owns_master = proc.returncode != 0
        except (OSError, subprocess.TimeoutExpired):
            # Unknown is treated as not ours: a master left running expires after
            # ControlPersist, while one exited under a peer kills its transfers.
            self.owns_master = False

    @property
    def rsh(self):
        return 'ssh ' + ' '.join(shlex.quote(item) for item in self.options)

    def _ssh(self, command, stdin, timeout):
        try:
            return subprocess.run(['ssh', *self.options, self.host, command],
                                  input=stdin, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired:
            # A worker that does not answer in time is unreachable for this
            # call; a bare TimeoutExpired is an error no caller handles.
            raise WorkerUnreachable('ssh %s: no answer within %d s'
                                    % (self.host, timeout)) from None

    def run(self, argv, *, stdin=None, timeout=120, check=True):
        """One remote command. `argv` is a list; it is quoted, never a shell line."""
        command = ' '.join(shlex.quote(item) for item in argv)
        self._probe()
        proc = self._ssh(command, stdin, timeout)
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
        self._probe()
        proc = self._ssh(command, stdin, timeout)
        out = (proc.stdout or b'').decode('utf-8', 'replace')
        err = (proc.stderr or b'').decode('utf-8', 'replace')
        if proc.returncode == 255:
            raise WorkerUnreachable('ssh %s: %s' % (self.host, err.strip()[:400] or 'no route'))
        if check and proc.returncode != 0:
            raise TransferError('remote python failed (%d): %s'
                                % (proc.returncode, err.strip()[:800]))
        return proc.returncode, out, err

    def close(self):
        """Exit the master only if this Link started it.

        `-O exit` ends every session multiplexed on the master, not just this
        Link's: a master another process started carries that process's
        in-flight rsyncs and engine calls, and exiting it fails them mid-stream.
        """
        if not self.owns_master:
            return
        subprocess.run(['ssh', *self.options, '-O', 'exit', self.host],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        self.owns_master = None


# The fixed `python3 -c` programs a send feeds the worker. Named, rather than
# inline at the call, because a shared worker's gateway allowlists exactly these
# scripts by sha256 (`pandora.engine.bundle.feed_digests`); a change to any of
# them changes its digest, so an edited feed needs a re-provisioned allowlist.
FEEDS = {
    # Is the input already in the cache? Presence and the renewed grace are one
    # locked fact, because the collector uses this same lock.
    'probe': '''
import fcntl, os, sys
root, final = sys.argv[1:]
os.makedirs(root, exist_ok=True)
with open(os.path.join(root, 'admission.lock'), 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    if os.path.isdir(final):
        os.utime(final, None)
        print('present')
    else:
        print('absent')
''',
    # The newest published snapshots, for --link-dest dedup.
    'bases': '''
import os, sys
base, final = sys.argv[1:]
os.makedirs(base, exist_ok=True)
entries = []
for name in os.listdir(base):
    path = os.path.join(base, name)
    try:
        if (os.path.isdir(path) and not os.path.islink(path)
                and '.partial.' not in name and os.path.realpath(path) != final):
            entries.append((os.path.getmtime(path), path))
    except FileNotFoundError:
        # GC can remove an entry between the directory check and the stat.
        continue
entries.sort(reverse=True)
for _, path in entries[:4]:
    print(path)
''',
    'stage': '''
import os, sys, tempfile
base, input_id = sys.argv[1:]
os.makedirs(base, exist_ok=True)
stage = tempfile.mkdtemp(prefix=input_id + '.partial.', dir=base)
# Isolated container UIDs must be able to read the mounted source tree.
os.chmod(stage, 0o755)
print(stage)
''',
    'publish': '''
import errno, fcntl, os, sys, uuid
stage, final, latest, root = sys.argv[1:]
with open(os.path.join(root, 'admission.lock'), 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    if not os.path.lexists(final):
        try:
            os.rename(stage, final)
        except OSError as exc:
            if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
            if not os.path.isdir(final):
                raise
    # A concurrent publisher may have won with an old cached tree. Renew its
    # grace too, before releasing the lock or acknowledging the shipment.
    os.utime(final, None)
temporary = latest + '.tmp.' + uuid.uuid4().hex
try:
    os.symlink(final, temporary)
    os.replace(temporary, latest)
finally:
    if os.path.lexists(temporary):
        os.unlink(temporary)
''',
    'clean': '''
import shutil, sys
shutil.rmtree(sys.argv[1], ignore_errors=True)
''',
}


def cache_paths(root, repo, input_id):
    base = '%s/src/%s' % (root, repo)
    return {'base': base, 'final': '%s/%s' % (base, input_id),
            'partial': '%s/%s.partial' % (base, input_id),
            'latest': '%s/latest' % base}


def log_stderr(text):
    sys.stderr.write('pandora: ' + text + '\n')
    sys.stderr.flush()


def _text(data):
    if isinstance(data, str):
        return data
    return (data or b'').decode('utf-8', 'replace')


def keep_stderr(path, data):
    """rsync's whole stderr beside the run, when there is any. Never fails a send."""
    if path is None or not data:
        return
    try:
        Path(path).write_text(_text(data))
    except OSError:
        pass


def send(link, manifest, *, worktree, root, repo, input_id, timeout=1800, on_send=None,
         log=None, stderr_path=None):
    """Place this input in the worker's source cache. Idempotent.

    `on_send` is called once, just before rsync starts, and only when there is
    something to send: a cache hit is silent here because the accepted line
    already says so. `log` receives what is worth a line but not a failure: a
    staging directory that could not be removed. `stderr_path`, when given,
    receives rsync's whole stderr; the exception carries only its first 600
    characters, and on 2026-09-24 the line that explained the failure was not
    among them. A TransferError from rsync carries `rsync_exit` (None on a
    timeout) and `stderr`.

    Returns {'path', 'reused', 'link_dests', 'seconds', 'files'}.
    """
    log = log or log_stderr
    paths = cache_paths(root, repo, input_id)
    # The collector uses the engine's admission lock too. Refresh the grace
    # period atomically with checking presence: a cache hit is a new use even
    # though no file content changes.
    _, out, _ = link.feed(FEEDS['probe'], (root, paths['final']), timeout=60)
    if out.strip() == 'present':
        return {'path': paths['final'], 'reused': True, 'link_dests': [],
                'seconds': 0.0, 'files': len(manifest)}
    # Dedup bases are the newest snapshots still on the worker, not only the
    # one `latest` names: with several worktrees `latest` hops between
    # unrelated trees, and a ship then pays a full copy -- measured 0% reuse
    # against a divergent base, ~90% against the worktree's own previous tree.
    # rsync tries each --link-dest in order, so four bases cost nothing when
    # none of them matches.
    _, listed, _ = link.feed(FEEDS['bases'], (paths['base'], paths['final']), timeout=60)
    link_dests = [line.strip() for line in listed.splitlines() if line.strip()]
    # Fresh worktrees have new mtimes for identical content. Match by checksum
    # and omit timestamp preservation so link-dest can reuse immutable files.
    # Checksums also catch equal-size edits whose mtime did not change.
    argv = ['rsync', '-a', '--no-times', '--checksum', '--delete',
            '--files-from=-', '--from0', '-e', link.rsh]
    for link_dest in link_dests:
        argv += ['--link-dest=' + link_dest]
    names = b'\0'.join(record['path'].encode() for record in manifest) + b'\0'
    _, staged, _ = link.feed(FEEDS['stage'], (paths['base'], input_id), timeout=120)
    stage = staged.rstrip('\n')
    argv += [str(worktree) + '/', '%s:%s/' % (link.host, stage)]
    import time
    try:
        if on_send is not None:
            on_send()
        started = time.monotonic()
        try:
            proc = subprocess.run(argv, input=names, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired as expired:
            # A TransferError, so the fallback policy decides: a bare
            # TimeoutExpired escaped every handler up to the connection thread.
            keep_stderr(stderr_path, expired.stderr)
            error = TransferError('rsync to %s timed out after %d s' % (link.host, timeout))
            error.rsync_exit, error.stderr = None, _text(expired.stderr)
            raise error from None
        keep_stderr(stderr_path, proc.stderr)
        if proc.returncode != 0:
            error = TransferError('rsync to %s failed (%d): %s'
                                  % (link.host, proc.returncode, _text(proc.stderr).strip()[:600]))
            error.rsync_exit, error.stderr = proc.returncode, _text(proc.stderr)
            raise error
        # A concurrent attempt may already have published this input. Keep that
        # completed tree, then atomically point latest at it. Each attempt uses
        # a separate temporary symlink, including across different inputs.
        link.feed(FEEDS['publish'], (stage, paths['final'], paths['latest'], root),
                  timeout=120)
    finally:
        # A cleanup that fails must not replace the error that brought us here:
        # the caller's fallback decision depends on which error that was. On
        # success the stage was renamed away, so a leftover is only garbage.
        try:
            link.feed(FEEDS['clean'], (stage,), timeout=120)
        except Exception as error:               # noqa: BLE001 - logged, never raised
            log('could not remove staging directory %s on %s: %s: %s'
                % (stage, link.host, type(error).__name__, error))
    return {'path': paths['final'], 'reused': False, 'link_dests': link_dests,
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
