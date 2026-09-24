"""The worker queue and learned size classes (rulings of 2026-09-24).

Engine side, with the real ledger, scheduler and service, and every detached
process faked: `runner.spawn` and `runner.spawn_waiter` record what they would
have started, and the waiter is driven by calling `waitlist.wait` in-process
with a fake clock. Nothing here starts a process or talks to Incus.
"""
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from pandora.engine import admission, runner, scheduler as scheduler_module, service, waitlist
from pandora.engine.ledger import Ledger
from pandora.engine.scheduler import Scheduler
from pandora.tests.test_engine import PLAN, FakeDriver, claim


class Engine(unittest.TestCase):
    """A real engine root with detached processes recorded instead of started."""

    BUDGET = '8192'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'engine'
        self.source = Path(self.tmp.name) / 'src'
        self.source.mkdir()
        self.spawned, self.waiters = [], []
        for patch in (
                mock.patch.dict(os.environ, {'PANDORA_BUDGET_MIB': self.BUDGET}),
                mock.patch.object(runner, 'spawn', lambda root, run_id, python=None:
                                  self.spawned.append(run_id) or 4242),
                mock.patch.object(runner, 'spawn_waiter', lambda root, run_id, python=None:
                                  self.waiters.append(run_id) or 4343),
                mock.patch.object(runner, 'disk_headroom',
                                  lambda paths, driver=None: {'ok': True})):
            patch.start()
            self.addCleanup(patch.stop)
        self.paths = runner.Paths(self.root).ensure()

    def call(self, *argv, stdin=''):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch('sys.stdin', io.StringIO(stdin)):
            service.main(['--root', str(self.root), *argv])
        return json.loads(out.getvalue())

    def submit(self, request_id, *, size='medium', job='suite', client=None):
        plan = dict(PLAN, size=size, job=job, shards=None)
        request = {'request_id': request_id, 'input_id': 'input-a',
                   'source_path': str(self.source), 'plan': plan}
        if client:
            request['client'] = client
        return self.call('submit', stdin=json.dumps(request))

    def ledger(self):
        ledger = Ledger(self.paths.ledger)
        self.addCleanup(ledger.close)
        return ledger

    def row(self, run_id):
        ledger = Ledger(self.paths.ledger)
        try:
            return dict(ledger.get(run_id))
        finally:
            ledger.close()

    def wait(self, run_id, *, now=None):
        """One pass of the waiter: it returns at once unless it would sleep."""
        clock = (lambda: now) if now is not None else time.time

        def sleep(_):
            raise StopIteration
        try:
            return waitlist.wait(self.root, run_id, clock=clock, sleep=sleep)
        except StopIteration:
            return 'waiting'

    def finish(self, run_id, seconds=None):
        """End an admitted run as a pass, as its supervisor would."""
        ledger = Ledger(self.paths.ledger)
        try:
            runner.write_result(self.paths, ledger, run_id, outcome='passed', layer='command',
                                exit_code=0, peak_mib=100, durations={}, evidence={},
                                receipt={'clean': True})
            if seconds is not None:
                row = ledger.get(run_id)
                ledger.update(run_id, finished=(row['admitted_at'] or row['created']) + seconds)
        finally:
            ledger.close()


class AFullWorkerQueues(Engine):
    def test_memory_is_the_only_obstacle_so_the_run_queues_and_starts_nothing(self):
        first = self.submit('a:suite', size='large')
        self.assertEqual(first['state'], 'admitted')
        queued = self.submit('b:suite', size='medium')
        self.assertTrue(queued['ok'])
        self.assertEqual(queued['state'], 'queued')
        self.assertEqual(queued['engine'], 4)
        self.assertEqual(queued['queued']['position'], 1)
        self.assertEqual(queued['queued']['running'], 1)
        self.assertEqual(queued['queued']['bound_seconds'], waitlist.BOUND_DEFAULT)
        self.assertEqual(self.spawned, [first['run_id']])
        self.assertEqual(self.waiters, [queued['run_id']])
        row = self.row(queued['run_id'])
        self.assertEqual(row['state'], 'queued')
        self.assertIsNone(row['supervisor_pid'])
        self.assertEqual(row['waiter_pid'], 4343)

    def test_the_waiter_admits_when_room_frees_and_only_then_spawns_a_supervisor(self):
        first = self.submit('a:suite', size='large')['run_id']
        queued = self.submit('b:suite', size='medium')['run_id']
        self.assertEqual(self.wait(queued), 'waiting')
        self.assertEqual(self.spawned, [first])
        self.finish(first)
        self.assertEqual(self.wait(queued), 'admitted')
        self.assertEqual(self.spawned, [first, queued])
        row = self.row(queued)
        self.assertEqual(row['state'], 'admitted')
        self.assertEqual(row['supervisor_pid'], 4242)
        self.assertIsNotNone(row['admitted_at'])
        self.assertIn('admitted after', self.paths.log(queued).read_text())

    def test_arrival_order_a_large_head_blocks_a_small_row_that_would_fit(self):
        self.submit('a:suite', size='medium', job='one')                    # 4 GiB held
        large = self.submit('b:suite', size='large', job='two')['run_id']   # needs 8
        small = self.submit('c:suite', size='small', job='three')           # 1 would fit
        self.assertEqual(small['state'], 'queued')
        self.assertEqual(small['admission']['reason'], 'queue')
        self.assertEqual(small['queued']['position'], 2)
        self.assertEqual(self.wait(small['run_id']), 'waiting')
        self.assertEqual(self.row(small['run_id'])['state'], 'queued')
        self.assertEqual(self.wait(large), 'waiting')

    def test_a_new_run_that_would_fit_still_queues_behind_a_waiting_one(self):
        self.submit('a:suite', size='medium', job='one')
        self.submit('b:suite', size='large', job='two')
        late = self.submit('c:suite', size='small', job='three')
        self.assertEqual(late['state'], 'queued')

    def test_a_row_whose_waiter_stopped_touching_it_leaves_the_queue(self):
        self.submit('a:suite', size='medium', job='one')
        large = self.submit('b:suite', size='large', job='two')['run_id']
        ledger = self.ledger()
        ledger.db.execute('UPDATE attempts SET updated=? WHERE run_id=?',
                          (time.time() - scheduler_module.QUEUE_STALE - 5, large))
        small = self.submit('c:suite', size='small', job='three')
        self.assertEqual(small['state'], 'admitted')

    def test_full_slots_queue_like_full_memory(self):
        with mock.patch.object(Scheduler, '__init__', _slots(1)):
            first = self.submit('a:suite', size='small')['run_id']
            waiting = self.submit('b:suite', size='small')
            self.assertEqual(waiting['state'], 'queued')
            self.assertEqual(waiting['admission']['reason'], 'slots')
            self.assertEqual(self.waiters, [waiting['run_id']])
            self.assertEqual(self.wait(waiting['run_id']), 'waiting')
            self.finish(first)
            self.assertEqual(self.wait(waiting['run_id']), 'admitted')

    def test_only_a_reservation_no_wait_can_fit_refuses_at_once(self):
        huge = self.submit('c:suite', size='xlarge')      # 12 GiB on an 8 GiB budget
        self.assertEqual(huge['code'], 'admission-refused')
        self.assertTrue(huge['admission']['never'])
        self.assertEqual(self.waiters, [])

    def test_admission_refuses_a_row_that_is_no_longer_queued(self):
        run = self.submit('a:suite', size='small')['run_id']
        ledger = self.ledger()
        store = admission.Store(str(self.paths.peaks))
        self.addCleanup(store.close)
        verdict = Scheduler(ledger, store, budget_mib=8192).admit(run, 'demo', 'suite', 'small')
        self.assertEqual((verdict['admitted'], verdict['reason']), (False, 'state'))
        self.assertEqual(self.spawned, [run])

    def test_the_disk_floor_still_refuses(self):
        with mock.patch.object(runner, 'disk_headroom',
                               lambda paths, driver=None: {'ok': False, 'reason': 'low'}):
            refused = self.submit('a:suite')
        self.assertEqual(refused['code'], 'disk-floor')

    def test_status_says_where_a_queued_row_stands(self):
        self.submit('a:suite', size='large')
        queued = self.submit('b:suite')['run_id']
        status = self.call('status', '--run', queued)
        self.assertEqual(status['state'], 'queued')
        self.assertEqual(status['queue']['position'], 1)
        self.assertEqual(status['queue']['running'], 1)

    def test_a_duplicate_submission_of_a_queued_row_is_still_queued(self):
        self.submit('a:suite', size='large')
        first = self.submit('b:suite')
        again = self.submit('b:suite')
        self.assertTrue(again['duplicate'])
        self.assertEqual(again['state'], 'queued')
        self.assertEqual(again['queued']['position'], 1)
        self.assertEqual(self.waiters, [first['run_id']])

    def test_a_lookup_of_a_queued_row_says_attach_and_queued(self):
        self.submit('a:suite', size='large')
        self.submit('b:suite')
        found = self.call('lookup', '--request-id', 'b:suite')
        self.assertTrue(found['spawned'])
        self.assertTrue(found['queued'])
        self.assertEqual(found['state'], 'queued')

    def test_a_supervisor_never_runs_a_queued_row(self):
        self.submit('a:suite', size='large')
        queued = self.submit('b:suite')['run_id']
        with self.assertRaises(SystemExit):
            runner.supervise(self.root, queued, driver=FakeDriver())


class CancelWithdraws(Engine):
    def test_cancel_of_a_queued_row_withdraws_it_and_nothing_ran(self):
        self.submit('a:suite', size='large')
        queued = self.submit('b:suite')['run_id']
        answer = self.call('cancel', '--run', queued)
        self.assertTrue(answer['withdrawn'])
        row = self.row(queued)
        self.assertEqual((row['state'], row['outcome']), ('finished', 'cancelled'))
        result = json.loads(self.paths.result(queued).read_text())
        self.assertEqual(result['cli_exit'], 130)
        self.assertTrue(result['evidence']['withdrawn'])
        self.assertEqual(self.wait(queued), 'finished')
        self.assertNotIn(queued, self.spawned)

    def test_a_cancel_the_waiter_sees_first_is_a_withdrawal_too(self):
        self.submit('a:suite', size='large')
        queued = self.submit('b:suite')['run_id']
        self.ledger().request_cancel(queued)
        self.assertEqual(self.wait(queued), 'cancelled')
        self.assertEqual(self.row(queued)['outcome'], 'cancelled')

    def test_cancel_of_an_admitted_row_asks_its_supervisor_as_before(self):
        first = self.submit('a:suite', size='large')['run_id']
        answer = self.call('cancel', '--run', first)
        self.assertTrue(answer['requested'])
        self.assertEqual(self.row(first)['cancel_requested'], 1)


class TheBound(Engine):
    def test_no_history_waits_ten_minutes(self):
        self.assertEqual(waitlist.bound(self.ledger(), 'demo', 'suite'), 600)

    def test_three_times_the_median_run_clamped_to_two_and_thirty_minutes(self):
        for seconds, expected in ((100, 300), (20, 120), (1000, 1800)):
            with self.subTest(seconds=seconds):
                job = 'job%d' % seconds
                for index in range(3):
                    run = self.submit('%s-%d:x' % (job, index), size='small', job=job)['run_id']
                    self.finish(run, seconds=seconds)
                self.assertEqual(waitlist.bound(self.ledger(), 'demo', job), expected)

    def test_time_spent_queued_does_not_stretch_the_bound(self):
        for index in range(3):
            run = self.submit('q-%d:x' % index, size='small', job='q')['run_id']
            ledger = self.ledger()
            ledger.update(run, created=time.time() - 5000)      # queued for ages
            self.finish(run, seconds=100)
        self.assertEqual(waitlist.bound(self.ledger(), 'demo', 'q'), 300)

    def test_past_its_bound_a_queued_row_is_infra_failed_queue_timeout_exit_70(self):
        self.submit('a:suite', size='large')
        queued = self.submit('b:suite')['run_id']
        later = self.row(queued)['queue_deadline'] + 1
        self.assertEqual(self.wait(queued, now=later), 'queue-timeout')
        result = json.loads(self.paths.result(queued).read_text())
        self.assertEqual(result['outcome'], 'infra_failed')
        self.assertEqual(result['cli_exit'], 70)
        self.assertEqual(result['evidence']['cause'], 'queue-timeout')
        self.assertEqual(result['evidence']['queue']['position'], 1)
        self.assertNotIn(queued, self.spawned)
        from pandora.engine import retry
        self.assertEqual(retry.cause_of(result), 'queue-timeout')
        self.assertFalse(retry.retryable('queue-timeout')[0])
        self.assertIn('in the worker queue, its bound', self.paths.log(queued).read_text())


class ReconcileKeepsTheQueue(Engine):
    def test_a_waiting_shard_gets_no_waiter_and_is_admitted_once(self):
        from pandora.engine import fanout
        big = self.submit('a:suite', size='large', job='big')['run_id']
        ledger = self.ledger()
        claim(ledger, request_id='p', run_id='rparent', role='parent', input_id='input-p')
        ledger.update('rparent', state='running', supervisor_pid=4141)
        claim(ledger, request_id='p:shard:1', run_id='rshard', role='shard', parent='rparent',
              source_path=str(self.source))
        plan = dict(ledger.get('rshard'))
        with mock.patch.object(fanout.time, 'sleep', side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                fanout.admit_and_spawn(self.paths, ledger, 'rshard', plan, note=lambda t: None)
        # The engine restarts; the parent's supervisor and the big run live on.
        with mock.patch.object(runner, 'alive', lambda pid: pid in (4141, 4242)):
            runner.reconcile(self.root, driver=FakeDriver())
        self.assertNotIn('rshard', self.waiters)
        self.finish(big)
        self.assertEqual(self.wait('rshard'), 'not-waitable')   # a stray waiter refuses it
        self.assertEqual(self.row('rshard')['state'], 'queued')
        fanout.admit_and_spawn(self.paths, ledger, 'rshard', plan, note=lambda t: None)
        fanout.admit_and_spawn(self.paths, ledger, 'rshard', plan, note=lambda t: None)
        self.assertEqual(self.spawned.count('rshard'), 1)

    def test_a_queued_row_whose_waiter_died_gets_a_new_one_and_keeps_its_place(self):
        self.submit('a:suite', size='large')
        queued = self.submit('b:suite')['run_id']
        before = self.row(queued)
        with mock.patch.object(runner, 'alive', lambda pid: False):
            answer = runner.reconcile(self.root, driver=FakeDriver())
        self.assertIn(queued, answer['adopted'])
        after = self.row(queued)
        self.assertEqual(after['state'], 'queued')
        self.assertEqual(after['queued_at'], before['queued_at'])
        self.assertEqual(self.waiters, [queued, queued])


class ShardsStandInTheSameLine(Engine):
    def test_a_waiting_shard_is_ahead_of_a_later_plain_run(self):
        from pandora.engine import fanout
        self.submit('a:suite', size='large', job='big')
        ledger = self.ledger()
        claim(ledger, request_id='p:shard:1', run_id='rshard', role='shard',
              source_path=str(self.source))
        plan = dict(ledger.get('rshard'))
        notes = []
        with mock.patch.object(fanout.time, 'sleep', side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                fanout.admit_and_spawn(self.paths, ledger, 'rshard', plan, note=notes.append)
        self.assertIsNotNone(ledger.get('rshard')['queued_at'])
        late = self.submit('c:suite', size='small', job='three')
        self.assertEqual(late['state'], 'queued')
        self.assertEqual(late['queued']['position'], 2)


def _slots(count):
    real = Scheduler.__init__

    def init(self, *args, **kwargs):
        kwargs['max_running'] = count
        real(self, *args, **kwargs)
    return init


class LearnedSizeClasses(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Ledger(Path(self.tmp.name) / 'ledger.db')
        self.store = admission.Store(str(Path(self.tmp.name) / 'peaks.db'))
        self.addCleanup(self.store.close)
        self.addCleanup(self.ledger.close)
        self.scheduler = Scheduler(self.ledger, self.store, budget_mib=14336, cores=4)
        claim(self.ledger, size_class='large')
        self.row = self.ledger.get('r1')

    def learn(self, peak, outcome='passed'):
        return self.scheduler.learn(self.row, peak, outcome)

    def test_the_declared_class_holds_until_three_clean_samples(self):
        self.learn(800)
        learned = self.learn(800)
        self.assertEqual(learned['size_class'], 'large')
        self.assertIsNone(learned['size_change'])
        learned = self.learn(800)
        self.assertEqual(learned['size_class'], 'small')        # 800 x 1.25 fits 1 GiB
        self.assertEqual(learned['size_change']['from'], 'large')
        self.assertEqual(learned['size_change']['p95_mib'], 800)
        self.assertEqual(learned['size_change']['samples'], 3)
        reserve, ceiling, size_class, _ = self.scheduler.reservation('demo', 'suite', 'large')
        self.assertEqual((size_class, ceiling), ('small', 1024))
        self.assertEqual(reserve, 1000)                          # p95 800 x 1.25, under it

    def test_it_learns_up_as_well_as_down(self):
        claim(self.ledger, request_id='req-2', run_id='r2', size_class='small')
        row = self.ledger.get('r2')
        for _ in range(3):
            learned = self.scheduler.learn(row, 7000, 'passed')
        self.assertEqual(learned['size_class'], 'xlarge')        # 7000 x 1.25 > 8192

    def test_p95_not_an_old_maximum_decides(self):
        for peak in [6000] + [3000] * 19:
            learned = self.learn(peak)
        self.assertEqual(learned['size_class'], 'medium')        # p95 3000 -> 3750

    def test_never_a_ceiling_below_the_newest_peak(self):
        # p95 of 20 drops the top one; the newest run used 6000 MiB, and a
        # 4096 MiB ceiling would kill the next one.
        for peak in [3000] * 19 + [6000]:
            learned = self.learn(peak)
        self.assertEqual(learned['size_class'], 'large')         # 6000 x 1.25 = 7500

    def test_raising_size_in_the_toml_restarts_learning_from_it(self):
        for _ in range(5):
            self.learn(2500)                                     # large -> medium
        self.assertEqual(self.store.size_class('demo', 'suite', 'large'), 'medium')
        claim(self.ledger, request_id='req-x', run_id='rx', size_class='xlarge')
        row = self.ledger.get('rx')
        for _ in range(2):
            learned = self.scheduler.learn(row, 2600, 'passed')
            self.assertEqual(learned['size_class'], 'xlarge')    # old peaks do not count
        learned = self.scheduler.learn(row, 2600, 'passed')
        self.assertEqual(learned['size_class'], 'medium')        # 3 under the new one

    def test_a_plan_step_and_its_shards_keep_separate_histories(self):
        claim(self.ledger, request_id='p:plan', run_id='rplan', role='plan', size_class='large')
        claim(self.ledger, request_id='p:shard:1', run_id='rs1', role='shard',
              size_class='large')
        for _ in range(3):
            self.scheduler.learn(self.ledger.get('rplan'), 7000, 'passed')
            learned = self.scheduler.learn(self.ledger.get('rs1'), 700, 'passed')
        self.assertEqual(learned['size_class'], 'small')
        plan = self.scheduler.reservation('demo', 'suite', 'large', 'plan')
        shard = self.scheduler.reservation('demo', 'suite', 'large', 'shard')
        self.assertEqual((plan[2], shard[2]), ('xlarge', 'small'))
        self.assertEqual(self.scheduler.reservation('demo', 'suite', 'large')[3], 0)

    def test_an_oom_resets_to_declared_and_learning_restarts_after_it(self):
        for _ in range(3):
            self.learn(3000)
        self.assertEqual(self.store.size_class('demo', 'suite', 'large'), 'medium')
        learned = self.learn(4096, 'oom')
        self.assertEqual(learned['size_class'], 'large')
        self.assertEqual(learned['size_change']['reason'], 'oom')
        # The peaks before the oom chose `medium`; they do not choose it again.
        learned = self.learn(3000)
        self.assertEqual(learned['size_class'], 'large')
        self.learn(5000)
        learned = self.learn(5000)
        self.assertEqual(learned['size_class'], 'large')         # p95 5000 -> 6250
        self.assertEqual(self.store.peaks('demo', 'suite')[:1], [5000])

    def test_a_timeout_or_infra_failure_teaches_nothing_about_size(self):
        for _ in range(3):
            learned = self.learn(900, 'timed_out')
        self.assertEqual(learned['size_class'], 'large')

    def test_a_changed_declaration_starts_learning_again_from_it(self):
        for _ in range(3):
            self.learn(800)
        self.assertEqual(self.store.size_class('demo', 'suite', 'large'), 'small')
        self.assertEqual(self.store.size_class('demo', 'suite', 'xlarge'), 'xlarge')

    def test_the_change_is_one_line(self):
        from pandora.engine.scheduler import size_line
        self.assertEqual(size_line({'job': 'build', 'from': 'large', 'to': 'medium',
                                    'reason': 'learned', 'p95_mib': 2900, 'samples': 5}),
                         'size for build: large -> medium (p95 2900 MiB over 5 runs)')
        self.assertIsNone(size_line(None))


class TheOomHint(unittest.TestCase):
    def test_an_oom_under_a_learned_smaller_class_says_the_declared_one_applies_again(self):
        from pandora.engine.result import hint_for
        facts = {'outcome': 'oom', 'job': 'build', 'peak_mib': 1024, 'ceiling_mib': 1024,
                 'size_declared': 'large', 'size_used': 'small', 'evidence': {}}
        hint = hint_for(facts)
        self.assertIn('learned class for job build was small', hint)
        self.assertIn('declared large applies again', hint)
        self.assertNotIn('raise', hint)
        facts.update(size_declared='small')
        self.assertIn('raise the size class', hint_for(facts))


class TheRunUsesTheLearnedClass(Engine):
    def test_reservation_ceiling_limits_result_and_stderr_follow_the_learned_class(self):
        store = admission.Store(str(self.paths.peaks))
        for _ in range(3):
            store.record('demo', 'suite', 800, 'ok')
        store.set_class('demo', 'suite', 'small', declared='large')
        store.close()
        run = self.submit('a:suite', size='large')
        self.assertEqual(run['admission']['size_class'], 'small')
        self.assertEqual(run['admission']['ceiling_mib'], 1024)
        seen = {}

        class Driver(FakeDriver):
            def clone(self, golden, run_id, limits=None):
                seen['limits'] = limits
                return super().clone(golden, run_id, limits=limits)
        (self.paths.attempt(run['run_id']) / 'toolchain.json').write_text(
            json.dumps(PLAN['worker']))
        result = runner.supervise(self.root, run['run_id'], driver=Driver(peak=800 * 1048576))
        self.assertEqual(seen['limits'].ceiling_mib, 1024)
        self.assertEqual((result['size_declared'], result['size_used']), ('large', 'small'))

    def test_a_learning_failure_never_leaves_the_run_running(self):
        run = self.submit('a:suite', size='large')['run_id']
        with mock.patch.object(Scheduler, 'learn', side_effect=RuntimeError('store locked')):
            result = runner.supervise(self.root, run, driver=FakeDriver())
        self.assertEqual(result['outcome'], 'passed')
        self.assertIn('store locked', result['evidence']['learn_error'])
        self.assertEqual(self.row(run)['state'], 'finished')

    def test_a_change_is_said_on_the_runs_stderr_before_it_finishes(self):
        store = admission.Store(str(self.paths.peaks))
        for _ in range(2):
            store.record('demo', 'suite', 800, 'ok', declared='large')
        store.close()
        run = self.submit('a:suite', size='large')['run_id']
        (self.paths.attempt(run) / 'toolchain.json').write_text(json.dumps(PLAN['worker']))
        result = runner.supervise(self.root, run, driver=FakeDriver(peak=800 * 1048576))
        self.assertEqual(result['learned']['size_change']['to'], 'small')
        self.assertIn('pandora: size for suite: large -> small (p95 800 MiB over 3 runs)',
                      self.paths.log(run).read_text())
        self.assertEqual(result['size_used'], 'large')      # this run already had its class


if __name__ == '__main__':
    unittest.main()
