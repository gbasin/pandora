"""`pandora upgrade`: Pandora runs from a pinned snapshot, never from the checkout.

The daemon runs under launchd and keeps the code it imported, while every
client -- `pandora` on PATH, and the shim through the marker's `home` -- imported
the checkout directly. So a `git pull` changed live client behavior at once and
left the daemon on the old code, for hours, until a digest warning in `doctor`
(#97) made the skew visible. This removes the skew rather than reporting it:

* `<data>/versions/<name>/` is the checkout's committed tree at one commit. It
  is written into a scratch directory beside it, renamed into place, and never
  changed again. `<name>` is the 12-hex short commit; a snapshot of uncommitted
  edits (`--dirty`) is `<commit>-dirty-<code8>`, because the same commit with
  other edits must not overwrite a directory a daemon may be running.
* `<data>/current` is a symlink to one of them, flipped with rename(2), so a
  reader finds the old version or the new one and never neither.
* The plist, the launchers on PATH and the marker's `home` name paths through
  `current`, so none of them goes stale. The launchers resolve `current` to
  the version directory as they start (`cd -P`), so a process that lives across
  a flip keeps importing from the version it started with.
* The daemon is restarted into the new version only when a restart ends
  nothing: it is drained first (`drain.drain_and_restart`), so new commands
  wait instead of starting, and `current` moves once no local run is executing
  and no remote run is before `accepted`, just before the kickstart.

`<data>` is `$XDG_DATA_HOME/pandora`, else `~/.local/share/pandora`. Pulling
the checkout changes nothing live until the next `pandora upgrade`. An install
that never ran `upgrade` has no `current` and runs its checkout as before.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

CURRENT = 'current'
VERSIONS = 'versions'
# Inside each version directory: where it came from, and its code digest.
META = '.pandora-version'
KEEP = 3
WAIT_SECONDS = 600
LAUNCHERS = ('pandora', 'pnpm')
RUNNING = Path(__file__).resolve().parents[2]
# The parts of the tree the digest covers: what runs, not the notes.
CODE = ('bin', 'pandora')


class Refused(Exception):
    """An upgrade that would snapshot the wrong thing, or flip onto nothing."""


# -- where things are --------------------------------------------------------------

def data_root(env=None, home=None):
    """`$XDG_DATA_HOME/pandora`, else `<home>/.local/share/pandora`.

    A relative XDG_DATA_HOME is ignored, as the XDG spec says it must be.
    """
    env = os.environ if env is None else env
    xdg = env.get('XDG_DATA_HOME') or ''
    if os.path.isabs(xdg):
        return Path(xdg) / 'pandora'
    base = Path(home) if home else Path(env.get('HOME') or os.path.expanduser('~'))
    return base / '.local' / 'share' / 'pandora'


def current_link(data):
    return Path(data) / CURRENT


def read_meta(path):
    try:
        return json.loads((Path(path) / META).read_text())
    except (OSError, ValueError):
        return {}


def installed(data):
    """The version `current` names, or None when there is no usable `current`.

    `{'link', 'path', 'name', 'meta'}`: `path` is the version directory, links
    resolved. A `current` that names no package reads as None; `doctor` says so.
    """
    link = current_link(data)
    if not link.is_symlink():
        return None
    path = Path(os.path.realpath(link))
    if not (path / 'pandora' / 'cli.py').is_file():
        return None
    return {'link': str(link), 'path': str(path), 'name': path.name, 'meta': read_meta(path)}


def package_home(env=None, home=None, running=None):
    """The package directory to write down: `<data>/current` once a snapshot exists.

    The one rule for the marker's `home`, the plist's program and `doctor`. A
    path through `current`, not the version it names today, so what was written
    before an upgrade names the live version after it. Without a snapshot it is
    the checkout this code runs from, as before `pandora upgrade` existed.

    Code that runs from a version directory names that directory's own
    `current` before anything else: a daemon whose environment does not say
    where the data root is (a plist from before XDG_DATA_HOME was written into
    it) must still never write down the version it happens to be.
    """
    running = Path(running or RUNNING).resolve()
    if running.parent.name == VERSIONS:
        return str(running.parent.parent / CURRENT)
    now = installed(data_root(env, home))
    if now:
        return now['link']
    return str(running)


def version_label(home, data):
    """A daemon's or launcher's home as a person reads it: a version name or a path."""
    if not home:
        return '(unknown)'
    real = Path(os.path.realpath(home))
    versions = Path(os.path.realpath(Path(data) / VERSIONS))
    return real.name if real.parent == versions else str(real)


def is_version(home, data):
    real = Path(os.path.realpath(home))
    return real.parent == Path(os.path.realpath(Path(data) / VERSIONS))


def update_fix(home, data=None):
    """The one command that brings the code at `home` up to its source, as a person types it.

    A version directory is updated by pulling its source checkout and
    upgrading, which restarts the daemon at a safe moment. A checkout is pulled
    and the daemon restarted once `pandora ps` shows nothing running. No
    version numbers: the fix is the same whichever side is older.
    """
    data = data_root() if data is None else data
    if home and is_version(home, data):
        source = (read_meta(Path(os.path.realpath(home))) or {}).get('source')
        return ('`git -C %s pull && pandora upgrade`' % source if source
                else '`pandora upgrade --from <your pandora checkout>`')
    return ('when `pandora ps` shows nothing running, `git -C %s pull && pandora daemon '
            '--restart`' % home)


def chain_end(path):
    """Where a launcher's own symlinks end, directories left unresolved.

    The walk `bin/pandora` does: `~/.local/bin/pandora -> <data>/current/bin/pandora`
    ends through `current`, which is what is wanted; resolving everything
    would name the version directory, which goes stale at the next upgrade.
    """
    here, hops = str(path), 0
    while os.path.islink(here) and hops < 40:
        target = os.readlink(here)
        here = target if os.path.isabs(target) else os.path.join(os.path.dirname(here), target)
        hops += 1
    return os.path.normpath(here)


def same_entry(one, other):
    """The same directory entry, compared without following the last component."""
    try:
        first, second = os.lstat(one), os.lstat(other)
    except OSError:
        return False
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def through_current(path, data):
    """Whether a launcher, or a `home`, reaches its package by way of `current`.

    A launcher counts when its link chain ends at `<current>/bin/<name>`; a
    directory counts when it is `current` itself. Compared as directory
    entries, so `/var` and `/private/var` spellings agree.
    """
    link = current_link(data)
    if not link.is_symlink():
        return False
    if same_entry(os.path.normpath(str(path)), link):
        return True
    end = chain_end(path)
    if os.path.basename(os.path.dirname(end)) == 'bin':
        return same_entry(os.path.dirname(os.path.dirname(end)), link)
    return same_entry(end, link)


# -- the checkout ------------------------------------------------------------------

def git(source, *args, run=subprocess.run, binary=False):
    # A hook or a rebase exports GIT_DIR and friends; the checkout is named
    # explicitly, so they must not redirect these reads to another repository.
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    proc = run(['git', '-C', str(source), *args], capture_output=True, text=not binary,
               env=env, timeout=120)
    if proc.returncode != 0:
        said = proc.stderr.decode(errors='replace') if binary else proc.stderr
        raise Refused('`git %s` in %s failed: %s'
                      % (' '.join(args), source, (said or '').strip() or 'exit %d' % proc.returncode))
    return proc.stdout


def source_for(given, data):
    """The checkout to snapshot: `--from`, else the one `current` came from, else this one."""
    if given:
        return str(Path(given).expanduser())
    now = installed(data)
    if now and now['meta'].get('source'):
        return now['meta']['source']
    if (RUNNING / '.git').exists():
        return str(RUNNING)
    raise Refused('no checkout to upgrade from: this Pandora is a snapshot with no '
                  'recorded source; pass --from <checkout>')


def describe(source, *, dirty_ok=False, run=subprocess.run):
    """The checkout's top level and commit, refused when it has uncommitted edits.

    Untracked files are neither copied nor counted: a snapshot is the tracked
    tree, and a scratch file beside the code is not a reason to refuse.
    """
    top = git(source, 'rev-parse', '--show-toplevel', run=run).strip()
    if not ((Path(top) / 'pandora' / 'cli.py').is_file()
            and (Path(top) / 'bin' / 'pandora').is_file()):
        raise Refused('%s is not a Pandora checkout (no pandora/cli.py and bin/pandora)' % top)
    commit = git(top, 'rev-parse', 'HEAD', run=run).strip()
    changed = [line for line in git(top, 'status', '--porcelain', '--untracked-files=no',
                                    run=run).splitlines() if line.strip()]
    if changed and not dirty_ok:
        raise Refused('%s has uncommitted changes (%d file%s); commit them, or pass --dirty '
                      'to snapshot the working tree as it is'
                      % (top, len(changed), '' if len(changed) == 1 else 's'))
    return {'source': top, 'commit': commit, 'dirty': bool(changed)}


def code_digest(root):
    """sha256 over `bin/` and `pandora/` of a tree: paths, the executable bit, bytes."""
    digest = hashlib.sha256()
    for part in CODE:
        base = Path(root) / part
        for path in sorted(p for p in base.rglob('*') if p.is_file() or p.is_symlink()):
            if '__pycache__' in path.parts:
                continue
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                body = b'link:' + os.readlink(path).encode()
            else:
                body = path.read_bytes()
            mode = 'x' if os.access(path, os.X_OK) and not path.is_symlink() else '-'
            digest.update(('%s\0%s\0%d\0' % (rel, mode, len(body))).encode())
            digest.update(body)
    return digest.hexdigest()


def export_commit(source, commit, stage, *, run=subprocess.run):
    data = git(source, 'archive', '--format=tar', commit, run=run, binary=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        if hasattr(tarfile, 'tar_filter'):
            archive.extractall(stage, filter='tar')
        else:                                        # a 3.11 without the backport
            archive.extractall(stage)


def export_worktree(source, stage, *, run=subprocess.run):
    """The tracked files as they are on disk; a deleted one is left out."""
    names = git(source, 'ls-files', '-z', run=run).split('\0')
    for name in filter(None, names):
        origin, target = Path(source) / name, Path(stage) / name
        if not os.path.lexists(origin) or (origin.is_dir() and not origin.is_symlink()):
            continue                                 # deleted, or a submodule
        target.parent.mkdir(parents=True, exist_ok=True)
        if origin.is_symlink():
            os.symlink(os.readlink(origin), target)
        else:
            shutil.copy2(origin, target)


def intact(path):
    """A version directory whose files still hash to the digest written at build time."""
    meta = read_meta(path)
    return bool(meta.get('code')) and code_digest(path) == meta['code']


def found(target, **extra):
    return dict({'name': target.name, 'path': str(target), 'meta': read_meta(target),
                 'reused': True}, **extra)


def snapshot(info, data, *, run=subprocess.run, clock=time.time):
    """Write the version directory for `info`, or find it already written and intact.

    Built in `versions/.incoming-<pid>-*` and renamed into place, so a version
    directory either holds a whole tree and its META or does not exist. One
    that exists is reused only while its files still hash to its META: a
    version someone edited is never handed out again, and the fresh build goes
    under `<name>-<code8>` instead, since the edited one may be running.
    """
    versions = Path(data) / VERSIONS
    versions.mkdir(parents=True, exist_ok=True)
    short = info['commit'][:12]
    if not info['dirty'] and (versions / short / META).is_file() and intact(versions / short):
        return found(versions / short)
    stage = Path(tempfile.mkdtemp(prefix='.incoming-%d-' % os.getpid(), dir=str(versions)))
    try:
        if info['dirty']:
            export_worktree(info['source'], stage, run=run)
        else:
            export_commit(info['source'], info['commit'], stage, run=run)
        code = code_digest(stage)
        name = short + ('-dirty-' + code[:8] if info['dirty'] else '')
        edited = None
        if (versions / name / META).is_file():
            if intact(versions / name):
                return found(versions / name)
            edited, name = name, name + '-' + code[:8]
            if (versions / name / META).is_file() and intact(versions / name):
                return found(versions / name, edited=edited)
        target = versions / name
        meta = {'name': name, 'commit': info['commit'], 'dirty': info['dirty'],
                'source': info['source'], 'code': code, 'created': clock()}
        (stage / META).write_text(json.dumps(meta, indent=1, sort_keys=True) + '\n')
        os.chmod(stage, 0o755)                       # mkdtemp made it 0700
        try:
            os.rename(stage, target)
        except OSError as error:
            # Another upgrade renamed the same version in first, which is fine;
            # a directory there without META is not ours to replace.
            if (target / META).is_file() and intact(target):
                return found(target, edited=edited)
            raise Refused('cannot place %s: %s' % (target, error))
        return {'name': name, 'path': str(target), 'meta': meta, 'reused': False,
                'edited': edited}
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def flip(data, name):
    """Point `current` at `versions/<name>` in one rename(2).

    The link is relative, so the data directory can move as a whole. The
    version directory's mtime is touched: pruning keeps the most recently
    installed versions, which a rollback to an old commit must count as.
    """
    link = current_link(data)
    if os.path.lexists(link) and not link.is_symlink():
        raise Refused('%s exists and is not a symlink; move it aside' % link)
    target = Path(data) / VERSIONS / name
    if not (target / 'pandora' / 'cli.py').is_file():
        raise Refused('%s holds no pandora package' % target)
    scratch = Path(data) / ('.current-%d' % os.getpid())
    if os.path.lexists(scratch):
        scratch.unlink()
    os.symlink(os.path.join(VERSIONS, name), scratch)
    os.replace(scratch, link)
    os.utime(target)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def prune(data, *, keep=KEEP, protect=(), is_alive=alive):
    """Remove all but the `keep` most recently installed versions.

    Never `current`, never a directory named in `protect` (the running
    daemon's home), and never a stage a live upgrade is still writing.
    """
    versions = Path(data) / VERSIONS
    if not versions.is_dir():
        return []
    now = installed(data)
    kept = {os.path.realpath(path) for path in protect if path}
    if now:
        kept.add(now['path'])
    entries = [path for path in versions.iterdir()
               if path.is_dir() and not path.is_symlink() and not path.name.startswith('.')]
    entries.sort(key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
    removed = []
    for path in entries[max(keep, 1):]:
        if os.path.realpath(path) in kept:
            continue
        # Renamed out of the namespace first, so a reader never finds half a
        # version under its name while the tree is deleted.
        trash = versions / ('.trash-%d-%s' % (os.getpid(), path.name))
        try:
            os.rename(path, trash)
        except OSError:
            continue
        shutil.rmtree(trash, ignore_errors=True)
        removed.append(path.name)
    for path in versions.glob('.trash-*'):
        shutil.rmtree(path, ignore_errors=True)
    for path in versions.glob('.incoming-*'):
        try:
            pid = int(path.name.split('-')[1])
        except (IndexError, ValueError):
            continue
        if not is_alive(pid):
            shutil.rmtree(path, ignore_errors=True)
    return removed


# -- the launchers on PATH -----------------------------------------------------------

def path_entries(env):
    return [entry or '.' for entry in (env.get('PATH') or '').split(':')]


def is_launcher(path, name):
    """A Pandora launcher: the shim by its signature, `pandora` by the package beside it."""
    from .doctor import is_shim
    if name == 'pnpm':
        return is_shim(path)
    real = Path(os.path.realpath(path))
    return real.name == 'pandora' and (real.parent.parent / 'pandora' / 'cli.py').is_file()


def find_launcher(name, env):
    """The first `pandora` on PATH, or the first `pnpm` there that is the shim."""
    for entry in path_entries(env):
        candidate = os.path.join(entry, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            if name == 'pandora' or is_launcher(candidate, name):
                return candidate
    return None


def ours(end, data, source):
    """Whether a launcher's link chain ends in the checkout being upgraded, or in a version here."""
    package = os.path.dirname(os.path.dirname(end))
    if source and os.path.realpath(package) == os.path.realpath(source):
        return True
    return is_version(package, data)


def launcher_links(env, data, *, source=None, fix=True):
    """Each launcher on PATH: already through `current`, re-pointed, or reported.

    Only a symlink that reaches a Pandora launcher in the checkout being
    upgraded, or in a version directory of this data root, is re-pointed, with
    a scratch link and one rename, so no shell ever finds the name missing. A
    link into another checkout is someone else's install (a second data
    directory, a test) and is reported, as is a copy or a file that is not
    Pandora's.
    """
    out = []
    for name in LAUNCHERS:
        found = find_launcher(name, env)
        wanted = str(current_link(data) / 'bin' / name)
        if found is None:
            out.append({'name': name, 'path': None, 'status': 'missing', 'wanted': wanted})
        elif through_current(found, data):
            out.append({'name': name, 'path': found, 'status': 'ok', 'wanted': wanted})
        elif os.path.islink(found) and is_launcher(found, name):
            was = chain_end(found)
            if not ours(was, data, source):
                out.append({'name': name, 'path': found, 'status': 'other', 'was': was,
                            'wanted': wanted})
                continue
            if fix:
                scratch = '%s.pandora-%d' % (found, os.getpid())
                if os.path.lexists(scratch):
                    os.unlink(scratch)
                os.symlink(wanted, scratch)
                os.replace(scratch, found)
            out.append({'name': name, 'path': found, 'status': 'fixed' if fix else 'stale',
                        'was': was, 'wanted': wanted})
        else:
            out.append({'name': name, 'path': found, 'status': 'foreign', 'wanted': wanted})
    return out


def link_lines(links):
    lines = []
    for item in links:
        if item['status'] == 'ok':
            lines.append('%s runs through current' % item['path'])
        elif item['status'] == 'fixed':
            lines.append('re-pointed %s from %s to %s' % (item['path'], item['was'], item['wanted']))
        elif item['status'] == 'other':
            lines.append('%s runs %s, which is not the checkout upgraded here; left alone. '
                         'To run current: ln -sf %s %s'
                         % (item['path'], item['was'], item['wanted'], item['path']))
        elif item['status'] == 'stale':
            lines.append('%s runs %s, not current; left alone because this data directory '
                         'is not the default one. To run current: ln -sf %s %s, or '
                         '`pandora upgrade --relink`'
                         % (item['path'], item['was'], item['wanted'], item['path']))
        elif item['status'] == 'missing':
            lines.append('no %s on PATH; link one: ln -s %s ~/.local/bin/%s'
                         % ('`pandora`' if item['name'] == 'pandora' else 'pnpm shim',
                            item['wanted'], item['name']))
        else:
            lines.append('%s is not a symlink to a Pandora launcher; replace it with a link to %s'
                         % (item['path'], item['wanted']))
    return lines


# -- the daemon ----------------------------------------------------------------------

def probe(ping):
    """`('pong', answer)`, `('absent', why)` or `('silent', why)`.

    Absent: nothing listens on the socket. Silent: something does, or may, and
    did not answer as this client's daemon -- a timeout (a daemon busy on a
    swapping Mac), a close, an error frame such as a protocol mismatch. Only
    absent is evidence that no run is being driven, and even that is checked
    against the lock before anything is restarted.
    """
    try:
        answer = ping()
    except (FileNotFoundError, ConnectionRefusedError) as error:
        return 'absent', str(error)
    except (OSError, ValueError) as error:
        return 'silent', str(error) or type(error).__name__
    if not isinstance(answer, dict) or answer.get('t') != 'pong':
        said = answer.get('msg') if isinstance(answer, dict) else None
        return 'silent', 'it answered %s' % (said or answer)
    return 'pong', answer


# -- the whole verb ------------------------------------------------------------------

IMPORTS = 'import pandora.cli, pandora.client.daemon, pandora.client.shim'
NOW_NOTE = ('restarting now (--now): a local run ends with exit 70; a remote run still '
            'freezing or shipping ends with exit 70; one submitting is looked up on the '
            'worker and followed if it started, closed if not. Rerun what ended. Accepted '
            'remote runs continue')


def check_imports(path, pythons, *, run=subprocess.run):
    """Each interpreter that will run the version can import its client and daemon.

    From `/`, with only the version on PYTHONPATH, so neither the cwd nor an
    inherited path can make a broken tree look whole.
    """
    env = {key: value for key, value in os.environ.items()
           if key not in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSAFEPATH')}
    env['PYTHONPATH'] = str(path)
    for python in pythons:
        try:
            proc = run([python, '-B', '-c', IMPORTS], cwd='/', env=env, capture_output=True,
                       text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as error:
            raise Refused('%s could not run to check %s: %s. Nothing changed'
                          % (python, path, error))
        if proc.returncode != 0:
            last = ((proc.stderr or '').strip().splitlines() or ['exit %d' % proc.returncode])[-1]
            raise Refused('%s cannot import %s: %s. Nothing changed' % (python, path, last))


def interpreters(env, state, home):
    """The plist's pinned interpreter and the one the launchers find, first found first."""
    import sys
    from . import launchd
    found = [launchd.agent_python(launchd.label_for(state), home),
             env.get('PANDORA_PYTHON') or launchd.first_on_path('python3', env)]
    return list(dict.fromkeys(path for path in found if path)) or [sys.executable]


def pick(data, name):
    """An installed version, for `--version`: present, and intact."""
    path = Path(data) / VERSIONS / name
    if not (path / META).is_file():
        have = sorted(p.name for p in (Path(data) / VERSIONS).glob('[!.]*')) \
            if (Path(data) / VERSIONS).is_dir() else []
        raise Refused('no installed version %s; installed: %s' % (name, ', '.join(have) or 'none'))
    if not intact(path):
        raise Refused('%s was edited after it was built; pick another, or build the commit '
                      'again with `pandora upgrade --from <checkout>`' % path)
    return found(path)


def names_current(program, data):
    """Whether a plist's program goes through `current`, even before `current` exists."""
    if not program:
        return False
    if through_current(program, data):
        return True
    return os.path.normpath(program) == os.path.normpath(
        str(current_link(data) / 'bin' / 'pandora'))


def default_data(home=None):
    """The data root for this user with no XDG_DATA_HOME: where the real launchers point."""
    return Path(home or Path.home()) / '.local' / 'share' / 'pandora'


def upgrade(*, state, source=None, version=None, data=None, env=None, home=None,
            dirty_ok=False, now=False, no_restart=False, relink=None, wait=WAIT_SECONDS,
            keep=KEEP, platform=None, git_run=subprocess.run, check_run=subprocess.run,
            launchctl=subprocess.run, ping=None, ask=None, clock=time.monotonic,
            sleep=time.sleep, say=print, idle_cancel=None):
    """Build or pick a version, drain the daemon, flip `current`, restart, prune.

    `current` moves only when the daemon can move with it (or `--no-restart`
    asks for the flip alone), so a timed-out wait leaves nothing changed. The
    wait is `drain.drain_and_restart`: the daemon holds new submissions while
    the runs a restart would end finish, and `current` moves once nothing
    blocks, just before the kickstart. `ask` is its transport, for tests.
    Exits: 0 the daemon runs the new version, or no daemon runs; 75 no safe
    moment came, or the daemon did not answer, and nothing changed; 1 refused,
    upgrade cannot restart this daemon, or the new daemon never answered.
    """
    import sys
    from . import drain
    from .doctor import ping as doctor_ping
    env = dict(os.environ if env is None else env)
    data = Path(data or data_root(env, home))
    sock = Path(state) / 'client.sock'
    if relink is None:
        # Only the default data root is the one the machine's launchers use; a
        # scratch XDG_DATA_HOME must never re-point them (it did, once).
        relink = os.path.realpath(data) == os.path.realpath(default_data(home))
    job = Job(state=Path(state), data=data, env=env, home=home, now=now, wait=wait,
              idle_cancel=drain.DEFAULT_IDLE_CANCEL if idle_cancel is None else idle_cancel,
              platform=platform or sys.platform, launchctl=launchctl, relink=relink,
              ping=ping or (lambda: doctor_ping(sock)), ask=ask or drain.ask,
              # After the restart, one ask per half second against a 10 s
              # deadline: a 30 s timeout each would make it ten minutes.
              quick_ping=ping or (lambda: doctor_ping(sock, timeout=2.0)),
              clock=clock, sleep=sleep, say=say, before=installed(data), source=None)
    if version:
        job.target = pick(data, version)
        say('version  %s (tree %s), built before' % (job.target['name'],
                                                      short_code(job.target['meta'].get('code'))))
    else:
        info = describe(source_for(source, data), dirty_ok=dirty_ok, run=git_run)
        job.source = info['source']
        job.target = snapshot(info, data, run=git_run)
        if job.target.get('edited'):
            say('version  %s was edited after it was built; built the commit again as %s'
                % (job.target['edited'], job.target['name']))
        say('version  %s (tree %s), %s' % (job.target['name'],
                                           short_code(job.target['meta'].get('code')),
                                           'already built' if job.target['reused']
                                           else 'built from %s' % info['source']))
    check_imports(job.target['path'], interpreters(env, state, home), run=check_run)
    kind, value = probe(job.ping)
    if kind == 'pong':
        code, keep_home = job.with_daemon(value, no_restart)
    elif kind == 'absent':
        code, keep_home = job.without_daemon(value)
    else:
        code, keep_home = job.silent(value)
    if job.flipped:
        protect = [keep_home, job.before and job.before['path']]
        removed = prune(data, keep=keep, protect=protect)
        if removed:
            say('pruned %s (keeping %d)' % (', '.join(sorted(removed)), keep))
    return code


def short_code(text):
    return (text or '?')[:12]


class Job:
    """One upgrade's state between the checks, so each step reads as the rule it applies."""

    def __init__(self, **fields):
        self.__dict__.update(fields)
        self.flipped = False
        self.target = None

    # -- moving current

    def install(self):
        flip(self.data, self.target['name'])
        self.flipped = True
        self.say('current  %s -> %s' % (self.before['name'] if self.before else '(none)',
                                        self.target['name']))
        for line in link_lines(launcher_links(self.env, self.data, source=self.source,
                                              fix=self.relink)):
            self.say(line)

    def undo(self):
        if self.before:
            flip(self.data, self.before['name'])
        else:
            os.unlink(current_link(self.data))
        self.flipped = False

    def unchanged(self):
        return ('Nothing changed: current is still %s; %s waits in versions/'
                % (self.before['name'] if self.before else '(none)', self.target['name']))

    # -- the three things the socket can say

    def with_daemon(self, pong, no_restart):
        old_home = pong.get('home')
        old = version_label(old_home, self.data)
        self.say('daemon   pid %s runs %s' % (pong.get('pid'), old))
        if old_home and os.path.realpath(old_home) == os.path.realpath(self.target['path']):
            self.install()
            self.say('the daemon already runs %s' % self.target['name'])
            return 0, old_home
        why = self.cannot_restart(pong)
        if why and not no_restart:
            self.say(why)
            self.say(self.unchanged() + '. `pandora upgrade --no-restart` moves current '
                     'without restarting the daemon')
            return 1, old_home
        if why or no_restart:
            self.install()
            self.say('%s--no-restart: the daemon runs %s until it restarts; `pandora doctor` '
                     'says so meanwhile' % (why + '; ' if why else '', old))
            return 0, old_home
        return self.restart_when_safe(pong.get('pid'), old_home)

    def without_daemon(self, why):
        from . import launchd
        label = launchd.recorded_label(self.state) if self.platform == 'darwin' else None
        holder = launchd.lock_holder(self.state)
        if holder:
            # The socket is gone or refusing, yet a daemon holds the lock: one
            # starting, or wedged. Either may be driving runs.
            return self.silent('%s, but pid %s holds %s' % (why, holder,
                                                            self.state / 'daemon.lock'))
        self.install()
        if label is None or not launchd.status(label, run=self.launchctl)['loaded']:
            self.say('no daemon answers on %s; the next one started runs %s'
                     % (self.state / 'client.sock', self.target['name']))
            return 0, None
        # Loaded, not running, and nobody holds the lock: start it now rather
        # than at launchd's next throttle.
        return self.restart(label, None)

    def silent(self, why):
        from . import launchd
        if not self.now:
            self.say('the daemon on %s did not answer (%s). It may be driving runs, and '
                     'a restart would end them unseen. %s. Try again, or `pandora upgrade '
                     '--now`' % (self.state / 'client.sock', why, self.unchanged()))
            return 75, None
        label = launchd.recorded_label(self.state) if self.platform == 'darwin' else None
        agent = launchd.status(label, run=self.launchctl) if label else {'loaded': False}
        if not agent['loaded']:
            self.say('the daemon did not answer (%s), and launchd supervises no daemon for '
                     '%s, so upgrade cannot restart it. %s' % (why, self.state, self.unchanged()))
            return 1, None
        self.say(NOW_NOTE)
        self.install()
        return self.restart(label, agent.get('pid'))

    # -- restarting

    def cannot_restart(self, pong):
        """Why upgrade cannot restart the daemon that answered, or None."""
        from . import launchd
        if self.platform != 'darwin':
            return 'launchd is macOS only; restart the daemon yourself'
        self.label = launchd.label_for(self.state)
        agent = launchd.status(self.label, run=self.launchctl)
        if not agent['loaded'] or agent['pid'] != pong.get('pid'):
            return ('launchd does not run this daemon, so upgrade cannot restart it: stop it '
                    'with `pandora daemon --stop`, then start it with `pandora daemon '
                    '--install` once current has moved')
        program = launchd.agent_program(self.label, self.home)
        if not names_current(program, self.data):
            return ('the launchd agent runs %s, not current: run `pandora daemon --install` '
                    'once current has moved (it restarts the daemon; check `pandora ps` '
                    'first)' % (program or '(unknown)'))
        return None

    def restart_when_safe(self, old_pid, old_home):
        """Drain, flip `current` once nothing blocks, kickstart, then check the new daemon.

        `--now` does not wait: the drain asks once, withdraws queued local runs
        for resubmission, and the restart ends the rest (`drain.NOW_NOTE`). A
        wait that runs out leaves the daemon admitting runs and `current` where
        it was.
        """
        from . import drain, launchd
        kicked, report = [], {}

        def kickstart():
            # `kicked` the moment launchd took it: a status line that fails
            # afterwards must not move `current` back under a new daemon.
            launchd.restart(self.label, run=self.launchctl, say=self.say,
                            kicked=lambda: kicked.append(True))

        def undo():
            if self.flipped:
                self.undo()
                self.say('current  back to %s' % (self.before['name'] if self.before
                                                  else '(none)'))
        try:
            code = drain.drain_and_restart(
                self.state, restart=kickstart, wait=0 if self.now else self.wait,
                now=self.now, say=self.say, before_restart=self.install,
                undo_before_restart=undo, ask=self.ask, clock=self.clock, sleep=self.sleep,
                again='`pandora upgrade --now`', report=report,
                idle_cancel=self.idle_cancel)
        except launchd.Refused as error:
            if self.flipped:
                self.undo()
            self.say('%s. %s' % (error, self.unchanged()))
            return 1, old_home
        except BaseException:
            # Before the kickstart, the old daemon still runs the old version.
            if self.flipped and not kicked:
                self.undo()
            raise
        if code == 75:
            if report.get('undrained', True):
                self.say('%s. Run `pandora upgrade` again later, or with --now'
                         % self.unchanged())
            else:
                # Honest: `current` did not move, but the daemon may still
                # answer `draining` until its lease runs out.
                self.say('current is still %s and %s waits in versions/, but the daemon may '
                         'hold new commands for up to %ds more. Run `pandora upgrade` again '
                         'later, or with --now'
                         % (self.before['name'] if self.before else '(none)',
                            self.target['name'], drain.LEASE_SECONDS))
            return 75, old_home
        if code != 0:
            self.say(self.way_back())
            return 1, old_home
        return self.confirm(old_pid, old_home)

    def restart(self, label, old_pid, old_home=None):
        from . import launchd
        try:
            launchd.restart(label, run=self.launchctl, say=self.say)
        except launchd.Refused as error:
            self.undo()
            self.say('%s. %s' % (error, self.unchanged()))
            return 1, old_home
        return self.confirm(old_pid, old_home)

    def way_back(self):
        return ('To go back: `pandora upgrade --version %s`' % self.before['name']
                if self.before else '')

    CONFIRM_SECONDS = 10.0

    def confirm(self, old_pid, old_home):
        """A new daemon answers within `CONFIRM_SECONDS`, and runs the target version."""
        deadline = self.clock() + self.CONFIRM_SECONDS
        while self.clock() < deadline:
            kind, fresh = probe(self.quick_ping)
            if kind == 'pong' and fresh.get('pid') != old_pid:
                runs = version_label(fresh.get('home'), self.data)
                self.say('daemon   pid %s runs %s' % (fresh.get('pid'), runs))
                if os.path.realpath(fresh.get('home') or '') == os.path.realpath(
                        self.target['path']):
                    return 0, None
                self.say('the new daemon does not run %s; `pandora doctor` says why'
                         % self.target['name'])
                return 1, fresh.get('home')
            self.sleep(0.5)
        back = self.way_back()
        self.say('no daemon has answered 10 s after the restart; read %s.%s'
                 % (self.state / 'logs' / 'daemon.log', ' ' + back if back else ''))
        return 1, old_home
