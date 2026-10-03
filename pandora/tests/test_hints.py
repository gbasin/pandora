"""The five hint rules, one test each, plus the two that need the worktree.

A hint is advice an agent will act on, so the interesting assertions are the
negative ones: a rule that fires on a run it knows nothing about is worse than a
rule that never fires at all.
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from pandora.client import hints
from pandora.client.protocol import log_frame
from pandora.engine import result as rules


MIB = 1048576
# The eichler `check` of 2026-10-02 (gbasin/pandora#193): declared and run at
# `large`, stalled at memory.high, 90% of the 8192 MiB ceiling.
THRASH = {'reason': 'memory-thrash', 'throttle_events_per_second': 6736.0,
          'thrashing_seconds': 15.4, 'memory_current': 7730851840,
          'memory_high': 7730102272, 'memory_max': 8589934592,
          'memory_wall': 7730102272}
# The breakdown is the same job's cold rerun at `xlarge`, at its peak: the
# shape of an anon-dominated run, not a reading taken at the large limit.
ANON = {'anon': 8450 * MIB, 'file': 1492 * MIB, 'kernel': 314 * MIB, 'shmem': 7 * MIB}
FILE = {'anon': 600 * MIB, 'file': 6500 * MIB, 'kernel': 200 * MIB, 'shmem': 5 * MIB}


def thrash(size_used='large', size_declared='large', ceiling_mib=8192, cpus_hint=8,
           **evidence):
    return {'outcome': 'oom', 'job': 'check', 'peak_mib': 7405, 'ceiling_mib': ceiling_mib,
            'size_used': size_used, 'size_declared': size_declared, 'cpus_hint': cpus_hint,
            'evidence': dict(THRASH, **evidence)}


class ThrashHint(unittest.TestCase):
    """The watchdog's oom: built from the class used and the recorded memory."""

    def test_at_large_declared_large_it_names_xlarge_and_the_limit_that_applied(self):
        hint = rules.hint_for(thrash(memory_stat=ANON))
        self.assertIn("at the large class's limit", hint)
        self.assertIn('memory.high 7372 MiB of the 8192 MiB ceiling', hint)
        self.assertIn('stalled 15 s', hint)
        self.assertIn('declare size = "xlarge" for job check', hint)
        self.assertIn('PANDORA_CPUS=8', hint)
        self.assertNotIn('size = "large"', hint)
        self.assertNotIn('file-cache', hint)

    def test_it_never_names_the_class_the_run_already_had(self):
        for index, used in enumerate(rules.ORDER):
            hint = rules.hint_for(thrash(size_used=used, size_declared=used))
            self.assertNotIn('size = "%s"' % used, hint)
            if index + 1 < len(rules.ORDER):
                self.assertIn('size = "%s"' % rules.ORDER[index + 1], hint)

    def test_at_xlarge_there_is_no_bigger_class(self):
        hint = rules.hint_for(thrash(size_used='xlarge', size_declared='xlarge',
                                     ceiling_mib=12288, memory_high=11059 * MIB,
                                     memory_max=12288 * MIB, memory_wall=11059 * MIB))
        self.assertIn('xlarge is the largest class', hint)
        self.assertIn('memory.high 11059 MiB of the 12288 MiB ceiling', hint)
        self.assertIn('PANDORA_CPUS=8', hint)
        self.assertIn('shards', hint)
        self.assertNotIn('declare size', hint)

    def test_parallelism_is_not_offered_to_a_run_that_had_one_cpu(self):
        hint = rules.hint_for(thrash(cpus_hint=1))
        self.assertNotIn('parallelism', hint)
        self.assertIn('declare size = "xlarge"', hint)
        self.assertNotIn('parallelism', rules.hint_for(thrash(cpus_hint=None)))

    def test_a_learned_class_below_the_declared_one_says_the_declared_one_applies(self):
        hint = rules.hint_for(thrash(size_used='large', size_declared='xlarge'))
        self.assertIn('learned class for job check was large', hint)
        self.assertIn('declared xlarge applies again from the next run', hint)
        self.assertIn('memory.high 7372 MiB of the 8192 MiB ceiling', hint)
        self.assertNotIn('declare size', hint)

    def test_anon_dominated_names_anonymous_memory(self):
        hint = rules.hint_for(thrash(memory_stat=ANON))
        self.assertIn('anon 8450 MiB, file 1492 MiB, kernel 314 MiB, shmem 7 MiB', hint)
        self.assertIn('mostly anonymous memory', hint)
        self.assertNotIn('file pages', hint)

    def test_file_dominated_names_file_pages(self):
        hint = rules.hint_for(thrash(memory_stat=FILE))
        self.assertIn('anon 600 MiB, file 6500 MiB', hint)
        self.assertIn('mostly file pages', hint)
        self.assertNotIn('anonymous memory', hint)

    def test_neither_dominating_says_both(self):
        hint = rules.hint_for(thrash(memory_stat={'anon': 3000 * MIB, 'file': 3000 * MIB}))
        self.assertIn('in similar shares', hint)
        self.assertNotIn('mostly', hint)

    def test_no_breakdown_makes_no_cause_claim(self):
        hint = rules.hint_for(thrash())
        self.assertIn('no memory breakdown recorded', hint)
        for claim in ('mostly', 'anonymous', 'file pages', 'file-cache'):
            self.assertNotIn(claim, hint)

    def test_an_unusable_breakdown_is_treated_as_none(self):
        for stat in ({}, {'anon': 'x'}, {'file': 5}, 'garbage', {'anon': 0, 'file': 0}):
            hint = rules.hint_for(thrash(memory_stat=stat))
            self.assertIn('no memory breakdown recorded', hint, stat)

    def test_an_old_record_without_memory_high_still_gets_a_hint(self):
        # A record from before memory.high was in the evidence: peak and ceiling.
        hint = rules.hint_for({'outcome': 'oom', 'job': 'check', 'peak_mib': 7405,
                               'ceiling_mib': 8192, 'size_used': 'large',
                               'size_declared': 'large',
                               'evidence': {'reason': 'memory-thrash'}})
        self.assertIn('peak 7405 MiB of a 8192 MiB ceiling', hint)
        self.assertIn('declare size = "xlarge"', hint)
        self.assertIn('no memory breakdown recorded', hint)

    def test_the_engine_passes_the_cpus_hint_and_the_breakdown_through(self):
        facts = rules.facts_from_result({
            'outcome': 'oom', 'job': 'check', 'peak_mib': 7405, 'ceiling_mib': 8192,
            'size_declared': 'large', 'size_used': 'large', 'cpus_hint': 8,
            'evidence': dict(THRASH, memory_stat=ANON, samples=[{'t': 0}])})
        self.assertEqual(facts['cpus_hint'], 8)
        self.assertEqual(facts['evidence']['memory_stat'], ANON)
        self.assertNotIn('samples', facts['evidence'])
        self.assertIn('mostly anonymous memory', rules.hint_for(facts))


def kernel(size_used='large', size_declared=None, cpus_hint=8, **extra):
    return dict({'outcome': 'oom', 'job': 'check', 'peak_mib': 8180, 'ceiling_mib': 8192,
                 'size_used': size_used, 'size_declared': size_declared or size_used,
                 'cpus_hint': cpus_hint, 'evidence': {'reason': 'oom_kill'}}, **extra)


def prepared(size_used='large', **kill):
    """An oom in `prepare_command`: the runner records it under `preparation`."""
    return kernel(size_used, evidence={
        'cause': 'prepare-command-oom', 'error': 'transferred source preparation oom',
        'preparation': dict({'outcome': 'oom', 'exit_code': -9}, **kill)})


class KernelHint(unittest.TestCase):
    """The kernel's oom: the same cure as the watchdog's, from the class used."""

    def test_it_names_the_next_class_and_the_ceiling(self):
        hint = rules.hint_for(kernel())
        self.assertIn("job check hit the large class's ceiling", hint)
        self.assertIn('peak 8180 MiB of a 8192 MiB ceiling', hint)
        self.assertIn('declare size = "xlarge" for job check', hint)
        self.assertIn('PANDORA_CPUS=8', hint)

    def test_at_xlarge_there_is_no_bigger_class(self):
        hint = rules.hint_for(kernel('xlarge'))
        self.assertIn('xlarge is the largest class', hint)
        self.assertIn('PANDORA_CPUS=8', hint)
        self.assertIn('split it into shards', hint)
        self.assertNotIn('raise the size class', hint)
        self.assertNotIn('declare size', hint)
        self.assertNotIn('parallelism', rules.hint_for(kernel('xlarge', cpus_hint=1)))

    def test_it_never_names_the_class_the_run_already_had(self):
        for index, used in enumerate(rules.ORDER[:-1]):
            hint = rules.hint_for(kernel(used))
            self.assertNotIn('size = "%s"' % used, hint)
            self.assertIn('size = "%s"' % rules.ORDER[index + 1], hint)


class PreparationHint(unittest.TestCase):
    """An oom in `prepare_command`: named as such, read from `evidence.preparation`."""

    def test_a_thrash_in_preparation_uses_its_limit_and_breakdown(self):
        hint = rules.hint_for(prepared(**dict(THRASH, memory_stat=ANON)))
        self.assertIn("watchdog killed the prepare_command of job check at the large "
                      "class's limit", hint)
        self.assertIn('memory.high 7372 MiB of the 8192 MiB ceiling', hint)
        self.assertIn("mostly anonymous memory, prepare_command's own processes", hint)
        self.assertIn('declare size = "xlarge" for job check', hint)
        self.assertNotIn('raise the size class', hint)
        # The job's parallelism and shards are not levers on its preparation.
        self.assertNotIn('parallelism', hint)
        self.assertNotIn('shards', hint)

    def test_a_thrash_in_preparation_at_xlarge_names_no_class(self):
        hint = rules.hint_for(prepared('xlarge', **THRASH))
        self.assertIn('prepare_command of job check', hint)
        self.assertIn('xlarge is the largest class', hint)
        self.assertIn('prepare_command holds in memory', hint)
        self.assertNotIn('declare size', hint)
        self.assertNotIn('parallelism', hint)
        self.assertNotIn('shards', hint)

    def test_a_kernel_kill_in_preparation_names_the_phase(self):
        hint = rules.hint_for(prepared(reason='oom_kill'))
        self.assertIn("the prepare_command of job check hit the large class's ceiling",
                      hint)
        self.assertIn('declare size = "xlarge"', hint)
        self.assertNotIn('parallelism', hint)
        hint = rules.hint_for(prepared('xlarge', reason='oom_kill'))
        self.assertIn('xlarge is the largest class', hint)
        self.assertNotIn('declare size', hint)

    def test_a_preparation_that_did_not_oom_is_not_read(self):
        # Only the kill's own record is evidence for the hint.
        facts = kernel(evidence={'reason': 'oom_kill',
                                 'preparation': {'outcome': 'ok', 'exit_code': 0}})
        self.assertIn("job check hit the large class's ceiling", rules.hint_for(facts))
        self.assertNotIn('prepare_command', rules.hint_for(facts))

    def test_the_result_summary_names_the_preparation(self):
        result = prepared(**THRASH)
        self.assertEqual(rules.thrash_summary(result),
                         'watchdog (prepare_command): memory.high 7372 MiB of the '
                         '8192 MiB ceiling, stalled 15 s; no memory breakdown recorded')

    def test_the_engine_passes_the_preparation_through(self):
        facts = rules.facts_from_result(prepared(**dict(THRASH, memory_stat=FILE)))
        self.assertIn('prepare_command of job check', rules.hint_for(facts))
        self.assertIn('mostly file pages', rules.hint_for(facts))


class Rules(unittest.TestCase):
    def test_oom_names_the_job_the_peak_and_the_ceiling(self):
        hint = rules.hint_for({'outcome': 'oom', 'job': 'journey', 'peak_mib': 7900,
                               'ceiling_mib': 8192, 'evidence': {'reason': 'oom_kill'}})
        self.assertIn('raise the size class for job journey', hint)
        self.assertIn('7900 MiB', hint)
        self.assertIn('8192 MiB', hint)

    def test_a_thrash_kill_is_not_told_to_raise_the_size_class_blindly(self):
        hint = rules.hint_for({'outcome': 'oom', 'job': 'install', 'peak_mib': 460,
                               'ceiling_mib': 482,
                               'evidence': {'reason': 'memory-thrash',
                                            'throttle_events_per_second': 1167}})
        self.assertIn('watchdog killed job install', hint)
        self.assertNotIn('raise the size class', hint)
        self.assertNotIn('file-cache', hint)

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

    def test_the_order_is_pinned(self):
        # Worst-first is the contract: a reorder changes which hint fires when
        # several apply, and nothing else would notice.
        self.assertEqual([rule.__name__ for rule in rules.RULES],
                         ['oom', 'timed_out', 'drifted', 'flaky',
                          'missing_executable', 'missing_report',
                          'written_back', 'gitignored'])

    def test_the_winner_names_itself(self):
        # Both `missing_executable` (the spawn ENOENT) and `gitignored` (the
        # absent config the second line blames) fire here; the earlier rule
        # wins and its name travels with the text.
        named = rules.hint_named({'outcome': 'command_failed', 'observed_exit': 1,
                                  'log_tail': "spawn ffmpeg ENOENT\n"
                                              "cannot find module './cfg/local.json'\n",
                                  'exists': lambda token: True,
                                  'ignored': lambda token: True,
                                  'shipped': frozenset()})
        self.assertEqual(named[0], 'missing_executable')
        self.assertIn('ffmpeg', named[1])


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


class MissingExecutable(unittest.TestCase):
    """The log-reading rule that outranks `gitignored` (GH #114)."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.root = Path(self.home.name)
        for argv in (['git', 'init', '-q'], ['git', 'config', 'user.email', 'a@b.c'],
                     ['git', 'config', 'user.name', 'a']):
            subprocess.run(argv, cwd=self.root, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (self.root / '.gitignore').write_text('node_modules/\ntmp/\n')
        (self.root / 'tmp').mkdir()
        (self.root / 'tmp' / 'fixture.json').write_text('{}')
        (self.root / 'node_modules').mkdir()
        (self.root / 'node_modules' / 'x.js').write_text('x')

    def test_a_missing_playwright_browser_names_the_executable(self):
        tail = ("browserType.launch: Executable doesn't exist at "
                "/ms-playwright/chromium-1117/chrome-mac/Chromium\n"
                "    at Object.<anonymous> (node_modules/x.js:3:1)\n")
        hint = hints.for_run({'outcome': 'command_failed'}, worktree=self.root,
                             tail=tail)
        self.assertIn("'Chromium' is not installed on the worker", hint)
        self.assertIn('golden', hint)
        self.assertNotIn('gitignored', hint)

    def test_the_executable_rule_wins_even_when_a_gitignored_path_is_blamed(self):
        # Both rules would fire on this tail; the program is the cause.
        tail = ("Error: spawn ffmpeg ENOENT\n"
                "Error: ENOENT, open 'tmp/fixture.json'\n")
        hint = hints.for_run({'outcome': 'command_failed'}, worktree=self.root,
                             tail=tail)
        self.assertIn("'ffmpeg' is not installed on the worker", hint)

    def test_command_not_found_names_the_program(self):
        hint = rules.missing_executable({'outcome': 'command_failed',
                                         'log_tail': 'bash: rg: command not found\n'})
        self.assertIn("'rg' is not installed on the worker", hint)

    def test_a_file_enoent_is_left_for_the_gitignored_rule(self):
        # `open` is a file access; a missing file is not a missing program.
        hint = hints.for_run({'outcome': 'command_failed'}, worktree=self.root,
                             tail="Error: ENOENT, open 'tmp/fixture.json'\n")
        self.assertIn('tmp/fixture.json exists locally but is gitignored', hint)

    def test_a_path_a_stack_frame_names_is_not_blamed(self):
        # The line does not read as an error; it only mentions a path.
        self.assertIsNone(hints.for_run(
            {'outcome': 'command_failed'}, worktree=self.root,
            tail='    at run (tmp/fixture.json:3:1)\n'))


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
            self.assertIn('declare size = "xlarge" for job journey', result['hint'])
            self.assertEqual(result['hint_rule'], 'oom')
            written = json.loads(paths.result('r1').read_text())
            self.assertEqual(written['hint'], result['hint'])
            self.assertEqual(written['hint_rule'], 'oom')

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
