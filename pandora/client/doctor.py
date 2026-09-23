"""`pandora doctor`: is this shell, in this worktree, wired the way routing assumes?

Every failure this looks for has already happened to somebody. A `pnpm` earlier
on PATH than the shim routes nothing and says nothing. A leaked
`PANDORA_ROUTE_DEPTH` makes every command a passthrough. A launcher symlinked
into `~/.local/bin` imported the package only when the cwd happened to be a
checkout. A marker that names a checkout since deleted, or an enrolment the
daemon's own config does not have, turns every claimed command into a silent
local run. None of these produce an error on their own; each of them shows up
later as "Pandora did not route my command", which is the hardest report to act
on.

So each check is one line, independent of the others, and proves one thing. A
check that needs the daemon fails with the reason when there is no daemon rather
than taking the rest down with it.

Read-only, all the way down: nothing here writes a file, starts a process that
writes one, or asks the daemon anything but `ping` -- whose answer carries the
worker-health reading the daemon already has cached, so not even a health poll
is triggered. `ps` is deliberately not used: it samples the pause gate, which
can move its counters. The one other process asked anything is `launchctl
print`, for whether launchd supervises the daemon that answered.
"""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from ..config.loader import FILENAME
from ..errors import ConfigError
from . import enrolment, placement, settings
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


def ping(sock_path, timeout=2.0):
    """The daemon's answer to `ping`, or raises OSError with the reason."""
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


def check_daemon(sock_path, launcher_home):
    """A daemon answers on the socket the shim will use, speaking this client's protocol."""
    try:
        answer = ping(sock_path)
    except FileNotFoundError:
        return check('daemon', FAIL, 'nothing at %s; start it with `pandora daemon` (claimed '
                     'commands fall back by size class until then)' % sock_path), None
    except ConnectionRefusedError:
        return check('daemon', FAIL, '%s exists but nobody listens: a daemon that died. '
                     'Start it with `pandora daemon`' % sock_path), None
    except (OSError, ValueError) as error:
        return check('daemon', FAIL, 'no answer on %s: %s' % (sock_path, error)), None
    if answer.get('t') == 'error':
        return check('daemon', FAIL, 'the daemon on %s refused: %s'
                     % (sock_path, answer.get('msg'))), None
    home = answer.get('home')
    detail = 'pid %s on %s, protocol v%s, worker %s' % (
        answer.get('pid'), sock_path, answer.get('v'), answer.get('worker') or '(none)')
    facts = {'pid': answer.get('pid'), 'socket': str(sock_path), 'home': home}
    expected = launcher_home or PACKAGE_HOME
    if not home:
        return check('daemon', WARN, detail + '; it predates `doctor` and does not say which '
                     'package it runs, so it cannot be compared with this client', **facts), answer
    if os.path.realpath(home) != os.path.realpath(expected):
        return check('daemon', WARN, '%s; it runs the package in %s and the client is %s. '
                     'Restart it from the checkout you mean' % (detail, home, expected),
                     **facts), answer
    return check('daemon', OK, detail + ', same package as the client', **facts), answer


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


def check_repository(cwd, config, sock_path):
    """This worktree's repository is enrolled on both sides: the marker and the daemon's config."""
    common = enrolment.common_dir(cwd)
    root = enrolment.worktree_root(cwd)
    if common is None:
        return [check('repository', FAIL, '%s is not inside a git repository' % cwd)]
    path = Path(common) / enrolment.MARKER
    if not path.is_file():
        has_config = root and (Path(root) / FILENAME).is_file()
        return [check('repository', FAIL,
                      'not enrolled (no %s)%s; nothing routes here'
                      % (path, '; it has a %s, so run `pandora enrol %s`' % (FILENAME, root)
                         if has_config else ''))]
    marker = enrolment.parse(path.read_text())
    out = [check('repository', OK, 'enrolled as %s: %d claimed form(s), marker %s'
                 % (marker.get('repo'), len(marker['claim']), path),
                 marker=str(path), claims=[' '.join(item) for item in marker['claim']])]
    home = marker.get('home')
    if home and not (Path(home) / 'pandora' / 'client' / 'shim.py').is_file():
        out.append(check('marker home', FAIL,
                         'the marker says the client lives in %s, which has no pandora '
                         'package (a removed checkout?). Claimed commands cannot start the '
                         'client; re-run `pandora enrol`' % home, home=home))
    if marker.get('sock') and os.path.realpath(marker['sock']) != os.path.realpath(sock_path):
        out.append(check('marker socket', WARN,
                         'the marker routes to %s but this doctor looked at %s; the shim '
                         'uses the marker' % (marker['sock'], sock_path)))
    if config is not None:
        known = settings.enrolment_for(config, cwd)
        if known is None:
            for repo in config['repos']:
                try:
                    if enrolment.common_dir(repo['root']) == common:
                        known = repo
                        break
                except OSError:
                    continue
        if known is None:
            out.append(check('daemon enrolment', FAIL,
                             'the marker claims commands but %s has no [[repos]] entry for '
                             'this repository, so the daemon passes every one of them through'
                             % (config.get('source') or settings.DEFAULT_PATH)))
        else:
            out.append(check('daemon enrolment', OK, '[[repos]] %s at %s'
                             % (known['name'], known['root'])))
    return out


def check_cwd(cwd):
    """Commands are typed from the worktree root, where they mean what they say."""
    root = enrolment.worktree_root(cwd)
    if root is None:
        return check('working directory', WARN, 'not inside a worktree')
    here = Path(cwd).resolve()
    if here == Path(root).resolve():
        return check('working directory', OK, 'the worktree root, %s' % root)
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

    eichler's `tools/validation/state.mjs` drops every PATH directory holding
    one from a queued job's environment (the slice note, Eichler change 2 and
    the open blast-radius item). Beside the shim it is what stops a queued job
    re-entering the shim; anywhere else it silently removes a directory of tools
    from every queued job.
    """
    marked = [entry for entry in dict.fromkeys(path_entries(env))
              if os.path.isfile(os.path.join(entry, SHIM_MARKER))]
    shimdir = os.path.dirname(shim) if shim else None
    stale = [entry for entry in marked
             if entry != shimdir and not is_shim(os.path.join(entry, 'pnpm'))]
    if stale:
        return check('shim markers', WARN,
                     'stale %s in %s: eichler\'s queued jobs drop that whole directory from '
                     'PATH' % (SHIM_MARKER, ', '.join(stale)), stale=stale)
    if shimdir and shimdir not in marked:
        return check('shim markers', WARN,
                     'no %s beside the shim in %s: eichler\'s queued jobs keep the shim on '
                     'PATH and only the depth guard stops them re-entering it'
                     % (SHIM_MARKER, shimdir))
    if not shimdir:
        return check('shim markers', INFO, 'no shim to look beside')
    return check('shim markers', OK, '%s beside the shim only' % SHIM_MARKER)


def check_supervision(pong, state, *, platform=None, launchctl=None, home=None):
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
    return check('daemon supervision', OK, 'launchd runs pid %s as %s, %s; %s; `pandora '
                 'daemon --restart` after updating the checkout' % (pid, label, runs, starts),
                 **facts)


# -- the whole report ------------------------------------------------------------

def run(*, state=None, config=None, env=None, cwd=None, runner=subprocess.run,
        launchctl=subprocess.run):
    env = dict(os.environ if env is None else env)
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
        _common, marker = enrolment.marker_for(cwd)
    except OSError:
        marker = None
    sock_path = Path((marker or {}).get('sock') or state_path / 'client.sock')

    pnpm = check_pnpm(env)
    checks.append(pnpm)
    checks.append(check_recursion(env))
    launched, launcher_home = check_launcher(env, run=runner)
    checks.append(launched)
    daemon, pong = check_daemon(sock_path, launcher_home)
    checks.append(daemon)
    checks.append(check_worker(pong, sock_path.parent))
    checks.append(check_supervision(pong, sock_path.parent, launchctl=launchctl))
    checks.extend(check_repository(cwd, loaded, state_path / 'client.sock'))
    checks.append(check_cwd(cwd))
    checks.append(check_variables(env))
    checks.append(check_shim_markers(env, pnpm['facts'].get('shim')))
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
