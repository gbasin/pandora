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
from . import provision as provisioner
from . import versions
from .remote import Remote


def notice(text):
    sys.stderr.write('pandora: ' + text + '\n')
    sys.stderr.flush()


def target(args):
    """(host, engine_root, control_dir) from the flags, then the configuration."""
    config = settings.load(args.config)
    host = args.host or config['worker']['host']
    state = Path(args.state or config['client']['state']).expanduser()
    state.mkdir(parents=True, exist_ok=True)
    return host, args.engine_root or config['worker']['engine_root'], state / 'ssh'


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


def cmd_provision(args):
    host, engine_root, control = target(args)
    manifest = manifest_of(args)
    remote = Remote(host, control_dir=control, engine_root=engine_root)
    try:
        root = remote.expand(manifest['worker']['root'])
        shipped = ship_toolchains(remote, root, args)
    finally:
        remote.close()
    canary = {'journey': shipped.get('journey'), 'surfaces': shipped.get('surfaces'),
              'source': args.source, 'quota_gib': args.quota_gib}
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
        argv = ['--root', root, '--engine-root', remote.root(), 'canary']
        for flag, value in (('journey', shipped.get('journey')),
                            ('surfaces', shipped.get('surfaces')),
                            ('source', args.source), ('hog', args.hog),
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


def cmd_gc(args):
    argv = ['gc'] + (['--dry-run'] if args.dry_run else [])
    if args.keep is not None:
        argv += ['--keep', str(args.keep)]
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
    node.add_argument('--journey', default=None, help='toolchain JSON for the journeys golden')
    node.add_argument('--surfaces', default=None, help='toolchain JSON for the surfaces golden')
    node.add_argument('--source', default=None, help='source tree on the worker for a cold build')
    node.add_argument('--quota-gib', type=int, default=1)
    node.add_argument('--no-canary', action='store_true')
    node.add_argument('--timeout', type=int, default=1800)
    node.set_defaults(func=cmd_provision)

    node = actions.add_parser('status', help='versions, drift, pool, goldens, ready state')
    node.add_argument('--versions', default=None)
    node.set_defaults(func=cmd_status)

    node = actions.add_parser('canary', help='the health gate')
    node.add_argument('--journey', default=None)
    node.add_argument('--surfaces', default=None)
    node.add_argument('--source', default=None)
    node.add_argument('--hog', default=None)
    node.add_argument('--quota-gib', type=int, default=None)
    node.add_argument('--mark', action='store_true', help='write the ready state from the verdict')
    node.add_argument('--timeout', type=int, default=900)
    node.add_argument('--versions', default=None)
    node.set_defaults(func=cmd_canary)

    node = actions.add_parser('gc', help='sweep leaked instances, volumes and old goldens')
    node.add_argument('--dry-run', action='store_true')
    node.add_argument('--keep', type=int, default=None)
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


def dispatch(args):
    try:
        return args.func(args)
    except PandoraError as error:
        notice('%s: %s' % (type(error).__name__, error))
        return INFRA
