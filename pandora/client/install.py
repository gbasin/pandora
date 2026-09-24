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
  nothing: no local run running or queued, no remote run before `accepted`.

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
POLL_SECONDS = 10
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
    """
    now = installed(data_root(env, home))
    if now:
        return now['link']
    return str(Path(running or RUNNING).resolve())


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


def snapshot(info, data, *, run=subprocess.run, clock=time.time):
    """Write the version directory for `info`, or find it already written.

    Built in `versions/.incoming-<pid>-*` and renamed into place, so a version
    directory either holds a whole tree and its META or does not exist. A clean
    commit's directory is reused as it stands: the same commit is the same tree.
    """
    versions = Path(data) / VERSIONS
    versions.mkdir(parents=True, exist_ok=True)
    short = info['commit'][:12]
    if not info['dirty'] and (versions / short / META).is_file():
        return {'name': short, 'path': str(versions / short), 'meta': read_meta(versions / short),
                'reused': True}
    stage = Path(tempfile.mkdtemp(prefix='.incoming-%d-' % os.getpid(), dir=str(versions)))
    try:
        if info['dirty']:
            export_worktree(info['source'], stage, run=run)
        else:
            export_commit(info['source'], info['commit'], stage, run=run)
        code = code_digest(stage)
        name = short + ('-dirty-' + code[:8] if info['dirty'] else '')
        target = versions / name
        if (target / META).is_file():
            return {'name': name, 'path': str(target), 'meta': read_meta(target), 'reused': True}
        meta = {'name': name, 'commit': info['commit'], 'dirty': info['dirty'],
                'source': info['source'], 'code': code, 'created': clock()}
        (stage / META).write_text(json.dumps(meta, indent=1, sort_keys=True) + '\n')
        os.chmod(stage, 0o755)                       # mkdtemp made it 0700
        try:
            os.rename(stage, target)
        except OSError as error:
            # Another upgrade renamed the same version in first, which is fine;
            # a directory there without META is not ours to replace.
            if (target / META).is_file():
                return {'name': name, 'path': str(target), 'meta': read_meta(target),
                        'reused': True}
            raise Refused('cannot place %s: %s' % (target, error))
        return {'name': name, 'path': str(target), 'meta': meta, 'reused': False}
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
        shutil.rmtree(path, ignore_errors=True)
        removed.append(path.name)
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


def launcher_links(env, data, *, fix=True):
    """Each launcher on PATH: already through `current`, re-pointed, or reported.

    Only a symlink that already reaches a Pandora launcher is re-pointed, with
    a scratch link and one rename, so no shell ever finds the name missing. A
    copy, or a file that is not Pandora's, is reported and left alone.
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
        elif item['status'] == 'stale':
            lines.append('%s runs %s, not current; `pandora upgrade` re-points it'
                         % (item['path'], item['was']))
        elif item['status'] == 'missing':
            lines.append('no %s on PATH; link one: ln -s %s ~/.local/bin/%s'
                         % ('`pandora`' if item['name'] == 'pandora' else 'pnpm shim',
                            item['wanted'], item['name']))
        else:
            lines.append('%s is not a symlink to a Pandora launcher; replace it with a link to %s'
                         % (item['path'], item['wanted']))
    return lines


# -- a safe moment -------------------------------------------------------------------

def blockers(rows):
    """The runs a daemon restart would end: local running or queued, remote not yet accepted.

    A remote row is `queued` until the worker says `accepted` -- through
    freezing, shipping and submitting -- and `running` after it; an accepted
    remote run survives a restart (`Daemon.resume_interrupted`).
    """
    out = []
    for row in rows:
        state, lane = row.get('state'), row.get('lane') or 'remote'
        if state == 'queued' or (lane == 'local' and state == 'running'):
            out.append(row)
    return out


def blocker_line(row):
    from ..cli import state_word
    return '%s %s %s: %s' % (row.get('id', '?'), row.get('lane') or 'remote', state_word(row),
                             ' '.join(row.get('argv') or [])[:60])


def wait_for_safe(ps, *, wait=WAIT_SECONDS, interval=POLL_SECONDS, clock=time.monotonic,
                  sleep=time.sleep, say=print):
    """True once `ps()` shows nothing a restart would end; False after `wait` seconds.

    `ps()` returns the daemon's rows, or raises OSError when no daemon answers,
    which is safe: nothing is driving a run. What is waited on is printed when
    it changes, not every poll.
    """
    deadline = clock() + max(wait, 0)
    said = None
    while True:
        try:
            rows = ps()
        except OSError:
            return True
        waiting = [blocker_line(row) for row in blockers(rows)]
        if not waiting:
            return True
        if waiting != said:
            say('waiting up to %ds for %d run%s a restart would end:'
                % (max(0, round(deadline - clock())), len(waiting),
                   '' if len(waiting) == 1 else 's'))
            for line in waiting:
                say('  ' + line)
            said = waiting
        remaining = deadline - clock()
        if remaining <= 0:
            return False
        sleep(min(interval, remaining))


# -- the whole verb ------------------------------------------------------------------

def short_code(text):
    return (text or '?')[:12]


def upgrade(*, state, source=None, data=None, env=None, home=None, dirty_ok=False, now=False,
            wait=WAIT_SECONDS, keep=KEEP, platform=None, git_run=subprocess.run,
            launchctl=subprocess.run, ping=None, ps=None, clock=time.monotonic,
            sleep=time.sleep, say=print):
    """Snapshot, flip, re-point the launchers, restart at a safe moment, prune.

    Returns the exit code: 0 when the daemon runs the new version or no daemon
    runs; 75 when the wait ran out, with `current` already flipped; 1 when
    this verb cannot restart the daemon itself (hand-started, or its plist
    runs a checkout) and says what to type instead.
    """
    import sys
    from . import launchd
    from .doctor import ping as doctor_ping
    env = dict(os.environ if env is None else env)
    data = Path(data or data_root(env, home))
    sock = Path(state) / 'client.sock'
    ping = ping or (lambda: doctor_ping(sock))
    if ps is None:
        def ps():
            from ..cli import ask
            answer = ask(sock, {'op': 'ps'})
            return (answer or {}).get('data') or []

    info = describe(source_for(source, data), dirty_ok=dirty_ok, run=git_run)
    before = installed(data)
    try:
        pong = ping()
    except (OSError, ValueError):
        pong = None
    version = snapshot(info, data, run=git_run)
    flip(data, version['name'])
    after = installed(data)
    say('current  %s -> %s (code %s -> %s)%s'
        % (before['name'] if before else '(none)', after['name'],
           short_code(before and before['meta'].get('code')), short_code(version['meta'].get('code')),
           ', already built' if version['reused'] else ', from %s' % info['source']))
    for line in link_lines(launcher_links(env, data)):
        say(line)

    code, keep_home = restart_phase(pong, after, data=data, state=state, home=home, now=now,
                                    wait=wait, platform=platform or sys.platform,
                                    launchctl=launchctl, ping=ping, ps=ps, clock=clock,
                                    sleep=sleep, say=say, launchd=launchd)
    removed = prune(data, keep=keep, protect=[keep_home])
    if removed:
        say('pruned %s (keeping %d)' % (', '.join(sorted(removed)), keep))
    return code


def restart_phase(pong, after, *, data, state, home, now, wait, platform, launchctl, ping, ps,
                  clock, sleep, say, launchd):
    """(exit code, a daemon home pruning must keep) for the restart half of `upgrade`."""
    new_path = after['path']
    if pong is None:
        if platform == 'darwin':
            label = launchd.label_for(state)
            if launchd.status(label, run=launchctl)['loaded']:
                # Loaded but not answering: nothing to protect, and a kickstart
                # starts it from the plist now instead of at the next throttle.
                try:
                    launchd.restart(label, run=launchctl, say=say)
                except launchd.Refused as error:
                    say(str(error))
                    return 1, None
                return 0, None
        say('no daemon answers on %s; the next one started runs %s'
            % (Path(state) / 'client.sock', after['name']))
        return 0, None
    old_home = pong.get('home')
    old = version_label(old_home, data)
    daemon_line = 'daemon   pid %s runs %s (code %s)' % (pong.get('pid'), old,
                                                         short_code(pong.get('code')))
    if old_home and os.path.realpath(old_home) == new_path:
        say(daemon_line + '; already current')
        return 0, old_home
    say(daemon_line)
    if platform != 'darwin':
        say('restart the daemon yourself when `pandora ps` shows nothing running: it runs '
            '%s until then' % old)
        return 1, old_home
    label = launchd.label_for(state)
    agent = launchd.status(label, run=launchctl)
    if not agent['loaded'] or agent['pid'] != pong.get('pid'):
        say('launchd does not run this daemon, so upgrade cannot restart it. Stop it with '
            '`pandora daemon --stop`, then start %s/bin/pandora daemon, or `pandora daemon '
            '--install`' % after['link'])
        return 1, old_home
    program = launchd.agent_program(label, home)
    if not (program and through_current(program, data)):
        say('the launchd agent runs %s, not current. Run `pandora daemon --install` once '
            '(it restarts the daemon: check `pandora ps` first)' % (program or '(unknown)'))
        return 1, old_home
    if now:
        say('restarting now (--now): local runs end with exit 70 and are rerun by hand')
    elif not wait_for_safe(ps, wait=wait, clock=clock, sleep=sleep, say=say):
        say('gave up after %ds: current is %s, the daemon still runs %s. Run `pandora upgrade` '
            'again later, or `pandora upgrade --now`' % (wait, after['name'], old))
        return 75, old_home
    try:
        launchd.restart(label, run=launchctl, say=say)
    except launchd.Refused as error:
        say(str(error))
        return 1, old_home
    for _ in range(20):
        try:
            fresh = ping()
        except (OSError, ValueError):
            fresh = None
        if fresh and fresh.get('pid') != pong.get('pid'):
            say('daemon   pid %s runs %s (code %s)' % (fresh.get('pid'),
                version_label(fresh.get('home'), data), short_code(fresh.get('code'))))
            return 0, None
        sleep(0.5)
    say('the daemon has not answered since the restart; `pandora doctor` will say why')
    return 0, old_home
