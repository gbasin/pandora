"""The ledger, the scheduler and the run supervisor, without a worker.

The executor is faked here. What is being tested is the engine's own promises:
idempotent submission, an honest state line, a reservation that is learned from
observation and survives a restart, and -- the one that matters most -- that no
path through `supervise` can produce `passed` unless the command itself said so.
"""
import json
import tempfile
import unittest
from pathlib import Path

from pandora.engine import admission, runner
from pandora.engine.ledger import Ledger, row_to_dict
from pandora.engine.scheduler import Scheduler
from pandora.errors import StaleRun
from pandora.executor.interface import (DestroyIncomplete, Golden, Instance, Receipt,
                                        Result, Usage)

PLAN = {'repo': 'demo', 'job': 'suite', 'size': 'medium',
        'argv': ['node', 'run.mjs'], 'env': {}, 'cwd': '.',
        'outputs': [{'kind': 'artifacts', 'paths': ['reports']}],
        'worker': {'base_image': 'images:ubuntu/26.04', 'packages': [], 'node_version': '',
                   'pnpm_version': '', 'service_images': [], 'install_command': '',
                   'source_id': 'x', 'env': {}, 'workdir': '/work'}}


def claim(ledger, request_id='req-1', run_id='r1', **over):
    fields = {'repo': PLAN['repo'], 'job': PLAN['job'], 'input_id': 'input-a',
              'source_path': '/src/a', 'argv': PLAN['argv'], 'env': {}, 'cwd': '.',
              'outputs': PLAN['outputs'], 'size_class': 'medium'}
    fields.update(over)
    return ledger.claim(request_id, run_id, **fields)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / 'ledger.db')

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_a_new_request_is_created_queued(self):
        row, created = claim(self.ledger)
        self.assertTrue(created)
        self.assertEqual(row['state'], 'queued')
        self.assertIsNone(row['outcome'])

    def test_the_same_request_id_never_starts_a_second_attempt(self):
        first, created = claim(self.ledger)
        second, again = claim(self.ledger, run_id='r2')
        self.assertTrue(created)
        self.assertFalse(again)
        self.assertEqual(first['run_id'], second['run_id'])

    def test_a_repeated_input_records_what_it_repeats(self):
        claim(self.ledger)
        row, _ = claim(self.ledger, request_id='req-2', run_id='r2')
        self.assertEqual(row['same_input_as'], 'r1')

    def test_a_different_input_repeats_nothing(self):
        claim(self.ledger)
        row, _ = claim(self.ledger, request_id='req-2', run_id='r2', input_id='input-b')
        self.assertIsNone(row['same_input_as'])

    def test_an_unknown_outcome_is_refused(self):
        claim(self.ledger)
        with self.assertRaises(StaleRun):
            self.ledger.finish('r1', outcome='probably_fine', exit_code=0)

    def test_an_unknown_state_is_refused(self):
        claim(self.ledger)
        with self.assertRaises(StaleRun):
            self.ledger.update('r1', state='vibing')

    def test_updating_a_run_that_is_not_there_is_an_error_not_a_silent_no_op(self):
        with self.assertRaises(StaleRun):
            self.ledger.update('nobody', state='running')

    def test_live_excludes_finished(self):
        claim(self.ledger)
        claim(self.ledger, request_id='req-2', run_id='r2')
        self.ledger.finish('r1', outcome='passed', exit_code=0)
        self.assertEqual([row['run_id'] for row in self.ledger.live()], ['r2'])


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / 'ledger.db')
        self.store = admission.Store(str(Path(self.tmp.name) / 'peaks.db'))

    def tearDown(self):
        self.store.close()
        self.ledger.close()
        self.tmp.cleanup()

    def scheduler(self, budget=12288, cores=4):
        return Scheduler(self.ledger, self.store, budget_mib=budget, cores=cores)

    def test_a_cold_job_reserves_its_whole_ceiling(self):
        reserve, ceiling, size_class, samples = self.scheduler().reservation('demo', 'suite')
        self.assertEqual(reserve, ceiling)
        self.assertEqual(samples, 0)

    def test_three_observations_replace_the_cold_reservation(self):
        for _ in range(3):
            self.store.record('demo', 'suite', 1000, 'ok')
        reserve, ceiling, _, samples = self.scheduler().reservation('demo', 'suite')
        self.assertEqual(samples, 3)
        self.assertEqual(reserve, 1250)          # p95 1000 * 1.25
        self.assertLess(reserve, ceiling)

    def test_an_oom_peak_is_stored_and_never_learned_from(self):
        for _ in range(3):
            self.store.record('demo', 'suite', 1000, 'ok')
        before = self.scheduler().reservation('demo', 'suite')[0]
        for _ in range(5):
            self.store.record('demo', 'suite', 4096, 'oom')
        self.assertEqual(self.scheduler().reservation('demo', 'suite')[0], before)

    def test_admission_writes_its_decision_into_the_ledger(self):
        claim(self.ledger)
        verdict = self.scheduler().admit('r1', 'demo', 'suite', 'medium')
        self.assertTrue(verdict['admitted'])
        row = self.ledger.get('r1')
        self.assertEqual(row['state'], 'admitted')
        self.assertEqual(row['reservation_mib'], verdict['reservation_mib'])

    def test_held_memory_is_the_ledger_so_a_restart_does_not_forget(self):
        claim(self.ledger)
        self.scheduler().admit('r1', 'demo', 'suite', 'medium')
        # A brand new Scheduler, as a second SSH call or a restarted engine sees it.
        self.assertEqual(self.scheduler().held_mib(), 4096)

    def test_a_run_that_does_not_fit_is_refused_with_the_numbers(self):
        claim(self.ledger)
        self.scheduler(budget=5000).admit('r1', 'demo', 'suite', 'medium')
        claim(self.ledger, request_id='req-2', run_id='r2')
        verdict = self.scheduler(budget=5000).admit('r2', 'demo', 'suite', 'medium')
        self.assertFalse(verdict['admitted'])
        self.assertEqual(verdict['reason'], 'memory')
        self.assertEqual(verdict['held_mib'], 4096)

    def test_the_cpu_hint_is_a_share_of_the_box_not_its_core_count(self):
        scheduler = self.scheduler(cores=4)
        self.assertEqual(scheduler.cpus_hint(1), 4)
        self.assertEqual(scheduler.cpus_hint(2), 2)
        self.assertEqual(scheduler.cpus_hint(3), 1)
        self.assertEqual(scheduler.cpus_hint(8), 1)

    def test_two_admitted_runs_each_get_half_the_box(self):
        claim(self.ledger)
        first = self.scheduler().admit('r1', 'demo', 'suite', 'medium')
        claim(self.ledger, request_id='req-2', run_id='r2')
        second = self.scheduler().admit('r2', 'demo', 'suite', 'medium')
        self.assertEqual(first['cpus_hint'], 4)      # alone when it was admitted
        self.assertEqual(second['cpus_hint'], 2)

    def test_learning_from_a_finished_run_lowers_the_next_reservation(self):
        claim(self.ledger)
        scheduler = self.scheduler()
        scheduler.admit('r1', 'demo', 'suite', 'medium')
        row = self.ledger.get('r1')
        for _ in range(3):
            learned = scheduler.learn(row, 900, 'passed')
        self.assertTrue(learned['learned'])
        self.assertEqual(learned['next_reservation_mib'], 1125)


class FakeDriver:
    """An executor that does what it is told, so the engine's logic is what fails."""

    def __init__(self, *, outcome='ok', exit_code=0, peak=1000 * 1048576,
                 destroy_clean=True, explode=None, log='hello\n'):
        self.outcome, self.exit_code, self.peak = outcome, exit_code, peak
        self.destroy_clean, self.explode, self.log = destroy_clean, explode, log
        self.destroyed = []

    def prepare(self, toolchain, source=None, log=print):
        if self.explode == 'prepare':
            from pandora.executor.interface import PrepareFailed
            raise PrepareFailed('no image')
        return Golden(name='golden-x', fingerprint='x', snapshot='warm', reused=True)

    def clone(self, golden, run_id, limits=None):
        if self.explode == 'clone':
            from pandora.executor.interface import CloneFailed
            raise CloneFailed('no space')
        return Instance(name='run-' + run_id, run_id=run_id, golden=golden.name,
                        clone_seconds=0.1, start_seconds=0.2)

    def inject(self, name, source, dest, method='device-rsync'):
        return 0.4

    def harden(self, instance, limits):
        return {'memory.high': '1'}

    def execute(self, instance, argv, env=None, cwd='/work', limits=None, on_log=None,
                on_tick=None, reattach=False):
        if on_log:
            on_log(self.log)
        if on_tick and on_tick() == 'cancel':
            return Result(exit_code=-9, outcome='cancelled', seconds=1.0, usage=Usage())
        return Result(exit_code=self.exit_code, outcome=self.outcome, seconds=12.5,
                      usage=Usage(memory_peak=self.peak), evidence={'samples': []})

    def incus(self, *args, check=True, timeout=None):
        return 0, '', ''

    def destroy(self, instance):
        self.destroyed.append(instance.name)
        receipt = Receipt(run_id=instance.run_id, instance=instance.name, seconds=0.9,
                          instance_gone=True, volume_gone=True, veth_gone=True,
                          cgroup_gone=self.destroy_clean,
                          leftovers=() if self.destroy_clean else ('cgroup',))
        if not receipt.clean:
            raise DestroyIncomplete('left objects', receipt.__dict__)
        return receipt


class SuperviseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = runner.Paths(self.root).ensure()
        self.ledger = Ledger(self.paths.ledger)
        claim(self.ledger)
        self.paths.attempt('r1').mkdir(parents=True, exist_ok=True)
        (self.paths.attempt('r1') / 'toolchain.json').write_text(json.dumps(PLAN['worker']))
        self.ledger.update('r1', state='admitted', reservation_mib=2048, ceiling_mib=4096,
                           cpus_hint=2)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def run_with(self, driver):
        import os
        os.environ['PANDORA_BUDGET_MIB'] = '8192'
        return runner.supervise(self.root, 'r1', driver=driver)

    def test_a_clean_zero_exit_is_the_only_way_to_pass(self):
        result = self.run_with(FakeDriver())
        self.assertEqual(result['outcome'], 'passed')
        self.assertEqual(result['cli_exit'], 0)
        self.assertEqual(result['layer'], 'command')
        self.assertEqual(result['peak_mib'], 1000)

    def test_a_non_zero_exit_is_the_commands_failure_and_keeps_its_code(self):
        result = self.run_with(FakeDriver(outcome='failed', exit_code=7))
        self.assertEqual(result['outcome'], 'command_failed')
        self.assertEqual(result['cli_exit'], 7)
        self.assertEqual(result['layer'], 'command')

    def test_an_oom_never_reports_the_kill_signal_as_the_commands_verdict(self):
        result = self.run_with(FakeDriver(outcome='oom', exit_code=-9))
        self.assertEqual(result['outcome'], 'oom')
        self.assertEqual(result['observed_exit'], -9)
        self.assertEqual(result['cli_exit'], 70)
        self.assertEqual(result['layer'], 'watchdog')

    def test_a_timeout_is_not_a_failing_test_run(self):
        result = self.run_with(FakeDriver(outcome='timeout', exit_code=-9))
        self.assertEqual(result['outcome'], 'timed_out')
        self.assertEqual(result['cli_exit'], 70)

    def test_a_clone_failure_is_infra_and_destroys_nothing_that_was_never_made(self):
        driver = FakeDriver(explode='clone')
        result = self.run_with(driver)
        self.assertEqual(result['outcome'], 'infra_failed')
        self.assertEqual(result['layer'], 'executor')
        self.assertEqual(driver.destroyed, [])

    def test_a_passing_run_whose_machine_survives_destroy_is_not_a_pass(self):
        result = self.run_with(FakeDriver(destroy_clean=False))
        self.assertEqual(result['outcome'], 'infra_failed')
        self.assertFalse(result['receipt']['clean'])

    def test_a_cancelled_run_exits_130(self):
        self.ledger.request_cancel('r1')
        result = self.run_with(FakeDriver())
        self.assertEqual(result['outcome'], 'cancelled')
        self.assertEqual(result['cli_exit'], 130)

    def test_the_instance_is_always_destroyed(self):
        driver = FakeDriver(outcome='failed', exit_code=1)
        self.run_with(driver)
        self.assertEqual(driver.destroyed, ['run-r1'])

    def test_the_log_is_a_file_beside_the_attempt(self):
        self.run_with(FakeDriver(log='line one\n'))
        self.assertIn('line one', self.paths.log('r1').read_text())

    def test_a_declared_output_that_produced_nothing_is_missing_not_empty(self):
        result = self.run_with(FakeDriver())
        self.assertEqual(result['evidence']['collected'], {'reports': 'missing'})

    def test_running_it_twice_returns_the_first_result_rather_than_rerunning(self):
        first = self.run_with(FakeDriver())
        second = self.run_with(FakeDriver(outcome='failed', exit_code=1))
        self.assertEqual(first['outcome'], second['outcome'])


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = runner.Paths(self.root).ensure()
        self.ledger = Ledger(self.paths.ledger)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_a_run_whose_supervisor_is_gone_becomes_infra_failed_never_passed(self):
        claim(self.ledger)
        self.ledger.update('r1', state='running', supervisor_pid=999999, instance='run-r1')
        answer = runner.reconcile(self.root, driver=FakeDriver())
        self.assertEqual(answer['infra_failed'], ['r1'])
        self.assertEqual(self.ledger.get('r1')['outcome'], 'infra_failed')

    def test_a_live_supervisor_is_adopted_and_left_alone(self):
        import os
        claim(self.ledger)
        self.ledger.update('r1', state='running', supervisor_pid=os.getpid())
        answer = runner.reconcile(self.root, driver=FakeDriver())
        self.assertEqual(answer['adopted'], ['r1'])
        self.assertEqual(self.ledger.get('r1')['state'], 'running')

    def test_the_orphans_instance_is_destroyed(self):
        claim(self.ledger)
        self.ledger.update('r1', state='running', supervisor_pid=999999, instance='run-r1')
        driver = FakeDriver()
        runner.reconcile(self.root, driver=driver)
        self.assertEqual(driver.destroyed, ['run-r1'])


class RetentionTest(unittest.TestCase):
    def test_old_attempts_go_and_recent_ones_stay(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = runner.Paths(root).ensure()
            ledger = Ledger(paths.ledger)
            for name in ('old', 'new'):
                claim(ledger, request_id='req-' + name, run_id=name,
                      input_id='input-' + name)
                paths.attempt(name).mkdir(parents=True, exist_ok=True)
                ledger.finish(name, outcome='passed', exit_code=0)
            ledger.db.execute('UPDATE attempts SET finished=? WHERE run_id=?',
                              (time.time() - 200000, 'old'))
            answer = runner.retain(root, keep_seconds=3600, keep_failed_seconds=3600)
            ledger.close()
            self.assertEqual(answer['removed'], ['old'])
            self.assertTrue(paths.attempt('new').is_dir())
            self.assertFalse(paths.attempt('old').is_dir())


if __name__ == '__main__':
    unittest.main()
