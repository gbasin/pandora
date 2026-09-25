"""The worker's concurrent-run cap follows the host unless the manifest says otherwise."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pandora.engine import runner, scheduler
from pandora.errors import ConfigError
from pandora.worker import versions


class DerivedCap(unittest.TestCase):
    def test_every_run_keeps_two_threads(self):
        self.assertEqual(runner.derived_max_running(32), 16)
        self.assertEqual(runner.derived_max_running(4), 2)
        self.assertEqual(runner.derived_max_running(5), 2)

    def test_a_tiny_host_still_admits_two(self):
        for threads in (0, 1, 2, 3, None):
            self.assertEqual(runner.derived_max_running(threads), 2)


class ConfiguredCap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = runner.Paths(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_default_derives_from_threads(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PANDORA_MAX_RUNNING', None)
            self.assertEqual(runner.max_running_of(self.paths, threads=32), (16, 'threads 32 / 2'))

    def test_manifest_value_wins_over_threads(self):
        Path(self.paths.root, 'max_running').write_text('6\n')
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PANDORA_MAX_RUNNING', None)
            self.assertEqual(runner.max_running_of(self.paths, threads=32), (6, 'manifest max_running'))

    def test_zero_in_the_file_means_derive(self):
        Path(self.paths.root, 'max_running').write_text('0\n')
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PANDORA_MAX_RUNNING', None)
            self.assertEqual(runner.max_running_of(self.paths, threads=8)[0], 4)

    def test_environment_wins_over_everything(self):
        Path(self.paths.root, 'max_running').write_text('6\n')
        with mock.patch.dict(os.environ, {'PANDORA_MAX_RUNNING': '3'}):
            self.assertEqual(runner.max_running_of(self.paths, threads=32), (3, 'PANDORA_MAX_RUNNING'))


class SchedulerDefault(unittest.TestCase):
    def test_scheduler_derives_when_not_told(self):
        s = scheduler.Scheduler(ledger=None, store=None, budget_mib=4096, cores=32)
        self.assertEqual(s.max_running, 16)
        s = scheduler.Scheduler(ledger=None, store=None, budget_mib=4096, cores=32, max_running=8)
        self.assertEqual(s.max_running, 8)


class Manifest(unittest.TestCase):
    def test_default_is_derive(self):
        self.assertEqual(versions.normalize({})['worker']['max_running'], 0)

    def test_positive_count_is_kept(self):
        self.assertEqual(versions.normalize({'worker': {'max_running': 12}})['worker']['max_running'], 12)

    def test_negative_is_refused(self):
        with self.assertRaises(ConfigError):
            versions.normalize({'worker': {'max_running': -1}})


if __name__ == '__main__':
    unittest.main()
