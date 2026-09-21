"""Strict loader for a repo-owned Pandora configuration.

The file is data, never code: it is read with ``tomllib``, every key is checked
against a closed schema, and unknown keys are refused with the allowed set.  A
configuration that loads is a configuration the classifier can execute without
further repository knowledge.
"""
import re
import tomllib
from pathlib import Path

VERSION = 1
NAME = re.compile(r'[a-z][a-z0-9-]*\Z')
FLAG = re.compile(r'-{1,2}[A-Za-z][A-Za-z0-9-]*\Z')
TOKEN = re.compile(r'\{([^{}]+)\}')
FAULTS = ('worker-unreachable', 'queue-timeout', 'admission-refused')
KINDS = ('enum', 'pattern', 'rest')
OUTPUTS = ('artifacts', 'generated', 'writeback')
EXTRA = ('local', 'reject')


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
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            raise ConfigError(where + ' has an invalid variable name: ' + key)
        _str(item, where + '.' + key, allow_empty=True)
    return dict(value)


def _choice(value, where, allowed):
    if value not in allowed:
        raise ConfigError('%s must be one of %s' % (where, ', '.join(allowed)))
    return value


def _service(value, index):
    where = 'services[%d]' % index
    _keys(value, where, {'id', 'image', 'cpu_millis', 'memory_mib'},
          {'env', 'host', 'port', 'exports', 'healthcheck', 'memory_swap'})
    service = {
        'id': _str(value['id'], where + '.id', NAME),
        'image': _str(value['image'], where + '.image'),
        'cpu_millis': _int(value['cpu_millis'], where + '.cpu_millis'),
        'memory_mib': _int(value['memory_mib'], where + '.memory_mib'),
        'env': _env(value.get('env', {}), where + '.env'),
        'host': _str(value.get('host', '127.0.0.1'), where + '.host'),
        'port': _int(value.get('port', 1), where + '.port', 1, 65535),
        'exports': {},
        'healthcheck': None,
    }
    if '@sha256:' not in service['image'] and not service['image'].startswith('local/'):
        raise ConfigError(where + '.image must be digest-pinned: ' + service['image'])
    scope = {'host': service['host'], 'port': str(service['port'])}
    for key, item in _env(value.get('exports', {}), where + '.exports').items():
        service['exports'][key] = _render(item, scope, where + '.exports.' + key)
    if 'healthcheck' in value:
        check = _keys(value['healthcheck'], where + '.healthcheck', {'argv'}, {'attempts', 'interval_ms'})
        service['healthcheck'] = {
            'argv': _strs(check['argv'], where + '.healthcheck.argv'),
            'attempts': _int(check.get('attempts', 60), where + '.healthcheck.attempts', 1, 600),
            'interval_ms': _int(check.get('interval_ms', 500), where + '.healthcheck.interval_ms', 10, 60000),
        }
        if not service['healthcheck']['argv']:
            raise ConfigError(where + '.healthcheck.argv must not be empty')
    return service


def _render(text, scope, where):
    """Substitute {name} tokens from a flat scope; unknown names are a load error."""
    def replace(match):
        name = match.group(1)
        if name not in scope:
            raise ConfigError('%s uses unknown template value {%s}; known: %s' % (
                where, name, ', '.join(sorted(scope)) or 'none'))
        return scope[name]
    return TOKEN.sub(replace, text)


def _check_template(text, names, where):
    for match in TOKEN.finditer(text):
        if match.group(1) not in names:
            raise ConfigError('%s uses unknown template value {%s}; known: %s' % (
                where, match.group(1), ', '.join(sorted(names))))
    return text


def _param(value, index, job):
    where = 'jobs.%s.params[%d]' % (job, index)
    _keys(value, where, {'name', 'kind'},
          {'values', 'pattern', 'required', 'allow_flags', 'path_like'})
    param = {
        'name': _str(value['name'], where + '.name', NAME),
        'kind': _choice(value['kind'], where + '.kind', KINDS),
        'values': None, 'pattern': None,
        'required': _bool(value.get('required', True), where + '.required'),
        'allow_flags': [], 'path_like': True,
    }
    if param['kind'] == 'enum':
        param['values'] = _strs(value.get('values', []), where + '.values', unique=True)
        if not param['values']:
            raise ConfigError(where + ' of kind enum needs values')
        if 'pattern' in value:
            raise ConfigError(where + ' of kind enum takes no pattern')
    elif param['kind'] == 'pattern':
        param['pattern'] = _str(value.get('pattern', ''), where + '.pattern')
        try:
            re.compile(param['pattern'])
        except re.error as error:
            raise ConfigError(where + '.pattern is not a regular expression: ' + str(error)) from None
        if 'values' in value:
            raise ConfigError(where + ' of kind pattern takes no values')
    else:
        param['path_like'] = _bool(value.get('path_like', True), where + '.path_like')
        for position, entry in enumerate(value.get('allow_flags', [])):
            spot = '%s.allow_flags[%d]' % (where, position)
            _keys(entry, spot, {'name'}, {'arity', 'max', 'nonempty'})
            param['allow_flags'].append({
                'name': _str(entry['name'], spot + '.name', FLAG),
                'arity': _int(entry.get('arity', 1), spot + '.arity', 0, 1),
                'max': _int(entry.get('max', 1), spot + '.max', 1, 32),
                'nonempty': _bool(entry.get('nonempty', True), spot + '.nonempty'),
            })
    return param


def _flag(value, index, job, params):
    where = 'jobs.%s.flags[%d]' % (job, index)
    _keys(value, where, {'name', 'kind'},
          {'arity', 'values', 'sets', 'requires', 'enables_writeback', 'forward'})
    flag = {
        'name': _str(value['name'], where + '.name', FLAG),
        'kind': _choice(value['kind'], where + '.kind', ('forward', 'pandora')),
        'arity': _int(value.get('arity', 0), where + '.arity', 0, 1),
        'values': None, 'sets': None, 'requires': None,
        'enables_writeback': _bool(value.get('enables_writeback', False), where + '.enables_writeback'),
        'forward': _bool(value.get('forward', False), where + '.forward'),
    }
    if 'values' in value:
        if flag['arity'] != 1:
            raise ConfigError(where + '.values needs arity 1')
        flag['values'] = _strs(value['values'], where + '.values', unique=True)
    if flag['kind'] == 'pandora':
        if flag['arity']:
            raise ConfigError(where + ' of kind pandora must not take a value')
        if not flag['name'].startswith('--'):
            raise ConfigError(where + ' of kind pandora must be a long option')
        flag['sets'] = _str(value.get('sets', flag['name'].lstrip('-').replace('-', '_')), where + '.sets')
    else:
        if 'sets' in value:
            raise ConfigError(where + '.sets applies to pandora flags only')
        if 'forward' in value:
            raise ConfigError(where + '.forward applies to pandora flags only')
        flag['forward'] = True
    if 'requires' in value:
        need = _keys(value['requires'], where + '.requires', {'param', 'equals'})
        name = _str(need['param'], where + '.requires.param', NAME)
        if name not in {p['name'] for p in params}:
            raise ConfigError(where + '.requires.param is not a parameter of this job: ' + name)
        flag['requires'] = {'param': name, 'equals': _str(need['equals'], where + '.requires.equals')}
    return flag


def _form(value, index, job):
    where = 'jobs.%s.forms[%d]' % (job, index)
    _keys(value, where, {'prefix'}, {'on_extra'})
    form = {'prefix': _strs(value['prefix'], where + '.prefix'), 'on_extra': None}
    if not form['prefix']:
        raise ConfigError(where + '.prefix must not be empty')
    if 'on_extra' in value:
        form['on_extra'] = _on_extra(value['on_extra'], where + '.on_extra')
    return form


def _on_extra(value, where):
    _keys(value, where, {'action'}, {'message'})
    return {'action': _choice(value['action'], where + '.action', EXTRA),
            'message': _str(value['message'], where + '.message') if 'message' in value else None}


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


def _output(value, index, job, names):
    where = 'jobs.%s.outputs[%d]' % (job, index)
    _keys(value, where, {'kind', 'paths'}, {'requires_option'})
    output = {
        'kind': _choice(value['kind'], where + '.kind', OUTPUTS),
        'paths': _strs(value['paths'], where + '.paths', unique=True),
        'requires_option': None,
    }
    if not output['paths']:
        raise ConfigError(where + '.paths must not be empty')
    for path in output['paths']:
        _check_template(path, names['scalar'], where + '.paths')
        if path.startswith('/') or '..' in path.split('/'):
            raise ConfigError(where + '.paths must stay inside the worktree: ' + path)
    if 'requires_option' in value:
        output['requires_option'] = _str(value['requires_option'], where + '.requires_option')
    return output


def _argv(value, where, names, extra=frozenset()):
    argv = _strs(value, where)
    if not argv:
        raise ConfigError(where + ' must not be empty')
    for item in argv:
        whole = TOKEN.fullmatch(item)
        if whole and whole.group(1) in names['splice']:
            continue
        _check_template(item, names['scalar'] | set(extra), where)
    return argv


def _run(value, where, names):
    _keys(value, where, {'argv'}, {'cwd', 'env'})
    run = {'argv': _argv(value['argv'], where + '.argv', names),
           'cwd': _str(value.get('cwd', '.'), where + '.cwd'),
           'env': _env(value.get('env', {}), where + '.env')}
    for key, item in run['env'].items():
        _check_template(item, names['scalar'], where + '.env.' + key)
    if run['cwd'].startswith('/') or '..' in run['cwd'].split('/'):
        raise ConfigError(where + '.cwd must stay inside the worktree: ' + run['cwd'])
    return run


def _shards(value, where, names):
    _keys(value, where, {'strategy'}, {'default', 'min', 'max', 'env', 'argv_append', 'plan'})
    shards = {
        'strategy': _choice(value['strategy'], where + '.strategy', ('env', 'argv')),
        'min': _int(value.get('min', 1), where + '.min', 1, 32),
        'max': _int(value.get('max', 32), where + '.max', 1, 32),
        'env': _env(value.get('env', {}), where + '.env'),
        'argv_append': _strs(value.get('argv_append', []), where + '.argv_append'),
        'plan': None,
    }
    shards['default'] = _int(value.get('default', 1), where + '.default', shards['min'], shards['max'])
    if shards['min'] > shards['max']:
        raise ConfigError(where + '.min exceeds max')
    sharded = names['scalar'] | {'shard.index', 'shard.total'}
    for key, item in shards['env'].items():
        _check_template(item, sharded, where + '.env.' + key)
    for item in shards['argv_append']:
        _check_template(item, sharded, where + '.argv_append')
    if (shards['strategy'] == 'env') != bool(shards['env']):
        raise ConfigError(where + " strategy 'env' requires env and forbids it otherwise")
    if (shards['strategy'] == 'argv') != bool(shards['argv_append']):
        raise ConfigError(where + " strategy 'argv' requires argv_append and forbids it otherwise")
    if 'plan' in value:
        plan = _keys(value['plan'], where + '.plan', {'run'},
                     {'services', 'cpu_millis', 'memory_mib', 'emits'})
        emits = None
        if 'emits' in plan:
            emits = _check_template(_str(plan['emits'], where + '.plan.emits'),
                                    names['scalar'], where + '.plan.emits')
            if emits.startswith('/') or '..' in emits.split('/'):
                raise ConfigError(where + '.plan.emits must stay inside the worktree: ' + emits)
        shards['plan'] = {
            'run': _run(plan['run'], where + '.plan.run', names),
            'emits': emits,
            'services': _strs(plan.get('services', []), where + '.plan.services', NAME, unique=True),
            'cpu_millis': _int(plan['cpu_millis'], where + '.plan.cpu_millis') if 'cpu_millis' in plan else None,
            'memory_mib': _int(plan['memory_mib'], where + '.plan.memory_mib') if 'memory_mib' in plan else None,
        }
    return shards


def _template_names(params, flags):
    """Scalar tokens usable anywhere; splice tokens usable as a whole argv element."""
    scalar, splice = {'job', 'params_json'}, set()
    for param in params:
        if param['kind'] == 'rest':
            splice.add('p.%s[]' % param['name'])
        else:
            scalar.add('p.' + param['name'])
    for flag in flags:
        if flag['kind'] == 'pandora':
            scalar.add('opt.' + flag['sets'])
        if flag['forward']:
            splice.add('f!.' + flag['name'])
        if flag['arity']:
            scalar.add('f.' + flag['name'])
    return {'scalar': scalar, 'splice': splice}


def _job(value, index, services):
    where = 'jobs[%d]' % index
    _keys(value, where, {'id', 'forms', 'run', 'cpu_millis', 'memory_mib'},
          {'summary', 'tool', 'params', 'flags', 'services', 'shards', 'outputs', 'fallback',
           'on_extra', 'usage', 'exclusive', 'reject_if_set'})
    job_id = _str(value['id'], where + '.id', NAME)
    where = 'jobs.' + job_id
    params = [_param(item, position, job_id) for position, item in enumerate(value.get('params', []))]
    if len({p['name'] for p in params}) != len(params):
        raise ConfigError(where + '.params has duplicate names')
    rest = [p for p in params if p['kind'] == 'rest']
    if len(rest) > 1 or (rest and params[-1]['kind'] != 'rest'):
        raise ConfigError(where + '.params allows at most one rest parameter, and it must come last')
    seen_optional = False
    for param in params:
        if param['kind'] == 'rest':
            continue
        if not param['required']:
            seen_optional = True
        elif seen_optional:
            raise ConfigError(where + '.params cannot require a parameter after an optional one')
    flags = [_flag(item, position, job_id, params) for position, item in enumerate(value.get('flags', []))]
    if len({f['name'] for f in flags}) != len(flags):
        raise ConfigError(where + '.flags has duplicate names')
    names = _template_names(params, flags)
    job = {
        'id': job_id,
        'summary': _str(value.get('summary', job_id), where + '.summary'),
        'tool': _str(value['tool'], where + '.tool') if 'tool' in value else None,
        'reject_if_set': _strs(value.get('reject_if_set', []), where + '.reject_if_set', unique=True),
        'forms': [_form(item, position, job_id) for position, item in enumerate(value['forms'])],
        'params': params, 'flags': flags,
        'services': _strs(value.get('services', []), where + '.services', NAME, unique=True),
        'run': _run(value['run'], where + '.run', names),
        'cpu_millis': _int(value['cpu_millis'], where + '.cpu_millis'),
        'memory_mib': _int(value['memory_mib'], where + '.memory_mib'),
        'exclusive': _strs(value.get('exclusive', []), where + '.exclusive', NAME, unique=True),
        'shards': _shards(value['shards'], where + '.shards', names) if 'shards' in value else None,
        'outputs': [_output(item, position, job_id, names)
                    for position, item in enumerate(value.get('outputs', []))],
        'fallback': _fallback(value['fallback'], where + '.fallback') if 'fallback' in value else None,
        'on_extra': _on_extra(value['on_extra'], where + '.on_extra') if 'on_extra' in value
                    else {'action': 'reject', 'message': None},
        'usage': _str(value['usage'], where + '.usage') if 'usage' in value else None,
    }
    if not job['forms']:
        raise ConfigError(where + '.forms must not be empty')
    unknown = sorted(set(job['services']) - set(services))
    if unknown:
        raise ConfigError(where + '.services names undeclared service(s): ' + ', '.join(unknown))
    options = {f['sets'] for f in flags if f['kind'] == 'pandora'}
    for output in job['outputs']:
        if output['requires_option'] and output['requires_option'] not in options:
            raise ConfigError('%s.outputs requires option %r, which no flag of this job sets'
                              % (where, output['requires_option']))
        if output['kind'] == 'writeback' and not output['requires_option']:
            raise ConfigError(where + '.outputs of kind writeback must name a requires_option')
    if job['shards'] and job['shards']['plan']:
        unknown = sorted(set(job['shards']['plan']['services']) - set(services))
        if unknown:
            raise ConfigError(where + '.shards.plan.services names undeclared service(s): ' + ', '.join(unknown))
    return job


def validate(value):
    """Return a normalized configuration or raise ConfigError."""
    _keys(value, 'configuration', {'version', 'repo', 'runtime', 'prepare', 'jobs'},
          {'services', 'env', 'secrets', 'fallback', 'feedback', 'matching'})
    if type(value['version']) is not int or value['version'] != VERSION:
        raise ConfigError('configuration version must be %d' % VERSION)
    repo = _keys(value['repo'], 'repo', {'name', 'entrypoints'}, {'root_markers'})
    runtime = _keys(value['runtime'], 'runtime', {'base_image'}, {'setup', 'env', 'workdir', 'user'})
    prepare = _keys(value['prepare'], 'prepare', {'argv', 'cache_key_paths'}, {'cache_key_env', 'check_argv'})
    matching = _keys(value.get('matching', {}), 'matching', (), {'strip_prefixes', 'subdirectory'})
    feedback = _keys(value.get('feedback', {}), 'feedback', (), {'reject_suffix', 'extra_message'})
    secrets = _keys(value.get('secrets', {}), 'secrets', (), {'exclude_globs'})
    environment = _keys(value.get('env', {}), 'env', (), {'set', 'passthrough', 'reject_if_set'})
    if '@sha256:' not in runtime['base_image']:
        raise ConfigError('runtime.base_image must be digest-pinned: ' + runtime['base_image'])
    services = {}
    for index, item in enumerate(value.get('services', [])):
        service = _service(item, index)
        if service['id'] in services:
            raise ConfigError('services has duplicate id ' + service['id'])
        services[service['id']] = service
    jobs = {}
    for index, item in enumerate(value['jobs']):
        job = _job(item, index, services)
        if job['id'] in jobs:
            raise ConfigError('jobs has duplicate id ' + job['id'])
        jobs[job['id']] = job
    entrypoints = _strs(repo['entrypoints'], 'repo.entrypoints', unique=True)
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
    config = {
        'version': VERSION,
        'repo': {'name': _str(repo['name'], 'repo.name'),
                 'entrypoints': entrypoints,
                 'root_markers': _strs(repo.get('root_markers', []), 'repo.root_markers', unique=True)},
        'runtime': {'base_image': runtime['base_image'],
                    'setup': _strs(runtime.get('setup', []), 'runtime.setup'),
                    'env': _env(runtime.get('env', {}), 'runtime.env'),
                    'workdir': _str(runtime.get('workdir', '/workspace'), 'runtime.workdir'),
                    'user': _str(runtime.get('user', 'root'), 'runtime.user')},
        'prepare': {'argv': _strs(prepare['argv'], 'prepare.argv'),
                    'cache_key_paths': _strs(prepare['cache_key_paths'], 'prepare.cache_key_paths', unique=True),
                    'cache_key_env': _strs(prepare.get('cache_key_env', []), 'prepare.cache_key_env', unique=True),
                    'check_argv': _strs(prepare['check_argv'], 'prepare.check_argv') if 'check_argv' in prepare else []},
        'env': {'set': _env(environment.get('set', {}), 'env.set'),
                'passthrough': _strs(environment.get('passthrough', []), 'env.passthrough', unique=True),
                'reject_if_set': _strs(environment.get('reject_if_set', []), 'env.reject_if_set', unique=True)},
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
        'services': services,
        'jobs': jobs,
    }
    if not config['prepare']['argv']:
        raise ConfigError('prepare.argv must not be empty')
    if not config['repo']['entrypoints']:
        raise ConfigError('repo.entrypoints must not be empty')
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
        return validate(raw)
    except ConfigError as error:
        raise ConfigError('%s: %s' % (path, error)) from None
