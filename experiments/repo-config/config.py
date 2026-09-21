"""Strict loader for a repo-owned Pandora configuration.

The file is data, never code: it is read with ``tomllib``, every key is checked
against a closed schema, and unknown keys are refused with the allowed set.  A
configuration that loads is a configuration the classifier can execute without
further repository knowledge.

Two things this loader deliberately does *not* do.

It does not re-implement the repository's command-line parser.  A job declares
which literal argv forms Pandora **claims**, which options Pandora itself
consumes, which tokens it refuses outright with a message, and whether a focused
form stays on the agent's machine.  Everything else is forwarded verbatim to the
repository's own runner, which is the only thing that knows whether ``S9-01`` is
a journey.

It does not state resource numbers.  A job declares a size class; the worker's
configuration maps classes and service roles to CPU and memory, because those
are facts about the operator's machine, not about the repository.

Facts the repository already maintains in its GitHub Actions workflow may be
inherited rather than restated: a job that names ``ci_job`` takes its services,
job environment, shard pattern, timeout and artifact paths from that workflow
job.  See ``ci_import.py``.
"""
import hashlib
import re
import tomllib
from pathlib import Path

import ci_import

VERSION = 1
NAME = re.compile(r'[a-z][a-z0-9-]*\Z')
FLAG = re.compile(r'-{1,2}[A-Za-z][A-Za-z0-9-]*\Z')
TOKEN = re.compile(r'\{([^{}]+)\}')
VARIABLE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
# registry/name[:tag]@sha256:<64 hex> -- a substring test is not enough: it would
# accept "postgres:16 @sha256:..." and "evil@sha256:short".
REFERENCE = re.compile(
    r'(?:(?P<registry>[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?)/)?'
    r'(?P<name>[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*)'
    r'(?::(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}))?'
    r'(?:@(?P<digest>sha256:[0-9a-f]{64}))?\Z')
FAULTS = ('worker-unreachable', 'queue-timeout', 'admission-refused')
OUTPUTS = ('artifacts', 'generated', 'writeback')
EXTRA = ('local', 'reject')
ARGS = ('none', 'required', 'optional')
SIZES = ('small', 'medium', 'large')
SHARD_NAMES = frozenset({'shard.index', 'shard.total'})
LOCAL_ORIGIN = 'pandora.toml'


class ConfigError(ValueError):
    """A repo configuration Pandora refuses to load."""


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
            where, '' if len(unknown) == 1 else 's', ', '.join(unknown), ', '.join(sorted(allowed))))
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


def _image(value, where, pins):
    """Resolve a pin, then insist on a complete digest-pinned reference."""
    text = _str(value, where)
    resolved = pins.get(text, text)
    if resolved.startswith('local/'):
        return resolved
    match = REFERENCE.fullmatch(resolved)
    if match is None:
        raise ConfigError('%s is not a valid image reference: %s' % (where, resolved))
    if match.group('digest') is None:
        raise ConfigError(
            '%s must be digest-pinned: %s.  Add [pins] "%s" = "%s@sha256:<digest>" so the worker '
            'reproduces what CI merely re-pulls.' % (where, resolved, text, resolved.split(':')[0]))
    return resolved


def _check_template(text, names, where):
    for match in TOKEN.finditer(text):
        if match.group(1) not in names:
            raise ConfigError('%s uses unknown template value {%s}; known: %s' % (
                where, match.group(1), ', '.join(sorted(names)) or 'none'))
    return text


def _render(text, scope, where):
    def replace(match):
        name = match.group(1)
        if name not in scope:
            raise ConfigError('%s uses unknown template value {%s}; known: %s' % (
                where, name, ', '.join(sorted(scope)) or 'none'))
        return scope[name]
    return TOKEN.sub(replace, text)


def _inside(path, where):
    if path.startswith('/') or '..' in path.split('/'):
        raise ConfigError('%s must stay inside the worktree: %s' % (where, path))
    return path


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

def _healthcheck(value, where):
    check = _keys(value, where, {'argv'}, {'attempts', 'interval_ms'})
    argv = _strs(check['argv'], where + '.argv')
    if not argv:
        raise ConfigError(where + '.argv must not be empty')
    return {'argv': argv,
            'attempts': _int(check.get('attempts', 60), where + '.attempts', 1, 600),
            'interval_ms': _int(check.get('interval_ms', 500), where + '.interval_ms', 10, 60000)}


def _service(value, where, pins):
    _keys(value, where, {'id', 'image'}, {'role', 'env', 'host', 'port', 'exports', 'healthcheck'})
    service = {
        'id': _str(value['id'], where + '.id', NAME),
        'image': _image(value['image'], where + '.image', pins),
        'env': _env(value.get('env', {}), where + '.env'),
        'host': _str(value.get('host', '127.0.0.1'), where + '.host'),
        'port': _int(value.get('port', 1), where + '.port', 1, 65535),
        'exports': {},
        'healthcheck': _healthcheck(value['healthcheck'], where + '.healthcheck')
                       if 'healthcheck' in value else None,
    }
    service['role'] = _str(value.get('role', service['id']), where + '.role', NAME)
    scope = {'host': service['host'], 'port': str(service['port'])}
    for key, item in _env(value.get('exports', {}), where + '.exports').items():
        service['exports'][key] = _render(item, scope, where + '.exports.' + key)
    return service


def _imported_service(fact, role, where, pins):
    """Turn one GitHub Actions service container into a Pandora service."""
    port = fact['ports'][0]['container'] if fact['ports'] else None
    health = fact['health']
    return {
        'id': role,
        'role': role,
        'ci_name': fact['name'],
        'image': _image(fact['image'], where + '.image', pins),
        'env': dict(fact['env']),
        'ci_ports': [dict(entry) for entry in fact['ports']],
        # Every container in the run shares one network namespace, so the address
        # CI writes into its URLs -- localhost -- is the address that works.
        'host': '127.0.0.1',
        'port': port or 1,
        'exports': {},
        'healthcheck': {'argv': health['argv'], 'attempts': health['attempts'],
                        'interval_ms': health['interval_ms']} if health else None,
    }


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

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
            'on_extra': _on_extra(value['on_extra'], where + '.on_extra') if 'on_extra' in value else None}


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
    output = {
        'kind': _choice(value['kind'], where + '.kind', OUTPUTS),
        'paths': _strs(value['paths'], where + '.paths', unique=True),
        'requires_option': None,
    }
    if not output['paths']:
        raise ConfigError(where + '.paths must not be empty')
    for path in output['paths']:
        _inside(path, where + '.paths')
    if 'requires_option' in value:
        output['requires_option'] = _str(value['requires_option'], where + '.requires_option')
    return output


def _run(value, where):
    _keys(value, where, {'argv'}, {'cwd', 'env', 'unset'})
    argv = _strs(value['argv'], where + '.argv')
    if not argv:
        raise ConfigError(where + '.argv must not be empty')
    splices = [index for index, item in enumerate(argv) if item == '{args}']
    if len(splices) > 1:
        raise ConfigError(where + '.argv uses {args} more than once')
    for item in argv:
        if item != '{args}':
            _check_template(item, set(), where + '.argv')
    run = {'argv': argv,
           'args_at': splices[0] if splices else None,
           'cwd': _inside(_str(value.get('cwd', '.'), where + '.cwd'), where + '.cwd'),
           'env': _env(value.get('env', {}), where + '.env'),
           'unset': _names(value.get('unset', []), where + '.unset')}
    for key, item in run['env'].items():
        _check_template(item, set(), where + '.env.' + key)
    overlap = sorted(set(run['unset']) & set(run['env']))
    if overlap:
        raise ConfigError('%s both sets and unsets %s' % (where, ', '.join(overlap)))
    return run


def _shards(value, where, imported):
    """Shard-count policy.  With an import, CI owns the pattern and this owns N."""
    optional = {'default', 'min', 'max'}
    if imported is None:
        _keys(value, where, {'strategy'}, optional | {'env', 'argv_append', 'plan'})
    else:
        _keys(value, where, (), optional)
    shards = {
        'min': _int(value.get('min', 1), where + '.min', 1, 32),
        'max': _int(value.get('max', 32), where + '.max', 1, 32),
        'plan': None,
    }
    if shards['min'] > shards['max']:
        raise ConfigError(where + '.min exceeds max')
    if imported is not None:
        consumed = imported['consumed']
        shards['strategy'] = consumed['kind']
        shards['env'] = ({consumed['name']: '{shard.index}/{shard.total}'}
                         if consumed['kind'] == 'env' else {})
        shards['argv_append'] = ([consumed['name'] + '={shard.index}/{shard.total}']
                                 if consumed['kind'] == 'argv' else [])
        fallback_default = imported['total']
    else:
        shards['strategy'] = _choice(value['strategy'], where + '.strategy', ('env', 'argv'))
        shards['env'] = _env(value.get('env', {}), where + '.env')
        shards['argv_append'] = _strs(value.get('argv_append', []), where + '.argv_append')
        for key, item in shards['env'].items():
            _check_template(item, SHARD_NAMES, where + '.env.' + key)
        for item in shards['argv_append']:
            _check_template(item, SHARD_NAMES, where + '.argv_append')
        if (shards['strategy'] == 'env') != bool(shards['env']):
            raise ConfigError(where + " strategy 'env' requires env and forbids it otherwise")
        if (shards['strategy'] == 'argv') != bool(shards['argv_append']):
            raise ConfigError(where + " strategy 'argv' requires argv_append and forbids it otherwise")
        fallback_default = 1
    default = value.get('default', min(max(fallback_default, shards['min']), shards['max']))
    shards['default'] = _int(default, where + '.default', shards['min'], shards['max'])
    if imported is not None and shards['max'] == 1:
        # A job pinned to a single shard inherits the world but not the marker:
        # writing SHARD=1/1 would change what the repository's runner selects.
        shards['env'], shards['argv_append'] = {}, []
    if imported is None and 'plan' in value:
        plan = _keys(value['plan'], where + '.plan', {'run'}, {'services', 'emits'})
        emits = _inside(_str(plan['emits'], where + '.plan.emits'), where + '.plan.emits') \
                if 'emits' in plan else None
        shards['plan'] = {'run': _run(plan['run'], where + '.plan.run'),
                          'emits': emits,
                          'services': _strs(plan.get('services', []), where + '.plan.services',
                                            NAME, unique=True)}
    return shards


JOB_REQUIRED = {'id', 'forms', 'run'}
JOB_OPTIONAL = {'summary', 'tool', 'size', 'args', 'options', 'value_flags', 'reject',
                'services', 'shards', 'outputs', 'fallback', 'on_extra', 'usage', 'exclusive',
                'reject_if_set', 'timeout_minutes', 'ci_job', 'ci_matrix_params', 'ci_lint'}


def _ci_lint(value, where):
    _keys(value, where, {'job'}, {'except', 'matrix_params'})
    return {'job': _str(value['job'], where + '.job'),
            'except': _strs(value.get('except', []), where + '.except', unique=True),
            'matrix_params': _strs(value.get('matrix_params', []), where + '.matrix_params',
                                   unique=True)}


def _job(value, index, library, pins, workflow):
    where = 'jobs[%d]' % index
    _keys(value, where, JOB_REQUIRED, JOB_OPTIONAL)
    job_id = _str(value['id'], where + '.id', NAME)
    where = 'jobs.' + job_id
    origin = {}

    facts = None
    if 'ci_job' in value:
        if workflow is None:
            raise ConfigError(where + '.ci_job needs a top-level ci_workflow')
        params = _strs(value.get('ci_matrix_params', []), where + '.ci_matrix_params', unique=True)
        try:
            facts = ci_import.import_job(workflow['document'], _str(value['ci_job'], where + '.ci_job'),
                                         source=workflow['name'], matrix_params=params)
        except ci_import.CiImportError as error:
            raise ConfigError('%s.ci_job cannot be imported: %s' % (where, error)) from None
    elif 'ci_matrix_params' in value:
        raise ConfigError(where + '.ci_matrix_params applies only with ci_job')
    if 'ci_job' in value and 'ci_lint' in value:
        raise ConfigError(where + ' cannot both import ci_job and lint against ci_lint')

    def note(field, suffix=None):
        origin[field] = ('%s:%s.%s' % (workflow['name'], facts['job'], suffix)
                         if suffix is not None else LOCAL_ORIGIN)

    options = [_option(item, '%s.options[%d]' % (where, position))
               for position, item in enumerate(value.get('options', []))]
    if len({o['name'] for o in options}) != len(options):
        raise ConfigError(where + '.options has duplicate names')
    args = _choice(value.get('args', 'none'), where + '.args', ARGS)
    run = _run(value['run'], where + '.run')
    if args == 'none' and run['args_at'] is not None:
        raise ConfigError(where + ".run.argv uses {args} but the job declares args = 'none'")
    if args != 'none' and run['args_at'] is None:
        raise ConfigError(where + ".run.argv must place {args} when the job forwards arguments")

    # Services: the workflow's, then anything the configuration states itself.
    services, seen = [], {}
    if facts is not None:
        for name in sorted(facts['services']):
            role = workflow['roles'].get(name, name)
            spot = '%s.services.%s' % (where, name)
            service = _imported_service(facts['services'][name], role, spot, pins)
            seen[role] = len(services)
            services.append(service)
            note('services.' + role, 'services.' + name)
    for name in _strs(value.get('services', []), where + '.services', NAME, unique=True):
        if name not in library:
            raise ConfigError(where + '.services names an undeclared service: ' + name)
        if name in seen:
            services[seen[name]] = dict(library[name])
        else:
            seen[name] = len(services)
            services.append(dict(library[name]))
        note('services.' + name)

    environment = dict(facts['env']) if facts is not None else {}
    for key in environment:
        note('env.' + key, 'env.' + key)
    for key in run['env']:
        note('env.' + key)
    for key in run['unset']:
        note('env.' + key)

    imported_shards = facts['shards'] if facts is not None else None
    if 'shards' in value:
        shards = _shards(value['shards'], where + '.shards', imported_shards)
        note('shards', 'strategy.matrix.' + imported_shards['dimension'] if imported_shards else None)
    elif imported_shards is not None:
        shards = _shards({}, where + '.shards', imported_shards)
        note('shards', 'strategy.matrix.' + imported_shards['dimension'])
    else:
        shards = None

    outputs = [_output(item, '%s.outputs[%d]' % (where, position))
               for position, item in enumerate(value.get('outputs', []))]
    for output in outputs:
        note('outputs.' + output['kind'])
    if facts is not None and facts['artifacts'] and not any(o['kind'] == 'artifacts' for o in outputs):
        paths = []
        for entry in facts['artifacts']:
            for path in entry['paths']:
                if path.startswith('/') or '..' in path.split('/'):
                    raise ConfigError(
                        '%s inherits the artifact path %s from %s, which is outside the worktree.  '
                        'Pandora collects results from the snapshot only; declare outputs in '
                        'pandora.toml to override the inherited list.'
                        % (where, path, entry['where']))
                if TOKEN.search(path) or '${{' in path:
                    raise ConfigError(
                        '%s inherits the artifact path %s from %s, which is a matrix expression.  '
                        'Pandora has no matrix; declare outputs in pandora.toml as a glob.'
                        % (where, path, entry['where']))
                paths.append(path)
        outputs.append({'kind': 'artifacts', 'paths': sorted(set(paths)), 'requires_option': None})
        note('outputs.artifacts', 'steps[*].uses=actions/upload-artifact')

    timeout = None
    if 'timeout_minutes' in value:
        timeout = _int(value['timeout_minutes'], where + '.timeout_minutes', 1, 1440)
        note('timeout_minutes')
    elif facts is not None and facts['timeout_minutes'] is not None:
        timeout = facts['timeout_minutes']
        note('timeout_minutes', 'timeout-minutes')

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
        'services': services,
        'run': run,
        'env': environment,
        'exclusive': _strs(value.get('exclusive', []), where + '.exclusive', NAME, unique=True),
        'shards': shards,
        'outputs': outputs,
        'timeout_minutes': timeout,
        'fallback': _fallback(value['fallback'], where + '.fallback') if 'fallback' in value else None,
        'on_extra': _on_extra(value['on_extra'], where + '.on_extra') if 'on_extra' in value
                    else {'action': 'reject', 'message': None},
        'usage': _str(value['usage'], where + '.usage') if 'usage' in value else None,
        'ci_job': facts['job'] if facts is not None else None,
        'ci_facts': facts,
        'ci_lint': _ci_lint(value['ci_lint'], where + '.ci_lint') if 'ci_lint' in value else None,
        'provenance': origin,
    }
    for field in ('size', 'args', 'forms', 'run'):
        origin.setdefault(field, LOCAL_ORIGIN)
    if not job['forms']:
        raise ConfigError(where + '.forms must not be empty')
    if job['value_flags'] and args == 'none':
        raise ConfigError(where + ".value_flags needs args = 'required' or 'optional'")
    if job['reject'] and args == 'none':
        raise ConfigError(where + ".reject needs args = 'required' or 'optional'")
    claimed = {o['name'] for o in options}
    for entry in job['reject']:
        overlap = sorted(set(entry['args']) & (claimed | set(job['value_flags'])))
        if overlap:
            raise ConfigError('%s.reject names %s, which this job also accepts'
                              % (where, ', '.join(overlap)))
    armed = {o['sets'] for o in options if o['writeback']}
    for output in job['outputs']:
        if output['kind'] == 'writeback':
            if not output['requires_option']:
                raise ConfigError(where + '.outputs of kind writeback must name a requires_option')
            if output['requires_option'] not in armed:
                raise ConfigError('%s.outputs requires option %r, which no writeback option of this '
                                  'job sets' % (where, output['requires_option']))
        elif output['requires_option'] and output['requires_option'] not in {o['sets'] for o in options}:
            raise ConfigError('%s.outputs requires option %r, which no option of this job sets'
                              % (where, output['requires_option']))
    if shards and shards['plan']:
        unknown = sorted(set(shards['plan']['services']) - set(library))
        if unknown:
            raise ConfigError(where + '.shards.plan.services names undeclared service(s): '
                              + ', '.join(unknown))
    return job


# ---------------------------------------------------------------------------
# Whole configuration
# ---------------------------------------------------------------------------

def _pins(value):
    table = _table(value, 'pins')
    pins = {}
    for key, item in table.items():
        floating = _str(key, 'pins key')
        pinned = _str(item, 'pins.' + floating)
        if '@sha256:' not in pinned:
            raise ConfigError('pins.%s must map to a digest: %s' % (floating, pinned))
        if '@sha256:' in floating:
            raise ConfigError('pins.%s is already pinned' % floating)
        pins[floating] = pinned
    return pins


def _workflow(value, root, roles):
    path = Path(root) / _str(value, 'ci_workflow')
    try:
        document, parser = ci_import.load_workflow(path)
    except ci_import.CiImportError as error:
        raise ConfigError(str(error)) from None
    return {'path': str(path), 'name': path.name, 'document': document,
            'parser': parser, 'roles': roles}


def validate(value, *, root='.'):
    """Return a normalized configuration or raise ConfigError."""
    _keys(value, 'configuration', {'version', 'repo', 'runtime', 'prepare', 'jobs'},
          {'services', 'env', 'secrets', 'fallback', 'feedback', 'matching',
           'pins', 'ci_workflow', 'ci_service_roles'})
    if type(value['version']) is not int or value['version'] != VERSION:
        raise ConfigError('configuration version must be %d' % VERSION)
    pins = _pins(value.get('pins', {}))
    roles = {}
    for key, item in _table(value.get('ci_service_roles', {}), 'ci_service_roles').items():
        roles[_str(key, 'ci_service_roles key')] = _str(item, 'ci_service_roles.' + key, NAME)
    workflow = _workflow(value['ci_workflow'], root, roles) if 'ci_workflow' in value else None

    repo = _keys(value['repo'], 'repo', {'name', 'entrypoints'}, {'root_markers'})
    runtime = _keys(value['runtime'], 'runtime', {'base_image'},
                    {'platform', 'setup', 'env', 'workdir', 'user'})
    prepare = _keys(value['prepare'], 'prepare', {'argv', 'cache_key_paths'},
                    {'cache_key_env', 'check_argv'})
    matching = _keys(value.get('matching', {}), 'matching', (), {'strip_prefixes', 'subdirectory'})
    feedback = _keys(value.get('feedback', {}), 'feedback', (), {'reject_suffix', 'extra_message'})
    secrets = _keys(value.get('secrets', {}), 'secrets', (), {'exclude_globs'})
    environment = _keys(value.get('env', {}), 'env', (), {'set', 'passthrough', 'unset', 'reject_if_set'})

    setup = _strs(runtime.get('setup', []), 'runtime.setup')
    platform = _str(runtime.get('platform', 'linux/amd64'), 'runtime.platform')
    base_image = _image(runtime['base_image'], 'runtime.base_image', pins)
    library = {}
    for index, item in enumerate(value.get('services', [])):
        service = _service(item, 'services[%d]' % index, pins)
        if service['id'] in library:
            raise ConfigError('services has duplicate id ' + service['id'])
        library[service['id']] = service
    jobs = {}
    for index, item in enumerate(value['jobs']):
        job = _job(item, index, library, pins, workflow)
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
            raise ConfigError('jobs.%s.tool is not a declared entrypoint: %s' % (job['id'], job['tool']))
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
        'repo': {'name': _str(repo['name'], 'repo.name'),
                 'entrypoints': entrypoints,
                 'root_markers': _strs(repo.get('root_markers', []), 'repo.root_markers', unique=True)},
        'runtime': {'base_image': base_image,
                    'platform': platform,
                    'setup': setup,
                    'env': _env(runtime.get('env', {}), 'runtime.env'),
                    'workdir': _str(runtime.get('workdir', '/workspace'), 'runtime.workdir'),
                    'user': _str(runtime.get('user', 'root'), 'runtime.user')},
        'prepare': {'argv': _strs(prepare['argv'], 'prepare.argv'),
                    'cache_key_paths': _strs(prepare['cache_key_paths'], 'prepare.cache_key_paths', unique=True),
                    'cache_key_env': _names(prepare.get('cache_key_env', []), 'prepare.cache_key_env'),
                    'check_argv': _strs(prepare['check_argv'], 'prepare.check_argv') if 'check_argv' in prepare else []},
        'env': {'set': base_env,
                'passthrough': _names(environment.get('passthrough', []), 'env.passthrough'),
                'unset': unset,
                'reject_if_set': _names(environment.get('reject_if_set', []), 'env.reject_if_set')},
        'secrets': {'exclude_globs': _strs(secrets.get('exclude_globs', []), 'secrets.exclude_globs', unique=True)},
        'matching': {
            'strip_prefixes': [_strs(x, 'matching.strip_prefixes entry')
                               for x in matching.get('strip_prefixes', [])],
            'subdirectory': _choice(matching.get('subdirectory', 'reroot'), 'matching.subdirectory',
                                    ('reroot', 'local', 'reject')),
        },
        'feedback': {
            'reject_suffix': _str(feedback.get('reject_suffix', ''), 'feedback.reject_suffix', allow_empty=True),
            'extra_message': _str(feedback.get('extra_message',
                                               '{job} runs as one complete job and takes no arguments.'),
                                  'feedback.extra_message'),
        },
        'fallback': _fallback(value['fallback'], 'fallback') if 'fallback' in value
                    else {'action': 'fail', 'on': list(FAULTS), 'notice': None},
        'pins': pins,
        'ci_workflow': {'path': workflow['path'], 'parser': workflow['parser']} if workflow else None,
        'ci_service_roles': roles,
        'services': library,
        'jobs': jobs,
    }
    if not config['prepare']['argv']:
        raise ConfigError('prepare.argv must not be empty')
    config['dependency_cache'] = dependency_cache(config)
    for job in jobs.values():
        if job['fallback'] is None:
            job['fallback'] = config['fallback']
    return config


def dependency_cache(config):
    """Everything that changes what the prepared dependency image contains.

    ``runtime.setup`` runs ``apt-get`` against a moving mirror, so a digest-pinned
    base is not by itself reproducible.  Folding the setup text and the platform
    into the key at least stops a changed setup line from silently reusing an
    image built by the old one.
    """
    material = '\n'.join([config['runtime']['base_image'], config['runtime']['platform'],
                          *config['runtime']['setup'], *config['prepare']['argv']])
    return {'base_image': config['runtime']['base_image'],
            'platform': config['runtime']['platform'],
            'setup_sha256': hashlib.sha256(material.encode()).hexdigest(),
            'paths': config['prepare']['cache_key_paths'],
            'env': config['prepare']['cache_key_env']}


NODE_TAG = re.compile(r'node:([0-9]+)[.\-@]')


def job_facts(config, job, ci_flat):
    """The same normalized facts, read off the configuration instead of the workflow."""
    flat = {}
    for service in job['services']:
        role = service['role']
        flat['services.%s.image' % role] = service['image']
        flat['services.%s.env' % role] = dict(service['env'])
        flat['services.%s.ports' % role] = [service['port']]
        flat['services.%s.health' % role] = (service['healthcheck'] or {}).get('argv')
    environment = dict(config['env']['set'])
    environment.update(job['env'])
    environment.update(job['run']['env'])
    for name in [*config['env']['unset'], *job['run']['unset']]:
        environment.pop(name, None)
    # Only variables CI also states are compared: Pandora sets plenty that CI has
    # no opinion about, and reporting those as drift would bury the real ones.
    for key, item in environment.items():
        if 'env.' + key in ci_flat:
            flat['env.' + key] = item
    shards = job['shards']
    flat['shards.total'] = shards['default'] if shards else None
    if shards and shards['strategy'] == 'env':
        flat['shards.consumed'] = 'env ' + next(iter(shards['env']), '')
    elif shards and shards['argv_append']:
        flat['shards.consumed'] = 'argv ' + shards['argv_append'][0].split('=')[0]
    else:
        flat['shards.consumed'] = None
    flat['timeout_minutes'] = job['timeout_minutes']
    match = NODE_TAG.search(config['runtime']['base_image'])
    flat['node'] = match.group(1) if match else None
    flat['artifacts'] = sorted(ci_import.clean_path(path) for output in job['outputs']
                               if output['kind'] == 'artifacts' for path in output['paths'])
    return flat


def lint(config, *, root='.'):
    """Report drift for jobs that restate a CI job instead of importing it."""
    reports = []
    for job in config['jobs'].values():
        spec = job['ci_lint']
        if spec is None:
            continue
        if config['ci_workflow'] is None:
            raise ConfigError('jobs.%s.ci_lint needs a top-level ci_workflow' % job['id'])
        document, _parser = ci_import.load_workflow(config['ci_workflow']['path'])
        name = Path(config['ci_workflow']['path']).name
        try:
            facts = ci_import.import_job(document, spec['job'], source=name,
                                         matrix_params=spec['matrix_params'])
        except ci_import.CiImportError as error:
            raise ConfigError('jobs.%s.ci_lint: %s' % (job['id'], error)) from None
        ci_flat = ci_import.normalize(facts, pins=config['pins'], roles=config['ci_service_roles'])
        findings = ci_import.drift(ci_flat, job_facts(config, job, ci_flat), spec['except'])
        reports.append({'job': job['id'], 'ci_job': spec['job'], 'source': name,
                        'exceptions': spec['except'], 'findings': findings})
    return reports


def load(path, root=None):
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as error:
        raise ConfigError('%s is not valid TOML: %s' % (path, error)) from None
    except OSError as error:
        raise ConfigError('cannot read %s: %s' % (path, error)) from None
    try:
        return validate(raw, root=root if root is not None else path.parent)
    except ConfigError as error:
        raise ConfigError('%s: %s' % (path, error)) from None
