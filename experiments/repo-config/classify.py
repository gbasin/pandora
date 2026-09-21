"""Config-driven argv classification: remote job plan, local passthrough, or refusal.

Nothing here knows a repository.  Every accepted spelling, every option, every
service and every output comes from the loaded configuration; this module owns
only the matching order and the plan shape that Pandora's core consumes.
"""
import json
import re
from pathlib import PurePosixPath

from config import TOKEN

PLAN_VERSION = 1


class Refused(ValueError):
    """An argv that a configured job claims but cannot accept."""


def _posix(value):
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts:
        raise Refused('arguments must stay inside the worktree: ' + value)
    return path


def strip_prefixes(config, argv):
    """Remove each declared wrapper prefix at most once, in declaration order."""
    rest = list(argv)
    for prefix in config['matching']['strip_prefixes']:
        if rest[:len(prefix)] == prefix:
            rest = rest[len(prefix):]
    return rest


def match_form(config, argv, tool=None):
    """Return (job, form, remainder) for the longest claimed prefix, else None."""
    best = None
    for job in config['jobs'].values():
        if tool is not None and job['tool'] != tool:
            continue
        for form in job['forms']:
            prefix = form['prefix']
            if argv[:len(prefix)] == prefix and (best is None or len(prefix) > len(best[1]['prefix'])):
                best = (job, form, argv[len(prefix):])
    return best


def _usage(job):
    if job['usage']:
        return job['usage']
    parts = ['pnpm', *job['forms'][0]['prefix']]
    for param in job['params']:
        if param['kind'] == 'enum':
            parts.append('<' + '|'.join(param['values']) + '>')
        elif param['kind'] == 'pattern':
            parts.append('<' + param['name'] + '>')
        else:
            parts.append('[' + param['name'] + '...]')
    for flag in job['flags']:
        parts.append('[%s%s]' % (flag['name'], ' VALUE' if flag['arity'] else ''))
    return 'Use ' + ' '.join(parts) + '.'


def _rest(param, tokens, job_flags, take_flag):
    """Collect forwarded tokens, honouring the param's own inline flags."""
    values, counts, index = [], {}, 0
    allowed = {entry['name']: entry for entry in param['allow_flags']}
    while index < len(tokens):
        token = tokens[index]
        if token in job_flags:
            index = take_flag(tokens, index)
            continue
        entry = allowed.get(token)
        if entry is not None:
            counts[token] = counts.get(token, 0) + 1
            if counts[token] > entry['max']:
                raise Refused('provide at most %d %s' % (entry['max'], token))
            values.append(token)
            if entry['arity']:
                if index + 1 >= len(tokens):
                    raise Refused('provide one value for ' + token)
                value = tokens[index + 1]
                if entry['nonempty'] and not value:
                    raise Refused('provide one nonempty value for ' + token)
                values.append(value)
            index += 1 + entry['arity']
            continue
        if not token or '\x00' in token:
            raise Refused('empty arguments are not routed')
        if param['path_like']:
            if token.startswith('-'):
                raise Refused('unsupported option ' + token)
            _posix(token)
        values.append(token)
        index += 1
    return values


def parse(job, tokens):
    """Return (params, flags, options) or raise Refused."""
    params, flags, options = {}, {}, {}
    by_name = {flag['name']: flag for flag in job['flags']}
    for flag in job['flags']:
        if flag['kind'] == 'pandora':
            options[flag['sets']] = False

    def take_flag(source, index):
        flag = by_name[source[index]]
        if flag['name'] in flags or (flag['kind'] == 'pandora' and options[flag['sets']]):
            raise Refused('use at most one ' + flag['name'])
        if flag['kind'] == 'pandora':
            options[flag['sets']] = True
            flags[flag['name']] = None
            return index + 1
        if not flag['arity']:
            flags[flag['name']] = ''
            return index + 1
        if index + 1 >= len(source):
            raise Refused('provide one value for ' + flag['name'])
        value = source[index + 1]
        if flag['values'] is not None and value not in flag['values']:
            raise Refused('%s accepts %s' % (flag['name'], ', '.join(flag['values'])))
        flags[flag['name']] = value
        return index + 2

    positional = [p for p in job['params'] if p['kind'] != 'rest']
    rest = next((p for p in job['params'] if p['kind'] == 'rest'), None)
    index = 0
    for param in positional:
        if index >= len(tokens) or tokens[index] in by_name:
            if param['required']:
                raise Refused('missing ' + param['name'])
            continue
        value = tokens[index]
        if param['kind'] == 'enum' and value not in param['values']:
            raise Refused('%s must be one of %s' % (param['name'], ', '.join(param['values'])))
        if param['kind'] == 'pattern' and not re.fullmatch(param['pattern'], value):
            raise Refused('%s is not a recognized %s' % (value, param['name']))
        params[param['name']] = value
        index += 1
    tail = tokens[index:]
    if rest is not None:
        params[rest['name']] = _rest(rest, tail, by_name, take_flag)
    else:
        position = 0
        while position < len(tail):
            if tail[position] not in by_name:
                raise Refused('unsupported argument ' + (tail[position] or '(empty)'))
            position = take_flag(tail, position)
    for flag in job['flags']:
        need = flag['requires']
        if need and flag['name'] in flags and params.get(need['param']) != need['equals']:
            raise Refused('%s applies only when %s is %s' % (flag['name'], need['param'], need['equals']))
    return params, flags, options


def _scope(config, job, params, flags, options):
    scope = {'job': job['id']}
    for param in job['params']:
        if param['kind'] != 'rest':
            scope['p.' + param['name']] = params.get(param['name'], '')
    for flag in job['flags']:
        if flag['kind'] == 'pandora':
            scope['opt.' + flag['sets']] = 'true' if options[flag['sets']] else 'false'
        if flag['arity']:
            scope['f.' + flag['name']] = flags.get(flag['name']) or ''
    payload = {'version': PLAN_VERSION, 'job': job['id'], 'repo': config['repo']['name'],
               'params': params, 'flags': {k: v for k, v in flags.items() if v is not None},
               'options': options}
    scope['params_json'] = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return scope


def _splices(job, params, flags):
    splice = {}
    for param in job['params']:
        if param['kind'] == 'rest':
            splice['p.%s[]' % param['name']] = list(params.get(param['name'], []))
    for flag in job['flags']:
        if not flag['forward']:
            continue
        if flag['name'] not in flags:
            splice['f!.' + flag['name']] = []
        elif flag['arity']:
            splice['f!.' + flag['name']] = [flag['name'], flags[flag['name']]]
        else:
            splice['f!.' + flag['name']] = [flag['name']]
    return splice


def render(text, scope):
    return TOKEN.sub(lambda match: scope[match.group(1)], text)


def render_argv(argv, scope, splice):
    result = []
    for item in argv:
        whole = TOKEN.fullmatch(item)
        if whole and whole.group(1) in splice:
            result.extend(splice[whole.group(1)])
        else:
            result.append(render(item, scope))
    return result


def _reroot(config, job, params, cwd):
    """Re-express worktree-relative arguments against the repository root."""
    if cwd in ('', '.'):
        return params, None
    base = _posix(cwd)
    moved = dict(params)
    touched = []
    for param in job['params']:
        if param['kind'] != 'rest' or not param['path_like']:
            continue
        allowed = {entry['name']: entry for entry in param['allow_flags']}
        values, index, source = [], 0, moved.get(param['name'], [])
        while index < len(source):
            token = source[index]
            if token in allowed:
                step = 1 + allowed[token]['arity']
                values.extend(source[index:index + step])
                index += step
                continue
            values.append(str(base / token))
            touched.append(token)
            index += 1
        moved[param['name']] = values
    return moved, ('re-rooted %s against %s' % (', '.join(touched), cwd) if touched else None)


def shard_count(job, requested):
    if job['shards'] is None:
        if requested is not None and requested != 1:
            raise Refused('%s is not a sharded job' % job['id'])
        return 1
    count = job['shards']['default'] if requested is None else requested
    if type(count) is not int or not job['shards']['min'] <= count <= job['shards']['max']:
        raise Refused('%s accepts %d through %d shards' % (
            job['id'], job['shards']['min'], job['shards']['max']))
    return count


def _services(config, ids):
    return [config['services'][name] for name in ids]


def _resources(job, services, extra=None):
    cpu = (extra or {}).get('cpu_millis') or job['cpu_millis']
    memory = (extra or {}).get('memory_mib') or job['memory_mib']
    return {'cpu_millis': cpu + sum(s['cpu_millis'] for s in services),
            'memory_mib': memory + sum(s['memory_mib'] for s in services),
            'exclusive': list(job['exclusive'])}


def build_plan(config, job, params, flags, options, *, shards=None, cwd='.'):
    params, reroot = _reroot(config, job, params, cwd)
    scope = _scope(config, job, params, flags, options)
    splice = _splices(job, params, flags)
    total = shard_count(job, shards)
    services = _services(config, job['services'])
    base_env = dict(config['env']['set'])
    for service in services:
        base_env.update(service['exports'])
    base_env.update({k: render(v, scope) for k, v in job['run']['env'].items()})
    argv = render_argv(job['run']['argv'], scope, splice)
    steps = []
    for index in range(1, total + 1):
        env = dict(base_env)
        shard_argv = list(argv)
        if job['shards'] is not None:
            marks = dict(scope, **{'shard.index': str(index), 'shard.total': str(total)})
            env.update({k: render(v, marks) for k, v in job['shards']['env'].items()})
            shard_argv += [render(v, marks) for v in job['shards']['argv_append']]
        steps.append({'index': index, 'total': total, 'argv': shard_argv,
                      'cwd': job['run']['cwd'], 'env': env})
    plan_step = None
    if job['shards'] is not None and job['shards']['plan'] is not None:
        spec = job['shards']['plan']
        plan_services = _services(config, spec['services'])
        plan_env = dict(config['env']['set'])
        for service in plan_services:
            plan_env.update(service['exports'])
        plan_env.update({k: render(v, scope) for k, v in spec['run']['env'].items()})
        plan_step = {'argv': render_argv(spec['run']['argv'], scope, splice),
                     'cwd': spec['run']['cwd'], 'env': plan_env,
                     'emits': render(spec['emits'], scope) if spec['emits'] else None,
                     'services': [s['id'] for s in plan_services],
                     'resources': _resources(job, plan_services, spec)}
    outputs = []
    for output in job['outputs']:
        if output['requires_option'] and not options.get(output['requires_option']):
            continue
        outputs.append({'kind': output['kind'],
                        'paths': [render(p, scope) for p in output['paths']]})
    return {
        'version': PLAN_VERSION,
        'repo': config['repo']['name'],
        'job': job['id'],
        'summary': job['summary'],
        'params': params,
        'flags': {k: v for k, v in flags.items() if v is not None},
        'options': options,
        'cwd': cwd,
        'reroot': reroot,
        'runtime': config['runtime'],
        'prepare': config['prepare'],
        'env_passthrough': config['env']['passthrough'],
        'secrets_exclude_globs': config['secrets']['exclude_globs'],
        'services': [{k: v for k, v in service.items()} for service in services],
        'resources': _resources(job, services),
        'shard_count': total,
        'plan_step': plan_step,
        'shards': steps,
        'outputs': outputs,
        'fallback': job['fallback'],
    }


def _message(config, text):
    suffix = config['feedback']['reject_suffix']
    if suffix and not text.endswith(suffix.strip()):
        text = text.rstrip() + ' ' + suffix.strip()
    return text


def classify(config, argv, cwd='.', *, shards=None, env=None):
    """Return {'decision', 'reason'|'message', 'plan'} for one agent invocation."""
    tokens, tool = list(argv), None
    if tokens[:1] and tokens[0] in config['repo']['entrypoints']:
        tool, tokens = tokens[0], tokens[1:]
    tokens = strip_prefixes(config, tokens)
    found = match_form(config, tokens, tool)
    if found is None:
        return {'decision': 'local', 'reason': 'no configured job claims this command', 'plan': None}
    job, form, rest = found
    if cwd not in ('', '.') and config['matching']['subdirectory'] != 'reroot':
        if config['matching']['subdirectory'] == 'local':
            return {'decision': 'local', 'reason': 'routed jobs run from the repository root', 'plan': None}
        return {'decision': 'reject', 'plan': None,
                'message': _message(config, 'Run this command from the repository root.')}
    for name in [*config['env']['reject_if_set'], *job['reject_if_set']]:
        if (env or {}).get(name):
            return {'decision': 'reject', 'plan': None, 'message': _message(
                config, 'Unset %s before %s; it would silently change the routed job.'
                % (name, ' '.join(form['prefix'])))}
    try:
        params, flags, options = parse(job, rest)
    except Refused as error:
        if rest and not job['params'] and not job['flags']:
            action = form['on_extra'] or job['on_extra']
            if action['action'] == 'local':
                return {'decision': 'local', 'reason': 'focused form stays local', 'plan': None}
            text = action['message'] or config['feedback']['extra_message']
            return {'decision': 'reject', 'plan': None,
                    'message': _message(config, render(text, {'job': job['id']}))}
        return {'decision': 'reject', 'plan': None,
                'message': _message(config, '%s (%s)' % (_usage(job), error))}
    try:
        plan = build_plan(config, job, params, flags, options, shards=shards, cwd=cwd)
    except Refused as error:
        return {'decision': 'reject', 'plan': None, 'message': _message(config, str(error))}
    return {'decision': 'remote', 'reason': '', 'plan': plan}
