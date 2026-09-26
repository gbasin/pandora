"""`pandora doctor`: is this shell, in this worktree, wired the way routing assumes?

Every failure this looks for has already happened to somebody. A `pnpm` earlier
on PATH than the shim routes nothing and says nothing. A leaked
`PANDORA_ROUTE_DEPTH` makes every command a passthrough. A launcher symlinked
into `~/.local/bin` imported the package only when the cwd happened to be a
checkout. A claim file that names a checkout since deleted, or an enrollment
the daemon's own config does not have, turns every claimed command into a silent
local run. None of these produce an error on their own; each of them shows up
later as "Pandora did not route my command", which is the hardest report to act
on.

So each check is one line, independent of the others, and proves one thing. A
check that needs the daemon fails with the reason when there is no daemon rather
than taking the rest down with it.

Read-only, all the way down: nothing here writes a file, starts a process that
writes one, or asks the daemon anything but `ping` -- whose answer carries the
worker-health reading the daemon already has cached, so not even a health poll
is triggered. The other processes asked anything are `launchctl
print`, for whether launchd supervises the daemon that answered, and `git
rev-parse HEAD` in the checkout `current` was built from, for whether it has
moved on since.
"""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from ..engine import bundle
from ..config.loader import FILENAME
from ..errors import ConfigError
from . import drain, enrollment, install, placement, settings
from .health import DEFAULT_INTERVAL, STALE_FACTOR
from .protocol import Reader, VERSION, dump

PACKAGE_HOME = str(Path(__file__).resolve().parents[2])
SHIM_SIGNATURE = b'Pandora pnpm shim'
SHIM_MARKER = '.pandora-shim'
# Path fragments of the version managers that put their own `pnpm` on PATH. Each
# picks a pnpm per directory, so the one the shim hands a command to can differ
# from the one a person gets with the shim removed.
VERSION_MANAGERS = (('corepack', 'corepack'), ('/.volta/', 'Volta'), ('/mise/', 'mise'),
                    ('/.asdf/', 'asdf'), ('/.nodenv/', 'nodenv'), ('/fnm', 'fnm'))
OK, WARN, FAIL, INFO = 'ok', 'warn', 'fail', 'info'


def check(name, status, detail, **facts):
    return {'name': name, 'status': status, 'detail': detail, 'facts': facts}


def path_entries(env):
    """PATH as the shim walks it: an empty entry is the current directory."""
    return [entry or '.' for entry in (env.get('PATH') or '').split(':')]


def executable(path):
    return os.path.isfile(path) and os.access(path, os.X_OK)


def is_shim(path):
    try:
        with open(path, 'rb') as handle:
            return SHIM_SIGNATURE in handle.read(4096)
    except OSError:
        return False


def find_all(name, env):
    return [os.path.join(entry, name) for entry in path_entries(env)
            if executable(os.path.join(entry, name))]


def version_manager(path):
    """The version manager behind a `pnpm`, or None. Checked on both spellings."""
    for spelling in (path, os.path.realpath(path)):
        for fragment, label in VERSION_MANAGERS:
            if fragment in spelling:
                return label
    return None


# -- the checks ----------------------------------------------------------------

def check_pnpm(env):
    """The shim is the first `pnpm`, and the real one is found behind it the way the shim finds it."""
    found = find_all('pnpm', env)
    if not found:
        return check('pnpm on PATH', FAIL, 'no pnpm on PATH at all')
    first = found[0]
    if not is_shim(first):
        later = next((path for path in found[1:] if is_shim(path)), None)
        return check('pnpm on PATH', FAIL,
                     '`pnpm` is %s, not Pandora\'s shim%s; nothing typed in this shell routes'
                     % (first, ', which is later on PATH at ' + later if later
                        else ', and no shim is on PATH'), shim=None, first=first)
    # Exactly the shim's own walk: skip the directory it was found in, as a
    # string, and PANDORA_SHIM_DIR; take the next executable `pnpm`.
    selfdir = os.path.dirname(first) or '.'
    skip = {selfdir, env.get('PANDORA_SHIM_DIR') or selfdir}
    real = next((os.path.join(entry, 'pnpm') for entry in path_entries(env)
                 if entry not in skip and executable(os.path.join(entry, 'pnpm'))), None)
    facts = {'shim': first, 'shim_resolved': os.path.realpath(first), 'real': real,
             'real_resolved': os.path.realpath(real) if real else None}
    if real is None:
        return check('pnpm on PATH', FAIL, 'the shim is %s but there is no real pnpm behind '
                     'it; every pnpm exits 127' % first, **facts)
    if is_shim(real):
        return check('pnpm on PATH', FAIL,
                     'the next pnpm behind the shim (%s) is another Pandora shim; a command '
                     're-enters it until the depth guard exits 70. Keep one shim directory '
                     'on PATH, or set PANDORA_SHIM_DIR to the other spelling' % real, **facts)
    manager = version_manager(real)
    summary = 'shim %s, real pnpm %s' % (first, real)
    if facts['real_resolved'] != real:
        summary += ' (-> %s)' % facts['real_resolved']
    if manager:
        return check('pnpm on PATH', WARN,
                     '%s; the real pnpm is a %s shim, which chooses a pnpm per directory, '
                     'so a local run and a worker run can use different versions'
                     % (summary, manager), manager=manager, **facts)
    return check('pnpm on PATH', OK, summary, **facts)


def check_recursion(env):
    """The shim's recursion guard is not already set in this shell."""
    depth = env.get('PANDORA_ROUTE_DEPTH')
    if depth:
        also = ' (PANDORA_REAL_PNPM=%s too)' % env['PANDORA_REAL_PNPM'] \
            if env.get('PANDORA_REAL_PNPM') else ''
        return check('recursion guard', FAIL,
                     'PANDORA_ROUTE_DEPTH=%s is set%s: this shell is inside a Pandora run or '
                     'the variable leaked into it, so every pnpm here runs the real pnpm and '
                     'nothing routes. Unset it' % (depth, also), depth=depth)
    return check('recursion guard', OK, 'PANDORA_ROUTE_DEPTH is not set')


def check_launcher(env, *, run=subprocess.run):
    """The `pandora` on PATH imports its package from outside any checkout.

    Run from `/`, because `python -m` puts the cwd on sys.path: from inside a
    checkout, a launcher that cannot find its own package still appears to work.
    """
    found = find_all('pandora', env)
    if not found:
        return check('pandora on PATH', WARN, 'no `pandora` on PATH; the shim does not need '
                     'it, but `pandora ps`, `wait` and `result` do'), None
    launcher = found[0]
    # Without PYTHONPATH, too: this process was usually started by that very
    # launcher, which exported its own guess there, and inheriting the guess
    # would make a broken launcher look like a working one.
    clean = {key: value for key, value in env.items() if key != 'PYTHONPATH'}
    try:
        proc = run([launcher, 'doctor', '--package-home'], cwd='/', env=clean,
                   capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        return check('pandora on PATH', FAIL, '%s could not be run: %s' % (launcher, error),
                     launcher=launcher), None
    home = (proc.stdout or '').strip().splitlines()[-1:] or ['']
    home = home[0]
    if proc.returncode != 0 or not home:
        last = ((proc.stderr or '').strip().splitlines() or ['no output'])[-1]
        return check('pandora on PATH', FAIL,
                     '%s cannot import its package from outside a checkout: %s'
                     % (launcher, last), launcher=launcher), None
    if os.path.realpath(home) != os.path.realpath(PACKAGE_HOME):
        return check('pandora on PATH', WARN,
                     '%s runs the package in %s, but this doctor is %s; two checkouts are '
                     'in play' % (launcher, home, PACKAGE_HOME),
                     launcher=launcher, home=home), home
    return check('pandora on PATH', OK, '%s imports %s from any directory' % (launcher, home),
                 launcher=launcher, home=home), home


def ping(sock_path, timeout=30.0):
    """The daemon's answer to `ping`, or raises OSError with the reason.

    30 s, as for every operator verb: a starved daemon answered in 66 s on
    2026-09-24, and a doctor that gives up after 2 s reports it as not running.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(sock_path))
        sock.sendall(dump({'v': VERSION, 'op': 'ping'}))
        answer = Reader(sock).line()
    finally:
        sock.close()
    if answer is None:
        raise OSError('the daemon closed the connection without answering')
    return answer


def source_head(now, runner=subprocess.run):
    """The commit the checkout `current` came from is at now, or None if unknown."""
    source = (now or {}).get('meta', {}).get('source')
    if not source:
        return None
    try:
        proc = runner(['git', '-C', source, 'rev-parse', 'HEAD'], capture_output=True,
                      text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return (proc.stdout.strip() or None) if proc.returncode == 0 else None


def restart_advice(supervised, pid):
    """How to restart this daemon: `--restart` drains it, and refuses one launchd does not run.

    A stop does not drain: it ends local runs, so that advice says to look first.
    """
    answer = supervised(pid) if supervised else None
    if answer is True:
        return '`pandora daemon --restart`'
    if answer is False:
        return ('`pandora daemon --stop`, then start it again (launchd does not run it, so '
                '`--restart` cannot; a stop ends local runs, so check `pandora ps` first)')
    return ('`pandora daemon --restart` if launchd runs it, else check `pandora ps`, stop it '
            'and start it again')


def check_daemon(sock_path, launcher_home, data=None, runner=subprocess.run, supervised=None):
    """A daemon answers on the socket the shim will use, speaking this client's protocol.

    With a snapshot installed, the daemon should run the version `current`
    names; without one, the package the launcher runs, as before `upgrade`.
    `supervised(pid)` says whether launchd runs it, which decides how to
    restart it; None when unknown.
    """
    try:
        answer = ping(sock_path)
    except FileNotFoundError:
        return check('daemon', FAIL, 'nothing at %s; start it with `pandora daemon --install` '
                     '(until then a claimed command exits 70 where the client configuration '
                     'exists, and runs here unmanaged where it does not)' % sock_path), None
    except ConnectionRefusedError:
        return check('daemon', FAIL, '%s exists but nobody listens: a daemon that died. '
                     'Start it with `pandora daemon --install`, or `pandora daemon --restart` '
                     'when it is installed; until then a claimed command exits 70'
                     % sock_path), None
    except (OSError, ValueError) as error:
        return check('daemon', FAIL, 'no answer on %s: %s' % (sock_path, error)), None
    if answer.get('t') == 'error':
        return check('daemon', FAIL, 'the daemon on %s refused: %s'
                     % (sock_path, answer.get('msg'))), None
    home = answer.get('home')
    detail = 'pid %s on %s, worker %s' % (
        answer.get('pid'), sock_path, answer.get('worker') or '(none)')
    facts = {'pid': answer.get('pid'), 'socket': str(sock_path), 'home': home}
    expected = launcher_home or PACKAGE_HOME
    if not home:
        return check('daemon', WARN, detail + '; it predates `doctor` and does not say which '
                     'package it runs, so it cannot be compared with this client', **facts), answer
    now = install.installed(data) if data is not None else None
    if now and os.path.realpath(home) != now['path']:
        old = install.version_label(home, data)
        facts.update(current=now['name'], daemon_version=old)
        if not install.is_version(home, data):
            return check('daemon', WARN, '%s; daemon runs the checkout %s, current is %s. '
                         '`pandora daemon --install` runs it from current (that restarts '
                         'it: check `pandora ps` first)' % (detail, old, now['name']),
                         **facts), answer
        head = source_head(now, runner)
        if head and head != now['meta'].get('commit'):
            return check('daemon', WARN, '%s; daemon runs %s, current is %s, and %s is at %s '
                         'since; run `pandora upgrade --from %s`'
                         % (detail, old, now['name'], now['meta'].get('source'), head[:12],
                            now['meta'].get('source')), **facts), answer
        return check('daemon', WARN, '%s; daemon runs %s, current is %s; restart it: %s'
                     % (detail, old, now['name'], restart_advice(supervised, answer.get('pid'))),
                     **facts), answer
    if not now and os.path.realpath(home) != os.path.realpath(expected):
        return check('daemon', WARN, '%s; it runs the package in %s and the client is %s. '
                     'Restart it from the checkout you mean' % (detail, home, expected),
                     **facts), answer
    code = answer.get('code')
    try:
        # The files the daemon says it loaded, as they are in its home now: a
        # checkout that moved on, or a version directory, which never should.
        mine = (bundle.code_digest(Path(os.path.realpath(home)) / 'pandora',
                                   names=answer['code_modules'])
                if answer.get('code_modules') else None)
    except OSError:
        mine = None
    facts.update(code=code, client_code=mine)
    if code and mine and code != mine and now:
        again = ('`pandora upgrade --from %s`' % now['meta']['source']
                 if now['meta'].get('source')
                 else '`pandora upgrade --release %s`' % now['meta'].get('release', '<tag>'))
        return check('daemon', WARN, '%s; daemon code differs from %s on disk: something '
                     'edited the version directory. %s builds it again under a new name '
                     'and restarts the daemon after a drain'
                     % (detail, now['path'], again), **facts), answer
    if code and mine and code != mine:
        # Same checkout, different bytes: it was updated after the daemon started.
        return check('daemon', WARN, '%s; daemon code differs from the checkout; restart '
                     'it: %s' % (detail, restart_advice(supervised, answer.get('pid'))),
                     **facts), answer
    if now:
        return check('daemon', OK, '%s, runs current (%s)' % (detail, now['name']),
                     **facts), answer
    return check('daemon', OK, detail + ', same package as the client', **facts), answer


def check_install(data, launcher, shim, runner=subprocess.run):
    """The launchers run through `<data>/current`, which names a whole version.

    Information when there is no snapshot: an install that runs its checkout
    still works, it just changes live code whenever the checkout moves.
    """
    link = install.current_link(data)
    now = install.installed(data)
    if now is None:
        if os.path.lexists(link):
            return check('install', FAIL, '%s names %s, which holds no pandora package; run '
                         '`pandora upgrade --from <checkout>`'
                         % (link, os.path.realpath(link)))
        return check('install', INFO, 'no snapshot in %s: the launchers run their checkout, '
                     'so pulling it changes live code. `pandora upgrade` installs a version'
                     % data)
    meta = now['meta']
    facts = {'current': now['name'], 'path': now['path'], 'source': meta.get('source'),
             'commit': meta.get('commit')}
    stray = []
    for label, path in (('`pandora` on PATH', launcher), ('the pnpm shim', shim)):
        if path and not install.through_current(path, data):
            runs = os.path.dirname(os.path.dirname(install.chain_end(path)))
            stray.append('%s (%s) runs %s' % (label, path, install.version_label(runs, data)))
    if stray:
        return check('install', WARN, '%s, not current (%s); `pandora upgrade` re-points a '
                     'link into the checkout it upgrades or into a version directory; replace '
                     'any other link with one through current' % ('; '.join(stray), now['name']),
                     stray=stray, **facts)
    detail = 'current is %s, from %s' % (now['name'],
                                         meta.get('source') or meta.get('release')
                                         or '(unrecorded)')
    head = source_head(now, runner)
    if head and head != meta.get('commit'):
        # Not a warning: a checkout that moves on changes nothing live.
        detail += '; the checkout is at %s since, which `pandora upgrade --from %s` installs' \
                  % (head[:12], meta.get('source'))
    return check('install', OK, detail + '; `pandora` and the shim run through it', **facts)


def check_worker(pong, state):
    """The worker as the daemon last saw it. Never a fresh poll."""
    from ..cli import worker_line
    if pong is None:
        return check('worker', FAIL, 'unknown: only the daemon polls the worker, and it did '
                     'not answer')
    health = pong.get('health')
    source = 'the daemon'
    if health is None:
        # A daemon from before `pong` carried it: its own cache file, read as-is.
        try:
            health = json.loads((Path(state) / 'worker-health.json').read_text())
            source = 'the daemon\'s cache file'
            # Aged the way `Monitor.state` ages it, at the default interval: a
            # file nobody has rewritten for three polls says nothing about now.
            health['age_seconds'] = round(time.time() - (health.get('at') or 0))
            if health['age_seconds'] > DEFAULT_INTERVAL * STALE_FACTOR:
                health['reason'] = 'last reading %ds old' % health['age_seconds']
                health['worker'] = 'unknown'
        except (OSError, ValueError):
            return check('worker', WARN, 'the daemon does not report worker health and has '
                         'no cache file; `pandora ps` will say')
    line = worker_line(health) + ', from ' + source
    status = {'reachable': OK, 'degraded': WARN, 'down': FAIL}.get(health.get('worker'), WARN)
    return check('worker', status, line, worker=health.get('worker'))


def check_repository(cwd, config, sock_path, data=None):
    """This repository is enrolled on both sides, and this worktree's claim cache is fresh."""
    common = enrollment.common_dir(cwd)
    root = enrollment.worktree_root(cwd)
    if common is None:
        return [check('repository', FAIL, '%s is not inside a git repository' % cwd)]
    _common, source, kind = enrollment.source_for(cwd)
    registration = Path(common) / enrollment.REGISTRATION
    legacy = Path(common) / enrollment.MARKER
    if source is None:
        has_config = root and (Path(root) / FILENAME).is_file()
        return [check('repository', FAIL,
                      'not enrolled (no %s)%s; nothing routes here'
                      % (registration, '; it has a %s, so run `pandora enroll %s`'
                         % (FILENAME, root) if has_config else ''))]
    parsed = enrollment.parse(source.read_text())
    if registration.is_file():
        out = [check('repository', OK, 'enrolled as %s, registration %s'
                     % (parsed.get('repo'), registration), registration=str(registration))]
    elif legacy.is_file():
        out = [check('repository', WARN,
                     'enrolled by the old marker %s, which a worktree with no claim cache '
                     'routes by as it stands. Run `pandora enroll %s` once: it registers the '
                     'repository and each worktree then routes by its own %s'
                     % (legacy, root or cwd, FILENAME), marker=str(legacy))]
    else:
        out = [check('repository', WARN,
                     'a claim cache but no registration (%s); worktrees without a cache do '
                     'not route. Run `pandora enroll %s`' % (registration, root or cwd))]
    out += check_caches(cwd, root, common, kind)
    home = parsed.get('home')
    if home:
        # Read for one release, never acted on: the shim runs the client from
        # its own checkout, and three code versions were live at once on
        # 2026-09-24 because a file pinned another.
        fix = ('the next claimed command here rewrites it without the line' if kind == 'cache'
               else '`pandora enroll %s` rewrites it without the line' % (root or cwd))
        out.append(check('client home', INFO,
                         '%s names %s as the client home, which is no longer read: claimed '
                         'commands run the client from the checkout the shim is in. %s'
                         % (source, home, fix[0].upper() + fix[1:]), home=home))
    if parsed.get('sock') and os.path.realpath(parsed['sock']) != os.path.realpath(sock_path):
        out.append(check('client socket', WARN,
                         '%s routes to %s but this doctor looked at %s; the shim uses the file'
                         % (source, parsed['sock'], sock_path)))
    known = None
    if config is not None:
        known = settings.enrollment_for(config, cwd)
        if known is None:
            for repo in config['repos']:
                try:
                    if enrollment.common_dir(repo['root']) == common:
                        known = repo
                        break
                except OSError:
                    continue
        if known is None:
            out.append(check('daemon enrollment', FAIL,
                             'the repository is enrolled here but %s has no [[repos]] entry '
                             'for it, so the daemon passes every command through; `pandora '
                             'enroll %s` adds one'
                             % (config.get('source') or settings.DEFAULT_PATH, root or cwd)))
        else:
            out.append(check('daemon enrollment', OK, '[[repos]] %s at %s'
                             % (known['name'], known['root'])))
    return out


def check_caches(cwd, root, common, kind):
    """This worktree's claim cache, then every worktree's, by the shim's own freshness rule.

    Never a failure: a stale or missing cache costs one Python start, on the
    next command the shim finds it stale, and the daemon rewrites it then. A
    cache whose config changed without a newer mtime is found only by its
    digest, and only a claimed command reaches the daemon to rewrite it.
    """
    cache = enrollment.cache_path(cwd)
    state, why, parsed, seen = enrollment.cache_state(root or cwd, cache)
    if state == 'fresh':
        here = check('claim cache', OK, 'fresh: %d claimed form(s), %s; cache %s'
                     % (len(parsed['claim']), why, cache), cache=str(cache),
                     claims=[' '.join(item) for item in parsed['claim']])
    elif state == 'stale':
        # The shim sees a date, and then any command refreshes the cache; it
        # cannot see a digest, and then only a claimed command reaches the daemon.
        here = check('claim cache', WARN, 'cache stale for this worktree (%s); the next %s '
                     'refreshes it' % (why, 'command here' if seen else 'claimed command'),
                     cache=str(cache))
    elif kind == 'marker':
        here = check('claim cache', INFO, 'none yet; this worktree routes by the old marker '
                     'until the next claimed command writes %s' % cache, cache=str(cache))
    else:
        here = check('claim cache', WARN, 'none yet for this worktree; the next command '
                     'here asks the daemon and writes %s' % cache, cache=str(cache))
    counts = {'fresh': 0, 'stale': 0, 'missing': 0}
    stale = []
    for other, path in enrollment.caches_of(common):
        if other is None or not Path(other).is_dir():
            continue                     # a bare repository, or a pruned worktree
        found, _why, _parsed, _seen = enrollment.cache_state(other, path)
        counts[found] += 1
        if found == 'stale':
            stale.append(other)
    total = sum(counts.values())
    every = check('claim caches', INFO, '%d worktree(s): %d fresh, %d stale, %d without a '
                  'cache; each refreshes on its next command'
                  % (total, counts['fresh'], counts['stale'], counts['missing']),
                  stale_worktrees=stale, **counts)
    return [here, every]


def check_cwd(cwd):
    """Commands are typed from the worktree root, where they mean what they say."""
    root = enrollment.worktree_root(cwd)
    if root is None:
        return check('working directory', WARN, 'not inside a worktree')
    here = Path(cwd).resolve()
    if here == Path(root).resolve():
        return check('working directory', OK, 'the worktree root, %s' % root)
    try:
        _common, marker = enrollment.marker_for(cwd)
    except OSError:
        marker = None
    if enrollment.claims_nothing_here(cwd, marker):
        return check('working directory', WARN,
                     'you are in %s, below the worktree root %s. This repository claims '
                     'commands only at the root, so every command typed here runs as if '
                     'Pandora were not installed'
                     % (here.relative_to(Path(root).resolve()), root))
    return check('working directory', WARN,
                 'you are in %s, below the worktree root %s. A claimed command typed here '
                 'runs from the root when no argument names a path, and is refused with exit '
                 '64 (`run from the repo root to route`) when one does'
                 % (here.relative_to(Path(root).resolve()), root))


def check_variables(env):
    """Pandora's own switches, as set in this shell. Information, except a bad placement."""
    try:
        placement.parse(env.get(placement.ENV))
    except ValueError as error:
        return check('variables', FAIL, '%s; every claimed command exits 64 until it is '
                     'fixed' % error)
    said = []
    if env.get('PANDORA_OFF'):
        said.append('PANDORA_OFF=%s: every pnpm here bypasses Pandora entirely' % env['PANDORA_OFF'])
    if env.get(placement.ENV):
        said.append('%s=%s: claimed commands run in that lane' % (placement.ENV, env[placement.ENV]))
    for name in ('PANDORA_SHARDS', 'PANDORA_KEEP_GOING', 'PANDORA_SHIM_DIR', 'PANDORA_HOME',
                 'PANDORA_CONFIG'):
        if env.get(name):
            said.append('%s=%s' % (name, env[name]))
    if not said:
        return check('variables', OK, 'none of PANDORA_OFF, PANDORA_WHERE, PANDORA_SHARDS set')
    return check('variables', INFO, '; '.join(said))


def check_shim_markers(env, shim):
    """`.pandora-shim` sits beside the shim and nowhere else.

    A repository's job tooling may drop every PATH directory holding
    one from a queued job's environment. Beside the shim it is what stops a
    queued job re-entering the shim; anywhere else it silently removes a
    directory of tools from every queued job.
    """
    marked = [entry for entry in dict.fromkeys(path_entries(env))
              if os.path.isfile(os.path.join(entry, SHIM_MARKER))]
    shimdir = os.path.dirname(shim) if shim else None
    stale = [entry for entry in marked
             if entry != shimdir and not is_shim(os.path.join(entry, 'pnpm'))]
    if stale:
        return check('shim markers', WARN,
                     'stale %s in %s: queued jobs drop that whole directory from '
                     'PATH' % (SHIM_MARKER, ', '.join(stale)), stale=stale)
    if shimdir and shimdir not in marked:
        return check('shim markers', WARN,
                     'no %s beside the shim in %s: queued jobs keep the shim on '
                     'PATH and only the depth guard stops them re-entering it'
                     % (SHIM_MARKER, shimdir))
    if not shimdir:
        return check('shim markers', INFO, 'no shim to look beside')
    return check('shim markers', OK, '%s beside the shim only' % SHIM_MARKER)


def check_drain(state, *, clock=time.time):
    """A `draining` marker: a restart in progress, or one that never finished. None when absent."""
    marker = drain.read_marker(state, clock=clock)
    if marker is None:
        return None
    path = drain.marker_path(state)
    if marker['age'] >= drain.STALE_SECONDS:
        return check('restart drain', WARN,
                     '%s is %d min old: a restart that never finished. Clients ignore it. '
                     'Unless `pandora ps` says draining, remove it; `pandora daemon '
                     '--restart` also clears it' % (path, marker['age'] // 60),
                     marker=str(path), age_seconds=int(marker['age']), pid=marker.get('pid'))
    return check('restart drain', INFO,
                 'a restart is draining the daemon (asked by pid %s, %ds ago); commands wait '
                 'for it' % (marker.get('pid') or '?', marker['age']),
                 marker=str(path), age_seconds=int(marker['age']), pid=marker.get('pid'))


def check_supervision(pong, state, *, platform=None, launchctl=None, home=None,
                      upgraded=False):
    """Whether launchd supervises the daemon that answered, or nothing does.

    ok: the agent is loaded and its pid is the daemon's. warn: a daemon runs and
    launchd has no agent for it, so a crash or a reboot leaves no daemon. fail:
    the agent is loaded but its pid is not the daemon's -- a hand-started daemon
    holds the lock, and the agent's own daemon exits and is restarted every ten
    seconds -- or the agent is loaded and no daemon answers at all. Reads
    `launchctl print` and the plist only.

    Every line names the interpreter: the one the answering daemon runs under,
    and the one the plist pins. An agent that restarts every ten seconds is most
    often a daemon launchd started under a Python too old to import `tomllib`.
    """
    from . import launchd
    if (platform or sys.platform) != 'darwin':
        return check('daemon supervision', INFO, 'launchd is macOS only; not checked')
    label = launchd.label_for(state)
    agent = launchd.status(label, run=launchctl or subprocess.run)
    pid = (pong or {}).get('pid')
    pinned = launchd.agent_python(label, home) if agent['loaded'] else None
    facts = {'label': label, 'launchd_pid': agent['pid'], 'daemon_pid': pid,
             'launchd_state': agent['state'], 'python': (pong or {}).get('python'),
             'python_version': (pong or {}).get('python_version'), 'plist_python': pinned}
    starts = ('launchd starts it with %s' % pinned if pinned else
              'launchd starts it with the first python3 on its PATH (the plist sets no '
              'PANDORA_PYTHON; `pandora daemon --install` again pins it)')
    runs = ('interpreter %s (%s)' % (facts['python'], facts['python_version'] or '?')
            if facts['python'] else 'interpreter not reported by this daemon')
    if pong is None:
        if agent['loaded']:
            return check('daemon supervision', FAIL,
                         '%s is loaded in launchd (%s) but no daemon answers; %s; read %s'
                         % (label, agent['line'], starts,
                            Path(state) / 'logs' / 'daemon.log'), **facts)
        return check('daemon supervision', INFO, 'no daemon, and no launchd agent %s' % label,
                     **facts)
    if not agent['loaded']:
        return check('daemon supervision', WARN,
                     'pid %s was started by hand; nothing restarts it after a crash or a '
                     'reboot. `pandora daemon --install`; %s' % (pid, runs), **facts)
    if agent['pid'] != pid:
        return check('daemon supervision', FAIL,
                     'launchd has %s loaded (%s) but the daemon answering is pid %s, which '
                     'it did not start. `pandora daemon --stop`, then `pandora daemon '
                     '--restart`; %s' % (label, agent['line'], pid, starts), **facts)
    kind = launchd.agent_process_type(label, home)
    facts['process_type'] = kind
    if kind is not None and kind != launchd.PROCESS_TYPE:
        return check('daemon supervision', WARN,
                     'launchd runs pid %s as %s at ProcessType %s, the class macOS starves '
                     'first under load; run `pandora daemon --install` to rewrite it as %s '
                     '(that restarts the daemon without a drain: check `pandora ps` first)'
                     % (pid, label, kind or '(unset, which launchd treats as Standard)',
                        launchd.PROCESS_TYPE), **facts)
    return check('daemon supervision', OK, 'launchd runs pid %s as %s, %s; %s; %s restarts '
                 'it into new code' % (pid, label, runs, starts,
                                       '`pandora upgrade`' if upgraded
                                       else '`pandora daemon --restart`'), **facts)


# -- the whole report ------------------------------------------------------------

def run(*, state=None, config=None, env=None, cwd=None, runner=subprocess.run,
        launchctl=subprocess.run, data=None):
    env = dict(os.environ if env is None else env)
    # Where this user's versions live: a fact about the user, not the shell
    # being checked, so it comes from this process's own environment.
    data = Path(data) if data else install.data_root()
    cwd = cwd or os.getcwd()
    checks = []
    try:
        loaded = settings.load(config)
    except ConfigError as error:
        loaded = None
        checks.append(check('client config', FAIL, str(error)))
    state_path = Path(state or env.get('PANDORA_STATE')
                      or (loaded or {}).get('client', {}).get('state')
                      or settings.DEFAULT_STATE).expanduser()
    try:
        _common, source, _kind = enrollment.source_for(cwd)
        sock = enrollment.parse(source.read_text()).get('sock') if source else None
    except OSError:
        sock = None
    sock_path = Path(sock or state_path / 'client.sock')

    pnpm = check_pnpm(env)
    checks.append(pnpm)
    checks.append(check_recursion(env))
    launched, launcher_home = check_launcher(env, run=runner)
    checks.append(launched)
    checks.append(check_install(data, launched['facts'].get('launcher'),
                                pnpm['facts'].get('shim'), runner=runner))
    def supervised(pid):
        from . import launchd
        if sys.platform != 'darwin':
            return False
        agent = launchd.status(launchd.label_for(sock_path.parent), run=launchctl)
        return bool(agent['loaded'] and agent['pid'] == pid)
    daemon, pong = check_daemon(sock_path, launcher_home, data, runner=runner,
                                supervised=supervised)
    checks.append(daemon)
    checks.append(check_worker(pong, sock_path.parent))
    checks.append(check_supervision(pong, sock_path.parent, launchctl=launchctl,
                                    upgraded=install.installed(data) is not None))
    checks.extend(check_repository(cwd, loaded, state_path / 'client.sock', data))
    checks.append(check_cwd(cwd))
    checks.append(check_variables(env))
    checks.append(check_shim_markers(env, pnpm['facts'].get('shim')))
    # Last: shown only while a marker exists, after every row that is always there.
    drained = check_drain(sock_path.parent)
    if drained is not None:
        checks.append(drained)
    return {'ok': not any(item['status'] == FAIL for item in checks),
            'cwd': cwd, 'state': str(state_path), 'socket': str(sock_path),
            'package': PACKAGE_HOME, 'checks': checks}


def render(report):
    lines = ['%-4s  %-18s %s' % (item['status'], item['name'], item['detail'])
             for item in report['checks']]
    failed = sum(item['status'] == FAIL for item in report['checks'])
    lines.append('')
    lines.append('%d check(s) failed' % failed if failed else 'all checks passed')
    return '\n'.join(lines)
