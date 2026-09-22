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
  `shards` / `plan`           one-shard jobs only in the slice.
  `pins`                      the golden's toolchain is pinned by `[worker]`,
                              whose fingerprint is the golden's identity.

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
FAULTS = ('worker-unreachable', 'queue-timeout', 'admission-refused')
OUTPUTS = ('artifacts', 'writeback')
EXTRA = ('local', 'reject')
ARGS = ('none', 'required', 'optional')
SIZES = ('small', 'medium', 'large', 'xlarge')
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
        raise ConfigError('%s must be one of %s' % (where, ', '.join(allowed)))
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
    _keys(value, where, {'action'}, {'on', 'notice'})
    fallback = {
        'action': _choice(value['action'], where + '.action', ('local', 'fail')),
        'on': _strs(value.get('on', list(FAULTS)), where + '.on', unique=True),
        'notice': _str(value['notice'], where + '.notice') if 'notice' in value else None,
    }
    for item in fallback['on']:
        _choice(item, where + '.on entry', FAULTS)
    return fallback


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


def _argv_with_args(value, where, *, allow_args=True):
    """An argv list that may splice the forwarded arguments once, at `{args}`."""
    argv = _strs(value, where)
    if not argv:
        raise ConfigError(where + ' must not be empty')
    splices = [index for index, item in enumerate(argv) if item == '{args}']
    if len(splices) > 1:
        raise ConfigError(where + ' uses {args} more than once')
    if splices and not allow_args:
        raise ConfigError(where + ' may not use {args}')
    for item in argv:
        if item != '{args}':
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


JOB_REQUIRED = {'id', 'forms', 'run'}
JOB_OPTIONAL = {'summary', 'tool', 'size', 'args', 'options', 'value_flags', 'reject',
                'outputs', 'fallback', 'on_extra', 'usage', 'reject_if_set',
                'timeout_minutes', 'validate'}


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
    """What a golden instance for this repository contains.

    Its fingerprint is the golden's identity, so every field here is part of a
    cache key: change one and the next `prepare` builds a new golden rather than
    reusing a stale one.
    """
    _keys(value, where, {'base_image'},
          {'packages', 'node_version', 'pnpm_version', 'service_images',
           'install_command', 'source_id', 'env', 'workdir'})
    return {
        'base_image': _str(value['base_image'], where + '.base_image'),
        'packages': _strs(value.get('packages', []), where + '.packages', unique=True),
        'node_version': _str(value.get('node_version', ''), where + '.node_version', allow_empty=True),
        'pnpm_version': _str(value.get('pnpm_version', ''), where + '.pnpm_version', allow_empty=True),
        'service_images': _strs(value.get('service_images', []), where + '.service_images', unique=True),
        'install_command': _str(value.get('install_command', ''), where + '.install_command',
                                allow_empty=True),
        'source_id': _str(value.get('source_id', ''), where + '.source_id', allow_empty=True),
        'env': _env(value.get('env', {}), where + '.env'),
        'workdir': _str(value.get('workdir', '/work'), where + '.workdir'),
    }


# --- whole configuration ----------------------------------------------------

def validate(value):
    """Return a normalized configuration, or raise ConfigError."""
    _keys(value, 'configuration', {'version', 'repo', 'worker', 'jobs'},
          {'env', 'secrets', 'fallback', 'feedback', 'matching'})
    if type(value['version']) is not int or value['version'] != VERSION:
        raise ConfigError('configuration version must be %d' % VERSION)

    repo = _keys(value['repo'], 'repo', {'name', 'entrypoints'}, {'root_markers'})
    matching = _keys(value.get('matching', {}), 'matching', (), {'strip_prefixes', 'subdirectory'})
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
        'env': {'set': base_env,
                'passthrough': _names(environment.get('passthrough', []), 'env.passthrough'),
                'unset': unset,
                'reject_if_set': _names(environment.get('reject_if_set', []), 'env.reject_if_set')},
        'secrets': {'exclude_globs': _strs(secrets.get('exclude_globs', []),
                                           'secrets.exclude_globs', unique=True)},
        'matching': {
            'strip_prefixes': [_strs(x, 'matching.strip_prefixes entry')
                               for x in matching.get('strip_prefixes', [])],
            'subdirectory': _choice(matching.get('subdirectory', 'local'), 'matching.subdirectory',
                                    ('local', 'reject')),
        },
        'feedback': {
            'reject_suffix': _str(feedback.get('reject_suffix', ''), 'feedback.reject_suffix',
                                  allow_empty=True),
            'extra_message': _str(feedback.get('extra_message',
                                               '{job} runs as one complete job and takes no '
                                               'arguments.'), 'feedback.extra_message'),
        },
        'fallback': _fallback(value['fallback'], 'fallback') if 'fallback' in value
                    else {'action': 'fail', 'on': list(FAULTS), 'notice': None},
        'jobs': jobs,
    }
    for job in jobs.values():
        if job['fallback'] is None:
            job['fallback'] = config['fallback']
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
    Eichler PR will put it. The enrolment-referenced path second, so a repository
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
            return candidate, 'enrolment'
        raise ConfigError('the enrolment names a configuration that is not there: %s' % candidate)
    raise ConfigError('no %s at %s, and the enrolment names no other path' % (FILENAME, repo_root))


def load_for(repo_root, fallback_path=None):
    path, origin = resolve(repo_root, fallback_path)
    config = load(path)
    config['origin'] = origin
    return config
