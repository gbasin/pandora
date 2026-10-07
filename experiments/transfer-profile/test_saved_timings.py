"""Saved-record audit regressions over scratch records, never live state."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('saved_timings', Path(__file__).with_name('saved_timings.py'))
audit_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_module)
SINCE = '2026-10-06T22:04:00+00:00'


class SavedTimingAudit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        (self.state / 'runs').mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, name, value, result=None):
        run = self.state / 'runs' / name
        run.mkdir()
        (run / 'meta.json').write_text(json.dumps(value))
        if result is not None:
            (run / 'result.json').write_text(json.dumps(result))
        return run

    def meta(self, repo='alpha', **fields):
        return {'repo': repo, 'started': audit_module.cutoff(SINCE), **fields}

    def audit(self, **options):
        return audit_module.audit(self.state, SINCE, **options)

    def test_repository_selection_and_interpolated_percentiles(self):
        for index, seconds in enumerate((1, 3, 5, 7)):
            self.record(str(index), self.meta(pre_accept={'freeze': seconds, 'ship': seconds * 2}))
        self.record('other', self.meta('beta', pre_accept={'freeze': 60}))
        report = self.audit()
        self.assertEqual(set(report['repositories']), {'alpha', 'beta'})
        freeze = report['repositories']['alpha']['all_retained']['freeze']
        self.assertEqual(freeze, {'count': 4, 'p50': 4, 'p95': 6.7, 'max': 7})
        self.assertEqual(len(report['repositories']['beta']['slow_capture_samples']), 1)
        self.assertEqual(set(self.audit(repos=['beta', 'beta'])['repositories']), {'beta'})

    def test_malformed_records_and_timing_maps_are_counted(self):
        self.record('outer', [], result=[])
        bad = self.record('syntax', self.meta())
        (bad / 'meta.json').write_text('{')
        self.record('maps', self.meta(pre_accept=[], freeze_steps='invalid'))
        (self.state / 'runs' / 'missing').mkdir()
        report = self.audit()
        counters = report['record_counters']
        self.assertEqual(counters['malformed_records'], 3)
        self.assertEqual(counters['unreadable_records'], 1)
        self.assertEqual(counters['malformed_timing_maps'], 2)
        self.assertEqual(report['repositories']['alpha']['all_retained']['freeze']['count'], 0)

    def test_boolean_negative_and_nonfinite_times_are_excluded(self):
        invalid = [True, -1, float('nan'), float('inf'), '2']
        for index, value in enumerate(invalid):
            self.record('started%d' % index, self.meta(started=value))
            self.record('timing%d' % index, self.meta(pre_accept={'freeze': value},
                                                    freeze_steps={'pass1.entries': value}))
        self.record('valid', self.meta(pre_accept={'freeze': 0, 'ship': 2},
                                      freeze_steps={'pass1.entries': .1, 'pass2.entries': .2}))
        report = self.audit()
        self.assertEqual(report['record_counters']['invalid_started_values'], 5)
        self.assertEqual(report['record_counters']['invalid_timing_values'], 10)
        self.assertEqual(report['repositories']['alpha']['all_retained']['freeze']['count'], 1)
        json.dumps(report, allow_nan=False)

    def test_symlink_runs_and_records_are_skipped(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'meta.json').write_text(json.dumps(self.meta('external', pre_accept={'freeze': 999})))
        (self.state / 'runs' / 'external').symlink_to(outside, target_is_directory=True)
        run = self.record('internal', self.meta())
        (run / 'result.json').symlink_to(outside / 'meta.json')
        report = self.audit()
        self.assertEqual(set(report['repositories']), {'alpha'})
        self.assertEqual(report['result_count'], 0)
        self.assertEqual(report['record_counters']['symlink_runs_skipped'], 1)
        self.assertEqual(report['record_counters']['symlink_records_skipped'], 1)

    def test_cutoff_requires_an_explicit_timezone(self):
        for value in ('2026-10-06T22:04:00', '2026-10-06', 'invalid'):
            with self.assertRaises(argparse.ArgumentTypeError):
                audit_module.cutoff(value)
        self.assertEqual(audit_module.cutoff(SINCE), audit_module.cutoff('2026-10-06T22:04:00Z'))

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'named pipes unavailable')
    def test_fifo_records_are_skipped_without_opening_them(self):
        fifo_meta = self.state / 'runs' / 'fifo-meta'
        fifo_meta.mkdir()
        os.mkfifo(fifo_meta / 'meta.json')
        fifo_result = self.record('fifo-result', self.meta())
        os.mkfifo(fifo_result / 'result.json')
        # Failing any attempted content read also avoids hanging if the
        # regular-file guard regresses: only the ordinary meta may be read.
        original = Path.read_text

        def read(path, *args, **kwargs):
            self.assertEqual(path, fifo_result / 'meta.json')
            return original(path, *args, **kwargs)

        from unittest import mock
        with mock.patch.object(Path, 'read_text', read):
            report = self.audit()
        self.assertEqual(report['record_counters']['nonregular_records_skipped'], 2)
        self.assertEqual(report['metadata_count'], 1)
        self.assertEqual(report['result_count'], 0)


if __name__ == '__main__':
    unittest.main()
