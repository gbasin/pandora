"""Strict loader for a repo-owned `pandora.toml`.

The file is data, never code: it is read with `tomllib`, every key is checked
against a closed schema, and an unknown key is refused with the allowed set
printed beside it.

This is the v0.2-slice subset of the contract drafted in
`notes/repo-config-contract-draft.md`. What it keeps: which argv forms Pandora
claims, the options Pandora consumes itself, the pre-flight validator, the size
class, the environment contract, the secret exclusions, the output paths and the
fallback policy. What the slice drops, and why:

  `ci_workflow` / `ci_job`    import by reference is decided against for v0.2
                              (direction note, decision 4): CI is linted, not
                              read. The importer stays in experiments/repo-config.
  `services` / network pod    a run is a machine with its own dockerd, so the
                              repository's compose stack boots unmodified and
                              there is nothing for Pandora to wire.
  `pins`                      the golden's toolchain is pinned by `[worker]`,
                              whose fingerprint is the golden's identity.

`shards` is in, in both tiers. Tier 1 is a shard index handed to the job and
nothing else; tier 2 adds a `plan` command that emits a JSON inventory, the
build-once outputs the shards mount, and a per-shard report Pandora checks
against that inventory. The difference is a receipt, not a speed: a tier-1
result says `unverified`, because `--shard=2/4` on its own proves only that
something was asked to run, never that the partition was complete.

The loader does not re-implement the repository's command line. A job declares
the literal forms Pandora claims, the options Pandora itself consumes, the flags
whose value it must not read, and an explicit refusal list; everything else is
forwarded verbatim to the repository's own runner, which is the only thing that
knows whether `S0-01` is a journey. Since the slice, that runner also gets to
say so *before* anything is queued, through `validate`.
"""
import re
import tomllib
from pathlib import Path

from ..errors import ConfigError

VERSION = 1
NAME = re.compile(r'[a-z][a-z0-9-]*\Z')
FLAG = re.compile(r'-{1,2}[A-Za-z][A-Za-z0-9-]*\Z')
TOKEN = re.compile(r'\{([^{}]+)\}')
VARIABLE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
# Every way a remote submission can fail to proceed, named once. The list is
# closed because it is also the fallback policy's vocabulary: a cause nobody can
# spell is a cause nobody can decide about.
FAULTS = ('daemon-unreachable', 'daemon-closed', 'handshake-timeout',
          # `worker-down` is `worker-unreachable` already known, from the health
          # poll, rather than discovered by paying an SSH timeout.
          'worker-down',
          'worker-unreachable', 'snapshot-failed', 'transfer-failed',
          'queue-timeout', 'admission-refused', 'engine-error')
OUTPUTS = ('artifacts', 'writeback', 'evidence')
EXTRA = ('local', 'reject')
ARGS = ('none', 'required', 'optional')
SIZES = ('small', 'medium', 'large', 'xlarge')
# The local lane's fallback verdicts. `fail` is the v0.1 spelling of `refuse`.
FALLBACKS = ('local', 'refuse')
DRIFTS = ('off', 'warn', 'fail')
GITS = ('none', 'synthetic')
# What a cancel is allowed to send first. SIGKILL is not offered: it is what the
# grace escalates to, and a job that asks for it directly is asking for no grace.
SIGNALS = ('SIGINT', 'SIGTERM', 'SIGHUP', 'SIGQUIT')
FILENAME = 'pandora.toml'


# --- primitives -------------------------------------------------------------

def _table(value, where):
    if not isinstance(value, dict):
        raise ConfigError(where + ' must be a table')
    return value


def _keys(value, where, required=(), optional=()):
    _table(value, where)
    allowed = set(required) | set(optional)
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError('%s has unknown key%s %s; allowed: %s' % (
            where, '' if len(unknown) == 1 else 's', ', '.join(unknown),
            ', '.join(sorted(allowed))))
    missing = sorted(set(required) - set(value))
    if missing:
        raise ConfigError('%s is missing %s' % (where, ', '.join(missing)))
    return value


def _str(value, where, pattern=None, allow_empty=False):
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ConfigError(where + ' must be a nonempty string')
    if pattern is not None and not pattern.fullmatch(value):
        raise ConfigError(where + ' is not a valid name: ' + value)
    return value


def _strs(value, where, pattern=None, unique=False):
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise ConfigError(where + ' must be a list of strings')
    for item in value:
        _str(item, where + ' entry', pattern)
    if unique and len(set(value)) != len(value):
        raise ConfigError(where + ' has duplicate entries')
    return list(value)


def _int(value, where, low=1, high=2 ** 31):
    if type(value) is not int or not low <= value <= high:
        raise ConfigError('%s must be an integer from %d through %d' % (where, low, high))
    return value


def _bool(value, where):
    if type(value) is not bool:
        raise ConfigError(where + ' must be true or false')
    return value


def _env(value, where):
    _table(value, where)
    for key, item in value.items():
        if not VARIABLE.fullmatch(key):
            raise ConfigError(where + ' has an invalid variable name: ' + key)
        _str(item, where + '.' + key, allow_empty=True)
    return dict(value)


def _names(value, where):
    return _strs(value, where, VARIABLE, unique=True)


def _choice(value, where, allowed):
    if value not in allowed:
        raise ConfigError('%s must be one of %s, not %r' % (where, ', '.join(allowed), value))
    return value


def _inside(path, where):
    if path.startswith('/') or '..' in path.split('/'):
        raise ConfigError('%s must stay inside the worktree: %s' % (where, path))
    return path


def _no_template(text, where):
    match = TOKEN.search(text)
    if match:
        raise ConfigError('%s uses template value {%s}; the slice substitutes only {args}'
                          % (where, match.group(1)))
    return text


# --- job parts --------------------------------------------------------------

def _option(value, where):
    _keys(value, where, {'name'}, {'sets', 'forward', 'writeback'})
    name = _str(value['name'], where + '.name', FLAG)
    if not name.startswith('--'):
        raise ConfigError(where + '.name must be a long option: ' + name)
    return {'name': name,
            'sets': _str(value.get('sets', name.lstrip('-').replace('-', '_')), where + '.sets'),
            'forward': _bool(value.get('forward', False), where + '.forward'),
            'writeback': _bool(value.get('writeback', False), where + '.writeback')}


def _on_extra(value, where):
    _keys(value, where, {'action'}, {'message'})
    return {'action': _choice(value['action'], where + '.action', EXTRA),
            'message': _str(value['message'], where + '.message') if 'message' in value else None}


def _form(value, where):
    _keys(value, where, {'prefix'}, {'on_extra'})
    prefix = _strs(value['prefix'], where + '.prefix')
    if not prefix:
        raise ConfigError(where + '.prefix must not be empty')
    return {'prefix': prefix,
            'on_extra': _on_extra(value['on_extra'], where + '.on_extra')
                        if 'on_extra' in value else None}


def _reject(value, where):
    _keys(value, where, {'args', 'message'})
    args = _strs(value['args'], where + '.args', unique=True)
    if not args:
        raise ConfigError(where + '.args must not be empty')
    return {'args': args, 'message': _str(value['message'], where + '.message')}


def _fallback(value, where):
    """`fallback = "local"` or `"refuse"`, or the long form with an `on` list.

    The short form is the one a job should use, because the decision it expresses
    is binary: when a remote submission does not proceed, is this job allowed
    into the local lane or not. The long form survives because a repository may
    want the answer to depend on *why*, and because v0.1 wrote it.

    Declaring nothing is not the same as declaring `local`. An undeclared job is
    decided by its size class at the moment it would fall back, which is the rule
    that keeps a `large` browser suite off this Mac without anyone remembering to
    write it down.
    """
    if isinstance(value, str):
        return {'action': _choice(value, where, FALLBACKS), 'on': list(FAULTS), 'notice': None}
    _keys(value, where, {'action'}, {'on', 'notice'})
    action = _choice(value['action'], where + '.action', FALLBACKS + ('fail',))
    fallback = {
        'action': 'refuse' if action == 'fail' else action,
        'on': _strs(value.get('on', list(FAULTS)), where + '.on', unique=True),
        'notice': _str(value['notice'], where + '.notice') if 'notice' in value else None,
    }
    for item in fallback['on']:
        _choice(item, where + '.on entry', FAULTS)
    return fallback


def _cancel(value, where):
    """How a cancel reaches this job's process tree: one signal, then a grace.

    Real suites need real graces -- eichler's journeys ask for 240 s to tear down
    a compose stack, its surfaces ask for SIGINT so Playwright writes its trace.
    Pandora's own escalation to SIGKILL is not configurable: the grace is how
    long a job gets to clean up, never whether it may refuse to die.
    """
    _keys(value, where, (), {'signal', 'grace_ms'})
    return {'signal': _choice(value.get('signal', 'SIGTERM'), where + '.signal', SIGNALS),
            'grace_ms': _int(value.get('grace_ms', 15000), where + '.grace_ms', 0, 3600000)}


def _output(value, where):
    _keys(value, where, {'kind', 'paths'}, {'requires_option'})
    output = {'kind': _choice(value['kind'], where + '.kind', OUTPUTS),
              'paths': _strs(value['paths'], where + '.paths', unique=True),
              'requires_option': None}
    if not output['paths']:
        raise ConfigError(where + '.paths must not be empty')
    for path in output['paths']:
        _inside(path, where + '.paths')
    if 'requires_option' in value:
        output['requires_option'] = _str(value['requires_option'], where + '.requires_option')
    return output


def _writeback_path(path, where):
    """A write-back path names a file, a directory, or files in one directory.

    The engine has to pull these out of an instance, and it can pull a path,
    not a search. So a glob may appear only in the last component, and that
    component must have a directory above it: `fixtures/*.ledger.jsonl` is one
    pull of `fixtures`, while `*.json` or `**/x.json` would be a pull of the
    whole tree to find out what matched.
    """
    parts = path.rstrip('/').split('/')
    if '**' in path:
        raise ConfigError('%s: write-back path %s uses **; a glob may match file names in '
                          'one directory only' % (where, path))
    if any(char in part for part in parts[:-1] for char in '*?['):
        raise ConfigError('%s: write-back path %s has a glob in a directory component; a '
                          'glob may match file names in one directory only' % (where, path))
    if len(parts) == 1 and any(char in parts[0] for char in '*?['):
        raise ConfigError('%s: write-back path %s globs the worktree root; name the '
                          'directory it lives in' % (where, path))
    return path


def _argv_with_args(value, where, *, allow_args=True, tokens=True):
    """An argv list that may splice the forwarded arguments once, at `{args}`.

    `tokens=False` leaves the remaining braces to the caller, which is what the
    shard plan needs: it substitutes `{n}` and `{plan}` as well, and checking
    those here would mean this function knowing about sharding.
    """
    argv = _strs(value, where)
    if not argv:
        raise ConfigError(where + ' must not be empty')
    splices = [index for index, item in enumerate(argv) if item == '{args}']
    if len(splices) > 1:
        raise ConfigError(where + ' uses {args} more than once')
    if splices and not allow_args:
        raise ConfigError(where + ' may not use {args}')
    for item in argv:
        if tokens and item != '{args}':
            _no_template(item, where)
    return argv, (splices[0] if splices else None)


def _run(value, where):
    _keys(value, where, {'argv'}, {'cwd', 'env', 'unset'})
    argv, args_at = _argv_with_args(value['argv'], where + '.argv')
    run = {'argv': argv, 'args_at': args_at,
           'cwd': _inside(_str(value.get('cwd', '.'), where + '.cwd'), where + '.cwd'),
           'env': _env(value.get('env', {}), where + '.env'),
           'unset': _names(value.get('unset', []), where + '.unset')}
    for key, item in run['env'].items():
        _no_template(item, where + '.env.' + key)
    overlap = sorted(set(run['unset']) & set(run['env']))
    if overlap:
        raise ConfigError('%s both sets and unsets %s' % (where, ', '.join(overlap)))
    return run


def _validate(value, where):
    """The repository's own pre-flight check, run locally before anything queues.

    The owner's rule: a bad flag must be rejected now, in milliseconds, not after
    a container boot. Pandora cannot know what a valid selector is, so it asks
    the repository -- but it asks *here*, in the worktree, with a deadline and no
    network, and it treats a non-zero exit as the repository's own refusal.
    """
    _keys(value, where, {'argv'}, {'cwd', 'env', 'timeout_ms'})
    argv, args_at = _argv_with_args(value['argv'], where + '.argv')
    return {'argv': argv, 'args_at': args_at,
            'cwd': _inside(_str(value.get('cwd', '.'), where + '.cwd'), where + '.cwd'),
            'env': _env(value.get('env', {}), where + '.env'),
            'timeout_ms': _int(value.get('timeout_ms', 5000), where + '.timeout_ms', 50, 60000)}


WHERE = ('remote', 'local')

SHARD_TOKEN = re.compile(r'\{(i|n)\}')
PLAN_TOKEN = re.compile(r'\{(n|plan)\}')


def _shard_text(text, where, pattern, *, required=()):
    """A string in which only the named tokens may appear."""
    for match in TOKEN.finditer(text):
        if not pattern.fullmatch(match.group(0)):
            raise ConfigError('%s uses unknown template value %s' % (where, match.group(0)))
    missing = [token for token in required if token not in text]
    if missing:
        raise ConfigError('%s must use %s' % (where, ', '.join(missing)))
    return text


def _plan_argv(value, where):
    """The tier-2 plan command: `{args}` once, plus `{n}` and `{plan}`."""
    argv, args_at = _argv_with_args(value, where, tokens=False)
    rendered = [item if item == '{args}'
                else _shard_text(item, where + ' entry', PLAN_TOKEN) for item in argv]
    if not any('{plan}' in item for item in rendered):
        raise ConfigError(where + ' must write its inventory to {plan}')
    if not any('{n}' in item for item in rendered):
        raise ConfigError(where + ' must be told the shard count with {n}')
    return rendered, args_at


def _shards(value, where):
    """How a job is cut into shards, and what proves the cut was honest.

    Two strategies, because two real repositories need different ones: `argv`
    appends a rendered flag to the command, `env` sets named variables. Both
    also get `PANDORA_SHARD_INDEX` and `PANDORA_SHARD_TOTAL`, so a runner that
    wants neither spelling can read the pair.

    `plan` is what makes a result *verified*. Without it Pandora can say a shard
    ran; with it Pandora holds the planned partition beside every shard's own
    report and refuses a fan-out whose observed test ids are not exactly that
    partition. `plan` without `report` is therefore refused: an inventory that
    nothing is checked against is decoration.
    """
    _keys(value, where, {'strategy'},
          {'template', 'env', 'default', 'max', 'plan', 'expect_flag', 'report',
           'plan_outputs'})
    strategy = _choice(value['strategy'], where + '.strategy', ('argv', 'env'))
    shards = {'strategy': strategy, 'template': None, 'env': {},
              'plan': None, 'plan_args_at': None, 'expect_flag': None,
              'report': None, 'plan_outputs': []}

    if strategy == 'argv':
        if 'env' in value:
            raise ConfigError(where + ".env belongs to strategy = 'env'")
        if 'template' not in value:
            raise ConfigError(where + " with strategy = 'argv' needs a template")
        shards['template'] = _shard_text(_str(value['template'], where + '.template'),
                                         where + '.template', SHARD_TOKEN,
                                         required=('{i}', '{n}'))
    else:
        if 'template' in value:
            raise ConfigError(where + ".template belongs to strategy = 'argv'")
        shards['env'] = _env(value.get('env', {}), where + '.env')
        if not shards['env']:
            raise ConfigError(where + " with strategy = 'env' needs at least one variable")
        for key, item in shards['env'].items():
            _shard_text(item, '%s.env.%s' % (where, key), SHARD_TOKEN)

    shards['default'] = _int(value.get('default', 1), where + '.default', 1, 64)
    shards['max'] = _int(value.get('max', shards['default']), where + '.max', 1, 64)
    if shards['max'] < shards['default']:
        raise ConfigError('%s.max %d is below its default %d'
                          % (where, shards['max'], shards['default']))

    if 'plan' in value:
        shards['plan'], shards['plan_args_at'] = _plan_argv(value['plan'], where + '.plan')
        if value.get('expect_flag'):
            shards['expect_flag'] = _str(value['expect_flag'], where + '.expect_flag', FLAG)
        if 'report' not in value:
            raise ConfigError(where + '.plan needs a report path: an inventory nothing is '
                                      'checked against proves nothing')
        shards['report'] = _inside(_shard_text(_str(value['report'], where + '.report'),
                                               where + '.report', SHARD_TOKEN),
                                   where + '.report')
        shards['plan_outputs'] = _strs(value.get('plan_outputs', []),
                                       where + '.plan_outputs', unique=True)
        for path in shards['plan_outputs']:
            _inside(path, where + '.plan_outputs')
    else:
        for key in ('expect_flag', 'report', 'plan_outputs'):
            if key in value:
                raise ConfigError('%s.%s needs a plan; without one there is nothing to '
                                  'check against' % (where, key))
    return shards


JOB_REQUIRED = {'id', 'forms', 'run'}
JOB_OPTIONAL = {'summary', 'tool', 'size', 'args', 'options', 'value_flags', 'reject',
                'outputs', 'fallback', 'on_extra', 'usage', 'reject_if_set',
                'timeout_minutes', 'validate', 'where', 'singleton', 'shards',
                'cancel', 'drift', 'git'}


def _job(value, index):
    where = 'jobs[%d]' % index
    _keys(value, where, JOB_REQUIRED, JOB_OPTIONAL)
    job_id = _str(value['id'], where + '.id', NAME)
    where = 'jobs.' + job_id

    options = [_option(item, '%s.options[%d]' % (where, position))
               for position, item in enumerate(value.get('options', []))]
    if len({option['name'] for option in options}) != len(options):
        raise ConfigError(where + '.options has duplicate names')
    args = _choice(value.get('args', 'none'), where + '.args', ARGS)
    run = _run(value['run'], where + '.run')
    if args == 'none' and run['args_at'] is not None:
        raise ConfigError(where + ".run.argv uses {args} but the job declares args = 'none'")
    if args != 'none' and run['args_at'] is None:
        raise ConfigError(where + ".run.argv must place {args} when the job forwards arguments")

    job = {
        'id': job_id,
        'summary': _str(value.get('summary', job_id), where + '.summary'),
        'tool': _str(value['tool'], where + '.tool') if 'tool' in value else None,
        'size': _choice(value.get('size', 'medium'), where + '.size', SIZES),
        # Which lane runs it. `local` is not "Pandora declines": it is the
        # client daemon's own executor, with the same queue, receipt and exit
        # contract as the worker -- for work that must stay on this machine
        # (a dev stack owning a port) or is not worth shipping (a focused
        # `node --test`).
        'where': _choice(value.get('where', 'remote'), where + '.where', WHERE),
        # One at a time on this machine, across every worktree. What `dev:stack`
        # is, and the only reason a local job may hold a port.
        'singleton': _bool(value.get('singleton', False), where + '.singleton'),
        'cancel': _cancel(value['cancel'], where + '.cancel') if 'cancel' in value
                  else {'signal': 'SIGTERM', 'grace_ms': 15000},
        # None means "whatever the machine says". Freezing a 4,900-file worktree
        # twice is right for a 60-second suite and absurd for a 4-second
        # `node --test`, so the answer belongs to the job when the job has one.
        'drift': _choice(value['drift'], where + '.drift', DRIFTS) if 'drift' in value else None,
        # A run's tree arrives without `.git`. `synthetic` has the worker build
        # a one-commit repository over it whose index is this worktree's
        # tracked set, for suites that ask git what is tracked or changed.
        # `none` costs nothing and is right for anything that never runs git.
        'git': _choice(value.get('git', 'none'), where + '.git', GITS),
        'args': args,
        'options': options,
        'value_flags': _strs(value.get('value_flags', []), where + '.value_flags', FLAG, unique=True),
        'reject': [_reject(item, '%s.reject[%d]' % (where, position))
                   for position, item in enumerate(value.get('reject', []))],
        'reject_if_set': _names(value.get('reject_if_set', []), where + '.reject_if_set'),
        'forms': [_form(item, '%s.forms[%d]' % (where, position))
                  for position, item in enumerate(value['forms'])],
        'run': run,
        'validate': _validate(value['validate'], where + '.validate') if 'validate' in value else None,
        'shards': _shards(value['shards'], where + '.shards') if 'shards' in value else None,
        'outputs': [_output(item, '%s.outputs[%d]' % (where, position))
                    for position, item in enumerate(value.get('outputs', []))],
        'timeout_minutes': _int(value['timeout_minutes'], where + '.timeout_minutes', 1, 1440)
                           if 'timeout_minutes' in value else 30,
        'fallback': _fallback(value['fallback'], where + '.fallback') if 'fallback' in value else None,
        'on_extra': _on_extra(value['on_extra'], where + '.on_extra') if 'on_extra' in value
                    else {'action': 'reject', 'message': None},
        'usage': _str(value['usage'], where + '.usage') if 'usage' in value else None,
    }
    if not job['forms']:
        raise ConfigError(where + '.forms must not be empty')
    if job['shards'] and job['where'] == 'local':
        raise ConfigError(where + ' is sharded, which is a fan-out across worker '
                                  "instances; it cannot also be where = 'local'")
    if job['shards'] and job['shards']['plan'] and not job['outputs']:
        raise ConfigError(where + '.shards.plan writes a per-shard report, so the job must '
                                  'declare the artifacts that bring it home')
    if job['git'] != 'none' and job['where'] == 'local':
        raise ConfigError(where + ".git builds a repository on the worker; a local job "
                                  "already runs in a real checkout")
    if job['singleton'] and job['where'] != 'local':
        raise ConfigError(where + ".singleton is a machine-wide rule and needs where = 'local'")
    # A local job's outputs are not brought home -- it ran in the worktree, the
    # files are already there. What it declares is *evidence*: the paths a reader
    # of the receipt must look at, which is the answer to "should the local lane
    # set EICHLER_VALIDATION_DIRECTORY". No, it should not: a job that writes a
    # cleanup marker declares where it writes it, and the receipt records it.
    for output in job['outputs']:
        if job['where'] == 'local' and output['kind'] != 'evidence':
            raise ConfigError('%s runs in the worktree, so it brings nothing home; declare '
                              'kind = "evidence" paths or set where = "remote"' % where)
        if job['where'] != 'local' and output['kind'] == 'evidence':
            raise ConfigError('%s.outputs kind evidence is for where = "local" jobs; a remote '
                              'run brings its paths home as artifacts' % where)
    if job['value_flags'] and args == 'none':
        raise ConfigError(where + ".value_flags needs args = 'required' or 'optional'")
    if job['reject'] and args == 'none':
        raise ConfigError(where + ".reject needs args = 'required' or 'optional'")
    claimed = {option['name'] for option in options}
    for entry in job['reject']:
        overlap = sorted(set(entry['args']) & (claimed | set(job['value_flags'])))
        if overlap:
            raise ConfigError('%s.reject names %s, which this job also accepts'
                              % (where, ', '.join(overlap)))
    armed = {option['sets'] for option in options if option['writeback']}
    every = {option['sets'] for option in options}
    for output in job['outputs']:
        if output['kind'] == 'writeback':
            for path in output['paths']:
                _writeback_path(path, where + '.outputs')
            if not output['requires_option']:
                raise ConfigError(where + '.outputs of kind writeback must name a requires_option')
            if output['requires_option'] not in armed:
                raise ConfigError('%s.outputs requires option %r, which no writeback option of '
                                  'this job sets' % (where, output['requires_option']))
        elif output['requires_option'] and output['requires_option'] not in every:
            raise ConfigError('%s.outputs requires option %r, which no option of this job sets'
                              % (where, output['requires_option']))
    return job


# --- worker toolchain -------------------------------------------------------

def _worker(value, where):
    """Golden inputs and an optional per-clone command after source injection."""
    _keys(value, where, {'base_image'},
          {'packages', 'node_version', 'pnpm_version', 'service_images',
           'install_command', 'prepare_command', 'source_id', 'env', 'workdir', 'canary'})
    return {
        'base_image': _str(value['base_image'], where + '.base_image'),
        'packages': _strs(value.get('packages', []), where + '.packages', unique=True),
        'node_version': _str(value.get('node_version', ''), where + '.node_version', allow_empty=True),
        'pnpm_version': _str(value.get('pnpm_version', ''), where + '.pnpm_version', allow_empty=True),
        'service_images': _strs(value.get('service_images', []), where + '.service_images', unique=True),
        'install_command': _str(value.get('install_command', ''), where + '.install_command',
                                allow_empty=True),
        'prepare_command': _str(value.get('prepare_command', ''), where + '.prepare_command',
                                allow_empty=True),
        'source_id': _str(value.get('source_id', ''), where + '.source_id', allow_empty=True),
        'env': _env(value.get('env', {}), where + '.env'),
        'workdir': _str(value.get('workdir', '/work'), where + '.workdir'),
    }


def _canary(value, where):
    """What `pandora worker canary` runs in a clone of this repository's golden.

    It sits under `[worker]` because it is about the golden, and it is kept out
    of the returned `worker` table because that table is the golden's identity:
    choosing a different journey to prove the machine with must not mint a new
    golden. Every key is optional. A missing `journey` or `surface` means that
    check is not run. The verdict carries a row saying so, marked ok, so the
    omission is visible and does not fail the canary (`worker.enrolled`'s notes).

        journey      one journey id, spliced into the journey job's `run` argv
        surface      one surface id, given to the surface job's `validate`
                     (or, without one, its shard `plan` with one shard)
        compose      a compose file the journey check brings up and down first
        journey_job  the job id to take the journey argv from (default journey)
        surface_job  the job id to take the surface argv from (default surface)
    """
    _keys(value, where, (), {'journey', 'surface', 'compose', 'journey_job', 'surface_job'})
    return {
        'journey': _str(value['journey'], where + '.journey') if 'journey' in value else None,
        'surface': _str(value['surface'], where + '.surface') if 'surface' in value else None,
        'compose': (_inside(_str(value['compose'], where + '.compose'), where + '.compose')
                    if 'compose' in value else None),
        'journey_job': _str(value.get('journey_job', 'journey'), where + '.journey_job', NAME),
        'surface_job': _str(value.get('surface_job', 'surface'), where + '.surface_job', NAME),
    }


def _canary_jobs(canary, jobs):
    """A canary id must name a job that can take it, or the check is a lie."""
    for kind in ('journey', 'surface'):
        if canary[kind] is None:
            continue
        job_id = canary[kind + '_job']
        where = 'worker.canary.' + kind
        if job_id not in jobs:
            raise ConfigError('%s needs a job with id %s; declare it or set %s_job'
                              % (where, job_id, kind))
        job = jobs[job_id]
        if job['where'] != 'remote':
            raise ConfigError("%s names job %s, which is where = 'local'; the canary runs "
                              'on the worker' % (where, job_id))
        if job['args'] == 'none':
            raise ConfigError("%s names job %s, which takes no arguments, so there is "
                              'nowhere to put %r' % (where, job_id, canary[kind]))


# --- whole configuration ----------------------------------------------------

def validate(value):
    """Return a normalized configuration, or raise ConfigError."""
    _keys(value, 'configuration', {'version', 'repo', 'worker', 'jobs'},
          {'env', 'secrets', 'fallback', 'feedback', 'matching'})
    if type(value['version']) is not int or value['version'] != VERSION:
        raise ConfigError('configuration version must be %d' % VERSION)

    repo = _keys(value['repo'], 'repo', {'name', 'entrypoints'}, {'root_markers'})
    matching = _keys(value.get('matching', {}), 'matching', (), {'strip_prefixes', 'subdirectory'})
    subdirectory = _choice(matching.get('subdirectory', 'reroot'), 'matching.subdirectory',
                           ('reroot', 'local', 'reject', 'passthrough'))
    feedback = _keys(value.get('feedback', {}), 'feedback', (), {'reject_suffix', 'extra_message'})
    secrets = _keys(value.get('secrets', {}), 'secrets', (), {'exclude_globs'})
    environment = _keys(value.get('env', {}), 'env', (),
                        {'set', 'passthrough', 'unset', 'reject_if_set'})

    jobs = {}
    for index, item in enumerate(value['jobs']):
        job = _job(item, index)
        if job['id'] in jobs:
            raise ConfigError('jobs has duplicate id ' + job['id'])
        jobs[job['id']] = job
    entrypoints = _strs(repo['entrypoints'], 'repo.entrypoints', unique=True)
    if not entrypoints:
        raise ConfigError('repo.entrypoints must not be empty')
    seen = {}
    for job in jobs.values():
        if job['tool'] is None:
            job['tool'] = entrypoints[0]
        elif job['tool'] not in entrypoints:
            raise ConfigError('jobs.%s.tool is not a declared entrypoint: %s'
                              % (job['id'], job['tool']))
        for form in job['forms']:
            key = (job['tool'], tuple(form['prefix']))
            if key in seen:
                raise ConfigError('form %s %s is claimed by both %s and %s'
                                  % (job['tool'], ' '.join(form['prefix']), seen[key], job['id']))
            seen[key] = job['id']

    unset = _names(environment.get('unset', []), 'env.unset')
    base_env = _env(environment.get('set', {}), 'env.set')
    overlap = sorted(set(unset) & set(base_env))
    if overlap:
        raise ConfigError('env both sets and unsets ' + ', '.join(overlap))

    config = {
        'version': VERSION,
        'repo': {'name': _str(repo['name'], 'repo.name', NAME),
                 'entrypoints': entrypoints,
                 'root_markers': _strs(repo.get('root_markers', []), 'repo.root_markers',
                                       unique=True)},
        'worker': _worker(value['worker'], 'worker'),
        'canary': _canary(_table(value['worker'], 'worker').get('canary', {}), 'worker.canary'),
        'env': {'set': base_env,
                'passthrough': _names(environment.get('passthrough', []), 'env.passthrough'),
                'unset': unset,
                'reject_if_set': _names(environment.get('reject_if_set', []), 'env.reject_if_set')},
        'secrets': {'exclude_globs': _strs(secrets.get('exclude_globs', []),
                                           'secrets.exclude_globs', unique=True)},
        'matching': {
            'strip_prefixes': [_strs(x, 'matching.strip_prefixes entry')
                               for x in matching.get('strip_prefixes', [])],
            # `reroot`: run from the worktree root when no argument names a
            # path, refuse with 64 when one does. `local` is its old name, from
            # when the second case passed through to an unbounded local run.
            # `passthrough`: a claim holds only at the worktree root; typed
            # anywhere else the command is unclaimed, for a repository whose
            # bare root forms (`test`, `build`) mean something else in a package.
            'subdirectory': {'local': 'reroot'}.get(subdirectory, subdirectory),
        },
        'feedback': {
            'reject_suffix': _str(feedback.get('reject_suffix', ''), 'feedback.reject_suffix',
                                  allow_empty=True),
            'extra_message': _str(feedback.get('extra_message',
                                               '{job} runs as one complete job and takes no '
                                               'arguments.'), 'feedback.extra_message'),
        },
        # None, not a default: "nobody said" is a third answer, and it is the one
        # that lets the size class decide. A repository that writes this table
        # overrides that for every job it does not override individually.
        'fallback': _fallback(value['fallback'], 'fallback') if 'fallback' in value else None,
        'jobs': jobs,
    }
    for job in jobs.values():
        if job['fallback'] is None:
            job['fallback'] = config['fallback']
    _canary_jobs(config['canary'], jobs)
    return config


def load(path):
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as error:
        raise ConfigError('%s is not valid TOML: %s' % (path, error)) from None
    except OSError as error:
        raise ConfigError('cannot read %s: %s' % (path, error)) from None
    try:
        config = validate(raw)
    except ConfigError as error:
        raise ConfigError('%s: %s' % (path, error)) from None
    config['source'] = str(path)
    return config


def resolve(repo_root, fallback_path=None):
    """Where this repository's configuration lives.

    Repo root first: that is where v0.2 expects it, and where the combined
    Eichler PR will put it. The enrollment-referenced path second, so a repository
    can be routed before its own PR lands -- which is exactly the slice's
    position. Returning the path rather than the configuration keeps the
    precedence rule testable without a filesystem full of valid TOML.
    """
    in_repo = Path(repo_root) / FILENAME
    if in_repo.is_file():
        return in_repo, 'repo-root'
    if fallback_path:
        candidate = Path(fallback_path).expanduser()
        if candidate.is_file():
            return candidate, 'enrollment'
        raise ConfigError('the enrollment names a configuration that is not there: %s' % candidate)
    raise ConfigError('no %s at %s, and the enrollment names no other path' % (FILENAME, repo_root))


def load_for(repo_root, fallback_path=None):
    path, origin = resolve(repo_root, fallback_path)
    config = load(path)
    config['origin'] = origin
    return config
