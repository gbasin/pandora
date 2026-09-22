"""The worker's entry point: one process per SSH call, one detached one per run.

Every subcommand reads its argument as JSON on stdin or on the command line,
prints one JSON object on stdout, and exits 0 if it *answered* -- a refusal is a
successful answer with `ok: false`, because "the engine could not be reached" and
"the engine says no" are different facts and the client acts differently on them.

The driver runs here, on the worker, rather than over SSH from the Mac. An SSH
round trip to this host is ~90 ms and the watchdog samples the cgroup twice a
second, so a remote driver would spend more time in transport than in work and
would make a 0.06 s clone unmeasurable.

    submit    admit (or refuse) a request and start its supervisor
    status    one attempt's row
    logs      raw log bytes from an offset
    result    the finished result JSON
    cancel    ask a running attempt to stop
    ps        live attempts
    stats     scheduler picture plus outcome counts
    reconcile after a restart: adopt or fail, never fabricate
    retain    delete old attempt directories
    canary    the worker's own health gate
    supervise (internal) the detached per-run supervisor
"""
import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

if __package__ in (None, ''):                # invoked as a file by the bootstrap
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pandora.engine import admission, runner                      # noqa: E402
from pandora.engine.ledger import Ledger, row_to_dict             # noqa: E402
from pandora.engine.scheduler import Scheduler, gate              # noqa: E402

ENGINE_VERSION = 2


def emit(payload):
    sys.stdout.write(json.dumps(payload, sort_keys=True) + '\n')
    sys.stdout.flush()
    return 0


def open_ledger(root):
    paths = runner.Paths(root).ensure()
    return paths, Ledger(paths.ledger)


def cmd_submit(args):
    """Admit a request, or refuse it in a way the client can act on.

    Idempotent on `request_id`: a resubmission of a request the ledger already
    knows returns that attempt untouched, marked `duplicate`. That is what makes
    submission safe to retry across a connection that dropped after the worker
    had already read it.
    """
    request = json.loads(sys.stdin.read())
    paths, ledger = open_ledger(args.root)
    plan = request['plan']
    fanned = bool(plan.get('shards'))
    run_id = 'r' + uuid.uuid4().hex[:15]
    with gate(paths.root):
        row, created = ledger.claim(
            request['request_id'], run_id,
            repo=plan['repo'], job=plan['job'], input_id=request['input_id'],
            source_path=request['source_path'], argv=plan['argv'],
            env=plan['env'], cwd=plan['cwd'], outputs=plan['outputs'],
            size_class=plan['size'], role='parent' if fanned else 'single')
        if not created:
            return emit({'ok': True, 'duplicate': True, 'run_id': row['run_id'],
                         'state': row['state'], 'same_input_as': row['same_input_as'],
                         'engine': ENGINE_VERSION})
        run_id = row['run_id']
        (paths.attempt(run_id)).mkdir(parents=True, exist_ok=True)
        (paths.attempt(run_id) / 'toolchain.json').write_text(json.dumps(plan['worker']))
        (paths.attempt(run_id) / 'request.json').write_text(json.dumps(request, indent=1))
        paths.log(run_id).touch()
        if fanned:
            # A parent reserves nothing and occupies no lane: it runs no command
            # and holds no instance. Its children are admitted one at a time, by
            # the parent, as the box has room for them -- so a fan-out cannot
            # hold memory it is not using while another repository waits.
            (paths.attempt(run_id) / 'shards.json').write_text(json.dumps({
                'shards': plan['shards'], 'args': plan.get('args') or [],
                'want': request.get('want_shards'),
                'keep_going': bool(request.get('keep_going'))}, indent=1))
            ledger.update(run_id, state='admitted', reservation_mib=0, ceiling_mib=0)
            pid = runner.spawn(paths.root, run_id, python=args.python)
            ledger.update(run_id, supervisor_pid=pid)
            verdict = {'admitted': True, 'fanout': True, 'reservation_mib': 0,
                       'shards': {'default': plan['shards']['default'],
                                  'max': plan['shards']['max'],
                                  'tier': 2 if plan['shards']['plan'] else 1}}
            return emit({'ok': True, 'run_id': run_id, 'state': 'admitted',
                         'duplicate': False, 'same_input_as': row['same_input_as'],
                         'admission': verdict, 'supervisor_pid': pid,
                         'engine': ENGINE_VERSION})
        store = admission.Store(str(paths.peaks))
        scheduler = Scheduler(ledger, store, budget_mib=runner.budget_of(paths))
        verdict = scheduler.admit(run_id, plan['repo'], plan['job'], plan['size'])
        store.close()
        if not verdict['admitted']:
            # Refused before anything ran: the row is closed so it cannot be
            # mistaken for work in progress, and the client may go local.
            runner.write_result(paths, ledger, run_id, outcome='infra_failed',
                                layer='engine', exit_code=None, peak_mib=0,
                                durations={}, evidence={'admission': verdict}, receipt=None)
            return emit({'ok': False, 'code': 'admission-refused', 'run_id': run_id,
                         'admission': verdict, 'engine': ENGINE_VERSION})
        pid = runner.spawn(paths.root, run_id, python=args.python)
        ledger.update(run_id, supervisor_pid=pid)
    return emit({'ok': True, 'run_id': run_id, 'state': 'admitted', 'duplicate': False,
                 'same_input_as': row['same_input_as'], 'admission': verdict,
                 'supervisor_pid': pid, 'engine': ENGINE_VERSION})


def cmd_status(args):
    paths, ledger = open_ledger(args.root)
    row = ledger.get(args.run)
    if row is None:
        return emit({'ok': False, 'code': 'stale', 'run_id': args.run})
    item = row_to_dict(row)
    item['log_bytes'] = paths.log(args.run).stat().st_size if paths.log(args.run).exists() else 0
    item['ok'] = True
    return emit(item)


def cmd_logs(args):
    """Raw bytes from an offset. Not JSON: the client copies them through."""
    paths = runner.Paths(args.root)
    path = paths.log(args.run)
    if not path.exists():
        return 1
    with path.open('rb') as handle:
        handle.seek(args.offset)
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
    sys.stdout.buffer.flush()
    return 0


def cmd_result(args):
    paths, ledger = open_ledger(args.root)
    path = paths.result(args.run)
    if not path.is_file():
        row = ledger.get(args.run)
        return emit({'ok': False, 'code': 'not-finished' if row is not None else 'stale',
                     'run_id': args.run, 'state': row['state'] if row is not None else None})
    return emit({'ok': True, 'result': json.loads(path.read_text())})


def cmd_cancel(args):
    paths, ledger = open_ledger(args.root)
    row = ledger.get(args.run)
    if row is None:
        return emit({'ok': False, 'code': 'stale', 'run_id': args.run})
    if row['state'] == 'finished':
        return emit({'ok': True, 'already': row['outcome'], 'run_id': args.run})
    ledger.request_cancel(args.run)
    return emit({'ok': True, 'requested': True, 'run_id': args.run, 'state': row['state']})


def cmd_ps(args):
    paths, ledger = open_ledger(args.root)
    rows = ledger.live() if args.live else ledger.recent(limit=args.limit)
    return emit({'ok': True, 'runs': [row_to_dict(row) for row in rows]})


def cmd_stats(args):
    paths, ledger = open_ledger(args.root)
    store = admission.Store(str(paths.peaks))
    scheduler = Scheduler(ledger, store, budget_mib=runner.budget_of(paths))
    reservations = []
    for row in ledger.recent(limit=200):
        key = (row['repo'], row['job'])
        if key in [(item['repo'], item['job']) for item in reservations]:
            continue
        reserve, ceiling, size_class, samples = scheduler.reservation(*key, row['size_class'])
        reservations.append({'repo': key[0], 'job': key[1], 'reservation_mib': reserve,
                             'ceiling_mib': ceiling, 'size_class': size_class,
                             'samples': samples})
    answer = {'ok': True, 'scheduler': scheduler.snapshot(), 'outcomes': ledger.counts(),
              'reservations': reservations, 'engine': ENGINE_VERSION}
    store.close()
    ledger.close()
    return emit(answer)


def cmd_reconcile(args):
    return emit({'ok': True, **runner.reconcile(args.root)})


def cmd_retain(args):
    return emit({'ok': True, **runner.retain(args.root, keep_seconds=args.keep,
                                             keep_failed_seconds=args.keep_failed)})


def cmd_supervise(args):
    result = runner.supervise(args.root, args.run)
    return emit({'ok': True, 'run_id': args.run, 'outcome': result['outcome']})


def cmd_canary(args):
    """The checks that gate a worker image rebuild, as one JSON verdict.

    Two of them are the ones that matter and pull in opposite directions: a
    journey-sized run must cross its soft limit *without* being killed, and an
    over-ceiling run must be killed as `oom` with evidence. A watchdog that
    passes only one of those is worse than none.
    """
    from pandora.executor.incus import IncusDriver
    from pandora.executor.interface import Limits
    from pandora.executor.memtest import hog

    paths = runner.Paths(args.root).ensure()
    driver = IncusDriver(root=paths.root)
    started, checks = time.monotonic(), []

    def check(name, condition, detail=''):
        checks.append({'check': name, 'ok': bool(condition), 'detail': str(detail)[:400],
                       'at': round(time.monotonic() - started, 1)})
        return bool(condition)

    code, out, _ = driver.incus('project', 'list', '--format', 'csv', check=False)
    check('project %s exists' % driver.project, driver.project in out)
    source = args.source or str(paths.src)
    toolchain = runner.toolchain_of(json.loads(Path(args.toolchain).read_text()))
    golden = driver.prepare(toolchain, source=source if Path(source).is_dir() else None,
                            log=lambda text: None)
    check('golden %s ready' % golden.name, bool(golden.name),
          'reused' if golden.reused else 'built in %.1fs' % golden.built_seconds)

    limits = Limits(memory_mib=512, ceiling_mib=512, cpus_hint=1, wall_seconds=180)
    instance = driver.clone(golden, args.run, limits=limits)
    check('clone under 2s', instance.clone_seconds < 2.0, '%.2fs' % instance.clone_seconds)
    wrote = driver.harden(instance, limits)
    check('cgroup arrangement written',
          all(not str(value).startswith('ERR') for value in wrote.values()), json.dumps(wrote))
    result = driver.execute(instance, hog(args.hog), env={}, cwd='/work', limits=limits)
    check('over-ceiling run is killed as oom', result.outcome == 'oom',
          json.dumps({key: value for key, value in result.evidence.items()
                      if key not in ('samples',)})[:400])
    check('oom verdict inside 60s', result.seconds < 60, '%.1fs' % result.seconds)
    try:
        receipt = driver.destroy(instance)
        check('receipt clean', receipt.clean, json.dumps(receipt.__dict__, default=str)[:300])
    except Exception as error:                        # noqa: BLE001
        check('receipt clean', False, str(error))
    failures = [item for item in checks if not item['ok']]
    return emit({'ok': not failures, 'checks': checks, 'failures': len(failures),
                 'seconds': round(time.monotonic() - started, 1),
                 'oom_evidence': {key: value for key, value in result.evidence.items()
                                  if key != 'samples'}})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=os.environ.get('PANDORA_ENGINE_ROOT',
                                                         str(Path.home() / 'pandora-engine')))
    parser.add_argument('--python', default=sys.executable)
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('submit').set_defaults(func=cmd_submit)
    for name, function in (('status', cmd_status), ('result', cmd_result),
                           ('cancel', cmd_cancel), ('supervise', cmd_supervise)):
        node = sub.add_parser(name)
        node.add_argument('--run', required=True)
        node.set_defaults(func=function)
    logs = sub.add_parser('logs')
    logs.add_argument('--run', required=True)
    logs.add_argument('--offset', type=int, default=0)
    logs.set_defaults(func=cmd_logs)
    ps = sub.add_parser('ps')
    ps.add_argument('--live', action='store_true')
    ps.add_argument('--limit', type=int, default=25)
    ps.set_defaults(func=cmd_ps)
    sub.add_parser('stats').set_defaults(func=cmd_stats)
    sub.add_parser('reconcile').set_defaults(func=cmd_reconcile)
    retain = sub.add_parser('retain')
    retain.add_argument('--keep', type=int, default=86400)
    retain.add_argument('--keep-failed', type=int, default=86400)
    retain.set_defaults(func=cmd_retain)
    canary = sub.add_parser('canary')
    canary.add_argument('--run', default='canary')
    canary.add_argument('--toolchain', required=True)
    canary.add_argument('--source', default=None)
    canary.add_argument('--hog', default='file')
    canary.set_defaults(func=cmd_canary)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
