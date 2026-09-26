"""`pandora worker ...`: everything about the machine rather than the run.

Kept out of `pandora/cli.py` on purpose. The run-facing commands take their
worker from the client's configuration and go through the daemon's `Worker`;
these take a host on the command line, because the first thing `provision` does
to a machine is make it the kind of machine that configuration could describe.
"""
import json
import sys
from pathlib import Path

from ..client import settings
from ..errors import PandoraError
from ..exits import INFRA
from . import enrolled
from . import gc as gc_protect
from . import provision as provisioner
from . import versions
from .remote import Remote


def notice(text):
    sys.stderr.write('pandora: ' + text + '\n')
    sys.stderr.flush()


# The operator CLI's control-master anchor. Not the daemon's `ssh`: every verb
# here ends with `remote.close()`, and on a shared master that `-O exit` killed
# the daemon's in-flight rsyncs (2026-09-24, four runs refused `transfer-failed`).
CONTROL = 'ssh-cli'


def target(args):
    """(host, engine_root, control_dir) from the flags, then the configuration."""
    config = settings.load(args.config)
    host = args.host or config['worker']['host']
    state = Path(args.state or config['client']['state']).expanduser()
    state.mkdir(parents=True, exist_ok=True)
    return host, args.engine_root or config['worker']['engine_root'], state / CONTROL


def manifest_of(args):
    manifest = versions.load(args.versions)
    worker = manifest['worker']
    if getattr(args, 'device', None):
        worker['device'] = args.device
    if getattr(args, 'loop_file', None):
        worker['device'] = ''
        worker['loop_size_gib'] = int(str(args.loop_file).rstrip('Gg'))
    if getattr(args, 'root', None):
        worker['root'] = args.root
    return manifest


def ship_toolchains(remote, root, args):
    """A toolchain path that exists here is shipped; one that does not is the
    worker's own path. Both are useful: a rebuild has the JSON in the checkout,
    and a re-canary of a live worker names what is already on it."""
    out = {}
    for name in ('journey', 'surfaces'):
        value = getattr(args, name, None)
        if not value:
            continue
        local = Path(value).expanduser()
        if local.is_file():
            out[name] = remote.put('%s/worker/toolchains/%s.json' % (root, name),
                                   local.read_text())
        else:
            out[name] = value
    return out


def ship_plan(remote, root, args):
    """Derive the canary's targets from the enrolled configs and ship them.

    Only when no explicit toolchain file was named: `--journey` / `--surfaces`
    are the override for a worker no repository is enrolled against yet, and
    mixing the two would prove a toolchain nobody runs beside one somebody
    does. Returns the plan's path on the worker, or None.
    """
    if getattr(args, 'journey', None) or getattr(args, 'surfaces', None):
        return None
    entries = enrolled.configs(settings.load(args.config))
    targets = enrolled.canary_targets(entries, engine_root=remote.root(),
                                      source=getattr(args, 'source', None))
    for item in targets:
        notice('canary target %s: golden-%s from %s; journey %s, surface %s'
               % ('+'.join(item['repos']), item['fingerprint'], item['source'],
                  (item['journey'] or {}).get('id', '-'), (item['surface'] or {}).get('id', '-')))
    if not targets:
        notice('no enrolled repository names a [worker] table; pass --journey or --surfaces '
               'to prove a toolchain before the first enrollment')
    return remote.put('%s/worker/toolchains/canary-plan.json' % root,
                      json.dumps({'targets': targets}, indent=1, sort_keys=True) + '\n')


def cmd_provision(args):
    host, engine_root, control = target(args)
    manifest = manifest_of(args)
    remote = Remote(host, control_dir=control, engine_root=engine_root)
    try:
        root = remote.expand(manifest['worker']['root'])
        shipped = ship_toolchains(remote, root, args)
        plan = None if args.no_canary else ship_plan(remote, root, args)
    finally:
        remote.close()
    # A derived plan carries its own sources; `--source` was folded into it.
    canary = {'journey': shipped.get('journey'), 'surfaces': shipped.get('surfaces'),
              'plan': plan, 'source': None if plan else args.source,
              'quota_gib': args.quota_gib}
    report = provisioner.run(host, manifest=manifest, control_dir=control,
                             root=manifest['worker']['root'], engine_root=engine_root,
                             canary=canary, skip_canary=args.no_canary,
                             timeout=args.timeout, notice=notice)
    if args.json:
        print(json.dumps(report, indent=1, sort_keys=True))
    else:
        print(provisioner.render(report))
    return 0 if report.get('ok') else 1


def remote_call(args, argv, *, timeout=300):
    host, engine_root, control = target(args)
    remote = Remote(host, control_dir=control, engine_root=engine_root)
    try:
        root = remote.expand(args.root or versions.WORKER['root'])
        return remote.worker(['--root', root, '--engine-root', remote.root()] + argv,
                             timeout=timeout)
    finally:
        remote.close()


def cmd_status(args):
    answer = remote_call(args, ['status'])
    if args.json:
        print(json.dumps(answer, indent=1, sort_keys=True))
        return 0 if answer.get('ok') else 1
    print(render_status(answer))
    return 0 if answer.get('ok') else 1


def render_status(answer):
    host = (answer.get('installed') or {}).get('host') or {}
    pool = answer.get('pool') or {}
    lines = ['state: %s%s' % (answer.get('state'),
                              '  since ' + answer['ready_since'] if answer.get('ready_since') else ''),
             'host: %s, kernel %s, %s cores, %s MiB, %s'
             % (host.get('hostname'), host.get('kernel'), host.get('cores'),
                host.get('memory_mib'), host.get('incus')),
             'manifest: %s%s' % (answer.get('manifest_digest'),
                                 '' if answer.get('provisioned_digest') in
                                 (None, answer.get('manifest_digest'))
                                 else ' (provisioned as %s)' % answer['provisioned_digest'])]
    for name, version in sorted(((answer.get('installed') or {}).get('packages') or {}).items()):
        lines.append('  %-16s %s' % (name, version or '(absent)'))
    if pool.get('ok'):
        lines.append('pool %s: %.2f GiB free of %.2f, %.1f%% used'
                     % (pool['pool'], pool['free_gib'], pool['total_bytes'] / (1 << 30),
                        100 * pool['used_fraction']))
    capacity = answer.get('capacity') or {}
    lines.append('admission: %s (floor %s GiB)%s'
                 % ('open' if capacity.get('ok') else 'CLOSED', capacity.get('floor_gib'),
                    '' if capacity.get('ok') else ' -- ' + str(capacity.get('reason'))))
    cap = answer.get('run_cap') or {}
    if cap:
        lines.append('run cap: %s concurrent (%s)' % (cap.get('max_running'), cap.get('source')))
    lines.append('goldens:')
    for item in answer.get('goldens') or []:
        lines.append('  %-26s %-10s %6.2f GiB  %-7s %s'
                     % (item['name'], item['repo'] or '(unknown)',
                        item['referenced_bytes'] / (1 << 30),
                        'pinned' if item['pinned'] else 'unpinned',
                        last_use(item)))
    for item in answer.get('drift') or []:
        lines.append('drift: %s %s wanted %s, has %s (%s)'
                     % (item['kind'], item['name'], item['want'], item['have'], item['detail']))
    if answer.get('run_instances'):
        lines.append('run instances: ' + ', '.join(item['name'] for item in answer['run_instances']))
    canary = answer.get('canary') or {}
    if canary:
        lines.append('last canary: %s, %d failure(s) in %.1fs'
                     % ('pass' if canary.get('ok') else 'FAIL', canary.get('failures', 0),
                        canary.get('seconds', 0)))
    if answer.get('reason'):
        lines.append('reason: ' + str(answer['reason']))
    return '\n'.join(lines)


def last_use(item):
    import time
    if not item.get('last_used'):
        return 'never used by a recorded attempt'
    return 'last used %s by %s' % (time.strftime('%Y-%m-%d %H:%M',
                                                 time.gmtime(item['last_used'])),
                                   item.get('repo') or '?')


def cmd_canary(args):
    host, engine_root, control = target(args)
    remote = Remote(host, control_dir=control, engine_root=engine_root)
    try:
        root = remote.expand(args.root or versions.WORKER['root'])
        shipped = ship_toolchains(remote, root, args)
        plan = ship_plan(remote, root, args)
        argv = ['--root', root, '--engine-root', remote.root(), 'canary']
        for flag, value in (('journey', shipped.get('journey')),
                            ('surfaces', shipped.get('surfaces')),
                            ('plan', plan), ('source', None if plan else args.source),
                            ('hog', args.hog),
                            ('quota-gib', args.quota_gib)):
            if value is not None:
                argv += ['--' + flag, str(value)]
        if args.mark:
            argv.append('--mark')
        answer = remote.worker(argv, timeout=args.timeout)
    finally:
        remote.close()
    if args.json:
        print(json.dumps(answer, indent=1, sort_keys=True))
    else:
        for row in answer.get('checks') or []:
            print('%-4s %-46s %6.1fs %s' % ('ok' if row['ok'] else 'FAIL', row['check'],
                                            row['at'], row['detail'][:70]))
        print('\n%s: %d failure(s) in %.1fs'
              % ('pass' if answer.get('ok') else 'FAIL', answer.get('failures', 0),
                 answer.get('seconds', 0)))
        if answer.get('reason'):
            print('reason: ' + answer['reason'])
    return 0 if answer.get('ok') else 1


def protected(entries, args):
    """{fingerprint: repos} the sweep may never remove: every golden an enrolled
    repository's `[worker]` table names, plus any `--protect` given by hand."""
    named = enrolled.named_fingerprints(entries)
    for fingerprint, repo in gc_protect.parse_protect(args.protect).items():
        named.setdefault(fingerprint, repo or 'the command line')
    return named


def cmd_gc(args):
    config = settings.load(args.config)
    entries = enrolled.configs(config)
    argv = ['gc'] + (['--dry-run'] if args.dry_run else [])
    if args.keep is not None:
        argv += ['--keep', str(args.keep)]
    for fingerprint, repo in sorted(protected(entries, args).items()):
        argv += ['--protect', '%s=%s' % (fingerprint, repo)]
    # The enrollment lists are an answer only when a config file was actually
    # read: `load` of an absent file yields zero repos, and shipping
    # --families-known for it would turn "no file" into the authoritative
    # "nothing is enrolled".
    if config['source'] is not None:
        for repo, source_id in sorted(enrolled.named_families(entries)):
            argv += ['--family', '%s=%s' % (repo, source_id)]
        # One worker serves many Macs; --repos is what scopes the orphan rule
        # to the repositories this caller's own enrollment covers.
        for repo in sorted({item['repo']['name'] for _, item in entries}):
            argv += ['--repos', repo]
        argv.append('--families-known')
    for family in getattr(args, 'drop_family', None) or []:
        argv += ['--drop-family', family]
    if getattr(args, 'orphan_hours', None) is not None:
        argv += ['--orphan-hours', str(args.orphan_hours)]
    answer = remote_call(args, argv, timeout=900)
    if args.json:
        print(json.dumps(answer, indent=1, sort_keys=True))
    else:
        for item in answer.get('removed') or []:
            print('%-9s %-14s %-26s %s' % ('would' if answer['dry_run'] else 'removed',
                                           item['kind'], item['name'], item['why']))
        for item in answer.get('kept') or []:
            print('%-9s %-14s %-26s %s' % ('kept', item['kind'], item['name'], item['why']))
        for item in answer.get('failed') or []:
            print('%-9s %-14s %-26s %s' % ('FAILED', item['kind'], item['name'],
                                           item.get('error', item['why'])))
        pool = answer.get('pool') or {}
        print('\n%d removed, %d kept, %d failed; pool %.2f GiB free%s'
              % (len(answer.get('removed') or []), len(answer.get('kept') or []),
                 len(answer.get('failed') or []), pool.get('free_gib', 0),
                 '; receipt ' + answer['receipt'] if answer.get('receipt') else ''))
    return 0 if answer.get('ok') else 1


def cmd_goldens(args):
    answer = remote_call(args, ['goldens'])
    if args.json:
        print(json.dumps(answer, indent=1, sort_keys=True))
        return 0
    print('%-26s %-10s %10s %10s %-8s %s'
          % ('golden', 'repo', 'referenced', 'exclusive', 'pinned', 'last use'))
    for item in answer.get('goldens') or []:
        print('%-26s %-10s %9.2fG %9.2fG %-8s %s'
              % (item['name'], item['repo'] or '(unknown)',
                 item['referenced_bytes'] / (1 << 30), item['exclusive_bytes'] / (1 << 30),
                 'yes' if item['pinned'] else 'no', last_use(item)))
        for key, value in sorted((item.get('pins') or {}).items()):
            print('    %-22s %s' % (key, value))
    return 0


def cmd_engine(args):
    """`reconcile`, `retain` and `stats` are the engine's, not the machine's.

    They live under `pandora worker` because that is where a person looks for
    them, and they go straight to the engine over the same link rather than
    through the client daemon, so they work on a worker no client has adopted.
    """
    host, engine_root, control = target(args)
    remote = Remote(host, control_dir=control, engine_root=engine_root)
    try:
        answer = remote.engine([args.action], timeout=args.timeout)
    finally:
        remote.close()
    print(json.dumps(answer, indent=1, sort_keys=True))
    return 0 if answer.get('ok') else 1


def cmd_pins(args):
    host, engine_root, control = target(args)
    remote = Remote(host, control_dir=control, engine_root=engine_root)
    try:
        root = remote.expand(args.root or versions.WORKER['root'])
        # Resolution happens on the worker -- it is the machine with the image
        # server, the registry route and the shipped source -- so a toolchain
        # named by a local path has to travel there first.
        local = Path(args.toolchain).expanduser()
        path = (remote.put('%s/worker/toolchains/pins.json' % root, local.read_text())
                if local.is_file() else args.toolchain)
        answer = remote.worker(['--root', root, '--engine-root', remote.root(),
                                'pins', '--toolchain', path]
                               + (['--source', args.source] if args.source else []),
                               timeout=300)
    finally:
        remote.close()
    print(json.dumps(answer, indent=1, sort_keys=True))
    return 0 if answer.get('ok') else 1


def add_parser(sub):
    """Hang `pandora worker <action>` off the top-level parser."""
    worker = sub.add_parser('worker', help='provision, prove and keep a worker')
    worker.add_argument('--host', default=None, help='ubuntu@1.2.3.4; default: [worker] host')
    worker.add_argument('--engine-root', default=None)
    worker.add_argument('--root', default=None, help='worker layout root (default ~/pandora)')
    worker.add_argument('--json', action='store_true')
    actions = worker.add_subparsers(dest='action', required=True)

    node = actions.add_parser('provision', help='make this host a worker, idempotently')
    node.add_argument('--versions', default=None, help='a versions.toml')
    node.add_argument('--device', default=None, help='/dev/sdb: a real block device for the pool')
    node.add_argument('--loop-file', default=None, help='18G: size of the loop-file pool instead')
    node.add_argument('--journey', default=None,
                      help='override: toolchain JSON to prove with a journey, for a worker '
                           'no repository is enrolled against yet')
    node.add_argument('--surfaces', default=None,
                      help='override: toolchain JSON to prove with a surface listing')
    node.add_argument('--source', default=None,
                      help='source tree on the worker for a cold golden build '
                           '(default: <engine_root>/src/<repo>/latest)')
    node.add_argument('--quota-gib', type=int, default=1)
    node.add_argument('--no-canary', action='store_true')
    node.add_argument('--timeout', type=int, default=1800)
    node.set_defaults(func=cmd_provision)

    node = actions.add_parser('status', help='versions, drift, pool, goldens, ready state')
    node.add_argument('--versions', default=None)
    node.set_defaults(func=cmd_status)

    node = actions.add_parser(
        'canary', help='the health gate',
        description='Prove the worker. With no --journey/--surfaces, read every enrolled '
                    "repository's pandora.toml and prove each distinct [worker] golden: build "
                    'or reuse it, run the [worker.canary] journey through the journey job, run '
                    "the surface job's validate (or one-shard plan) for the [worker.canary] "
                    'surface, then the quota and memory-watchdog checks.')
    node.add_argument('--journey', default=None,
                      help='override: a toolchain JSON (local path is shipped) proved with '
                           'journey S0-01; for a worker before any enrollment')
    node.add_argument('--surfaces', default=None,
                      help='override: a toolchain JSON proved with a surface-runner plan')
    node.add_argument('--source', default=None,
                      help='source tree on the worker for a cold golden build '
                           '(default: <engine_root>/src/<repo>/latest, written by the first '
                           'routed run)')
    node.add_argument('--hog', default=None)
    node.add_argument('--quota-gib', type=int, default=None)
    node.add_argument('--mark', action='store_true', help='write the ready state from the verdict')
    node.add_argument('--timeout', type=int, default=900)
    node.add_argument('--versions', default=None)
    node.set_defaults(func=cmd_canary)

    node = actions.add_parser(
        'gc', help='sweep leaked instances, volumes and old goldens',
        description='Remove leaked run instances, leaked volumes, and goldens past the keep '
                    'count. Goldens are ranked by last use inside toolchain families -- one '
                    "family per (repository, [worker] source_id) -- never across them. A golden "
                    "whose fingerprint an enrolled repository's pandora.toml names, one a live "
                    'attempt uses, and a pinned one are never removed. With a config file this '
                    'command ships the families and repos its enrollments read, so a family none '
                    'of them names -- in a repo the enrollment covers -- is collected once it is '
                    'past its grace. Repos the caller never enrolled, and a bare `gc` on the '
                    'worker itself, keep `keep` per family and collect no orphans.')
    node.add_argument('--dry-run', action='store_true')
    node.add_argument('--keep', type=int, default=None, metavar='N',
                      help='keep the N most recently used goldens per toolchain '
                           'family, on top of every protected one (default: golden_keep in '
                           'the manifest, 2)')
    node.add_argument('--protect', action='append', default=[], metavar='FINGERPRINT',
                      help='never remove this golden, in addition to the fingerprints the '
                           'enrolled pandora.toml files name; repeatable')
    node.add_argument('--drop-family', action='append', default=[], metavar='FAMILY',
                      help='remove this family on sight -- the label the receipt prints, '
                           'like "acme acme-journeys" or "(unknown)"; a golden a live '
                           'attempt needs and a pinned one still hold; repeatable')
    node.add_argument('--orphan-hours', type=float, default=None, metavar='H',
                      help='grace after last use before a family no enrolled config names '
                           'is collected (default: 24)')
    node.add_argument('--versions', default=None)
    node.set_defaults(func=cmd_gc)

    node = actions.add_parser('goldens', help='what is baked in, and what it cost')
    node.add_argument('--versions', default=None)
    node.set_defaults(func=cmd_goldens)

    node = actions.add_parser('pins', help='resolve a toolchain to the digests it names')
    node.add_argument('--toolchain', required=True)
    node.add_argument('--source', default=None)
    node.add_argument('--versions', default=None)
    node.set_defaults(func=cmd_pins)

    for name, help_text in (('reconcile', 'adopt or fail runs after an engine restart'),
                            ('retain', 'delete old attempt directories'),
                            ('stats', 'the scheduler picture and outcome counts')):
        node = actions.add_parser(name, help=help_text)
        node.add_argument('--timeout', type=int, default=300)
        node.add_argument('--versions', default=None)
        node.set_defaults(func=cmd_engine)
    return worker


def cmd_cache(args):
    """`pandora cache stats|clear`: the worker's turbo remote cache."""
    host, engine_root, control = target(args)
    remote = Remote(host, control_dir=control, engine_root=engine_root)
    try:
        argv = ['cache-' + args.action] + (['--repo', args.repo]
                                           if getattr(args, 'repo', None) else [])
        answer = remote.engine(argv, timeout=120)
    finally:
        remote.close()
    if args.json or not answer.get('ok'):
        print(json.dumps(answer, indent=1, sort_keys=True))
    elif args.action == 'clear':
        print('cleared %s: %d entries, %.1f MiB' % (args.repo or 'every repository',
                                                     answer['removed'],
                                                     answer['bytes'] / 1048576))
    else:
        print(render_cache(answer))
    return 0 if answer.get('ok') else 1


def render_cache(answer):
    endpoint = answer.get('endpoint') or {}
    server = answer.get('server')
    lines = ['turbo cache  %.1f of %.0f MiB, %d entries; server %s' % (
        answer['bytes'] / 1048576, answer['max_bytes'] / 1048576, answer['entries'],
        ('%s:%s' % (endpoint.get('host'), endpoint.get('port'))) if server
        else 'NOT ANSWERING')]
    for name, item in sorted(answer['namespaces'].items()):
        lines.append('  %-24s %6d entries %9.1f MiB' % (name, item['entries'],
                                                         item['bytes'] / 1048576))
    if server:
        counters = server.get('counters') or {}
        gets = counters.get('hits', 0) + counters.get('misses', 0)
        lines.append('  since start: %d hits, %d misses (%s), %d puts, %d evictions' % (
            counters.get('hits', 0), counters.get('misses', 0),
            ('%.0f%% hit' % (100.0 * counters.get('hits', 0) / gets)) if gets else 'no reads',
            counters.get('puts', 0), counters.get('evictions', 0)))
    return '\n'.join(lines)


def add_cache_parser(sub):
    """Hang `pandora cache <action>` off the top-level parser."""
    cache = sub.add_parser('cache', help="the worker's turbo remote cache")
    cache.add_argument('--host', default=None)
    cache.add_argument('--engine-root', default=None)
    cache.add_argument('--json', action='store_true')
    actions = cache.add_subparsers(dest='action', required=True)
    actions.add_parser('stats', help='bytes, entries, hits and misses').set_defaults(func=cmd_cache)
    node = actions.add_parser('clear', help='empty the cache, or one repository of it')
    node.add_argument('--repo', default=None)
    node.set_defaults(func=cmd_cache)
    return cache


def dispatch(args):
    try:
        return args.func(args)
    except PandoraError as error:
        notice('%s: %s' % (type(error).__name__, error))
        return INFRA
