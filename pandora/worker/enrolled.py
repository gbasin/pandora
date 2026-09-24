"""What the enrolled repositories name, read on the client for the worker verbs.

The worker knows goldens by fingerprint and nothing else; which of them a
repository still *means* is written in that repository's `pandora.toml`, which
lives here. So `pandora worker gc` and `pandora worker canary` both start on
the client: read every `[[repos]]` enrollment through the loader the daemon
uses, and hand the worker the answer -- fingerprints to protect, or targets to
prove.

Runs on the Mac only, and is deliberately not in the engine bundle.
"""
from ..config import classify, loader
from ..engine.runner import toolchain_of
from ..errors import ConfigError
from ..snapshot import transfer

# Where the canary writes a shard plan when the surface job has no `validate`.
# Inside the instance, thrown away with it.
PLAN_PATH = '/tmp/pandora-canary-plan.json'


def configs(settings_config, load=loader.load_for):
    """[(enrollment name, loaded config)] for every enrolled repository.

    One unreadable configuration fails the whole call rather than being
    skipped: a `gc` that silently forgot one repository is issue #81 again,
    with the difference that nobody could see why.
    """
    out = []
    for repo in settings_config.get('repos') or []:
        try:
            config = load(repo['root'], repo.get('config') or None)
        except ConfigError as error:
            raise ConfigError('enrolled repository %s: %s' % (repo['name'], error)) from None
        out.append((repo['name'], config))
    return out


def fingerprint_of(config):
    """The golden fingerprint a run from this configuration would ask for.

    Exactly what the engine computes from the plan's `worker` table, so the
    name protected here is the name a routed run builds.
    """
    return toolchain_of(config['worker']).fingerprint()


def named_fingerprints(entries):
    """{fingerprint: 'repo[,repo]'} for every enrolled `[worker]` table."""
    named = {}
    for name, config in entries:
        fingerprint = fingerprint_of(config)
        repos = named.get(fingerprint)
        named[fingerprint] = name if not repos else ','.join(sorted({*repos.split(','), name}))
    return named


def splice(argv, args_at, tail):
    argv = list(argv)
    if args_at is not None:
        argv[args_at:args_at + 1] = list(tail)
    return argv


def journey_check(config):
    """The journey job's `run` argv for the `[worker.canary]` journey id."""
    canary = config['canary']
    if not canary['journey']:
        return None
    job = config['jobs'][canary['journey_job']]
    env, _ = classify.environment(config, job)
    return {'id': canary['journey'], 'job': job['id'],
            'argv': splice(job['run']['argv'], job['run']['args_at'], [canary['journey']]),
            'env': env, 'cwd': config['worker']['workdir'], 'compose': canary['compose']}


def surface_check(config):
    """The surface job's cheapest real step for the `[worker.canary]` surface id.

    `validate` when the job declares one: it is the repository's own answer to
    "would you run this", it needs node and the installed workspace, and it
    costs milliseconds. Otherwise the shard `plan` with one shard, which is
    what the canary ran before it read configurations. Never the suite itself:
    a browser run does not fit the canary's budget.
    """
    canary = config['canary']
    if not canary['surface']:
        return None
    job = config['jobs'][canary['surface_job']]
    env, _ = classify.environment(config, job)
    if job['validate']:
        spec = job['validate']
        env = dict(env, **spec['env'])
        argv, step = splice(spec['argv'], spec['args_at'], [canary['surface']]), 'validate'
    elif job['shards'] and job['shards']['plan']:
        argv = [item.replace('{n}', '1').replace('{plan}', PLAN_PATH)
                for item in splice(job['shards']['plan'], job['shards']['plan_args_at'],
                                   [canary['surface']])]
        step = 'plan'
    else:
        return None
    return {'id': canary['surface'], 'job': job['id'], 'step': step, 'argv': argv,
            'env': env, 'cwd': config['worker']['workdir']}


def canary_targets(entries, *, engine_root, source=None):
    """One canary target per distinct fingerprint the enrolled configs name.

    `source` overrides every target's source; without it each target builds
    from the client's own cache of that repository on the worker,
    `<engine_root>/src/<repo>/latest`, which the worker-side canary checks for
    and explains when it is missing.
    """
    targets, seen = [], {}
    for name, config in entries:
        fingerprint = fingerprint_of(config)
        if fingerprint in seen:
            seen[fingerprint]['repos'].append(name)
            continue
        repo = config['repo']['name']
        notes = []
        journey, surface = journey_check(config), surface_check(config)
        if journey is None:
            notes.append('no [worker.canary] journey, so no journey check')
        if surface is None:
            notes.append('no [worker.canary] surface (or no surface step to run), '
                         'so no surface check')
        target = {'label': repo, 'repos': [name], 'fingerprint': fingerprint,
                  'toolchain': dict(config['worker']),
                  'source': source or transfer.cache_paths(engine_root, repo, '-')['latest'],
                  'journey': journey, 'surface': surface, 'notes': notes}
        seen[fingerprint] = target
        targets.append(target)
    return targets
