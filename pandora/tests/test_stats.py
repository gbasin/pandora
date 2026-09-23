"""`pandora stats`: the report is built from files, and the renderer says what it
does and does not know."""
import json
import tempfile
import unittest
from pathlib import Path

from pandora.client import stats


class Windows(unittest.TestCase):
    def test_units(self):
        now = 10_000.0
        self.assertEqual(stats.parse_since('24h', now=now), now - 86400)
        self.assertEqual(stats.parse_since('7d', now=now), now - 7 * 86400)
        self.assertEqual(stats.parse_since('90m', now=now), now - 5400)
        self.assertEqual(stats.parse_since('3600', now=now), now - 3600)

    def test_all_and_empty_mean_no_window(self):
        for text in (None, '', 'all'):
            self.assertIsNone(stats.parse_since(text))

    def test_garbage_is_an_error_not_a_different_window(self):
        with self.assertRaises(ValueError):
            stats.parse_since('yesterday')


class Percentiles(unittest.TestCase):
    def test_empty_and_single(self):
        self.assertEqual(stats.percentile([], 0.5), 0)
        self.assertEqual(stats.percentile([7], 0.95), 7)

    def test_spread(self):
        self.assertEqual(stats.spread([1, 2, 3, 4, 100]),
                         {'n': 5, 'p50': 3, 'p95': 100})


class State:
    """A state directory with a few runs and a passthrough log."""

    def __init__(self, root):
        self.root = Path(root)
        (self.root / 'runs').mkdir()

    def run(self, run_id, *, job, lane, outcome, started, queue_ms=None, execute=None,
            reason='', drifted=False, drift='warn', hint=None):
        folder = self.root / 'runs' / run_id
        folder.mkdir()
        (folder / 'meta.json').write_text(json.dumps(
            {'id': run_id, 'job': job, 'lane': lane, 'started': started,
             'queue_ms': queue_ms, 'reason': reason, 'state': 'finished'}))
        result = {'outcome': outcome, 'lane': lane, 'drifted': drifted, 'drift': drift,
                  'hint': hint}
        if execute is not None:
            if lane == 'remote':
                result['durations'] = {'execute': execute}
            else:
                result['wall_seconds'] = execute
        (folder / 'result.json').write_text(json.dumps(result))

    def passthrough(self, argv, duration_ms, ts, reason='not claimed'):
        with (self.root / 'passthrough.jsonl').open('a') as handle:
            handle.write(json.dumps({'argv': argv, 'duration_ms': duration_ms, 'ts': ts,
                                     'reason': reason}) + '\n')


class Build(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.state = State(self.home.name)
        s = self.state
        s.run('r1', job='journey', lane='remote', outcome='passed', started=100,
              queue_ms=8000, execute=50.0)
        s.run('r2', job='journey', lane='remote', outcome='oom', started=200,
              queue_ms=2000, execute=20.0, hint='raise the size class')
        s.run('r3', job='check', lane='local', outcome='passed', started=300,
              queue_ms=500, execute=40.0, reason='fallback:worker-down')
        s.run('r4', job='check', lane='local', outcome='command_failed', started=400,
              queue_ms=0, execute=10.0, reason='fallback:worker-down', drifted=True)
        s.run('r5', job='unit', lane='remote', outcome='passed', started=5,
              queue_ms=1000, execute=5.0, drifted=True, drift='fail')
        s.passthrough(['pnpm', 'test:surface', 'desk'], 200_000, ts=150)
        s.passthrough(['pnpm', 'test:surface', 'borrower-web'], 100_000, ts=250)
        s.passthrough(['pnpm', 'build'], 30_000, ts=1)

    def test_counts_by_job_lane_outcome(self):
        report = stats.build(self.state.root)
        self.assertEqual(report['runs'], 5)
        rows = {(r['job'], r['lane'], r['outcome']): r['count'] for r in report['by_job']}
        self.assertEqual(rows[('journey', 'remote', 'passed')], 1)
        self.assertEqual(rows[('check', 'local', 'passed')], 1)
        self.assertEqual(rows[('journey', 'remote', 'oom')], 1)

    def test_waits_execute_fallbacks_oom_drift(self):
        report = stats.build(self.state.root)
        self.assertEqual(report['queue_wait_seconds']['remote']['n'], 3)
        self.assertEqual(report['queue_wait_seconds']['local']['p95'], 0.5)
        execute = {(r['job'], r['lane']): r for r in report['execute_seconds']}
        self.assertEqual(execute[('journey', 'remote')]['p95'], 50.0)
        self.assertEqual(execute[('check', 'local')]['n'], 2)
        self.assertEqual(report['fallbacks'], [{'reason': 'worker-down', 'count': 2}])
        self.assertEqual(report['oom'], 1)
        self.assertEqual(report['drift'], {'warned': 1, 'failed': 1})

    def test_passthrough_is_grouped_and_worst_first(self):
        report = stats.build(self.state.root)
        rows = report['passthrough']
        self.assertEqual(rows[0]['command'], 'pnpm test:surface')
        self.assertEqual(rows[0]['runs'], 2)
        self.assertEqual(rows[0]['total_seconds'], 300.0)
        self.assertEqual(rows[1]['command'], 'pnpm build')

    def test_the_window_drops_old_runs_and_passthroughs(self):
        report = stats.build(self.state.root, since=150, window='test')
        self.assertEqual(report['runs'], 3)
        self.assertEqual(report['window'], 'test')
        self.assertEqual(sum(r['runs'] for r in report['passthrough']), 2)

    def test_a_missing_state_directory_is_an_empty_report(self):
        report = stats.build(Path(self.home.name) / 'nowhere')
        self.assertEqual(report['runs'], 0)
        self.assertEqual(report['passthrough'], [])

    def test_a_corrupt_meta_is_skipped(self):
        (self.state.root / 'runs' / 'bad').mkdir()
        (self.state.root / 'runs' / 'bad' / 'meta.json').write_text('{not json')
        self.assertEqual(stats.build(self.state.root)['runs'], 5)


class Render(unittest.TestCase):
    def report(self, **over):
        base = {'window': '24h', 'runs': 1,
                'by_job': [{'job': 'journey', 'lane': 'remote', 'outcome': 'passed', 'count': 1}],
                'queue_wait_seconds': {'local': stats.spread([]), 'remote': stats.spread([3.0])},
                'execute_seconds': [dict({'job': 'journey', 'lane': 'remote'},
                                         **stats.spread([50.0]))],
                'fallbacks': [], 'oom': 0, 'drift': {'warned': 0, 'failed': 0},
                'pause': {}, 'local': {}, 'passthrough': [], 'worker': {}}
        base.update(over)
        return base

    def test_the_text_names_the_window_and_the_pre_accept_wait(self):
        text = stats.render(self.report())
        self.assertIn('window: 24h, 1 routed run(s)', text)
        self.assertIn('pre-accept wait', text)
        self.assertIn('remote p50   3.00s', text)
        self.assertIn('worker: not polled', text)

    def test_flags_appear_only_when_nonzero(self):
        quiet = stats.render(self.report())
        self.assertNotIn('oom kills', quiet)
        loud = stats.render(self.report(oom=2, fallbacks=[{'reason': 'worker-down', 'count': 3}],
                                        drift={'warned': 1, 'failed': 0}))
        self.assertIn('fallbacks: worker-down x3', loud)
        self.assertIn('oom kills: 2', loud)
        self.assertIn('drift: 1 warning(s), 0 failure(s)', loud)

    def test_the_pause_gate_line(self):
        text = stats.render(self.report(pause={'enabled': True, 'paused': True,
                                               'evidence': 'swap +900 MiB', 'episodes': 2,
                                               'paused_seconds': 31, 'jobs_delayed': 4,
                                               'jobs_refused': 1}))
        self.assertIn('pause gate: PAUSED (swap +900 MiB), 2 episode(s), 31s paused, '
                      '4 delayed, 1 refused', text)

    def test_a_down_worker_is_a_line_not_a_silence(self):
        lines = stats.render_worker({'worker': 'down', 'reason': 'ssh timed out'})
        self.assertEqual(lines, ['worker: unreachable (ssh timed out)'])

    def test_a_reachable_worker_shows_disk_goldens_ready_and_drift(self):
        lines = stats.render_worker({
            'worker': 'reachable',
            'health': {'ok': False, 'reason': 'kernel is 7.0.0-31; the canary passed on 7.0.0-14',
                       'state': 'ready', 'ready_since': '2026-09-22',
                       'capacity': {'ok': True, 'free_gib': 8.6, 'floor_gib': 4},
                       'goldens': ['golden-a'], 'canary': {'ok': True},
                       'kernel_drift': True, 'kernel': '7.0.0-31', 'canary_kernel': '7.0.0-14',
                       'scheduler': {'held_mib': 3200, 'budget_mib': 13000, 'lanes': 1},
                       'outcomes': [{'outcome': 'passed', 'state': 'finished', 'count': 9}]}})
        text = '\n'.join(lines)
        self.assertIn('worker: reachable -- kernel is 7.0.0-31', text)
        self.assertIn('scheduler: 3200 MiB held of 13000, 1 lane(s)', text)
        self.assertIn('disk: 8.60 GiB free (floor 4 GiB)', text)
        self.assertIn('goldens: golden-a', text)
        self.assertIn('ready: ready since 2026-09-22; last canary pass', text)
        self.assertIn('kernel drift: running 7.0.0-31, the canary passed on 7.0.0-14', text)
        self.assertIn('ledger: passed         finished   9', text)

    def test_below_floor_is_shouted(self):
        lines = stats.render_worker({'worker': 'degraded', 'health': {
            'ok': False, 'reason': 'below floor', 'capacity': {'ok': False, 'free_gib': 1.2,
                                                                'floor_gib': 4}}})
        self.assertIn('disk: 1.20 GiB free, BELOW FLOOR (floor 4 GiB)', '\n'.join(lines))


if __name__ == '__main__':
    unittest.main()
