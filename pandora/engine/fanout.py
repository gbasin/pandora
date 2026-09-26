"""One request, many instances: the parent attempt that owns a fan-out.

A parent runs nothing itself. It holds no memory reservation, occupies no CPU
lane, and exists so that a caller who typed one command follows one run id. What
it does own is the order, and the order is the whole design:

    plan (tier 2)      one instance, one build, one inventory
      collect          the inventory and the build-once outputs, onto the host
      decide N         config default, PANDORA_SHARDS, the worker's free lanes
    dispatch           N children, each admitted on its own, each its own clone,
                       each grafted with what the plan built
      drain            a failure stops *new* dispatch; what is running finishes
    aggregate          every report, checked against the planned partition
      merge            artifacts into one tree, collisions kept rather than
                       silently overwritten
    verdict            a parent passes only if every shard passed, every report
                       arrived, the observed ids were exactly the plan, and no
                       two shards disagreed about a file

Three rules this file exists to hold.

**A silent shard is not a passing shard.** The aggregator requires a report from
every dispatched child. A child that produced no report is a missing report, not
zero failures, and the parent cannot pass with one outstanding.

**The partition is checked, not assumed.** Tier 2 holds the plan's inventory
beside what the shards say they ran and refuses anything that is not exactly it
-- no gaps, no overlaps, no strangers. Tier 1 has no plan, so there is nothing
to check and the result says `unverified` rather than pretending.

**Two shards cannot quietly overwrite each other's evidence.** Identical bytes at
one path are fine. Different bytes are a collision: both are kept, and the run
exits 75 rather than handing back a merged tree that is missing half of what was
produced.
"""
import hashlib
import json
import shutil
import time
import uuid
from pathlib import Path

from . import admission, batches, history, retry, runner, writeback
from . import shards as sharding
from .ledger import Ledger, row_to_dict
from .scheduler import Scheduler, gate

POLL = 1.0
# How long a child may sit un-admitted before the fan-out gives up on it. A
# sibling has to finish first, and a surface shard is minutes, not hours.
ADMIT_SECONDS = 1800
COLLISION_EXIT = 75
# A child waiting for a lane says so once, then at most this often. The caller
# is usually an agent reading stderr into its context, where a line a second
# is not reassurance but cost.
STILL_EVERY = 60.0
# How long a finished row may go without its result file before the fan-out
# stops waiting for it and calls the child an engine failure.
RESULT_GRACE = 10.0


def supervise_parent(root, run_id, *, driver=None):
    """Run one fan-out to a receipt. Returns the parent's result dictionary."""
    paths = runner.Paths(root).ensure()
    ledger = Ledger(paths.ledger)
    row = ledger.get(run_id)
    if row is None:
        raise SystemExit('no attempt %s' % run_id)
    if row['state'] == 'finished':
        return json.loads(paths.result(run_id).read_text())

    attempt = paths.attempt(run_id)
    attempt.mkdir(parents=True, exist_ok=True)
    plan = row_to_dict(row)
    control = json.loads((attempt / 'shards.json').read_text())
    config, log = control['shards'], paths.log(run_id).open('a', buffering=1)
    durations, evidence = {}, {}
    started = time.monotonic()

    def note(text):
        log.write('pandora: ' + text + '\n')

    ledger.update(run_id, state='running')
    tails = {}
    children = None
    planned, plan_result, reports = None, None, {}
    outcome, layer, exit_code = 'infra_failed', 'engine', None
    try:
        want = control.get('want') or config['default']
        free = free_lanes(paths, ledger, plan)
        total, why = sharding.count(config, want=want, free_slots=free)
        note('%d shard%s (asked %d, max %d, free lanes %d%s)'
             % (total, '' if total == 1 else 's', want, config['max'], free,
                '; ' + ', '.join(why) if why else ''))
        evidence['shard_count'] = {'total': total, 'asked': want, 'free_lanes': free,
                                   'clamps': why}

        dispatch = list(range(1, total + 1))
        if config['plan']:
            mark = time.monotonic()
            plan_result = run_plan(paths, ledger, plan, config, run_id, total,
                                   control.get('args') or [],
                                   note=note, tails=tails, log=log)
            durations['plan'] = round(time.monotonic() - mark, 2)
            evidence['plan'] = {'run_id': plan_result['run_id'],
                                'outcome': plan_result['outcome'],
                                'seconds': plan_result.get('wall_seconds'),
                                'peak_mib': plan_result.get('peak_mib')}
            if plan_result['outcome'] != 'passed':
                note('the plan step did not finish; no shard was dispatched')
                outcome = ('command_failed' if plan_result['outcome'] == 'command_failed'
                           else plan_result['outcome'])
                layer = plan_result.get('layer', 'engine')
                exit_code = plan_result.get('observed_exit')
                if outcome == 'infra_failed':
                    evidence['cause'] = retry.cause_of(plan_result)
                raise _Stop()
            planned = sharding.inventory(sharding.read(plan_document(paths, plan_result)))
            # A selection Playwright puts entirely in shard 1 is a one-shard job.
            # The empty shards stay in the partition -- they are part of the
            # proof that nothing was lost -- but running them would observe
            # nothing at a full clone's price.
            dispatch = [index for index, tests in enumerate(planned, 1) if tests]
            note('plan lists %d test%s across %d shard%s; dispatching %d'
                 % (sum(len(x) for x in planned), '' if sum(len(x) for x in planned) == 1 else 's',
                    len(planned), '' if len(planned) == 1 else 's', len(dispatch)))

        if config['strategy'] == 'queue':
            tests = sharding.flatten(planned)
            children = dispatch_queue(paths, ledger, plan, config, run_id, total,
                                      tests, plan_result=plan_result, note=note,
                                      tails=tails, log=log,
                                      keep_going=bool(control.get('keep_going')))
            outcome, layer, exit_code, reports, evidence = finish_queue(
                paths, ledger, plan, config, run_id, total, tests, children,
                evidence, note)
        else:
            children = dispatch_shards(paths, ledger, plan, config, run_id, total,
                                       dispatch, plan_result=plan_result, note=note,
                                       tails=tails, log=log,
                                       keep_going=bool(control.get('keep_going')))
            outcome, layer, exit_code, reports, evidence = finish(
                paths, ledger, plan, config, run_id, total, planned, children,
                evidence, note)
    except _Stop:
        pass
    except Exception as error:                     # noqa: BLE001 - recorded, never swallowed
        outcome, layer = 'infra_failed', 'engine'
        evidence['error'] = '%s: %s' % (type(error).__name__, error)
        evidence['cause'] = ('admission-timeout' if isinstance(error, AdmissionTimeout)
                             else 'engine-error')
        note(evidence['error'])
    finally:
        drain_tails(tails, log)
        log.close()

    durations['total'] = round(time.monotonic() - started, 2)
    extra = {'role': 'parent', 'shards': evidence.get('shards', []),
             'verification': evidence.get('verification'),
             'collisions': evidence.get('collisions', [])}
    if writeback.patterns_of(plan['outputs']):
        extra['writeback'] = propose(paths, plan, run_id, children, outcome, evidence)
        if not extra['writeback']['complete'] and outcome == 'passed':
            note('write-back: ' + extra['writeback']['why'])
    result = runner.write_result(
        paths, ledger, run_id, outcome=outcome, layer=layer, exit_code=exit_code,
        peak_mib=evidence.get('peak_mib', 0), durations=durations, evidence=evidence,
        receipt={'clean': True, 'note': 'a parent owns no instance'}, extra=extra)
    if evidence.get('collisions') and result['cli_exit'] == 0:
        result['cli_exit'] = COLLISION_EXIT
        runner.write_json(paths.result(run_id), result)
    ledger.close()
    return result


def propose(paths, plan, run_id, children, outcome, evidence):
    """The fan-out's one write-back proposal, or a record of why there is none.

    All or nothing. A catalog `--update` whose shard 3 failed has correct
    fixtures for shards 1, 2 and 4, and publishing them would leave the tree
    describing a suite that half-ran -- which is v0.1.1's "partial failed suites
    do not update fixtures", kept.
    """
    if outcome != 'passed' or not children:
        bad = [str(row['shard']) for row in evidence.get('shards') or []
               if row['outcome'] != 'passed']
        why = ('shard %s did not pass' % ', '.join(bad) if bad
               else 'the fan-out did not pass')
        if evidence.get('not_dispatched'):
            why += '; shard %s never ran' % ', '.join(str(i) for i in evidence['not_dispatched'])
        return writeback.incomplete(why + ', so no shard\'s files were written back', None)
    shards = {index: ((children['results'].get(index) or {}).get('writeback'),
                      paths.attempt(child) / writeback.PROPOSAL)
              for index, child in children['runs'].items()}
    return writeback.merge(plan['source_path'], shards,
                           paths.attempt(run_id) / writeback.PROPOSAL)


class _Stop(Exception):
    """A verdict was reached early. Not an error; the finally block still runs."""


class AdmissionTimeout(RuntimeError):
    """A child waited the whole admission window and never got a lane."""


# --- deciding ---------------------------------------------------------------

def free_lanes(paths, ledger, plan):
    """How many more runs of this job the worker could admit right now.

    Memory and slots both bound it, and the memory bound is the one that moves:
    a job with no history reserves its whole class ceiling, so the first fan-out
    of an unknown suite is deliberately narrow and later ones widen as the
    scheduler learns what the job actually peaks at.
    """
    with gate(paths.root):
        store = admission.Store(str(paths.peaks))
        try:
            scheduler = Scheduler(ledger, store, budget_mib=runner.budget_of(paths),
                                          max_running=runner.max_running_of(paths)[0])
            reserve, _, _, _ = scheduler.reservation(plan['repo'], plan['job'],
                                                     plan['size_class'], 'shard')
            spare = scheduler.budget_mib - scheduler.held_mib()
            by_memory = spare // max(1, reserve)
            by_slots = scheduler.max_running - len(scheduler.live_rows())
            return max(1, min(by_memory, by_slots))
        finally:
            store.close()


# --- the plan step ----------------------------------------------------------

def plan_document(paths, plan_result):
    return paths.outputs(plan_result['run_id']) / sharding.PLAN_PATH


def run_plan(paths, ledger, plan, config, parent, total, args, *, note, tails, log):
    """Build once and freeze the partition, in one instance, before any shard."""
    argv = sharding.plan_argv(config, args, total=total)
    outputs = [{'kind': 'artifacts',
                'paths': [sharding.PLAN_PATH] + list(config['plan_outputs'])}]
    child = start_child(paths, ledger, plan, parent, role='plan', argv=argv,
                        env=dict(plan['env']), outputs=outputs, request_suffix='plan')
    note('plan %s: %s' % (child, ' '.join(argv)))
    tails[child] = {'label': 'plan', 'offset': 0, 'path': str(paths.log(child))}
    admit_and_spawn(paths, ledger, child, plan, note=note, label='plan')
    return wait_for(paths, ledger, [child], tails=tails, log=log)[child]


# --- dispatching ------------------------------------------------------------

def start_child(paths, ledger, plan, parent, *, role, argv, env, outputs,
                request_suffix, index=None, total=None, graft=None, retry_of=None,
                queue_dir=None, keep_going=False):
    """Create one child row and everything its supervisor reads from disk."""
    run_id = 'r' + uuid.uuid4().hex[:15]
    with gate(paths.root):
        # A child is its parent's client's work: attribution and cancel scope follow.
        owner = ledger.get(parent)
        row, _ = ledger.claim('%s:%s' % (parent, request_suffix), run_id,
                              repo=plan['repo'], job=plan['job'], input_id=plan['input_id'],
                              source_path=plan['source_path'], argv=argv, env=env,
                              cwd=plan['cwd'], outputs=outputs, size_class=plan['size_class'],
                              role=role, parent=parent, shard_index=index, shard_total=total,
                              retry_of=retry_of,
                              client=owner['client'] if owner is not None else None)
    run_id = row['run_id']
    attempt = paths.attempt(run_id)
    attempt.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(paths.attempt(parent) / 'toolchain.json', attempt / 'toolchain.json')
    request = paths.attempt(parent) / 'request.json'
    if request.is_file():
        # The child's supervisor reads the parts of the plan the ledger does
        # not carry -- timeout_minutes, the cancel contract, git -- from the
        # request beside the attempt. A child's request is its parent's.
        shutil.copyfile(request, attempt / 'request.json')
    if queue_dir is not None:
        (attempt / 'batchqueue.json').write_text(json.dumps(
            {'queue': str(queue_dir), 'keep_going': keep_going}))
    paths.log(run_id).touch()
    if graft is not None:
        # A symlink, not a copy: N shards share one build-once tree on the host
        # and each pays only for pushing it into its own instance.
        link = attempt / 'planout'
        if not link.exists():
            link.symlink_to(Path(graft).resolve())
    return run_id


def admit_and_spawn(paths, ledger, run_id, plan, *, note, deadline=None, label=None):
    """Hold the row until the scheduler has room for it, then start it.

    Admission is per child and never per fan-out. A parent that reserved its
    whole fan-out up front would hold memory it is not yet using while a
    sibling repository waits, and would deadlock the moment the box could fit
    three of its four shards.

    Waiting is said once, with an estimate when the ledger has one, and then at
    most every `STILL_EVERY` seconds. It used to be said every two seconds.
    """
    deadline = deadline or (time.monotonic() + ADMIT_SECONDS)
    said = None
    joined = None
    while True:
        with gate(paths.root):
            store = admission.Store(str(paths.peaks))
            try:
                scheduler = Scheduler(ledger, store, budget_mib=runner.budget_of(paths),
                                          max_running=runner.max_running_of(paths)[0])
                current = ledger.get(run_id)
                if current is not None and current['state'] != 'queued':
                    # Not queued any more: something else admitted or closed it.
                    # Spawning here would be a second supervisor; the poll that
                    # follows reads whatever became of it.
                    return {'admitted': False, 'reason': 'state',
                            'state': current['state']}
                # Disk has no reservation arithmetic; the floor check runs
                # before `admit` so a child never holds a memory reservation
                # for an instance the pool has no room to clone. A shard below
                # the floor waits exactly as one refused on memory does -- the
                # same queue, the same deadline -- rather than starting a run
                # that could not fit.
                room = runner.disk_headroom(paths)
                if not room.get('ok'):
                    verdict = {'admitted': False, 'reason': 'disk-floor',
                               'capacity': room}
                else:
                    verdict = scheduler.admit(run_id, plan['repo'], plan['job'],
                                              plan.get('size_declared')
                                              or plan['size_class'])
                if verdict['admitted']:
                    pid = runner.spawn(paths.root, run_id)
                    ledger.update(run_id, supervisor_pid=pid)
                    return verdict
                # The shard stands in the worker's one queue (`waitlist`), in
                # arrival order with every plain run, from its first refusal.
                # Re-stamping the same `queued_at` each pass is its heartbeat.
                joined = joined or time.time()
                ledger.update(run_id, queued_at=joined)
                ahead = [row for row in scheduler.live_rows() if row['run_id'] != run_id
                         and row['state'] in ('admitted', 'running', 'collecting')]
                ahead += scheduler.ahead_of(run_id)
            finally:
                store.close()
        if time.monotonic() > deadline:
            raise AdmissionTimeout('%s waited %ds for admission: %s'
                                   % (run_id, ADMIT_SECONDS, verdict.get('reason')))
        now = time.monotonic()
        if said is None or now - said >= STILL_EVERY:
            if verdict['reason'] == 'disk-floor':
                note('%s waits on disk: %s' % (label or run_id,
                                               (verdict['capacity'] or {}).get('reason')
                                               or 'the pool is below its floor'))
            else:
                note(queue_line(ledger, label or run_id, ahead, first=said is None))
            said = now
        time.sleep(POLL * 2)


def queue_line(ledger, label, ahead, *, first):
    """`shard 2/4 queued behind 1 run, ~40 s`, or the shorter repeat."""
    count = '%d run%s' % (len(ahead), '' if len(ahead) == 1 else 's')
    if not first:
        return '%s still queued behind %s' % (label, count)
    try:
        eta = history.queue_eta(ledger, ahead)
    except Exception:                               # noqa: BLE001 - a courtesy, never a verdict
        eta = None
    return '%s queued behind %s%s' % (label, count,
                                      ', ~%s' % history.fmt_seconds(eta) if eta is not None
                                      else '')


def dispatch_shards(paths, ledger, plan, config, parent, total, indices, *,
                    plan_result, note, tails, log, keep_going):
    """Start every shard, stopping new ones on the first failure unless told not to.

    "Stop" means stop *dispatching*. A shard already inside a container is left
    alone to reach its own verdict, because killing it would throw away the
    evidence it is in the middle of producing and would make the result depend
    on the order the shards happened to finish in.
    """
    graft = (paths.outputs(plan_result['run_id']) if plan_result else None)
    guest_plan = sharding.PLAN_PATH if plan_result else None

    def make(index, suffix, retry_of=None):
        argv = sharding.child_argv(plan['argv'], config, index=index, total=total,
                                   plan_path=guest_plan)
        env = sharding.child_env(plan['env'], config, index=index, total=total)
        return start_child(paths, ledger, plan, parent, role='shard', argv=argv, env=env,
                           outputs=plan['outputs'], request_suffix=suffix,
                           index=index, total=total, graft=graft, retry_of=retry_of)

    def launch(index, run_id):
        tails[run_id] = {'label': '%d/%d' % (index, total), 'offset': 0,
                         'path': str(paths.log(run_id))}
        admit_and_spawn(paths, ledger, run_id, plan, note=note,
                        label='shard %d/%d' % (index, total))
        live[index] = run_id

    created, pending, retried = {}, [], {}
    for index in indices:
        created[index] = make(index, 'shard:%d' % index)
        pending.append(index)

    live, done, stopped = {}, {}, False
    while True:
        if pending and not stopped:
            index = pending.pop(0)
            launch(index, created[index])
            note('shard %d/%d is %s' % (index, total, created[index]))
        if not live:
            break
        finished = poll(paths, ledger, list(live.values()), tails=tails, log=log)
        for index in [i for i, run_id in sorted(live.items()) if run_id in finished]:
            result = finished[live.pop(index)]
            # One shard, once, and only when nothing of that shard's own output
            # has been streamed: its siblings' verdicts stand, so repeating the
            # one that broke is the whole retry. The whole-run rule is the
            # daemon's and would, here, throw away every sibling that passed.
            if index not in retried and shard_retryable(paths, ledger, parent,
                                                        created[index], result):
                cause = retry.cause_of(result)
                note('shard %d/%d: infrastructure failure before output (%s); retrying once'
                     % (index, total, cause))
                fresh = make(index, 'shard:%d:retry' % index, retry_of=created[index])
                retried[index] = {'shard': index, 'failed_run': created[index],
                                  'cause': cause, 'retry_run': fresh}
                created[index] = fresh
                launch(index, fresh)
                continue
            done[index] = result
            if done[index]['outcome'] != 'passed' and not keep_going and pending and not stopped:
                stopped = True
                note('shard %d did not pass; the %d shard(s) not yet dispatched will not '
                     'start. Shards already running are left to finish.'
                     % (index, len(pending)))
        if not live and (stopped or not pending):
            break
        if not finished:
            time.sleep(POLL)
    for index in pending:
        # The row exists and nothing ever ran in it. Close it honestly rather
        # than leaving a queued row for `reconcile` to discover later.
        runner.write_result(paths, ledger, created[index], outcome='cancelled',
                            layer='engine', exit_code=None, peak_mib=0, durations={},
                            evidence={'reason': 'an earlier shard failed; never dispatched'},
                            receipt={'clean': True, 'note': 'no instance was created'})
    return {'runs': created, 'results': done, 'not_dispatched': sorted(pending),
            'retries': [retried[index] for index in sorted(retried)]}


def dispatch_queue(paths, ledger, plan, config, parent, total, tests, *,
                   plan_result, note, tails, log, keep_going):
    """Feed the plan's inventory to long-lived shards a batch at a time.

    A shard is a session, not a slice: its supervisor claims batch specs off
    the parent's `queue/` directory until it drains or halts. A shard that dies
    holding a batch leaves a lease the parent returns to `pending` while
    `batch_attempts` lasts, so a poison test or a lost instance costs a batch,
    not a partition.
    """
    graft = paths.outputs(plan_result['run_id'])
    size = sharding.batch_size(tests, total, config['batch_size'])
    cuts = sharding.batch_up(tests, size)
    queue = batches.Queue(paths.attempt(parent) / 'queue').seed(cuts)
    cap = config['batch_attempts']
    note('queue: %d tests in %d batches of %d, attempt cap %d'
         % (len(tests), len(cuts), size, cap))
    # Fewer batches than lanes means some slots would never see work.
    total = min(total, len(cuts))

    def finished(rid):
        row = ledger.get(rid)
        return row is not None and row['state'] == 'finished'

    def make(index, suffix):
        env = sharding.child_env(plan['env'], config, index=index, total=total)
        child = start_child(paths, ledger, plan, parent, role='shard',
                            argv=list(plan['argv']), env=env,
                            outputs=plan['outputs'], request_suffix=suffix,
                            index=index, total=total, graft=graft,
                            queue_dir=queue.dir, keep_going=keep_going)
        return child

    live, done = {}, {}

    def launch(index, run_id):
        tails[run_id] = {'label': 'shard %d/%d' % (index, total), 'offset': 0,
                         'path': str(paths.log(run_id))}
        admit_and_spawn(paths, ledger, run_id, plan, note=note,
                        label='shard %d/%d' % (index, total))
        live[index] = run_id

    created, every = {}, []
    for index in range(1, total + 1):
        created[index] = make(index, 'shard:%d' % index)
        every.append(created[index])
        launch(index, created[index])
        note('shard %d/%d is %s' % (index, total, created[index]))

    respawns = 0
    while live:
        requeued, dead = queue.release_dead(finished, cap=cap)
        for seq in requeued:
            note('batch %d\'s shard is gone; it rejoins the queue' % seq)
        for seq in dead:
            note('batch %d exhausted its %d attempts; its tests will read as unrun'
                 % (seq, cap))
        if not keep_going and not queue.halted() and queue.failed():
            queue.halt()
            note('a batch failed; the queue is halted. Shards finish their '
                 'current batch, then stop.')
        finished_now = poll(paths, ledger, list(live.values()), tails=tails, log=log)
        for index in [i for i, rid in sorted(live.items()) if rid in finished_now]:
            result = finished_now[live.pop(index)]
            done[index] = result
            parent_row = ledger.get(parent)
            cancelled = parent_row is not None and parent_row['cancel_requested']
            if (queue.snapshot()['pending'] and not queue.halted()
                    and respawns < total and not cancelled
                    and result['outcome'] not in ('passed', 'command_failed',
                                                  'cancelled')):
                # A shard that died abnormally while work stood unclaimed gets
                # a successor rather than leaving the queue staffed by whoever
                # outlived it. A shard that ended on a failed batch or a clean
                # drain earns none. `total` extra boots bound what flapping
                # instances can cost; the attempt cap bounds a poison batch.
                fresh = make(index, 'shard:%d:respawn%d' % (index, respawns + 1))
                respawns += 1
                note('shard %d ended with batch(es) still pending; a successor '
                     'is %s' % (index, fresh))
                created[index] = fresh
                every.append(fresh)
                launch(index, fresh)
        if not finished_now:
            time.sleep(POLL)
    return {'runs': created, 'results': done, 'not_dispatched': [],
            'retries': [], 'queue': queue, 'every': every}


def shard_retryable(paths, ledger, parent, run_id, result):
    """Whether one finished shard earns its one retry. See `retry` for the rules."""
    if result.get('outcome') != 'infra_failed':
        return False
    if not retry.retryable(retry.cause_of(result))[0]:
        return False
    row = ledger.get(parent)
    if row is not None and row['cancel_requested']:
        return False
    try:
        return not retry.command_output(paths.log(run_id).read_bytes())
    except OSError:
        return False                    # no log to prove silence with is not silence


# --- waiting and streaming --------------------------------------------------

def poll(paths, ledger, run_ids, *, tails, log):
    """Copy any new child output into the parent's log; return what has finished."""
    drain_tails(tails, log, only=run_ids)
    finished = {}
    for run_id in list(run_ids):
        row = ledger.get(run_id)
        if row is not None and row['state'] == 'finished':
            path = paths.result(run_id)
            if not path.is_file() and time.time() - (row['finished'] or 0) < RESULT_GRACE:
                # The ledger row is finished a moment before the result file is
                # written. Reading that moment as "no result" turned a passing
                # shard into an infra failure, intermittently, under load.
                continue
            finished[run_id] = (json.loads(path.read_text()) if path.is_file()
                                else {'run_id': run_id, 'outcome': 'infra_failed',
                                      'layer': 'engine', 'observed_exit': None,
                                      'evidence': {'error': 'no result file',
                                                   'cause': 'engine-error'}})
    return finished


def wait_for(paths, ledger, run_ids, *, tails, log):
    outstanding, results = list(run_ids), {}
    while outstanding:
        done = poll(paths, ledger, outstanding, tails=tails, log=log)
        for run_id in done:
            results[run_id] = done[run_id]
            outstanding.remove(run_id)
        if outstanding:
            time.sleep(POLL)
    drain_tails(tails, log, only=results)
    return results


def drain_tails(tails, log, only=None):
    """Interleave the children's logs into the parent's, each line labeled.

    The caller is attached to one log file, so a fan-out that says nothing for
    four minutes looks identical to one that has hung. Prefixing rather than
    merging blindly keeps it readable when four shards print at once.
    """
    for run_id, tail in list(tails.items()):
        if only is not None and run_id not in only:
            continue
        try:
            with open(tail['path'], 'rb') as handle:
                handle.seek(tail['offset'])
                chunk = handle.read()
        except OSError:
            continue
        if not chunk:
            continue
        # Whole lines only: a chunk that ends mid-line is left for the next
        # pass, so a label never lands in the middle of a sentence.
        cut = chunk.rfind(b'\n') + 1
        if not cut:
            continue
        tail['offset'] += cut
        for line in chunk[:cut].decode('utf-8', 'replace').splitlines():
            log.write('[%s] %s\n' % (tail['label'], line))


# --- aggregating ------------------------------------------------------------

def finish(paths, ledger, plan, config, run_id, total, planned, children, evidence, note):
    """Every report, the partition check, the merged artifacts, and the verdict."""
    results, runs = children['results'], children['runs']
    reports, rows = {}, []
    for index in sorted(runs):
        child = runs[index]
        result = results.get(index)
        row = {'shard': index, 'run_id': child,
               'outcome': result['outcome'] if result else 'not_dispatched',
               'exit_code': result.get('observed_exit') if result else None,
               'peak_mib': result.get('peak_mib') if result else None,
               'seconds': result.get('wall_seconds') if result else None,
               'durations': result.get('durations') if result else None,
               'report': None, 'observed': None}
        again = next((item for item in children.get('retries') or []
                      if item['shard'] == index), None)
        if again is not None:
            row['retried_from'] = again['failed_run']
            row['retry_cause'] = again['cause']
        if result is not None and config['report']:
            found = find_report(paths, child, config, index=index, total=total)
            if found is not None:
                row['report'] = str(found)
                try:
                    row['observed'] = sharding.observed(sharding.read(found))
                    reports[index] = row['observed']
                except (ValueError, OSError) as error:
                    note('shard %d wrote a report this engine cannot read: %s' % (index, error))
        rows.append(row)
    evidence['shards'] = rows
    if children.get('retries'):
        evidence['shard_retries'] = children['retries']
    evidence['peak_mib'] = max([row['peak_mib'] or 0 for row in rows] or [0])

    if planned is not None:
        verification = sharding.verify(planned, reports)
    else:
        verification = {'verified': False, 'reason': 'this job declares no plan step, so '
                                                     'nothing describes the partition',
                        'unverified': True, 'shards': total}
    evidence['verification'] = verification
    note('verification: %s' % verification['reason'])

    merged, collisions = merge_outputs(paths, run_id, runs, results)
    evidence['collisions'] = collisions
    evidence['merged_files'] = merged
    if collisions:
        note('%d output path(s) were written differently by more than one shard; '
             'every version is kept under .pandora-shards/' % len(collisions))

    outcomes = [row['outcome'] for row in rows]
    failed = [row for row in rows if row['outcome'] not in ('passed',)]
    if children['not_dispatched']:
        note('%d shard(s) were never dispatched' % len(children['not_dispatched']))
        evidence['not_dispatched'] = children['not_dispatched']
    if failed:
        first = failed[0]
        if all(row['outcome'] in ('passed', 'command_failed', 'not_dispatched')
               for row in rows):
            return 'command_failed', 'command', first['exit_code'] or 1, reports, evidence
        worst = next(row for row in rows if row['outcome'] not in ('passed', 'command_failed',
                                                                   'not_dispatched'))
        if worst['outcome'] == 'infra_failed':
            evidence['cause'] = 'shard-failed'
        return worst['outcome'], 'engine', worst['exit_code'], reports, evidence
    if planned is not None and not verification['verified']:
        # Every shard said it passed and the partition says they did not, between
        # them, run the suite. That is an engine verdict, and it is not a pass.
        evidence['cause'] = 'partition-unverified'
        return 'infra_failed', 'engine', None, reports, evidence
    if 'passed' not in outcomes:
        evidence['cause'] = 'engine-error'
        return 'infra_failed', 'engine', None, reports, evidence
    return 'passed', 'command', 0, reports, evidence


def finish_queue(paths, ledger, plan, config, run_id, total, tests, children,
                 evidence, note):
    """The queue fan-out's receipt: every batch report, coverage, and verdict.

    `tests` is the flattened inventory. Reports map a batch number to the ids
    its holder pulled home; the queue directory itself says which batches died
    unrun. The proof is coverage of the whole inventory, not a match against a
    per-shard split that never existed.
    """
    results, runs = children['results'], children['runs']
    queue = children['queue']
    reports, rows = {}, []
    for index in sorted(runs):
        child = runs[index]
        result = results.get(index)
        row = {'shard': index, 'run_id': child,
               'outcome': result['outcome'] if result else 'not_dispatched',
               'exit_code': result.get('observed_exit') if result else None,
               'peak_mib': result.get('peak_mib') if result else None,
               'seconds': result.get('wall_seconds') if result else None,
               'durations': result.get('durations') if result else None,
               'batches': ((result.get('evidence') or {}).get('batches')
                           if result else None)}
        rows.append(row)
    for child in children.get('every') or runs.values():
        # Every attempt the slot ever ran, not only the last: a dead shard's
        # pulled batch reports are evidence the successor does not carry.
        batch_dir = paths.attempt(child) / 'batches'
        for report in sorted(batch_dir.glob('batch-*.json')) if batch_dir.is_dir() else []:
            try:
                reports[int(report.stem[len('batch-'):])] = sharding.observed(
                    sharding.read(report))
            except (ValueError, OSError) as error:
                note('run %s left an unreadable batch report %s: %s'
                     % (child, report.name, error))
    evidence['shards'] = rows
    evidence['queue'] = queue.snapshot()
    evidence['peak_mib'] = max([row['peak_mib'] or 0 for row in rows] or [0])

    dead = {}
    for seq in queue.snapshot()['dead']:
        try:
            dead[seq] = sharding.ids(json.loads(
                (queue.dir / 'dead' / ('%06d.json' % seq)).read_text()).get('testIds'))
        except (ValueError, OSError):
            dead[seq] = []
    verification = sharding.verify_queue(tests, reports, dead)
    evidence['verification'] = verification
    note('verification: %s' % verification['reason'])

    merged, collisions = merge_outputs(paths, run_id, runs, results)
    evidence['collisions'] = collisions
    evidence['merged_files'] = merged
    if collisions:
        note('%d output path(s) were written differently by more than one shard; '
             'every version is kept under .pandora-shards/' % len(collisions))

    failed = [row for row in rows if row['outcome'] != 'passed']
    if failed:
        first = failed[0]
        if all(row['outcome'] in ('passed', 'command_failed') for row in rows):
            return 'command_failed', 'command', first['exit_code'] or 1, reports, evidence
        worst = next(row for row in rows
                     if row['outcome'] not in ('passed', 'command_failed'))
        if worst['outcome'] == 'infra_failed':
            evidence['cause'] = 'shard-failed'
        return worst['outcome'], 'engine', worst['exit_code'], reports, evidence
    if not verification['verified']:
        # Every shard said it passed and the queue says they did not, between
        # them, run the suite. That is an engine verdict, and it is not a pass.
        evidence['cause'] = 'partition-unverified'
        return 'infra_failed', 'engine', None, reports, evidence
    return 'passed', 'command', 0, reports, evidence


def find_report(paths, child, config, *, index, total):
    pattern = sharding.report_path(config, index=index, total=total)
    root = paths.outputs(child)
    found = sorted(root.glob(pattern))
    return found[0] if found else None


def merge_outputs(paths, parent, runs, results):
    """One artifact tree from N, with disagreements kept rather than resolved."""
    into = paths.outputs(parent)
    into.mkdir(parents=True, exist_ok=True)
    trees = {}
    for index in sorted(runs):
        if index not in results:
            continue
        root = paths.outputs(runs[index])
        trees[index] = {str(path.relative_to(root)): _digest(path)
                        for path in sorted(root.rglob('*')) if path.is_file()}
    collisions = sharding.collisions(trees)
    contested = {item['path'] for item in collisions}
    written = 0
    for index in sorted(trees):
        root = paths.outputs(runs[index])
        for relative in sorted(trees[index]):
            source = root / relative
            if relative in contested:
                target = into / '.pandora-shards' / ('shard-%d' % index) / relative
            else:
                target = into / relative
                if target.exists():
                    continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            written += 1
    return written, collisions


def _digest(path):
    hasher = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            hasher.update(block)
    return hasher.hexdigest()
