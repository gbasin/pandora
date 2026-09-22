"""The loader refuses what it cannot execute, and the classifier says why."""
import tempfile
import tomllib
import unittest
from pathlib import Path

from pandora.config import classify, loader
from pandora.errors import ConfigError, NotClaimed, Refused

EXAMPLE = Path(__file__).resolve().parents[1] / 'config/examples/eichler.pandora.toml'

MINIMAL = '''
version = 1
[repo]
name = "demo"
entrypoints = ["pnpm"]
[worker]
base_image = "images:ubuntu/26.04"
[[jobs]]
id = "suite"
forms = [{ prefix = ["suite"] }]
run = { argv = ["node", "run.mjs"] }
'''


def load_text(text):
    return loader.validate(tomllib.loads(text))


class LoaderTest(unittest.TestCase):
    def test_the_shipped_eichler_example_loads(self):
        config = loader.load(EXAMPLE)
        self.assertEqual(config['repo']['name'], 'eichler')
        self.assertIn('journey', config['jobs'])
        self.assertEqual(config['jobs']['journey']['size'], 'large')

    def test_a_minimal_configuration_loads(self):
        config = load_text(MINIMAL)
        self.assertEqual(list(config['jobs']), ['suite'])
        self.assertEqual(config['jobs']['suite']['tool'], 'pnpm')

    def test_an_unknown_key_names_the_allowed_ones(self):
        with self.assertRaises(ConfigError) as caught:
            load_text(MINIMAL.replace('[repo]', '[repo]\nnickname = "d"'))
        self.assertIn('nickname', str(caught.exception))
        self.assertIn('allowed:', str(caught.exception))

    def test_a_dropped_v01_key_is_refused_rather_than_ignored(self):
        # `ci_workflow` was in the draft contract and is deliberately not in the
        # slice. Silently ignoring it would let a repository believe CI facts
        # were being imported when nothing reads them.
        with self.assertRaises(ConfigError) as caught:
            load_text(MINIMAL.replace('version = 1', 'version = 1\nci_workflow = "ci.yml"'))
        self.assertIn('ci_workflow', str(caught.exception))

    def test_args_none_may_not_splice_arguments(self):
        with self.assertRaises(ConfigError) as caught:
            load_text(MINIMAL.replace('argv = ["node", "run.mjs"]',
                                      'argv = ["node", "run.mjs", "{args}"]'))
        self.assertIn("args = 'none'", str(caught.exception))

    def test_args_required_must_splice_arguments(self):
        with self.assertRaises(ConfigError) as caught:
            load_text(MINIMAL.replace('forms =', 'args = "required"\nforms ='))
        self.assertIn('{args}', str(caught.exception))

    def test_writeback_needs_an_option_that_arms_it(self):
        text = MINIMAL + '\noutputs = [{ kind = "writeback", requires_option = "update", paths = ["a"] }]\n'
        with self.assertRaises(ConfigError) as caught:
            load_text(text)
        self.assertIn('writeback', str(caught.exception))

    def test_two_jobs_cannot_claim_one_form(self):
        text = MINIMAL + '''
[[jobs]]
id = "other"
forms = [{ prefix = ["suite"] }]
run = { argv = ["node", "other.mjs"] }
'''
        with self.assertRaises(ConfigError) as caught:
            load_text(text)
        self.assertIn('claimed by both', str(caught.exception))

    def test_an_output_path_may_not_leave_the_worktree(self):
        text = MINIMAL + '\noutputs = [{ kind = "artifacts", paths = ["../outside"] }]\n'
        with self.assertRaises(ConfigError) as caught:
            load_text(text)
        self.assertIn('inside the worktree', str(caught.exception))


class ResolveTest(unittest.TestCase):
    def test_the_repo_root_wins_over_the_enrolment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'repo'
            root.mkdir()
            (root / 'pandora.toml').write_text(MINIMAL)
            other = Path(tmp) / 'elsewhere.toml'
            other.write_text(MINIMAL)
            path, origin = loader.resolve(root, other)
            self.assertEqual(path, root / 'pandora.toml')
            self.assertEqual(origin, 'repo-root')

    def test_the_enrolment_is_used_when_the_repo_has_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'repo'
            root.mkdir()
            other = Path(tmp) / 'elsewhere.toml'
            other.write_text(MINIMAL)
            path, origin = loader.resolve(root, other)
            self.assertEqual(path, other)
            self.assertEqual(origin, 'enrolment')

    def test_a_named_configuration_that_is_absent_is_an_error_not_a_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError):
                loader.resolve(tmp, Path(tmp) / 'missing.toml')

    def test_no_configuration_anywhere_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError) as caught:
                loader.resolve(tmp, None)
            self.assertIn('pandora.toml', str(caught.exception))


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.config = loader.load(EXAMPLE)

    def test_the_claimed_form_becomes_a_plan(self):
        verdict = classify.classify(self.config, ['pnpm', 'journey', 'S0-01'])
        self.assertEqual(verdict['decision'], 'remote')
        self.assertEqual(verdict['plan']['argv'],
                         ['node', 'tools/validation/journey-runner.mjs', 'run', 'S0-01'])
        self.assertEqual(verdict['plan']['env_unset'], ['CI'])

    def test_run_and_validate_spellings_reach_the_same_job(self):
        for argv in (['pnpm', 'run', 'journey', 'S0-01'],
                     ['pnpm', 'validate', 'journey', 'S0-01']):
            self.assertEqual(classify.classify(self.config, argv)['job'], 'journey',
                             msg=' '.join(argv))

    def test_an_unclaimed_command_is_local_and_not_an_error(self):
        verdict = classify.classify(self.config, ['pnpm', 'install'])
        self.assertEqual(verdict['decision'], 'local')
        with self.assertRaises(NotClaimed):
            classify.claimed_or_raise(self.config, ['pnpm', 'install'])

    def test_a_claimed_form_with_no_argument_is_refused_with_its_usage(self):
        verdict = classify.classify(self.config, ['pnpm', 'journey'])
        self.assertEqual(verdict['decision'], 'reject')
        self.assertIn('pnpm journey <id>', verdict['message'])
        self.assertIn('No validation started.', verdict['message'])

    def test_an_argument_may_not_escape_the_worktree(self):
        verdict = classify.classify(self.config, ['pnpm', 'journey', '../elsewhere'])
        self.assertEqual(verdict['decision'], 'reject')
        self.assertIn('inside the worktree', verdict['message'])

    def test_a_shard_variable_in_the_environment_is_refused_not_ignored(self):
        verdict = classify.classify(self.config, ['pnpm', 'journey', 'S0-01'],
                                    env={'JOURNEY_SHARD': '1/4'})
        self.assertEqual(verdict['decision'], 'reject')
        self.assertIn('JOURNEY_SHARD', verdict['message'])

    def test_a_subdirectory_invocation_stays_local(self):
        verdict = classify.classify(self.config, ['pnpm', 'journey', 'S0-01'], cwd='apps/agent')
        self.assertEqual(verdict['decision'], 'local')

    def test_update_arms_the_writeback_output(self):
        plain = classify.classify(self.config, ['pnpm', 'journey', 'S0-01'])['plan']
        armed = classify.classify(self.config, ['pnpm', 'journey', 'S0-01', '--update'])['plan']
        self.assertEqual([item['kind'] for item in plain['outputs']], ['artifacts'])
        self.assertIn('writeback', [item['kind'] for item in armed['outputs']])
        self.assertTrue(armed['options']['update'])

    def test_an_unknown_flag_is_forwarded_not_refused(self):
        # Pandora does not know eichler's flags. The repository's own validator
        # is what refuses them, one step later and in its own words.
        verdict = classify.classify(self.config, ['pnpm', 'journey', 'S0-01', '--nope'])
        self.assertEqual(verdict['decision'], 'remote')
        self.assertIn('--nope', verdict['forwarded'])

    def test_a_value_flag_keeps_its_value_unexamined(self):
        verdict = classify.classify(self.config, ['pnpm', 'journey', 'S0-01',
                                                  '--fault', '../weird'])
        self.assertEqual(verdict['decision'], 'remote')
        self.assertEqual(verdict['forwarded'][-2:], ['--fault', '../weird'])

    def test_the_claim_index_is_what_the_shim_reads(self):
        self.assertEqual(classify.claim_index(self.config),
                         [['check'], ['check:docs'], ['dev:stack'], ['journey'],
                          ['node'], ['test:unit'], ['unit']])


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.config = loader.load(EXAMPLE)
        self.job = self.config['jobs']['journey']

    def fake(self, code, stderr=''):
        class Done:
            returncode = code
            stdout = ''
        Done.stderr = stderr
        return lambda *a, **k: Done()

    def test_exit_zero_is_acceptance(self):
        answer = classify.preflight(self.job, ['S0-01'], root='.', run=self.fake(0))
        self.assertTrue(answer['ran'])

    def test_non_zero_is_the_repositorys_own_refusal(self):
        from pandora.errors import ValidationRejected
        with self.assertRaises(ValidationRejected) as caught:
            classify.preflight(self.job, ['--nope'], root='.',
                               run=self.fake(3, '[journey] --nope is not valid here.'))
        self.assertEqual(caught.exception.code, 3)
        self.assertIn('--nope', caught.exception.stderr)

    def test_a_broken_validator_is_a_skipped_check_not_a_refusal(self):
        def explode(*a, **k):
            raise OSError('no such interpreter')
        answer = classify.preflight(self.job, ['S0-01'], root='.', run=explode)
        self.assertFalse(answer['ran'])
        self.assertIn('could not start', answer['reason'])

    def test_a_slow_validator_is_a_skipped_check_not_a_refusal(self):
        import subprocess

        def slow(*a, **k):
            raise subprocess.TimeoutExpired('node', 4)
        answer = classify.preflight(self.job, ['S0-01'], root='.', run=slow)
        self.assertFalse(answer['ran'])
        self.assertIn('exceeded', answer['reason'])

    def test_a_job_with_no_validator_is_not_pre_checked(self):
        job = dict(self.job, validate=None)
        answer = classify.preflight(job, ['S0-01'], root='.')
        self.assertFalse(answer['ran'])


if __name__ == '__main__':
    unittest.main()
