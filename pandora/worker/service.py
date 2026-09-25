"""The worker's own entry point, beside the engine's and shipped with it.

`pandora.engine.service` answers about *runs*. This answers about the *machine*:
what it is made of, whether it is fit to take work, what is baked into it, and
what can be thrown away. Same contract as the engine's: one JSON object on
stdout, exit 0 when it answered, `ok: false` when the answer is no.

    status     manifest, drift, pool, goldens, ready state
    capacity   may another run be admitted (the engine's disk hook)
    canary     the health gate
    gc         sweep leaked instances, volumes and old goldens
    goldens    what is baked in, with fingerprints, sizes and last use
    pins       resolve a toolchain's inputs to digests
    ready      write the ready state after a canary
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

if __package__ in (None, ''):                # invoked as a file by the bootstrap
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pandora.engine.runner import Paths                              # noqa: E402
from pandora.executor.incus import IncusDriver                       # noqa: E402
from pandora.worker import facts, gc, goldens, versions              # noqa: E402

STATES = ('unprovisioned', 'unproven', 'ready', 'failed')


def emit(payload):
    sys.stdout.write(json.dumps(payload, sort_keys=True) + '\n')
    sys.stdout.flush()
    return 0


def worker_dir(root):
    path = Path(root).expanduser() / 'worker'
    path.mkdir(parents=True, exist_ok=True)
    return path


def manifest_of(root, path=None):
    """The manifest this worker was provisioned with, or the defaults.

    Read from the worker rather than from the caller on purpose: `status` is
    supposed to answer "is this machine what it was made to be", and taking the
    declaration from the person asking would make it answer "does this machine
    match what you are holding" instead.
    """
    target = Path(path) if path else worker_dir(root) / 'versions.toml'
    if target.is_file():
        return versions.load(target), str(target)
    return versions.load(None), None


def state_path(root):
    return worker_dir(root) / 'state.json'


def read_state(root):
    try:
        return json.loads(state_path(root).read_text())
    except (OSError, ValueError):
        return {'state': 'unprovisioned', 'at': 0, 'reason': 'no state file'}


def write_state(root, **fields):
    state = read_state(root)
    state.update(fields)
    state['at'] = time.time()
    state['at_iso'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    state_path(root).write_text(json.dumps(state, indent=1, sort_keys=True) + '\n')
    return state


def driver_for(manifest, engine_root):
    worker = manifest['worker']
    return IncusDriver(project=worker['project'], pool=worker['pool'],
                       profile=worker['profile'], root=Path(engine_root).expanduser())


def engine_root_of(manifest, override=None):
    return str(Path(override or manifest['worker']['engine_root']).expanduser())


def cmd_status(args):
    manifest, source = manifest_of(args.root, args.versions)
    engine_root = engine_root_of(manifest, args.engine_root)
    driver = driver_for(manifest, engine_root)
    observed = facts.survey(manifest)
    items = versions.drift(manifest, observed)
    state = read_state(args.root)
    pool = driver.pool_usage()
    listed = goldens.index(Paths(engine_root), driver)
    running = [item for item in driver.instances() if item['name'].startswith('run-')]
    # A worker whose manifest and machine disagree is not `ready`, whatever its
    # last canary said: the canary was run against a different machine.
    # The kernel is not a package the manifest pins, and a reboot can land on a
    # different one without anything in the declaration changing. It is still a
    # different machine than the one the canary passed on, so it is drift.
    kernel = observed['host']['kernel']
    if state.get('kernel') and state['kernel'] != kernel:
        items.append({'kind': 'host', 'name': 'kernel', 'want': state['kernel'],
                      'have': kernel, 'detail': 'the canary passed on a different kernel'})
    reported = state.get('state', 'unprovisioned')
    if items and reported == 'ready':
        reported = 'drifted'
    return emit({'ok': not items and reported == 'ready',
                 'state': reported, 'ready_since': state.get('at_iso'),
                 'manifest': manifest, 'manifest_source': source,
                 'manifest_digest': versions.digest(manifest),
                 'provisioned_digest': state.get('manifest_digest'),
                 'drift': items, 'installed': observed,
                 'pool': pool, 'capacity': driver.capacity(
                     floor_gib=manifest['worker']['disk_floor_gib']),
                 'goldens': listed, 'run_instances': running,
                 'canary': state.get('canary'), 'reason': state.get('reason')})


def cmd_capacity(args):
    manifest, _ = manifest_of(args.root, args.versions)
    driver = driver_for(manifest, engine_root_of(manifest, args.engine_root))
    floor = args.floor if args.floor is not None else manifest['worker']['disk_floor_gib']
    return emit(driver.capacity(floor_gib=floor))


def cmd_goldens(args):
    manifest, _ = manifest_of(args.root, args.versions)
    engine_root = engine_root_of(manifest, args.engine_root)
    driver = driver_for(manifest, engine_root)
    return emit(goldens.listing(engine_root, driver))


def cmd_gc(args):
    manifest, _ = manifest_of(args.root, args.versions)
    engine_root = engine_root_of(manifest, args.engine_root)
    driver = driver_for(manifest, engine_root)
    keep = args.keep if args.keep is not None else manifest['worker']['golden_keep']
    # `--family` present, or `--families-known` with none, is enrollment data;
    # a bare `gc` on the worker has neither, and None is the honest value --
    # the sweep must not read "nobody could say" as "nothing is enrolled".
    enrolled = (gc.parse_families(args.family)
                if args.family or args.families_known else None)
    receipt = gc.sweep(engine_root, driver, keep=keep, dry_run=args.dry_run,
                       protect=gc.parse_protect(args.protect),
                       enrolled=enrolled,
                       drop=args.drop_family,
                       orphan_grace=args.orphan_hours * 3600)
    if not args.dry_run:
        gc.write_receipt(worker_dir(args.root), receipt)
    return emit(receipt)


def cmd_canary(args):
    from pandora.worker import canary
    manifest, _ = manifest_of(args.root, args.versions)
    engine_root = engine_root_of(manifest, args.engine_root)
    driver = driver_for(manifest, engine_root)
    # `--plan` is the client's derivation from the enrolled configurations;
    # `--journey` / `--surfaces` are the explicit toolchain files it overrides.
    targets = json.loads(Path(args.plan).read_text())['targets'] if args.plan else None
    verdict = canary.run(engine_root, journey=args.journey, surfaces=args.surfaces,
                         targets=targets,
                         source=args.source, hog_kind=args.hog, keep=args.keep,
                         floor_gib=manifest['worker']['disk_floor_gib'],
                         quota_gib=args.quota_gib, driver=driver,
                         journey_argv=json.loads(args.journey_argv) if args.journey_argv else None,
                         surfaces_argv=(json.loads(args.surfaces_argv)
                                        if args.surfaces_argv else None))
    if args.mark:
        write_state(args.root, state='ready' if verdict['ok'] else 'failed',
                    manifest_digest=versions.digest(manifest),
                    kernel=facts.sh('uname -r'),
                    canary={'ok': verdict['ok'], 'failures': verdict['failures'],
                            'seconds': verdict['seconds'],
                            'checks': [row['check'] for row in verdict['checks']]},
                    reason=verdict['reason'])
    return emit(verdict)


def cmd_pins(args):
    from pandora.worker import pins as pinner
    spec = json.loads(Path(args.toolchain).read_text())
    resolved, problems = pinner.resolve(spec, source=args.source, strict=False)
    spec['pins'] = resolved
    from pandora.engine.runner import toolchain_of
    before = dict(spec)
    before.pop('pins')
    return emit({'ok': not problems, 'pins': resolved, 'problems': problems,
                 'fingerprint_unpinned': toolchain_of(before).fingerprint(),
                 'fingerprint_pinned': toolchain_of(spec).fingerprint(),
                 'toolchain': spec})


def cmd_ready(args):
    return emit({'ok': True, 'state': write_state(args.root, state=args.set,
                                                  reason=args.reason)})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=os.environ.get('PANDORA_WORKER_ROOT',
                                                         str(Path.home() / 'pandora')))
    parser.add_argument('--engine-root', default=None)
    parser.add_argument('--versions', default=None)
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('status').set_defaults(func=cmd_status)
    capacity = sub.add_parser('capacity')
    capacity.add_argument('--floor', type=int, default=None)
    capacity.set_defaults(func=cmd_capacity)
    sub.add_parser('goldens').set_defaults(func=cmd_goldens)
    sweep = sub.add_parser('gc')
    sweep.add_argument('--dry-run', action='store_true')
    sweep.add_argument('--keep', type=int, default=None)
    sweep.add_argument('--protect', action='append', default=[],
                       metavar='FINGERPRINT[=REPO]')
    sweep.add_argument('--family', action='append', default=None,
                       metavar='REPO=SOURCE_ID')
    sweep.add_argument('--families-known', action='store_true',
                       help='the caller read its enrolled configurations; '
                            'with no --family that means nothing is enrolled, '
                            'not that nobody looked')
    sweep.add_argument('--drop-family', action='append', default=[], metavar='FAMILY')
    sweep.add_argument('--orphan-hours', type=float, default=24.0)
    sweep.set_defaults(func=cmd_gc)
    gate = sub.add_parser('canary')
    gate.add_argument('--journey', default=None)
    gate.add_argument('--surfaces', default=None)
    gate.add_argument('--plan', default=None, help='targets JSON derived by the client')
    gate.add_argument('--source', default=None)
    gate.add_argument('--hog', default='file')
    gate.add_argument('--quota-gib', type=int, default=1)
    gate.add_argument('--journey-argv', default=None)
    gate.add_argument('--surfaces-argv', default=None)
    gate.add_argument('--keep', action='store_true')
    gate.add_argument('--mark', action='store_true')
    gate.set_defaults(func=cmd_canary)
    pin = sub.add_parser('pins')
    pin.add_argument('--toolchain', required=True)
    pin.add_argument('--source', default=None)
    pin.set_defaults(func=cmd_pins)
    ready = sub.add_parser('ready')
    ready.add_argument('--set', choices=STATES, required=True)
    ready.add_argument('--reason', default='')
    ready.set_defaults(func=cmd_ready)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
