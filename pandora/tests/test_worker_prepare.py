"""The optional per-clone source hook, without a live worker."""
import json
import tempfile
import tomllib
import unittest
from pathlib import Path

from pandora.config import loader
from pandora.client.local import LocalExecutor
from pandora.engine import runner
from pandora.engine.ledger import Ledger
from pandora.errors import ConfigError
from pandora.executor.interface import ExecutionFailed, Result, Usage
from pandora.tests.test_engine import FakeDriver, PLAN, claim
from pandora.tests.test_local import FakeRun, budget, plan_for


CONFIG = '''
version = 1
[repo]
name = "demo"
entrypoints = ["tool"]
[worker]
base_image = "images:ubuntu/26.04"
{prepare}
[[jobs]]
id = "suite"
forms = [{{ prefix = ["suite"] }}]
run = {{ argv = ["tool", "suite"] }}
'''


class PrepareConfigTest(unittest.TestCase):
    def test_old_configuration_has_an_empty_hook(self):
        config = loader.validate(tomllib.loads(CONFIG.format(prepare='')))
        self.assertEqual(config['worker']['prepare_command'], '')

    def test_command_is_validated_and_kept_out_of_the_golden_identity(self):
        config = loader.validate(tomllib.loads(CONFIG.format(
            prepare='prepare_command = "tool install --frozen"')))
        self.assertEqual(config['worker']['prepare_command'], 'tool install --frozen')
        self.assertEqual(runner.toolchain_of(config['worker']).fingerprint(),
                         runner.toolchain_of(loader.validate(tomllib.loads(
                             CONFIG.format(prepare='')))['worker']).fingerprint())
        with self.assertRaises(ConfigError):
            loader.validate(tomllib.loads(CONFIG.format(prepare='prepare_command = 4')))


class RecordingDriver(FakeDriver):
    def __init__(self, *, prep_outcome='ok', prep_exit=0, cancel_during_prep=False):
        super().__init__()
        self.events = []
        self.prep_outcome, self.prep_exit = prep_outcome, prep_exit
        self.cancel_during_prep = cancel_during_prep

    def inject(self, name, source, dest, method='device-rsync'):
        self.events.append('inject')
        return super().inject(name, source, dest, method=method)

    def harden(self, instance, limits):
        self.events.append('harden')
        return super().harden(instance, limits)

    def execute(self, instance, argv, env=None, cwd='/work', limits=None, on_log=None,
                on_tick=None, reattach=False):
        if argv[0] == 'bash':
            self.events.append('prepare')
            self.prep_call = (argv, cwd, limits, dict(env or {}))
            if on_log:
                on_log('dependency output\n')
            if self.cancel_during_prep and on_tick:
                # Model a cancellation observed by the running watchdog.
                self.request_cancel()
            if on_tick and on_tick() == 'cancel':
                return Result(exit_code=-9, outcome='cancelled', seconds=0.3,
                              usage=Usage(memory_peak=300 * 1048576))
            return Result(exit_code=self.prep_exit, outcome=self.prep_outcome,
                          seconds=0.3, usage=Usage(memory_peak=300 * 1048576))
        self.events.append('job')
        return super().execute(instance, argv, env=env, cwd=cwd, limits=limits,
                               on_log=on_log, on_tick=on_tick, reattach=reattach)


class PrepareRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = runner.Paths(self.root).ensure()
        self.ledger = Ledger(self.paths.ledger)
        self.addCleanup(self.ledger.close)
        claim(self.ledger)
        attempt = self.paths.attempt('r1')
        attempt.mkdir()
        (attempt / 'toolchain.json').write_text(json.dumps(
            dict(PLAN['worker'], prepare_command='tool install --frozen')))
        self.ledger.update('r1', state='admitted', reservation_mib=2048,
                           ceiling_mib=4096, cpus_hint=2)

    def run_with(self, driver):
        return runner.supervise(self.root, 'r1', driver=driver)

    def test_preparation_runs_after_injection_under_limits_before_the_job(self):
        driver = RecordingDriver()
        result = self.run_with(driver)
        self.assertEqual(result['outcome'], 'passed')
        self.assertEqual(driver.events, ['inject', 'harden', 'prepare', 'job'])
        argv, cwd, limits, _env = driver.prep_call
        self.assertEqual(argv, ['bash', '-c', 'tool install --frozen'])
        self.assertEqual(cwd, '/work')
        self.assertEqual((limits.ceiling_mib, limits.wall_seconds), (4096, 1800))
        self.assertEqual(result['durations']['prepare_command'], 0.3)
        self.assertIn('dependency output', self.paths.log('r1').read_text())
        self.assertEqual(driver.destroyed, ['run-r1'])

    def test_failed_preparation_stops_the_job_and_explains_the_exit(self):
        driver = RecordingDriver(prep_outcome='failed', prep_exit=17)
        result = self.run_with(driver)
        self.assertEqual(driver.events, ['inject', 'harden', 'prepare'])
        self.assertEqual(result['outcome'], 'infra_failed')
        self.assertEqual(result['cli_exit'], 70)
        self.assertIsNone(result['observed_exit'])
        self.assertEqual(result['evidence']['cause'], 'prepare-command-failed')
        self.assertEqual(result['evidence']['preparation']['exit_code'], 17)
        self.assertEqual(driver.destroyed, ['run-r1'])

    def test_cancellation_during_preparation_stops_the_job(self):
        driver = RecordingDriver(cancel_during_prep=True)
        driver.request_cancel = lambda: self.ledger.request_cancel('r1')
        result = self.run_with(driver)
        self.assertEqual(driver.events, ['inject', 'harden', 'prepare'])
        self.assertEqual((result['outcome'], result['cli_exit']), ('cancelled', 130))
        self.assertEqual(driver.destroyed, ['run-r1'])

    def test_executor_failure_during_preparation_is_named_and_not_retried(self):
        class BrokenDriver(RecordingDriver):
            def execute(self, instance, argv, **kwargs):
                if argv[0] == 'bash':
                    raise ExecutionFailed('could not launch preparation')
                return super().execute(instance, argv, **kwargs)

        driver = BrokenDriver()
        result = self.run_with(driver)
        self.assertEqual(result['outcome'], 'infra_failed')
        self.assertEqual(result['evidence']['cause'], 'prepare-command-execution-failed')
        self.assertFalse(hasattr(driver, 'executed'))
        self.assertEqual(driver.destroyed, ['run-r1'])

    def test_a_plan_without_the_hook_runs_only_the_job(self):
        (self.paths.attempt('r1') / 'toolchain.json').write_text(json.dumps(PLAN['worker']))
        driver = RecordingDriver()
        result = self.run_with(driver)
        self.assertEqual(result['outcome'], 'passed')
        self.assertEqual(driver.events, ['inject', 'harden', 'job'])


class LocalLaneTest(unittest.TestCase):
    def test_the_local_executor_ignores_the_worker_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = FakeRun(root / 'run')
            run.dir.mkdir()
            pool = budget()
            self.addCleanup(pool.admission.store.close)
            pool.reserve(run.id, repo='demo', job='suite', worktree=root, singleton=False)
            admission = pool.admit(run.id, repo='demo', job='suite')
            plan = plan_for(['sh', '-c', 'true'],
                            worker={'prepare_command': 'touch should-not-exist'})
            result = LocalExecutor(pool, drift='off').execute(
                run, plan, repo='demo', job='suite', worktree=root, request_env={},
                admission=admission, note=run.note)
            self.assertEqual(result['outcome'], 'passed')
            self.assertFalse((root / 'should-not-exist').exists())


if __name__ == '__main__':
    unittest.main()
