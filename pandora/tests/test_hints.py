"""The five hint rules, one test each, plus the two that need the worktree.

A hint is advice an agent will act on, so the interesting assertions are the
negative ones: a rule that fires on a run it knows nothing about is worse than a
rule that never fires at all.
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from pandora.client import hints
from pandora.client.protocol import log_frame
from pandora.engine import result as rules


class Rules(unittest.TestCase):
    def test_oom_names_the_job_the_peak_and_the_ceiling(self):
        hint = rules.hint_for({'outcome': 'oom', 'job': 'journey', 'peak_mib': 7900,
                               'ceiling_mib': 8192, 'evidence': {'reason': 'oom_kill'}})
        self.assertIn('raise the size class for job journey', hint)
        self.assertIn('7900 MiB', hint)
        self.assertIn('8192 MiB', hint)

    def test_file_cache_thrash_gets_the_other_cure(self):
        hint = rules.hint_for({'outcome': 'oom', 'job': 'install', 'peak_mib': 460,
                               'ceiling_mib': 482,
                               'evidence': {'reason': 'memory-thrash',
                                            'throttle_events_per_second': 1167}})
        self.assertIn('file-cache thrash', hint)
        self.assertIn('declare size large', hint)
        self.assertNotIn('raise the size class', hint)

    def test_timed_out_names_the_wall_and_the_knob(self):
        hint = rules.hint_for({'outcome': 'timed_out', 'job': 'surface',
                               'wall_seconds': 1800.0})
        self.assertIn('timed out after 1800s', hint)
        self.assertIn('timeout_minutes', hint)

    def test_missing_report_names_the_report_and_the_escape_hatch(self):
        hint = rules.hint_for({'outcome': 'command_failed', 'observed_exit': 3,
                               'collected': {'reports/junit.xml': 'missing',
                                             'reports/log.txt': 'present'}})
        self.assertIn('runner exited 3 without writing reports/junit.xml', hint)
        self.assertIn('PANDORA_OFF=1', hint)

    def test_a_killed_run_is_not_blamed_for_a_report_it_never_wrote(self):
        self.assertIsNone(rules.missing_report({'outcome': 'oom',
                                                'collected': {'r.xml': 'missing'}}))

    def test_drift_names_the_paths(self):
        hint = rules.hint_for({'outcome': 'command_failed', 'drifted': True, 'drift': 'fail',
                               'drift_paths': ['src/a.ts', 'src/b.ts']})
        self.assertIn('changed during the run at src/a.ts, src/b.ts', hint)

    def test_drift_caps_the_list_and_counts_the_rest(self):
        hint = rules.drifted({'drifted': True, 'drift': 'fail',
                              'drift_paths': ['p%d' % index for index in range(9)]})
        self.assertIn('and 4 more', hint)

    def test_drift_under_warn_is_not_a_hint(self):
        # The verdict stands under `warn`, and the run's own notice said so.
        self.assertIsNone(rules.hint_for({'outcome': 'passed', 'drifted': True,
                                          'drift': 'warn', 'drift_paths': ['src/a.ts']}))

    def test_a_passing_run_gets_no_hint(self):
        self.assertIsNone(rules.hint_for({'outcome': 'passed', 'job': 'unit',
                                          'collected': {'r.xml': 'present'}}))

    def test_a_rule_that_raises_does_not_take_the_others_down(self):
        def explode(_facts):
            raise RuntimeError('boom')

        original = rules.RULES
        rules.RULES = (explode, rules.timed_out)
        try:
            self.assertIn('timed out', rules.hint_for({'outcome': 'timed_out',
                                                       'wall_seconds': 10}))
        finally:
            rules.RULES = original

    def test_order_is_worst_first(self):
        # A run killed for memory that also failed to write its report is a
        # memory problem; the missing report is a consequence, not a cause.
        hint = rules.hint_for({'outcome': 'oom', 'job': 'j', 'peak_mib': 1, 'ceiling_mib': 2,
                               'collected': {'r.xml': 'missing'}, 'evidence': {}})
        self.assertIn('raise the size class', hint)


class PathTokens(unittest.TestCase):
    def test_it_finds_paths_inside_an_error_message(self):
        found = rules.path_tokens("Cannot find module 'tmp/fixture.json' from src/a.ts")
        self.assertIn('tmp/fixture.json', found)
        self.assertIn('src/a.ts', found)

    def test_a_bare_word_is_not_a_path(self):
        self.assertEqual(rules.path_tokens('FAIL 12 tests failed in 3.4s'), [])

    def test_the_cap_holds(self):
        text = ' '.join('dir%d/file.ts' % index for index in range(500))
        self.assertEqual(len(rules.path_tokens(text, cap=7)), 7)

    def test_duplicates_are_reported_once(self):
        self.assertEqual(rules.path_tokens('a/b a/b a/b'), ['a/b'])


class Gitignored(unittest.TestCase):
    """The one rule that needs the worktree, against a real git repository."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.root = Path(self.home.name)
        for argv in (['git', 'init', '-q'], ['git', 'config', 'user.email', 'a@b.c'],
                     ['git', 'config', 'user.name', 'a']):
            subprocess.run(argv, cwd=self.root, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (self.root / '.gitignore').write_text('tmp/\n')
        (self.root / 'tmp').mkdir()
        (self.root / 'tmp' / 'fixture.json').write_text('{}')
        (self.root / 'src').mkdir()
        (self.root / 'src' / 'a.ts').write_text('x')

    def test_an_ignored_path_the_command_named_is_the_hint(self):
        hint = hints.for_run(
            {'outcome': 'command_failed'}, worktree=self.root,
            tail="Error: ENOENT, open 'tmp/fixture.json'\n")
        self.assertIn('tmp/fixture.json exists locally but is gitignored', hint)
        self.assertIn('[sync] include', hint)

    def test_a_tracked_path_is_not_the_hint(self):
        self.assertIsNone(hints.for_run({'outcome': 'command_failed'}, worktree=self.root,
                                        tail="failed at src/a.ts:3\n"))

    def test_a_path_that_does_not_exist_here_is_not_the_hint(self):
        self.assertIsNone(hints.for_run({'outcome': 'command_failed'}, worktree=self.root,
                                        tail="cannot find tmp/absent.json\n"))

    def test_a_path_that_was_shipped_is_not_the_hint(self):
        self.assertIsNone(hints.for_run(
            {'outcome': 'command_failed'}, worktree=self.root,
            shipped={'tmp/fixture.json'}, tail="open 'tmp/fixture.json'\n"))

    def test_the_engines_own_hint_wins(self):
        self.assertEqual(
            hints.for_run({'outcome': 'oom', 'hint': 'from the engine'}, worktree=self.root,
                          tail="open 'tmp/fixture.json'\n"),
            'from the engine')


class LogTail(unittest.TestCase):
    def test_it_decodes_framed_output(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / 'log'
            with path.open('wb') as handle:
                handle.write(log_frame('out', b'hello '))
                handle.write(log_frame('err', b'world'))
            self.assertEqual(hints.log_tail(path), 'hello world')

    def test_an_absent_log_is_an_empty_tail_not_a_crash(self):
        self.assertEqual(hints.log_tail('/nowhere/at/all/log'), '')


class EngineAttachment(unittest.TestCase):
    """`write_result` attaches the hint, so `pandora result` carries it."""

    def test_a_result_written_by_the_engine_carries_its_hint(self):
        from pandora.engine.ledger import Ledger
        from pandora.engine.runner import Paths, write_result
        with tempfile.TemporaryDirectory() as home:
            paths = Paths(home).ensure()
            ledger = Ledger(paths.ledger)
            ledger.claim('q1', 'r1', repo='demo', job='journey', input_id='i',
                         source_path='/src', argv=['pnpm'], env={}, cwd='/work',
                         outputs=[], size_class='large')
            ledger.update('r1', ceiling_mib=8192)
            result = write_result(paths, ledger, 'r1', outcome='oom', layer='watchdog',
                                  exit_code=-9, peak_mib=7900, durations={'execute': 30.0},
                                  evidence={'reason': 'oom_kill'}, receipt=None)
            ledger.close()
            self.assertIn('raise the size class for job journey', result['hint'])
            written = json.loads(paths.result('r1').read_text())
            self.assertEqual(written['hint'], result['hint'])

    def test_a_passing_result_carries_a_null_hint_rather_than_no_field(self):
        from pandora.engine.ledger import Ledger
        from pandora.engine.runner import Paths, write_result
        with tempfile.TemporaryDirectory() as home:
            paths = Paths(home).ensure()
            ledger = Ledger(paths.ledger)
            ledger.claim('q2', 'r2', repo='demo', job='unit', input_id='i',
                         source_path='/src', argv=['pnpm'], env={}, cwd='/work',
                         outputs=[], size_class='small')
            result = write_result(paths, ledger, 'r2', outcome='passed', layer='command',
                                  exit_code=0, peak_mib=100, durations={},
                                  evidence={'collected': {'r.xml': 'present'}}, receipt=None)
            ledger.close()
            self.assertIn('hint', result)
            self.assertIsNone(result['hint'])


if __name__ == '__main__':
    unittest.main()
