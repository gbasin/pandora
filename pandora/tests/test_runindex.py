"""`pandora stats` reads only its window, and old finished runs are pruned.

On 2026-09-24 reading all 210 `meta.json` files on each call took 11.5 s at
load 28. `ps` answers from the published status since #110; `stats` still
reported from every row.
"""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pandora.client import runindex, settings, stats
from pandora.client.daemon import Run
from pandora.errors import ConfigError
from pandora.tests.test_fallback import DaemonCase

DAY = 86400.0


def write_row(runs, run_id, **fields):
    folder = Path(runs) / run_id
    folder.mkdir(parents=True)
    row = dict({'id': run_id, 'state': 'passed', 'lane': 'remote', 'argv': ['pnpm', 'x'],
                'started': time.time(), 'exit_code': 0}, **fields)
    (folder / 'meta.json').write_text(json.dumps(row) + '\n')
    return folder


class Counting:
    """`RunIndex.read`, counting the files it opens."""

    def __init__(self):
        self.paths = []

    def __call__(self, path):
        self.paths.append(Path(path))
        return runindex.read_json(path)

    def metas(self):
        return [path for path in self.paths if path.name == 'meta.json']


class AStatsWithAThousandRows(DaemonCase):
    ROWS = 1000

    def setUp(self):
        super().setUp()
        runs = self.state / 'runs'
        now = time.time()
        # Written after the daemon started, so its startup read did not see them:
        # the index finds them by listing the directory.
        for number in range(self.ROWS):
            stamp = now - (self.ROWS - number) * 60
            folder = write_row(runs, 'row%04d' % number, started=stamp)
            (folder / 'result.json').write_text(json.dumps({'outcome': 'passed'}))
            os.utime(folder, (stamp, stamp))
        self.counting = Counting()
        self.daemon.index.read = self.counting

    def test_a_window_reads_only_its_rows_and_a_second_report_reads_none(self):
        report = self.daemon.stats('30m')
        self.assertEqual(report['runs'], 29)
        self.assertLessEqual(len(self.counting.paths), 2 * 30 + 2)
        before = len(self.counting.paths)
        self.daemon.stats('30m')
        self.assertEqual(len(self.counting.paths), before)
        self.assertEqual(report['retention']['keep_days'], 7)


class TheIndex(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.state = Path(home.name)
        self.runs = self.state / 'runs'
        self.runs.mkdir()

    def test_a_finished_row_is_cached_and_a_live_one_is_not(self):
        index = runindex.RunIndex(self.runs)
        index.learn({'id': 'a', 'state': 'running', 'started': 5})
        self.assertNotIn('a', index.rows)
        index.learn({'id': 'a', 'state': 'passed', 'started': 5})
        self.assertEqual(index.rows['a']['state'], 'passed')

    def test_a_removed_directory_leaves_the_index(self):
        write_row(self.runs, 'a')
        index = runindex.RunIndex(self.runs)
        self.assertEqual(index.newest(), ['a'])
        (self.runs / 'a' / 'meta.json').unlink()
        (self.runs / 'a').rmdir()
        self.assertEqual(index.newest(), [])

    def result_file(self, run_id='a', **result):
        folder = write_row(self.runs, run_id)
        path = folder / 'result.json'
        path.write_text(json.dumps(result))
        return path

    def test_stats_retains_no_full_evidence_and_full_result_stays_available(self):
        detail = 'observation' * 100_000
        self.result_file(input_id='source', outcome='passed',
                         test_evidence={'tests': detail}, verdict={'tests': detail},
                         evidence={'cause': 'queue-timeout', 'tests': detail},
                         attempts=[{'cause': 'worker-lost', 'log': detail}],
                         flaky={'order': ['failed', 'passed'], 'shards': [detail, detail]})
        index = runindex.RunIndex(self.runs)
        report = stats.build(self.state, runs=index.history())
        self.assertEqual(report['worker_queue']['timeouts'], 1)
        self.assertEqual(report['retries']['causes'], {'worker-lost': 1})
        self.assertEqual(report['flaky'], {'pairs': 1, 'shard_pairs': 2})
        self.assertLess(len(json.dumps(index.results)), 1000)
        self.assertEqual(index.result('a')['input_id'], 'source')
        self.assertEqual(index.result('a')['test_evidence']['tests'], detail)

    def test_changed_missing_and_repaired_result_invalidate_summary(self):
        path = self.result_file(outcome='passed')
        index = runindex.RunIndex(self.runs)

        def outcome():
            return stats.build(self.state, runs=index.history())['by_job'][0]['outcome']

        self.assertEqual(outcome(), 'passed')
        path.write_text(json.dumps({'outcome': 'command_failed'}))
        self.assertEqual(outcome(), 'command_failed')
        path.unlink()
        self.assertEqual(outcome(), 'passed')  # meta state, not the cached failure
        self.assertEqual(index.result_bytes, 0)
        path.write_text('{broken')
        self.assertEqual(outcome(), 'passed')
        path.write_text(json.dumps({'outcome': 'oom'}))
        self.assertEqual(outcome(), 'oom')
        index.forget('a')
        self.assertEqual(index.results, {})
        self.assertEqual(index.result_bytes, 0)

    def test_beyond_capacity_keeps_newest_hits_instead_of_thrashing(self):
        for n in range(6):
            self.result_file(str(n), outcome='passed')
        counting = Counting()
        index = runindex.RunIndex(self.runs, read=counting)
        with mock.patch.object(runindex, 'RESULT_CACHE_ENTRIES', 3):
            first = stats.build(self.state, runs=index.history())
            self.assertEqual(set(index.results), set(index.newest()[:3]))
            counting.paths.clear()
            self.assertEqual(stats.build(self.state, runs=index.history()), first)
            self.assertEqual(len(counting.paths), 3)  # only uncached older results
            self.assertEqual(len(index.results), 3)

    def test_byte_cap_does_not_change_report_and_oversize_result_is_not_cached(self):
        self.result_file(outcome='passed', hint_rule='large-rule' * 1000)
        index = runindex.RunIndex(self.runs)
        with mock.patch.object(runindex, 'RESULT_CACHE_BYTES', 128):
            self.assertEqual(stats.build(self.state, runs=index.history()),
                             stats.build(self.state))
        self.assertEqual(index.results, {})
        self.assertEqual(index.result_bytes, 0)

    def test_prune_during_result_read_cannot_resurrect_cached_data(self):
        self.result_file(outcome='passed')
        index = runindex.RunIndex(self.runs)

        def read(path):
            value = runindex.read_json(path)
            if path.name == 'result.json':
                index.forget('a')
            return value

        index.read = read
        self.assertEqual(stats.build(self.state, runs=index.history())['runs'], 1)
        self.assertEqual(index.results, {})
        self.assertEqual(index.result_bytes, 0)


class Pruning(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.state = Path(home.name)
        self.runs = self.state / 'runs'
        self.runs.mkdir()
        self.now = time.time() + 8 * DAY          # every file here is 8 days old then
        long_ago = time.time() - 30 * DAY
        write_row(self.runs, 'old', started=long_ago, updated=long_ago)
        write_row(self.runs, 'old-live', state='running', started=long_ago, updated=long_ago)
        write_row(self.runs, 'driven', started=long_ago, updated=long_ago)
        # Started long ago, finished within the retention: young by one date.
        write_row(self.runs, 'recent', started=long_ago, updated=self.now - DAY)
        conflicted = write_row(self.runs, 'conflicted', started=long_ago, updated=long_ago)
        (conflicted / 'result.json').write_text(json.dumps(
            {'writeback': {'state': 'conflicted', 'conflicts': [{'path': 'x'}]}}))
        (self.runs / 'unreadable').mkdir()
        (self.runs / 'unreadable' / 'meta.json').write_text('{not json')
        # A run that died before its first save: a directory, a log, no meta.json.
        (self.runs / 'nometa').mkdir()
        (self.runs / 'nometa' / 'log').write_text('')

    def test_only_old_finished_rows_nobody_holds_are_removed(self):
        index = runindex.RunIndex(self.runs)
        index.newest()
        removed = runindex.prune(self.state, 7 * DAY, live={'driven'}, now=self.now,
                                 index=index)
        self.assertEqual(removed, ['nometa', 'old'])
        left = sorted(path.name for path in self.runs.iterdir())
        self.assertEqual(left, ['conflicted', 'driven', 'old-live', 'recent', 'unreadable'])
        self.assertNotIn('old', index.dates)
        self.assertEqual(list((self.state / runindex.TRASH).iterdir()), [])

    def test_nothing_is_old_enough_to_go_and_nothing_young_is_read(self):
        counting = Counting()
        self.assertEqual(runindex.prune(self.state, 7 * DAY, now=time.time(), read=counting), [])
        self.assertEqual(len(list(self.runs.iterdir())), 7)
        self.assertEqual(counting.paths, [], 'a row younger than the retention was read')

    def test_zero_keeps_everything(self):
        self.assertEqual(runindex.prune(self.state, 0, now=self.now), [])
        self.assertEqual(runindex.keep_seconds({'client': {'keep_runs_days': 0}}), 0)
        self.assertEqual(runindex.keep_seconds({'client': {}}), 7 * DAY)

    def test_the_setting_is_checked(self):
        self.assertEqual(settings.normalize({})['client']['keep_runs_days'], 7)
        self.assertEqual(settings.normalize({'client': {'keep_runs_days': 2.5}})
                         ['client']['keep_runs_days'], 2.5)
        for bad in (-1, 'week', True):
            with self.assertRaises(ConfigError):
                settings.normalize({'client': {'keep_runs_days': bad}})


class TheDaemonPrunes(DaemonCase):
    def test_at_start_and_on_its_own_never_a_live_row(self):
        long_ago = time.time() - 30 * DAY
        write_row(self.state / 'runs', 'old', started=long_ago, updated=long_ago)
        run = Run(self.state, 'mine', {'argv': ['pnpm', 'x']}, on_save=self.daemon.saved)
        run.state = 'running'
        run.save()
        with self.daemon.runs_lock:
            self.daemon.runs[run.id] = run
        self.addCleanup(run.finish, 0)
        self.daemon.status.update(json.loads(
            (self.state / 'runs' / 'old' / 'meta.json').read_text()))
        removed = self.daemon.prune(now=time.time() + 30 * DAY)
        self.assertEqual(removed, ['old'])
        self.assertNotIn('old', [row['id'] for row in self.daemon.ps(200)])
        self.assertTrue((self.state / 'runs' / 'mine').is_dir())
        self.assertTrue(any(thread.name == 'prune' for thread in threading.enumerate()))

    def test_one_line_at_start_says_pruning_is_on_and_the_retention(self):
        from pandora.client import daemon as daemon_module
        said, stopping = [], self.daemon.stopping
        self.daemon.stopping = threading.Event()
        self.daemon.stopping.set()
        self.addCleanup(setattr, self.daemon, 'stopping', stopping)
        with mock.patch.object(daemon_module, 'log', said.append):
            self.daemon.prune_loop()
        self.assertTrue(said[0].startswith('prune: on: finished runs older than 7 day(s)'),
                        said)


class Stats(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.state = Path(home.name)
        self.runs = self.state / 'runs'
        now = time.time()
        for number in range(100):
            folder = write_row(self.runs, 'r%03d' % number, job='unit',
                               started=now - (100 - number) * 3600)
            (folder / 'result.json').write_text(json.dumps({'outcome': 'passed'}))
            stamp = now - (100 - number) * 3600
            os.utime(folder, (stamp, stamp))

    def test_a_window_reads_only_its_rows(self):
        counting = Counting()
        index = runindex.RunIndex(self.runs, read=counting)
        pairs = list(index.history(since=time.time() - 10.5 * 3600))
        self.assertEqual(len(pairs), 10)
        opened = counting.paths
        self.assertLessEqual(len(opened), 2 * 10 + 2, opened)

    def test_the_report_says_what_history_it_can_see(self):
        index = runindex.RunIndex(self.runs)
        report = stats.build(self.state, since=stats.parse_since('30d'), window='30d',
                             runs=index.history(stats.parse_since('30d')),
                             retention={'keep_days': 7, 'oldest': index.oldest()})
        self.assertEqual(report['runs'], 100)
        self.assertTrue(report['retention']['clipped'])
        text = stats.render(report)
        self.assertIn('more than 7 day(s) ago are removed', text)
        self.assertIn('reaches further back', text)
        report = stats.build(self.state, since=stats.parse_since('24h'), window='24h',
                             runs=index.history(stats.parse_since('24h')),
                             retention={'keep_days': 7, 'oldest': index.oldest()})
        self.assertFalse(report['retention']['clipped'])
        self.assertNotIn('reaches further back', stats.render(report))

    def test_the_daemon_report_carries_the_retention(self):
        report = stats.build(self.state, retention={'keep_days': 0, 'oldest': None})
        self.assertIn('every run is kept', stats.render(report))


if __name__ == '__main__':
    unittest.main()
