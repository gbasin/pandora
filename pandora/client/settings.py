"""`~/.config/pandora/config.toml`: the worker, the state directory, the enrolments.

TOML rather than the POC's JSON because this is a file a person edits, and
because the repository configuration it points at is TOML too. It is read on
every daemon connection, so enrolling a repository or pointing at a different
worker never needs a restart -- a restart drops every attached client, which is
too high a price for changing a hostname.

    [worker]
    host = "ubuntu@10.0.0.1"
    engine_root = "pandora-engine"      # relative to the worker's home
    budget_mib = 14000                  # optional; the engine measures its own

    [client]
    state = "~/.local/state/pandora/default"
    fallback_slots = 2
    fallback_wait_seconds = 0

    [local]
    budget_mib = 0                      # 0: this machine's RAM minus the reserve
    reserve_mib = 4096                  # what the agents, the editors and the OS keep
    max_running = 4
    one_active_per_worktree = true
    drift = "warn"                      # off | warn | fail; a job may override it
    queue_timeout_seconds = 0           # 0: wait for the budget as long as it takes

    # The "stop piling onto a broken machine" gate. Sampled when something wants
    # in, never on a timer. A job held here has provably not run; a job that waits
    # the whole of max_wait_seconds exits 70 rather than being started anyway.
    [local.pause]
    enabled = true
    sample_seconds = 3
    swap_growth_mib_per_minute = 256    # growth, not level: yesterday's swap is not news
    psi_full_avg10 = 20.0               # Linux /proc/pressure/memory
    free_percent = 5.0                  # macOS memory_pressure -Q, or the vm_stat sum
    load_per_cpu = 8.0                  # the backstop signal; 0 disables it
    max_wait_seconds = 300

    [[repos]]
    name = "eichler"
    root = "/Users/me/Code/eichler"     # the git common dir's worktree, or any worktree
    config = "~/.config/pandora/repos/eichler.pandora.toml"   # used only if the repo has none
"""
import os
import tomllib
from pathlib import Path

from ..errors import ConfigError
from . import pressure

DEFAULT_PATH = Path('~/.config/pandora/config.toml')
DEFAULT_STATE = Path('~/.local/state/pandora/default')

DEFAULTS = {
    'worker': {'host': '', 'engine_root': 'pandora-engine', 'budget_mib': None,
               'ssh_persist': '10m'},
    'client': {'state': str(DEFAULT_STATE), 'fallback_slots': 2,
               'fallback_wait_seconds': 0.0, 'max_wait_seconds': 0},
    'repos': [],
    # The fake backend stays available, because a test that needs a worker is a
    # test that does not run. `mode` is only consulted when it is not "worker".
    'backend': {'mode': 'worker'},
    # The local lane's budget and its two exclusivity rules. `budget_mib` of 0
    # means "this machine's RAM minus the reserve", which is the honest default:
    # a number typed into a file goes stale the moment the Mac is replaced.
    'local': {'budget_mib': 0, 'reserve_mib': 4096, 'max_running': 4,
              'one_active_per_worktree': True, 'drift': 'warn',
              'queue_timeout_seconds': 0, 'pause': dict(pressure.DEFAULTS)},
}


def _expand(value):
    return str(Path(value).expanduser()) if value else value


def normalise(raw):
    config = {'worker': dict(DEFAULTS['worker']), 'client': dict(DEFAULTS['client']),
              'backend': dict(DEFAULTS['backend']), 'local': dict(DEFAULTS['local']),
              'repos': []}
    for section in ('worker', 'client', 'backend', 'local'):
        block = raw.get(section, {})
        if not isinstance(block, dict):
            raise ConfigError('[%s] must be a table' % section)
        unknown = sorted(set(block) - set(DEFAULTS[section]))
        if unknown:
            raise ConfigError('[%s] has unknown key%s %s; allowed: %s'
                              % (section, '' if len(unknown) == 1 else 's', ', '.join(unknown),
                                 ', '.join(sorted(DEFAULTS[section]))))
        config[section].update(block)
    # `[local.pause]` is the one nested table, and its keys are checked with the
    # same closed-schema rule as everything else: a misspelled threshold that is
    # silently ignored is a gate that is silently off.
    pause = dict(pressure.DEFAULTS)
    given = config['local'].get('pause') or {}
    if not isinstance(given, dict):
        raise ConfigError('[local.pause] must be a table')
    unknown = sorted(set(given) - set(pressure.DEFAULTS))
    if unknown:
        raise ConfigError('[local.pause] has unknown key%s %s; allowed: %s'
                          % ('' if len(unknown) == 1 else 's', ', '.join(unknown),
                             ', '.join(sorted(pressure.DEFAULTS))))
    pause.update(given)
    config['local']['pause'] = pause
    config['client']['state'] = _expand(config['client']['state'])
    for index, item in enumerate(raw.get('repos', [])):
        if not isinstance(item, dict):
            raise ConfigError('repos[%d] must be a table' % index)
        unknown = sorted(set(item) - {'name', 'root', 'config'})
        if unknown:
            raise ConfigError('repos[%d] has unknown key%s %s; allowed: config, name, root'
                              % (index, '' if len(unknown) == 1 else 's', ', '.join(unknown)))
        for key in ('name', 'root'):
            if key not in item:
                raise ConfigError('repos[%d] is missing %s' % (index, key))
        config['repos'].append({'name': item['name'], 'root': _expand(item['root']),
                                'config': _expand(item.get('config', ''))})
    names = [repo['name'] for repo in config['repos']]
    if len(set(names)) != len(names):
        raise ConfigError('two enrolments share a name: ' + ', '.join(sorted(names)))
    return config


def load(path=None):
    path = Path(path or os.environ.get('PANDORA_CONFIG') or DEFAULT_PATH).expanduser()
    if not path.is_file():
        config = normalise({})
        config['source'] = None
        return config
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as error:
        raise ConfigError('%s is not valid TOML: %s' % (path, error)) from None
    config = normalise(raw)
    config['source'] = str(path)
    return config


def enrolment_for(config, cwd):
    """Which enrolment, if any, owns this directory.

    Matching is by path prefix against the enrolled root *and* against every
    sibling worktree of it, because the point of enrolling a repository is that
    all of its worktrees are enrolled. The daemon resolves the sibling case by
    git common directory; this function handles the simple containment case so
    that a caller with no git available still gets an answer.
    """
    cwd = str(Path(cwd).resolve())
    best = None
    for repo in config['repos']:
        root = str(Path(repo['root']).resolve())
        if cwd == root or cwd.startswith(root.rstrip('/') + '/'):
            if best is None or len(root) > len(str(Path(best['root']).resolve())):
                best = repo
    return best


def state_dir(config):
    path = Path(config['client']['state']).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path
