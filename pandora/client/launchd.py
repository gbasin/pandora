"""`pandora daemon --install | --uninstall | --restart | --stop`: the daemon under launchd.

A daemon started by hand with `nohup` dies with a reboot, a logout, or a crash,
and nothing notices until a claimed command falls back. A launchd user agent
with `KeepAlive` restarts it on any exit and starts it at login, which is the
supervision the POC sketched in `experiments/client/launchd/` and never loaded.

Three things launchd does differently from a shell, each handled here:

* **The environment is nearly empty.** A launchd agent's PATH is
  `/usr/bin:/bin:/usr/sbin:/sbin`. The daemon runs `git`, `rsync` and `ssh` by
  name (`snapshot/freeze.py`, `snapshot/transfer.py`), the launcher runs
  `python3` by name, and the local lane runs the job's `pnpm` with the daemon's
  own PATH (`local.child_environment`). Homebrew's git and a Python new enough
  for `tomllib` are not on launchd's PATH, so the plist carries one: the
  directories of the `python3`, the real `pnpm` and the `node` this shell finds,
  then `/opt/homebrew/bin`, `/usr/local/bin` and the system directories. A
  directory holding Pandora's own shim is left out: the local lane's `pnpm`
  would otherwise re-enter the shim for every command. That can drop the
  interpreter's own directory -- uv installs `python3` in `~/.local/bin`, beside
  the shim -- and the launcher then found `/usr/bin/python3`, 3.9, which cannot
  import `tomllib`, so launchd restarted a daemon that died at import every ten
  seconds. So the plist also sets `PANDORA_PYTHON` to the interpreter running
  `--install`, which the launcher uses before anything on PATH.
* **The program is the checkout, not the symlink.** `ProgramArguments` names
  `<checkout>/bin/pandora` as the running client resolved it, so the agent runs
  the package this command came from. `~/.local/bin/pandora` may be re-pointed
  later; the agent should not silently follow.
* **One daemon per state directory is a lock, and launchd does not know it.** A
  `KeepAlive` agent whose daemon exits at once because a hand-started one holds
  `daemon.lock` is restarted every ten seconds for ever. So `--install` refuses
  while a daemon launchd does not own holds the lock, and says how to stop it.

The checkout moves often. A daemon keeps running the code it imported at start,
so after updating the checkout run `pandora daemon --restart`, which is
`launchctl kickstart -k`: launchd stops the daemon with SIGTERM and starts it
again from the same plist. Runs on the worker survive that; the daemon
re-attaches to them on start (`Daemon.resume_interrupted`).

`status` and `lock_holder` are read-only and are what `pandora doctor` uses.
Everything that talks to launchd goes through one `run` callable so the tests
never touch the real `launchctl`.
"""
import fcntl
import json
import os
import plistlib
import signal
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_LABEL = 'com.pandora.daemon'
LAUNCHCTL = '/bin/launchctl'
# Written into the state directory by `--install`, so `--restart`, `--uninstall`
# and `doctor` find a label that was not the default without being told it.
RECORD = 'launchd.json'
# Always on the agent's PATH, after the directories of the tools found here.
# `/usr/sbin` for `sysctl`, which the pause gate samples.
BASE_PATH = ('/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin', '/usr/sbin', '/sbin')
STOP_SECONDS = 10.0
RESTART_NOTE = ('after updating the checkout, run `pandora daemon --restart` '
                '(launchctl kickstart -k): the daemon runs the code it started with')
PACKAGE_HOME = Path(__file__).resolve().parents[2]


def launchctl(args, *, run=subprocess.run):
    """One `launchctl` call; a missing binary reads as a failed call, not a crash."""
    try:
        return run([LAUNCHCTL, *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        return subprocess.CompletedProcess([LAUNCHCTL, *args], 127, '', str(error))


def domain(uid=None):
    return 'gui/%d' % (os.getuid() if uid is None else uid)


def plist_path(label, home=None):
    return Path(home or Path.home()) / 'Library' / 'LaunchAgents' / (label + '.plist')


def launcher():
    """`bin/pandora` in the checkout this code was imported from, links resolved."""
    return (PACKAGE_HOME / 'bin' / 'pandora').resolve()


def label_for(state, given=None):
    """The label asked for, else the one `--install` recorded, else the default."""
    if given:
        return given
    try:
        return json.loads((Path(state) / RECORD).read_text())['label']
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_LABEL


# -- reading ---------------------------------------------------------------------

def status(label, *, uid=None, run=subprocess.run):
    """What launchd says about the agent: loaded or not, its state and pid.

    `launchctl print` rather than `list`, because it says `state = running` or
    `state = not running` in words and names the last exit. Only the service's
    own top-level `state =` and `pid =` lines are read; nested blocks (endpoints,
    the environment) repeat neither key at that indentation.
    """
    proc = launchctl(['print', '%s/%s' % (domain(uid), label)], run=run)
    if proc.returncode != 0:
        said = (proc.stderr or proc.stdout or '').strip().splitlines()
        return {'label': label, 'loaded': False, 'state': None, 'pid': None,
                'line': 'not loaded' + (' (%s)' % said[-1] if said else '')}
    return parse_print(label, proc.stdout)


def parse_print(label, text):
    state = pid = exit_line = None
    for raw in text.splitlines():
        # Top-level keys of the service block are indented by exactly one tab.
        if not raw.startswith('\t') or raw.startswith('\t\t'):
            continue
        key, _, value = raw.strip().partition(' = ')
        if key == 'state' and state is None:
            state = value
        elif key == 'pid' and pid is None:
            try:
                pid = int(value)
            except ValueError:
                pass
        elif key == 'last exit code' and exit_line is None:
            exit_line = value
    line = 'state = %s' % (state or 'unknown')
    if pid:
        line += ', pid = %d' % pid
    if exit_line:
        line += ', last exit code = %s' % exit_line
    return {'label': label, 'loaded': True, 'state': state, 'pid': pid, 'line': line}


def lock_holder(state):
    """The pid holding `daemon.lock`, or None when no daemon holds it. Read-only.

    The lock, not `daemon.json` and not the socket, is the one-daemon rule
    (`Daemon.acquire_lock`), so it is what is asked. A shared, non-blocking
    lock fails exactly when the daemon's exclusive one is held; when it
    succeeds it is released at once. The file is never created here.
    """
    path = Path(state) / 'daemon.lock'
    try:
        handle = path.open('r')
    except OSError:
        return None
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            text = handle.read().strip()
            try:
                return int(text.splitlines()[0])
            except (ValueError, IndexError):
                return -1                          # held, by a pid it did not write
        fcntl.flock(handle, fcntl.LOCK_UN)
        return None


# -- the plist -------------------------------------------------------------------

def is_shim_dir(directory):
    from .doctor import SHIM_MARKER, is_shim
    return (os.path.isfile(os.path.join(directory, SHIM_MARKER))
            or is_shim(os.path.join(directory, 'pnpm')))


def first_on_path(name, env, *, skip_shims=False):
    from .doctor import executable, path_entries
    for entry in path_entries(env):
        if not os.path.isabs(entry) or (skip_shims and is_shim_dir(entry)):
            continue
        candidate = os.path.join(entry, name)
        if executable(candidate):
            return candidate
    return None


def this_python(env):
    """The `python3` the launcher should find: this interpreter, by its PATH name.

    `sys.executable` is a versioned path (`.../python@3.14/bin/python3.14`) that
    a Homebrew major upgrade removes; the `python3` on PATH is the same file by
    a name that survives, when it is the same file. A different `python3` first
    on PATH may be the system's 3.9, which cannot import `tomllib`.
    """
    found = first_on_path('python3', env)
    if found and os.path.realpath(found) == os.path.realpath(sys.executable):
        return found
    return sys.executable


def service_path(env, *, python=None):
    """The PATH the agent runs with: the tools this shell finds, then the fixed list.

    The directory a tool was *found* in, not the one its symlink resolves to:
    Homebrew's `/opt/homebrew/bin/node` survives an upgrade, its Cellar target
    does not. `PANDORA_REAL_PNPM`, when set, is the real pnpm the shim chose.
    """
    found = [python or first_on_path('python3', env),
             env.get('PANDORA_REAL_PNPM') or first_on_path('pnpm', env, skip_shims=True),
             first_on_path('node', env, skip_shims=True)]
    entries = [os.path.dirname(path) for path in found if path] + list(BASE_PATH)
    return ':'.join(entry for entry in dict.fromkeys(entries) if not is_shim_dir(entry))


def render(label, *, program, config_path, state, path, state_arg=False, lang=None,
           python=None):
    """The agent's plist, as a dictionary `plistlib` writes."""
    arguments = [str(program), '--config', str(config_path)]
    if state_arg:
        arguments += ['--state', str(state)]
    arguments.append('daemon')
    log = str(Path(state) / 'logs' / 'daemon.log')
    environment = {'PATH': path}
    if python:
        environment['PANDORA_PYTHON'] = str(python)
    if lang:
        environment['LANG'] = lang
    return {
        'Label': label,
        'ProgramArguments': arguments,
        'EnvironmentVariables': environment,
        'RunAtLoad': True,
        # Restart on any exit. The POC's `SuccessfulExit = false` would leave a
        # daemon that exited 0 on SIGTERM from a stray `pandora daemon --stop`
        # stopped for good, and an unsupervised daemon is what this replaces.
        'KeepAlive': True,
        # launchd's own floor between restarts, stated so it is not a surprise:
        # a daemon that cannot start is retried every ten seconds, not in a loop.
        'ThrottleInterval': 10,
        'ProcessType': 'Background',
        'StandardOutPath': log,
        'StandardErrorPath': log,
        'WorkingDirectory': str(Path.home()),
    }


def agent_python(label, home=None):
    """The `PANDORA_PYTHON` the installed plist pins, or None. Read-only.

    None for a plist written before the key existed, or no plist: launchd then
    runs whichever `python3` is first on the agent's PATH.
    """
    try:
        with open(plist_path(label, home), 'rb') as handle:
            return (plistlib.load(handle).get('EnvironmentVariables') or {}).get('PANDORA_PYTHON')
    except (OSError, ValueError, plistlib.InvalidFileException, AttributeError):
        return None


# -- acting ----------------------------------------------------------------------

class Refused(Exception):
    """A verb that would leave two daemons, or none, when the caller meant one."""


def install(label, *, config_path, state, state_arg=False, env=None, uid=None, home=None,
            python=None, run=subprocess.run, say=print):
    env = dict(os.environ if env is None else env)
    before = status(label, uid=uid, run=run)
    holder = lock_holder(state)
    if holder and not (before['loaded'] and before['pid'] == holder):
        raise Refused(
            'a daemon (pid %s) already holds %s and launchd did not start it. Stop it '
            'first with `pandora daemon --stop` (SIGTERM, then up to %ds for it to '
            'let go), then run --install again. Runs on the worker keep going; the '
            'launchd daemon re-attaches to them'
            % (holder if holder > 0 else '?', Path(state) / 'daemon.lock', STOP_SECONDS))
    target = plist_path(label, home)
    # The interpreter running this install, by the stable name `this_python`
    # finds for it: proven able to import Pandora, since it is doing so now.
    interpreter = python or this_python(env)
    body = render(label, program=launcher(), config_path=config_path, state=state,
                  path=service_path(env, python=interpreter),
                  state_arg=state_arg, lang=env.get('LANG'), python=interpreter)
    Path(state).mkdir(parents=True, exist_ok=True)
    (Path(state) / 'logs').mkdir(exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, 'wb') as handle:
        plistlib.dump(body, handle)
    if before['loaded']:
        # A loaded agent keeps the plist it was loaded with; bootout, then
        # bootstrap, is how a rewritten one takes effect.
        launchctl(['bootout', '%s/%s' % (domain(uid), label)], run=run)
    loaded = launchctl(['bootstrap', domain(uid), str(target)], run=run)
    if loaded.returncode != 0:
        loaded = launchctl(['load', '-w', str(target)], run=run)
    if loaded.returncode != 0:
        raise Refused('launchctl could not load %s: %s'
                      % (target, (loaded.stderr or loaded.stdout).strip()))
    # Without `-k`: `RunAtLoad` has usually started it already, and `-k` would
    # SIGTERM a daemon that may not have installed its handler yet. This only
    # starts one when the bootstrap did not.
    launchctl(['kickstart', '%s/%s' % (domain(uid), label)], run=run)
    (Path(state) / RECORD).write_text(json.dumps({'label': label, 'plist': str(target)}) + '\n')
    after = status(label, uid=uid, run=run)
    say('wrote %s' % target)
    say('  runs     %s' % ' '.join(body['ProgramArguments']))
    say('  PATH     %s' % body['EnvironmentVariables']['PATH'])
    say('  PANDORA_PYTHON %s' % body['EnvironmentVariables']['PANDORA_PYTHON'])
    say('  log      %s' % body['StandardOutPath'])
    say('launchd  %s: %s' % (label, after['line']))
    say(RESTART_NOTE)
    return body


def uninstall(label, *, state, uid=None, home=None, run=subprocess.run, say=print):
    target = plist_path(label, home)
    before = status(label, uid=uid, run=run)
    if before['loaded']:
        out = launchctl(['bootout', '%s/%s' % (domain(uid), label)], run=run)
        if out.returncode != 0 and target.is_file():
            launchctl(['unload', '-w', str(target)], run=run)
        say('booted %s out of launchd (the daemon it ran has stopped)' % label)
    else:
        say('%s was not loaded in launchd' % label)
    if target.is_file():
        target.unlink()
        say('removed %s' % target)
    else:
        say('no plist at %s' % target)
    try:
        (Path(state) / RECORD).unlink()
    except OSError:
        pass
    say('nothing supervises the daemon now; start one with `pandora daemon` or '
        '`pandora daemon --install`')


def restart(label, *, uid=None, run=subprocess.run, say=print):
    before = status(label, uid=uid, run=run)
    if not before['loaded']:
        raise Refused('%s is not loaded in launchd; `pandora daemon --install` first, or '
                      'restart a hand-started daemon by stopping it and starting it' % label)
    out = launchctl(['kickstart', '-k', '%s/%s' % (domain(uid), label)], run=run)
    if out.returncode != 0:
        raise Refused('launchctl kickstart failed: %s' % (out.stderr or out.stdout).strip())
    say('launchd  %s: %s' % (label, status(label, uid=uid, run=run)['line']))


def stop(label, *, state, uid=None, run=subprocess.run, kill=os.kill, clock=time.monotonic,
         sleep=time.sleep, say=print, wait=STOP_SECONDS):
    """SIGTERM the daemon holding the lock and wait for it to let go.

    Refused for a daemon launchd owns: `KeepAlive` would start another at once,
    so the verb that means "stop" there is `--uninstall`.
    """
    holder = lock_holder(state)
    if not holder:
        say('no daemon holds %s' % (Path(state) / 'daemon.lock'))
        return
    if holder < 0:
        raise Refused('%s is held but names no pid; find the process with `lsof %s`'
                      % (Path(state) / 'daemon.lock', Path(state) / 'daemon.lock'))
    current = status(label, uid=uid, run=run)
    if current['loaded'] and current['pid'] == holder:
        raise Refused('launchd runs this daemon as %s and would restart it; '
                      '`pandora daemon --uninstall` stops it for good, `--restart` '
                      'restarts it' % label)
    kill(holder, signal.SIGTERM)
    deadline = clock() + wait
    while clock() < deadline:
        if lock_holder(state) != holder:
            say('stopped the daemon (pid %d)' % holder)
            return
        sleep(0.2)
    raise Refused('pid %d still holds the lock after %ds of SIGTERM; it may be finishing '
                  'a write. Look at it with `ps -p %d`' % (holder, wait, holder))
