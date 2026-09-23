"""Config-driven argv classification: remote job plan, local passthrough, or refusal.

Nothing here knows a repository.  Every claimed spelling, every option, every
service and every output comes from the loaded configuration; this module owns
only the matching order, the worker-side resource table and the plan shape that
Pandora's core consumes.

The argv boundary is deliberately thin.  Pandora recognizes the literal form it
claims, the options it must consume itself (an option that arms writeback, an
option that changes shard behaviour), the flags whose *value* it must not read,
and an explicit refusal list.  Everything else is forwarded to the repository's
own runner unexamined, because the repository's runner is the thing that knows
what a valid selector is.
"""
import re
from pathlib import PurePosixPath

PLAN_VERSION = 1

# Worker-owned, not repo-owned.  A repository declares a size class; this table
# is the operator's, and these numbers are the evaluated worker's real limits
# (main 1000 millis / 4096 MiB), not the code defaults an example once copied.
WORKER = {
    'source': 'worker-config.json (evaluated single-worker pilot)',
    'sizes': {
        'small': {'cpu_millis': 500, 'memory_mib': 2048},
        'medium': {'cpu_millis': 1000, 'memory_mib': 4096},
        # This worker cannot offer more than medium; a repo asking for large is
        # clamped and told so, rather than being refused or silently upgraded.
        'large': {'cpu_millis': 1000, 'memory_mib': 4096},
    },
    'roles': {
        'db': {'cpu_millis': 500, 'memory_mib': 768},
        'pool': {'cpu_millis': 500, 'memory_mib': 256},
        'proxy': {'cpu_millis': 500, 'memory_mib': 128},
        'cache': {'cpu_millis': 250, 'memory_mib': 256},
    },
}

PAUSE_IMAGE = 'registry.k8s.io/pause:3.10'


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
    if job['args'] == 'required':
        parts.append('<arguments>')
    elif job['args'] == 'optional':
        parts.append('[arguments]')
    parts.extend('[%s]' % option['name'] for option in job['options'])
    return 'Use ' + ' '.join(parts) + '.'


def split(job, tokens):
    """Separate the options Pandora consumes from the argv it forwards verbatim."""
    known = {option['name']: option for option in job['options']}
    values = set(job['value_flags'])
    forwarded, chosen, guarded, index = [], {}, set(), 0
    while index < len(tokens):
        token = tokens[index]
        if token in values:
            if index + 1 >= len(tokens):
                raise Refused('provide one value for ' + token)
            if not tokens[index + 1]:
                raise Refused('provide one nonempty value for ' + token)
            forwarded.append(token)
            guarded.add(len(forwarded))
            forwarded.append(tokens[index + 1])
            index += 2
            continue
        if token in known:
            if token in chosen:
                raise Refused('use at most one ' + token)
            chosen[token] = known[token]
            index += 1
            continue
        forwarded.append(token)
        index += 1
    return forwarded, chosen, guarded


def check_arguments(job, forwarded, guarded):
    """Reject only what Pandora itself cannot carry, plus the job's refusal list."""
    for entry in job['reject']:
        for token in forwarded:
            if token in entry['args']:
                raise Refused(entry['message'])
    for position, token in enumerate(forwarded):
        if position in guarded:
            continue
        if not token or '\x00' in token:
            raise Refused('empty arguments are not routed')
        if not token.startswith('-'):
            _posix(token)


def _rerootable(token):
    """A forwarded token that names a file: it has a path separator or a suffix."""
    if token.startswith('-'):
        return False
    return '/' in token or '.' in token.rsplit('/', 1)[-1]


def reroot(forwarded, guarded, cwd):
    """Re-express worktree-relative arguments against the repository root."""
    if cwd in ('', '.'):
        return list(forwarded), None
    base = _posix(cwd)
    moved, touched = [], []
    for position, token in enumerate(forwarded):
        if position in guarded or not _rerootable(token):
            moved.append(token)
            continue
        moved.append(str(base / token))
        touched.append(token)
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


def resources(job, worker, services=None):
    """Map the repo's size class and service roles onto the worker's limits."""
    services = job['services'] if services is None else services
    if job['size'] not in worker['sizes']:
        raise Refused('this worker offers sizes %s; %s asks for %s'
                      % (', '.join(sorted(worker['sizes'])), job['id'], job['size']))
    main = worker['sizes'][job['size']]
    total = dict(main)
    for service in services:
        role = service['role']
        if role not in worker['roles']:
            raise Refused('this worker has no limits for the service role %r; it knows %s'
                          % (role, ', '.join(sorted(worker['roles']))))
        for key in total:
            total[key] += worker['roles'][role][key]
    order = ('small', 'medium', 'large')
    below = [name for name in order[:order.index(job['size'])] if worker['sizes'].get(name) == main]
    return {'size': job['size'], 'main': dict(main), 'clamped_to': below[0] if below else None,
            'cpu_millis': total['cpu_millis'], 'memory_mib': total['memory_mib'],
            'exclusive': list(job['exclusive']), 'worker': worker['source']}


def network(job):
    """One network namespace per run, the way a Kubernetes pod does it.

    GitHub Actions puts the job and its service containers on one bridge where
    the job reaches a service on ``localhost:<published port>`` and a service
    reaches another service by its *service name*.  A shared namespace
    reproduces the first for free -- CI's ``localhost:5432`` URLs work byte for
    byte -- and nothing is published on the host, so concurrent runs cannot
    collide on a host port.  It does not reproduce the second: a container that
    joins another's namespace gets no DNS aliases, so ``DB_HOST: postgres`` and
    ``ALLOW_ADDR_REGEX: ^pgbouncer:6432$`` resolve nothing.  ``/etc/hosts`` is
    per container even when the namespace is shared, so every member of the pod
    gets an explicit ``--add-host <service name>:127.0.0.1``.
    """
    names = []
    for service in job['services']:
        for name in (service.get('ci_name'), service['id']):
            if name and name not in names:
                names.append(name)
    ports, conflicts, forwards = {}, [], []
    for service in job['services']:
        if service['port'] in ports:
            conflicts.append({'port': service['port'],
                              'services': [ports[service['port']], service['id']]})
        ports.setdefault(service['port'], service['id'])
        for entry in service.get('ci_ports', []):
            if entry['host'] != entry['container']:
                forwards.append({'service': service['id'], 'listen': entry['host'],
                                 'target': entry['container']})
    return {
        'mode': 'pod',
        'pause_image': PAUSE_IMAGE,
        'join': '--network container:<pause>',
        'add_host': ['%s:127.0.0.1' % name for name in sorted(names)],
        'published_ports': [],
        'port_conflicts': conflicts,
        'port_forwards': forwards,
        'notes': [
            'The pause container owns the namespace: it starts first, outlives every member, '
            'and is the only container that could publish a host port.',
            'A member joining with --network container:<pause> may not set --publish, --hostname, '
            '--dns or --add-host on the namespace itself; --add-host still writes that member\'s '
            'own /etc/hosts, which is what makes service-to-service names resolve.',
            'Two services cannot listen on the same port inside one namespace; CI hides this '
            'because each service container has its own.',
            'A ports: mapping whose host and container port differ does NOT survive: there is no '
            'publish step to translate it, so the job would dial a dead port.  port_forwards '
            'lists every such mapping; each needs a forwarder in the namespace '
            '(socat TCP-LISTEN:<listen>,fork,reuseaddr TCP:127.0.0.1:<target>) or a service '
            'reconfigured to listen on the port CI published.',
        ],
    }


def _environment(config, job, options):
    """Resolved environment, lowest precedence first, with an explicit unset list."""
    env = dict(config['env']['set'])
    for service in job['services']:
        env.update(service['exports'])
    env.update(job['env'])
    env.update(job['run']['env'])
    unset = [name for name in [*config['env']['unset'], *job['run']['unset']]]
    for name in unset:
        env.pop(name, None)
    return env, sorted(set(unset))


def build_plan(config, job, forwarded, guarded, chosen, *, shards=None, cwd='.', worker=None):
    worker = worker or WORKER
    forwarded, moved = reroot(forwarded, guarded, cwd)
    options = {option['sets']: False for option in job['options']}
    for option in chosen.values():
        options[option['sets']] = True
    tail = list(forwarded)
    for option in job['options']:
        if option['forward'] and option['name'] in chosen:
            tail.append(option['name'])
    argv = list(job['run']['argv'])
    if job['run']['args_at'] is not None:
        argv[job['run']['args_at']:job['run']['args_at'] + 1] = tail
    total = shard_count(job, shards)
    base_env, unset = _environment(config, job, options)
    steps = []
    for index in range(1, total + 1):
        env = dict(base_env)
        shard_argv = list(argv)
        if job['shards'] is not None:
            marks = {'shard.index': str(index), 'shard.total': str(total)}
            env.update({k: _render(v, marks) for k, v in job['shards']['env'].items()})
            shard_argv += [_render(v, marks) for v in job['shards']['argv_append']]
        steps.append({'index': index, 'total': total, 'argv': shard_argv,
                      'cwd': job['run']['cwd'], 'env': env, 'unset': unset})
    plan_step = None
    if job['shards'] is not None and job['shards']['plan'] is not None:
        spec = job['shards']['plan']
        plan_services = [config['services'][name] for name in spec['services']]
        plan_env = dict(base_env)
        for service in plan_services:
            plan_env.update(service['exports'])
        plan_env.update(spec['run']['env'])
        plan_argv = [item for item in spec['run']['argv'] if item != '{args}']
        if spec['run']['args_at'] is not None:
            plan_argv = list(spec['run']['argv'])
            plan_argv[spec['run']['args_at']:spec['run']['args_at'] + 1] = tail
        plan_step = {'argv': plan_argv, 'cwd': spec['run']['cwd'], 'env': plan_env,
                     'emits': spec['emits'], 'services': [s['id'] for s in plan_services],
                     'resources': resources(job, worker, plan_services)}
    outputs = []
    for output in job['outputs']:
        if output['requires_option'] and not options.get(output['requires_option']):
            continue
        outputs.append({'kind': output['kind'], 'paths': list(output['paths'])})
    return {
        'version': PLAN_VERSION,
        'repo': config['repo']['name'],
        'job': job['id'],
        'summary': job['summary'],
        'args': forwarded,
        'options': options,
        'cwd': cwd,
        'reroot': moved,
        'runtime': config['runtime'],
        'prepare': config['prepare'],
        'dependency_cache': config['dependency_cache'],
        'env_passthrough': config['env']['passthrough'],
        'env_unset': unset,
        'secrets_exclude_globs': config['secrets']['exclude_globs'],
        'network': network(job),
        'services': [dict(service) for service in job['services']],
        'resources': resources(job, worker),
        'timeout_minutes': job['timeout_minutes'],
        'shard_count': total,
        'plan_step': plan_step,
        'shards': steps,
        'outputs': outputs,
        'fallback': job['fallback'],
        'provenance': dict(job['provenance']),
    }


def _render(text, scope):
    return re.sub(r'\{([^{}]+)\}', lambda match: scope[match.group(1)], text)


def _message(config, text):
    suffix = config['feedback']['reject_suffix']
    if suffix and not text.endswith(suffix.strip()):
        text = text.rstrip() + ' ' + suffix.strip()
    return text


def classify(config, argv, cwd='.', *, shards=None, env=None, worker=None):
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
        forwarded, chosen, guarded = split(job, rest)
    except Refused as error:
        return {'decision': 'reject', 'plan': None,
                'message': _message(config, '%s (%s)' % (_usage(job), error))}
    if job['args'] == 'none':
        if forwarded:
            action = form['on_extra'] or job['on_extra']
            if action['action'] == 'local':
                return {'decision': 'local', 'reason': 'focused form stays local', 'plan': None}
            text = action['message'] or config['feedback']['extra_message']
            return {'decision': 'reject', 'plan': None,
                    'message': _message(config, _render(text, {'job': job['id']}))}
    else:
        if job['args'] == 'required' and not forwarded:
            return {'decision': 'reject', 'plan': None,
                    'message': _message(config, _usage(job))}
        try:
            check_arguments(job, forwarded, guarded)
        except Refused as error:
            return {'decision': 'reject', 'plan': None,
                    'message': _message(config, '%s (%s)' % (_usage(job), error))}
    try:
        plan = build_plan(config, job, forwarded, guarded, chosen,
                          shards=shards, cwd=cwd, worker=worker)
    except Refused as error:
        return {'decision': 'reject', 'plan': None, 'message': _message(config, str(error))}
    return {'decision': 'remote', 'reason': '', 'plan': plan}
