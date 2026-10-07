"""Transfer evidence records observed work without changing publication outcomes."""
import os
import subprocess
import unittest
from unittest import mock

from pandora.errors import TransferError
from pandora.snapshot import transfer


OUTPUT = b'''Number of regular files transferred: 2
Total transferred file size: 1,234 bytes
Literal data: 1 000 bytes
Matched data: 234 bytes
Total bytes sent: 1,111
Total bytes received: 222
'''


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def advance(self, amount):
        self.now += amount


class ObservedLink:
    host = 'worker'
    rsh = 'ssh worker'

    def __init__(self, clock, *, probe='absent', failure=None, cleanup_failure=False):
        self.clock = clock
        self.probe = probe
        self.failure = failure
        self.cleanup_failure = cleanup_failure

    def feed(self, script, args=(), **kwargs):
        phase = next(name for name, value in transfer.FEEDS.items() if value == script)
        name = 'cleanup' if phase == 'clean' else phase
        self.clock.advance({'probe': 1, 'bases': 2, 'stage': 3, 'publish': 6, 'cleanup': 7}[name])
        if self.failure == name or (name == 'cleanup' and self.cleanup_failure):
            raise TransferError(name + ' failed')
        output = {'probe': self.probe, 'bases': '/cache/a\n/cache/b\n',
                  'stage': '/cache/input.partial.unique\n', 'publish': '', 'clean': ''}[phase]
        return 0, output, ''


class TransferTimings(unittest.TestCase):
    def run_send(self, *, report, probe='absent', failure=None, cleanup_failure=False,
                 returncode=0, timeout=False, progress=True, output=OUTPUT):
        clock = Clock()
        link = ObservedLink(clock, probe=probe, failure=failure, cleanup_failure=cleanup_failure)
        logged = []

        def on_send():
            clock.advance(4)
            if failure == 'progress':
                raise TransferError('progress failed')

        def rsync(argv, **kwargs):
            clock.advance(5)
            self.assertIn('--stats', argv)
            self.assertEqual(kwargs['env']['LC_ALL'], 'C')
            self.assertEqual(kwargs['input'], b'file.txt\0other.txt\0')
            if timeout:
                raise subprocess.TimeoutExpired(argv, 9, output=output, stderr=b'timed out')
            return subprocess.CompletedProcess(argv, returncode, output, b'rsync failed' if returncode else b'')

        with mock.patch.object(transfer.time, 'monotonic', side_effect=clock.monotonic), \
                mock.patch.object(transfer.subprocess, 'run', side_effect=rsync), \
                mock.patch.dict(os.environ, {'LC_ALL': 'some-inherited-locale'}):
            try:
                result = transfer.send(link, [{'path': 'file.txt'}, {'path': 'other.txt'}],
                                       worktree='/source', root='/cache', repo='repo', input_id='input',
                                       timeout=9, report=report, log=logged.append,
                                       on_send=on_send if progress else None)
            finally:
                self.assertEqual(os.environ['LC_ALL'], 'some-inherited-locale')
        return result, logged

    def test_observed_phase_costs_and_counters_preserve_return_contract(self):
        report = {}
        result, logged = self.run_send(report=report)
        self.assertEqual(report['steps'], {'probe': 1, 'bases': 2, 'stage': 3,
                                          'progress': 4, 'rsync': 5, 'publish': 6, 'cleanup': 7})
        self.assertEqual(report['cache'], 'absent')
        self.assertEqual(report['base_count'], 2)
        self.assertEqual(report['files'], 2)
        self.assertEqual(report['rsync_exit'], 0)
        self.assertEqual(report['rsync']['literal_bytes'], 1000)
        self.assertEqual(report['rsync']['matched_bytes'], 234)
        self.assertEqual(result, {'path': '/cache/src/repo/input', 'reused': False,
                                 'link_dests': ['/cache/a', '/cache/b'], 'seconds': 18.0, 'files': 2})
        self.assertEqual(logged, [])

    def test_cache_hit_records_probe_without_claiming_rsync_or_base_work(self):
        report = {}
        result, _ = self.run_send(report=report, probe='present')
        self.assertEqual(report, {'steps': {'probe': 1}, 'files': 2, 'cache': 'present'})
        self.assertTrue(result['reused'])
        self.assertEqual(result['seconds'], 0)

    def test_unknown_probe_response_does_not_claim_a_cache_miss(self):
        report = {}
        result, _ = self.run_send(report=report, probe='unexpected response', progress=False)
        self.assertNotIn('cache', report)
        self.assertNotIn('progress', report['steps'])
        self.assertFalse(result['reused'])

    def test_each_failed_phase_keeps_elapsed_time_and_completed_evidence(self):
        prior = []
        for phase, cost in (('probe', 1), ('bases', 2), ('stage', 3),
                            ('progress', 4), ('publish', 6)):
            with self.subTest(phase=phase):
                report = {}
                with self.assertRaisesRegex(TransferError, phase + ' failed'):
                    self.run_send(report=report, failure=phase)
                self.assertEqual(report['steps'][phase], cost)
                self.assertTrue(all(name in report['steps'] for name in prior))
                if phase in ('progress', 'publish'):
                    self.assertEqual(report['steps']['cleanup'], 7)
                else:
                    self.assertNotIn('cleanup', report['steps'])
            prior.append(phase)

    def test_failed_rsync_and_failed_cleanup_preserve_original_error_and_counters(self):
        report = {}
        with self.assertRaisesRegex(TransferError, 'rsync failed') as caught:
            self.run_send(report=report, returncode=23, cleanup_failure=True)
        self.assertEqual(caught.exception.rsync_exit, 23)
        self.assertEqual(report['rsync_exit'], 23)
        self.assertEqual(report['rsync']['sent_bytes'], 1111)
        self.assertEqual(report['steps']['rsync'], 5)
        self.assertEqual(report['steps']['cleanup'], 7)
        self.assertNotIn('publish', report['steps'])

    def test_timeout_preserves_partial_stats_without_inventing_an_exit_code(self):
        report = {}
        with self.assertRaisesRegex(TransferError, 'timed out after 9 s') as caught:
            self.run_send(report=report, timeout=True)
        self.assertIsNone(caught.exception.rsync_exit)
        self.assertNotIn('rsync_exit', report)
        self.assertEqual(report['rsync']['received_bytes'], 222)
        self.assertEqual(report['steps']['rsync'], 5)
        self.assertEqual(report['steps']['cleanup'], 7)

    def test_successful_publication_remains_successful_when_cleanup_fails(self):
        report = {}
        result, logged = self.run_send(report=report, cleanup_failure=True)
        self.assertFalse(result['reused'])
        self.assertEqual(report['steps']['cleanup'], 7)
        self.assertEqual(len(logged), 1)
        self.assertIn('cleanup failed', logged[0])

    def test_timeout_discards_unfinished_counter_but_keeps_complete_observations(self):
        for tail in (b'Total bytes sent: 1,', b'Literal data: 1 00'):
            with self.subTest(tail=tail):
                report = {}
                output = b'Total bytes received: 222\nMatched data: 234 bytes\n' + tail
                with self.assertRaisesRegex(TransferError, 'timed out after 9 s'):
                    self.run_send(report=report, timeout=True, output=output)
                self.assertEqual(report['rsync'], {'received_bytes': 222, 'matched_bytes': 234})
                self.assertEqual(report['steps']['rsync'], 5)
                self.assertEqual(report['steps']['cleanup'], 7)
                self.assertNotIn('rsync_exit', report)

    def test_malformed_or_oversized_stats_cannot_change_a_successful_transfer(self):
        output = (b'Total bytes sent: 1,\nLiteral data: 1 00\nMatched data: 1 bytes extra\n'
                  b'Total transferred file size: ' + b'9' * 5000
                  + b'\nTotal bytes received: 222\nNumber of files transferred: 3\n')
        report = {}
        result, _ = self.run_send(report=report, output=output)
        self.assertFalse(result['reused'])
        self.assertEqual(report['rsync_exit'], 0)
        self.assertEqual(report['rsync'], {'received_bytes': 222, 'regular_files_transferred': 3})

    def test_report_is_optional(self):
        result, _ = self.run_send(report=None)
        self.assertFalse(result['reused'])

    def test_parser_handles_old_labels_and_does_not_fill_missing_stats_with_zero(self):
        self.assertEqual(transfer.rsync_stats(b'Number of files transferred: 3\n'
                                            b'Literal data: 0 bytes\n'),
                         {'regular_files_transferred': 3, 'literal_bytes': 0})
        self.assertEqual(transfer.rsync_stats('no supported counters\n'), {})
        self.assertEqual(transfer.rsync_stats(None), {})


if __name__ == '__main__':
    unittest.main()
