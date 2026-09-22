"""The local lane, with fake commands.

Everything here runs real processes, because the properties being tested are
about processes: that a cancel reaches a grandchild, that a peak is observed,
that two worktrees queue behind one budget. Nothing here needs a worker, a
container or a repository -- the commands are `sh -c` one-liners.
"""
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from pandora.client import local
from pandora.client.local import Budget, Busy, LocalExecutor, Supervisor, cli_exit
from pandora.engine.admission import Store


def plan_for(argv, **extra):
    plan = {'argv': list(argv), 'cwd': '.', 'env': {}, 'env_unset': [], 'env_passthrough': [],
            'secrets_exclude_globs': [], 'timeout_minutes': 5, 'args': [], 'outputs': []}
    plan.update(extra)
    return plan


class FakeRun:
    """Enough of the daemon's `Run` for the executor: an id, a log, a flag."""

    def __init__(self, directory, run_id='r0'):
        self.id = run_id
        self.dir = Path(directory)
        self.cancelled = threading.Event()
        self.chunks = []
        self.notes = []
        self.started = time.time()

    def stream_local(self, which, chunk):
        self.chunks.append((which, chunk))

    def note(self, text):
        self.notes.append(text)

    def output(self):
        return b''.join(chunk for _which, chunk in self.chunks).decode()


def budget(**config):
    settings = {'budget_mib': 4096, 'max_running': 4, 'one_active_per_worktree': True}
    settings.update(config)
    return Budget(settings, store=Store(':memory:'))


class BudgetRules(unittest.TestCase):
    def test_one_active_per_worktree_refuses_rather_than_queues(self):
        pool = budget()
        pool.reserve('a', repo='eichler', job='check', worktree='.', singleton=False)
        with self.assertRaises(Busy) as caught:
            pool.reserve('b', repo='eichler', job='unit', worktree='.', singleton=False)
        self.assertIn('already has an active local job', str(caught.exception))
        self.assertIn('a', str(caught.exception))

    def test_the_rule_is_configurable_off(self):
        pool = budget(one_active_per_worktree=False)
        pool.reserve('a', repo='eichler', job='check', worktree='.', singleton=False)
        pool.reserve('b', repo='eichler', job='unit', worktree='.', singleton=False)

    def test_two_worktrees_are_independent(self):
        pool = budget()
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            pool.reserve('a', repo='eichler', job='check', worktree=one, singleton=False)
            pool.reserve('b', repo='eichler', job='check', worktree=two, singleton=False)

    def test_a_singleton_owns_the_machine(self):
        pool = budget()
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            pool.reserve('a', repo='eichler', job='dev-stack', worktree=one, singleton=True)
            with self.assertRaises(Busy) as caught:
                pool.reserve('b', repo='eichler', job='dev-stack', worktree=two, singleton=True)
        self.assertIn('pandora cancel a', str(caught.exception))

    def test_releasing_frees_both_holds(self):
        pool = budget()
        pool.reserve('a', repo='eichler', job='dev-stack', worktree='.', singleton=True)
        pool.admit('a', repo='eichler', job='dev-stack')
        pool.finish('a', 100, 'ok')
        pool.reserve('b', repo='eichler', job='dev-stack', worktree='.', singleton=True)

    def test_a_cold_job_reserves_its_whole_class_and_a_second_waits(self):
        # A cold `medium` reserves its whole 4096 MiB ceiling, so a 4096 MiB
        # budget holds exactly one of them and the second has to wait.
        pool = budget(budget_mib=4096)
        pool.reserve('a', repo='eichler', job='check', worktree='/a', singleton=False)
        pool.reserve('b', repo='eichler', job='check', worktree='/b', singleton=False)
        self.assertIsNotNone(pool.admit('a', repo='eichler', job='check'))
        self.assertIsNone(pool.admit('b', repo='eichler', job='check', timeout=0.2))

    def test_a_release_wakes_the_waiter(self):
        pool = budget(budget_mib=4096)
        pool.reserve('a', repo='eichler', job='check', worktree='/a', singleton=False)
        pool.reserve('b', repo='eichler', job='check', worktree='/b', singleton=False)
        pool.admit('a', repo='eichler', job='check')
        admitted = []
        waiter = threading.Thread(
            target=lambda: admitted.append(
                pool.admit('b', repo='eichler', job='check', timeout=10)))
        waiter.start()
        time.sleep(0.3)
        self.assertEqual(admitted, [])
        pool.finish('a', 512, 'ok')
        waiter.join(timeout=10)
        self.assertTrue(admitted[0]['admitted'])

    def test_the_budget_defaults_to_ram_minus_the_reserve(self):
        total = local.total_memory_mib()
        self.assertEqual(local.budget_from({'reserve_mib': 1024}), total - 1024)
        self.assertEqual(local.budget_from({'budget_mib': 777}), 777)

    def test_a_job_that_can_never_fit_is_refused_rather_than_queued(self):
        pool = budget(budget_mib=1024)
        pool.reserve('a', repo='eichler', job='check', worktree='/a', singleton=False)
        with self.assertRaises(Busy) as caught:
            pool.admit('a', repo='eichler', job='check')
        self.assertIn('budget_mib', str(caught.exception))

    def test_a_cancelled_wait_is_not_an_admission(self):
        pool = budget(budget_mib=4096)
        pool.reserve('a', repo='eichler', job='check', worktree='/a', singleton=False)
        pool.reserve('b', repo='eichler', job='check', worktree='/b', singleton=False)
        pool.admit('a', repo='eichler', job='check')
        self.assertIsNone(pool.admit('b', repo='eichler', job='check',
                                     cancelled=lambda: True, timeout=10))


class SupervisorBehaviour(unittest.TestCase):
    def test_it_streams_both_pipes_and_reports_the_exit(self):
        seen = []
        supervisor = Supervisor(['sh', '-c', 'echo out; echo err >&2; exit 3'],
                                cwd='.', env=dict(os.environ), timeout_seconds=30,
                                on_log=lambda which, chunk: seen.append((which, chunk)))
        outcome, code = supervisor.run(lambda: False)
        self.assertEqual((outcome, code), ('command_failed', 3))
        text = b''.join(chunk for _which, chunk in seen).decode()
        self.assertIn('out', text)
        self.assertIn('err', text)
        self.assertEqual({which for which, _chunk in seen}, {'out', 'err'})

    def test_a_zero_exit_is_the_only_pass(self):
        supervisor = Supervisor(['sh', '-c', 'exit 0'], cwd='.', env=dict(os.environ),
                                timeout_seconds=30, on_log=lambda *_: None)
        self.assertEqual(supervisor.run(lambda: False), ('passed', 0))

    def test_a_missing_command_is_infrastructure_not_a_failing_test(self):
        seen = []
        supervisor = Supervisor(['/nonexistent/pandora-test'], cwd='.', env=dict(os.environ),
                                timeout_seconds=30,
                                on_log=lambda which, chunk: seen.append(chunk))
        outcome, code = supervisor.run(lambda: False)
        self.assertEqual(outcome, 'infra_failed')
        self.assertIsNone(code)
        self.assertEqual(cli_exit(outcome, code), 70)

    def test_a_cancel_reaches_the_whole_process_group(self):
        with tempfile.TemporaryDirectory() as home:
            marker = Path(home) / 'grandchild-alive'
            # The child backgrounds a grandchild and waits. Killing only the
            # child would leave the grandchild writing to the marker forever.
            script = ('sh -c \'while :; do date >> %s; sleep 0.1; done\' & '
                      'echo started; wait' % marker)
            supervisor = Supervisor(['sh', '-c', script], cwd=home, env=dict(os.environ),
                                    timeout_seconds=60, on_log=lambda *_: None)
            stop = threading.Event()
            threading.Timer(1.0, stop.set).start()
            outcome, _code = supervisor.run(stop.is_set)
            self.assertEqual(outcome, 'cancelled')
            self.assertTrue(marker.is_file())
            size = marker.stat().st_size
            time.sleep(0.6)
            self.assertEqual(marker.stat().st_size, size,
                             'the grandchild outlived the cancel')

    def test_a_timeout_is_not_a_command_failure(self):
        supervisor = Supervisor(['sh', '-c', 'sleep 30'], cwd='.', env=dict(os.environ),
                                timeout_seconds=0.5, on_log=lambda *_: None)
        outcome, _code = supervisor.run(lambda: False)
        self.assertEqual(outcome, 'timed_out')
        self.assertEqual(cli_exit(outcome, -15), 70)

    def test_a_peak_is_observed_for_the_group(self):
        # ~120 MiB held for a second, which the one-second sampler must see.
        script = ("python3 -c \"import time; x=bytearray(120*1024*1024); "
                  "time.sleep(2.5); print(len(x))\"")
        supervisor = Supervisor(['sh', '-c', script], cwd='.', env=dict(os.environ),
                                timeout_seconds=60, on_log=lambda *_: None)
        outcome, _code = supervisor.run(lambda: False)
        self.assertEqual(outcome, 'passed')
        self.assertGreater(supervisor.peak_mib, 80)

    def test_group_rss_sums_only_the_named_group(self):
        fake = lambda *a, **k: subprocess.CompletedProcess(
            a, 0, stdout='100 2048\n100 1024\n200 999999\nbroken\n', stderr='')
        self.assertEqual(local.group_rss_mib(100, run=fake), 3)


class ChildEnvironment(unittest.TestCase):
    def test_the_recursion_guard_is_always_set(self):
        env = local.child_environment(plan_for(['x']), {}, cpus_hint=2,
                                      run_id='r1', directory='/tmp/r1')
        self.assertEqual(env['PANDORA_ROUTE_DEPTH'], '1')
        self.assertEqual(env['PANDORA_CPUS'], '2')
        self.assertEqual(env['PANDORA_LOCAL'], '1')

    def test_the_job_declares_and_unsets(self):
        plan = plan_for(['x'], env={'CI': 'true', 'KEEP': '1'}, env_unset=['KEEP'],
                        env_passthrough=['TZ'])
        env = local.child_environment(plan, {'TZ': 'UTC', 'SECRET': 'no'}, cpus_hint=1,
                                      run_id='r1', directory='/tmp/r1')
        self.assertEqual(env['CI'], 'true')
        self.assertNotIn('KEEP', env)
        self.assertEqual(env['TZ'], 'UTC')
        self.assertNotIn('SECRET', env)


class ExecutorEndToEnd(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.root = Path(self.home.name)

    def git_worktree(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'config', 'user.email', 'a@b.c'], check=True)
        subprocess.run(['git', '-C', str(self.root), 'config', 'user.name', 'a'], check=True)
        (self.root / 'file.txt').write_text('one\n')
        subprocess.run(['git', '-C', str(self.root), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(self.root), 'commit', '-qm', 'one'], check=True)
        return self.root

    def execute(self, argv, *, drift='warn', worktree=None, run_id='r0'):
        worktree = worktree or self.root
        pool = budget()
        executor = LocalExecutor(pool, drift=drift)
        run = FakeRun(self.root / 'run', run_id)
        run.dir.mkdir(parents=True, exist_ok=True)
        pool.reserve(run.id, repo='eichler', job='fake', worktree=worktree, singleton=False)
        admission = pool.admit(run.id, repo='eichler', job='fake')
        result = executor.execute(run, plan_for(argv), repo='eichler', job='fake',
                                  worktree=worktree, request_env={}, admission=admission,
                                  note=run.note)
        return run, result, pool

    def test_a_passing_command_publishes_a_clean_receipt(self):
        run, result, pool = self.execute(['sh', '-c', 'echo hello'])
        self.assertEqual(result['outcome'], 'passed')
        self.assertEqual(result['cli_exit'], 0)
        self.assertEqual(result['lane'], 'local')
        self.assertIn('hello', run.output())
        self.assertEqual(pool.snapshot()['held_mib'], 0)
        self.assertIsNotNone(result['learned'])

    def test_a_failing_command_keeps_its_own_exit(self):
        _run, result, _pool = self.execute(['sh', '-c', 'exit 42'])
        self.assertEqual(result['outcome'], 'command_failed')
        self.assertEqual(result['cli_exit'], 42)

    def test_drift_is_a_note_by_default(self):
        self.git_worktree()
        _run, result, _pool = self.execute(
            ['sh', '-c', 'echo two >> file.txt'], drift='warn')
        self.assertTrue(result['drifted'])
        self.assertEqual(result['outcome'], 'passed')
        self.assertEqual(result['cli_exit'], 0)

    def test_drift_can_be_made_a_refusal(self):
        self.git_worktree()
        run, result, _pool = self.execute(
            ['sh', '-c', 'echo two >> file.txt'], drift='fail')
        self.assertTrue(result['drifted'])
        self.assertEqual(result['outcome'], 'drifted')
        self.assertEqual(result['cli_exit'], 75)
        self.assertTrue(any('no longer exists' in note for note in run.notes))

    def test_a_still_tree_never_drifts(self):
        self.git_worktree()
        _run, result, _pool = self.execute(['sh', '-c', 'true'], drift='fail')
        self.assertFalse(result['drifted'])
        self.assertIsNotNone(result['source_before'])
        self.assertEqual(result['source_before'], result['source_after'])

    def test_drift_off_skips_the_freeze_entirely(self):
        self.git_worktree()
        _run, result, _pool = self.execute(['sh', '-c', 'true'], drift='off')
        self.assertIsNone(result['source_before'])
        self.assertFalse(result['drifted'])

    def test_a_non_git_worktree_is_not_reported_as_drifted(self):
        _run, result, _pool = self.execute(['sh', '-c', 'true'], drift='fail')
        self.assertIsNone(result['source_before'])
        self.assertFalse(result['drifted'])

    def test_a_cancelled_run_exits_130_and_releases_its_holds(self):
        pool = budget()
        executor = LocalExecutor(pool, drift='off')
        run = FakeRun(self.root / 'run')
        run.dir.mkdir(parents=True, exist_ok=True)
        pool.reserve(run.id, repo='eichler', job='fake', worktree=self.root, singleton=False)
        admission = pool.admit(run.id, repo='eichler', job='fake')
        threading.Timer(0.8, run.cancelled.set).start()
        result = executor.execute(run, plan_for(['sh', '-c', 'sleep 30']), repo='eichler',
                                  job='fake', worktree=self.root, request_env={},
                                  admission=admission, note=run.note)
        self.assertEqual(result['outcome'], 'cancelled')
        self.assertEqual(result['cli_exit'], 130)
        self.assertEqual(pool.snapshot()['running'], [])


class ExitContract(unittest.TestCase):
    def test_no_outcome_without_a_verdict_ever_exits_zero(self):
        for outcome in ('cancelled', 'timed_out', 'infra_failed', 'drifted', 'oom'):
            self.assertNotEqual(cli_exit(outcome, 0), 0, outcome)

    def test_a_command_verdict_is_passed_through(self):
        self.assertEqual(cli_exit('passed', 0), 0)
        self.assertEqual(cli_exit('command_failed', 7), 7)
        self.assertEqual(cli_exit('command_failed', None), 70)


if __name__ == '__main__':
    unittest.main()
