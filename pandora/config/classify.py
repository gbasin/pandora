"""Config-driven argv classification: a remote job plan, a local passthrough, or a refusal.

Nothing here knows a repository. Every claimed spelling, every option and every
output path comes from the loaded configuration; this module owns only the
matching order and the plan shape the engine consumes.

The argv boundary is deliberately thin. Pandora recognizes the literal form it
claims, the options it must consume itself (an option that arms writeback), the
flags whose *value* it must not read, and an explicit refusal list. Everything
else is forwarded to the repository's own runner unexamined, because the
repository's runner is the only thing that knows what a valid selector is.

Since the v0.2 slice, `validate` closes the gap that used to leave: the
repository's runner is asked, locally and with a deadline, to accept the
forwarded arguments before anything is queued. See `preflight`.
"""
import subprocess
from pathlib import Path, PurePosixPath

from ..errors import ConfigError, NotClaimed, Refused, ValidationRejected
from ..exits import USAGE
from .loader import ARG_PATH, _writeback_path

PLAN_VERSION = 2


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


def usage_of(job):
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


def _splice(argv, args_at, tail):
    argv = list(argv)
    if args_at is not None:
        argv[args_at:args_at + 1] = tail
    return argv


def preflight(job, forwarded, *, root, extra_env=None, run=subprocess.run):
    """Ask the repository whether it will accept these arguments, before queuing.

    The contract is deliberately crude, because it has to hold for a repository
    Pandora has never seen: exit 0 means "I would run this", anything else means
    "I refuse, and my stderr is the message". Pandora adds nothing to that
    message; the repository already said it better.

    Three constraints make it safe to put on the submission path: it runs in the
    worktree with a millisecond budget, it is given no arguments Pandora has not
    already checked, and a timeout or a missing interpreter is *not* a refusal --
    it is a skipped check, because a broken validator must not block a run that
    would otherwise be fine.
    """
    if job['validate'] is None:
        return {'ran': False, 'reason': 'no validator declared'}
    spec = job['validate']
    argv = _splice(spec['argv'], spec['args_at'], list(forwarded))
    cwd = Path(root) / spec['cwd']
    env = dict(extra_env or {})
    env.update(spec['env'])
    try:
        proc = run(argv, cwd=str(cwd), env=env, capture_output=True, text=True,
                   timeout=spec['timeout_ms'] / 1000.0)
    except subprocess.TimeoutExpired:
        return {'ran': False, 'reason': 'validator exceeded %dms' % spec['timeout_ms']}
    except OSError as error:
        return {'ran': False, 'reason': 'validator could not start: %s' % error}
    if proc.returncode != 0:
        raise ValidationRejected('%s refused these arguments' % ' '.join(argv[:2]),
                                 code=proc.returncode,
                                 stderr=(proc.stderr or proc.stdout or '').rstrip())
    return {'ran': True, 'argv': argv, 'seconds': None}


def environment(config, job, caller=None):
    """Resolved environment for the run, lowest precedence first.

    The caller's values for the names `[env] passthrough` declares, then `[env]
    set`, then the job's `run.env`; `unset` last, over all three. `caller` is the
    already-filtered environment (`client.envfilter`), so a secret-shaped or
    platform name never arrives here whatever `passthrough` says.
    """
    caller = caller or {}
    env = {name: caller[name] for name in config['env']['passthrough'] if name in caller}
    env.update(config['env']['set'])
    env.update(job['run']['env'])
    unset = sorted(set([*config['env']['unset'], *job['run']['unset']]))
    for name in unset:
        env.pop(name, None)
    return env, unset


def _positional(forwarded, guarded):
    """The forwarded tokens that are neither a flag nor a flag's value.

    `{argN}` counts these, so `test:surface desk --grep x` binds `{arg1}` to
    `desk`, not to `--grep`. A guarded token -- the value `value_flags` paired
    with its flag -- was forwarded unexamined and may not even be a path, so
    it never fills a declared path.
    """
    return [token for position, token in enumerate(forwarded)
            if position not in guarded and not token.startswith('-')]


def _expand_arg_paths(paths, positional):
    """Render each declared path's `{argN}` against the positional arguments.

    A command that does not supply the argument is refused at claim, not after
    the run: an output that cannot be named cannot be declared, and a missing
    declared output is a verdict, so an unrenderable one must never ship.
    """
    expanded = []
    for path in paths:
        def fill(match):
            index = int(match.group(1))
            if index > len(positional):
                raise Refused('declared path %s needs a positional argument %d; the '
                              'command supplies %d' % (path, index, len(positional)))
            return positional[index - 1]
        path = ARG_PATH.sub(fill, path)
        _posix(path)
        expanded.append(path)
    return expanded


def build_plan(config, job, forwarded, chosen, caller_env=None, guarded=()):
    options = {option['sets']: False for option in job['options']}
    for option in chosen.values():
        options[option['sets']] = True
    tail = list(forwarded)
    for option in job['options']:
        if option['forward'] and option['name'] in chosen:
            tail.append(option['name'])
    argv = _splice(job['run']['argv'], job['run']['args_at'], tail)
    env, unset = environment(config, job, caller_env)
    positional = _positional(forwarded, guarded)
    outputs = []
    for output in job['outputs']:
        if output['requires_option'] and not options.get(output['requires_option']):
            continue
        paths = _expand_arg_paths(output['paths'], positional)
        if output['kind'] == 'writeback':
            for path in paths:
                try:
                    _writeback_path(path, 'outputs')
                except ConfigError as error:
                    raise Refused(str(error)) from error
        outputs.append({'kind': output['kind'], 'paths': paths})
    shards = job['shards']
    if shards and (shards['plan_outputs'] or shards['report']):
        shards = dict(shards)
        shards['plan_outputs'] = _expand_arg_paths(shards['plan_outputs'], positional)
        if shards['report']:
            shards['report'] = _expand_arg_paths([shards['report']], positional)[0]
    return {
        'version': PLAN_VERSION,
        'repo': config['repo']['name'],
        'job': job['id'],
        'summary': job['summary'],
        'size': job['size'],
        'args': list(forwarded),
        'options': options,
        'argv': argv,
        'cwd': job['run']['cwd'],
        'env': env,
        'env_unset': unset,
        'env_passthrough': list(config['env']['passthrough']),
        'secrets_exclude_globs': list(config['secrets']['exclude_globs']),
        'outputs': outputs,
        # None means one shard and no fan-out. The engine owns the count, not
        # the client: only the worker knows how many lanes are free.
        'shards': shards,
        'timeout_minutes': job['timeout_minutes'],
        'fallback': job['fallback'],
        'cancel': job['cancel'],
        'drift': job['drift'],
        'git': job['git'],
        'where': job['where'],
        'writeback': any(output['kind'] == 'writeback' for output in outputs),
        'worker': config['worker'],
    }


def _message(config, text):
    suffix = config['feedback']['reject_suffix']
    if suffix and not text.endswith(suffix.strip()):
        text = text.rstrip() + ' ' + suffix.strip()
    return text


SUBDIRECTORY_MESSAGE = 'run from the repo root to route'
SUBDIRECTORY_UNCLAIMED = 'claimed only at the worktree root, and this was typed in %s'


def path_like(tokens, exists=None):
    """The first token that could name a file, or None.

    This is the whole of the subdirectory rule. `pnpm journey S0-01` typed three
    directories down means the same thing everywhere, because `S0-01` is a
    selector the repository resolves against its own catalog; `pnpm unit
    ./foo.test.ts` does not, because the path is relative to where it was typed.
    So the first is re-rooted and the second is not -- and "could name a file" is
    answered by a slash or by the filesystem, never by guessing at extensions.
    """
    for token in tokens:
        if token.startswith('-'):
            continue
        if '/' in token:
            return token
        if exists is not None and exists(token):
            return token
    return None


def classify(config, argv, *, cwd='.', env=None, exists=None, present=None):
    """Return {'decision', 'reason'|'message', 'plan', 'job', 'forwarded'}.

    `remote` means Pandora will run it. `local` means no configured job claims
    it, which is not an error. `reject` means a claimed job will not take this
    argv, which is the repository's opinion reported verbatim; a reject verdict
    always carries `exit` = 64, because the command as typed cannot be routed.

    `cwd` is where the command was typed, relative to the worktree root. When it
    is not the root, the verdict carries `rerooted` naming that directory, and
    the caller runs the job from the root instead -- unless the repository says
    `subdirectory = "passthrough"`, and then nothing is claimed there at all.
    """
    tokens, tool = list(argv), None
    if tokens[:1] and tokens[0] in config['repo']['entrypoints']:
        tool, tokens = tokens[0], tokens[1:]
    tokens = strip_prefixes(config, tokens)
    found = match_form(config, tokens, tool)
    if found is None:
        return {'decision': 'local', 'reason': 'no configured job claims this command',
                'plan': None, 'job': None, 'forwarded': []}
    job, form, rest = found
    rerooted = None
    if cwd not in ('', '.'):
        if config['matching']['subdirectory'] == 'passthrough':
            # `pnpm test` in a package is that package's test, not the root's.
            return {'decision': 'local', 'reason': SUBDIRECTORY_UNCLAIMED % cwd,
                    'plan': None, 'job': job['id'], 'forwarded': []}
        if config['matching']['subdirectory'] == 'reject':
            return {'decision': 'reject', 'plan': None, 'job': job['id'], 'forwarded': [],
                    'exit': USAGE,
                    'message': _message(config, 'Run this command from the repository root.')}
        offender = path_like(rest, exists)
        if offender is not None:
            # Refused, never passed through. The command is claimed -- heavy by
            # the repository's own account -- and a pass-through would run it on
            # this Mac with no queue, no admission and no receipt, which is the
            # shape of the 2026-09-22 accident. Re-rooting would silently change
            # which file the path names. So the caller is told where to stand.
            return {'decision': 'reject', 'job': job['id'], 'plan': None, 'forwarded': [],
                    'code': 'subdirectory', 'exit': USAGE,
                    'message': SUBDIRECTORY_MESSAGE,
                    'rerooted': None, 'blocked_by': offender}
        rerooted = cwd
    # `present` is every name set in the caller's own environment. `env` has
    # been through the secret and platform filters, which remove exactly the
    # names worth refusing on (`NODE_OPTIONS`, `*_TOKEN`), so it answers only
    # for a caller that did not send `present`.
    if present is None:
        present = [name for name, value in (env or {}).items() if value]
    present = set(present)
    for name in [*config['env']['reject_if_set'], *job['reject_if_set']]:
        if name in present:
            return {'decision': 'reject', 'plan': None, 'job': job['id'], 'forwarded': [],
                    'exit': USAGE,
                    'message': _message(config, 'Unset %s before %s; it would silently change '
                                                'the routed job.' % (name, ' '.join(form['prefix'])))}
    try:
        forwarded, chosen, guarded = split(job, rest)
    except Refused as error:
        return {'decision': 'reject', 'plan': None, 'job': job['id'], 'forwarded': [],
                'exit': USAGE,
                'message': _message(config, '%s (%s)' % (usage_of(job), error))}
    if job['args'] == 'none':
        if forwarded:
            action = form['on_extra'] or job['on_extra']
            if action['action'] == 'local':
                return {'decision': 'local', 'reason': 'focused form stays local',
                        'plan': None, 'job': job['id'], 'forwarded': forwarded}
            text = action['message'] or config['feedback']['extra_message']
            return {'decision': 'reject', 'plan': None, 'job': job['id'], 'forwarded': forwarded,
                    'exit': USAGE,
                    'message': _message(config, text.replace('{job}', job['id']))}
    else:
        if job['args'] == 'required' and not forwarded:
            return {'decision': 'reject', 'plan': None, 'job': job['id'], 'forwarded': [],
                    'exit': USAGE,
                    'message': _message(config, usage_of(job))}
        try:
            check_arguments(job, forwarded, guarded)
        except Refused as error:
            return {'decision': 'reject', 'plan': None, 'job': job['id'], 'forwarded': forwarded,
                    'exit': USAGE,
                    'message': _message(config, '%s (%s)' % (usage_of(job), error))}
    try:
        plan = build_plan(config, job, forwarded, chosen, caller_env=env, guarded=guarded)
    except Refused as error:
        return {'decision': 'reject', 'plan': None, 'job': job['id'], 'forwarded': forwarded,
                'exit': USAGE,
                'message': _message(config, '%s (%s)' % (usage_of(job), error))}
    return {'decision': 'remote', 'reason': '', 'job': job['id'], 'forwarded': forwarded,
            'chosen': chosen, 'rerooted': rerooted, 'plan': plan}


def claim_index(config):
    """Every claimed argv prefix the configuration declares, shortest first.

    This is what the shim reads out of the enrollment marker. It is an
    optimization -- "could this be claimed?" answered in microseconds without
    loading 300 lines of TOML -- and never the decision: the daemon
    re-classifies every request it receives and may still refuse.
    """
    claims = []
    for job in config['jobs'].values():
        for form in job['forms']:
            prefix = list(form['prefix'])
            if prefix not in claims:
                claims.append(prefix)
    claims.sort(key=lambda item: (len(item), item))
    return claims


def policy_index(config):
    """Each claimed form's size class and fallback verdict, for the marker.

    Written into the enrollment so the client can answer "may this run on this
    Mac" without the daemon -- which matters precisely because the commonest
    reason to ask is that the daemon is not there to be asked.
    """
    policies = []
    for job in config['jobs'].values():
        writeback = any(output['kind'] == 'writeback' for output in job['outputs'])
        for form in job['forms']:
            policies.append({'prefix': list(form['prefix']), 'size': job['size'],
                             'fallback': (job['fallback'] or {}).get('action', 'auto'),
                             'writeback': writeback})
    policies.sort(key=lambda item: (len(item['prefix']), item['prefix']))
    return policies


def claimed_or_raise(config, argv, **kwargs):
    """`classify`, with the two non-remote verdicts raised as their exceptions."""
    verdict = classify(config, argv, **kwargs)
    if verdict['decision'] == 'local':
        raise NotClaimed(verdict['reason'])
    if verdict['decision'] == 'reject':
        error = Refused(verdict['message'])
        error.code, error.exit = verdict.get('code'), verdict.get('exit')
        raise error
    return verdict
