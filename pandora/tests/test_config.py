"""The loader refuses what it cannot execute, and the classifier says why."""
import tempfile
import tomllib
import unittest
from pathlib import Path

from pandora.config import classify, loader
from pandora.errors import ConfigError, NotClaimed

EXAMPLE = Path(__file__).resolve().parents[1] / 'config/examples/acme.pandora.toml'

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
    def test_the_shipped_acme_example_loads(self):
        config = loader.load(EXAMPLE)
        self.assertEqual(config['repo']['name'], 'acme')
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

    def test_a_writeback_glob_matches_file_names_in_one_directory_only(self):
        armed = MINIMAL + ('\noptions = [{ name = "--update", sets = "update", writeback = true }]'
                           '\noutputs = [{ kind = "writeback", requires_option = "update", '
                           'paths = ["%s"] }]\n')
        load_text(armed % 'fixtures/*.ledger.jsonl')
        load_text(armed % 'fixtures/routes.json')
        for path in ('fixtures/**/x.json', 'fix*/routes.json', '*.json'):
            with self.subTest(path=path):
                with self.assertRaises(ConfigError) as caught:
                    load_text(armed % path)
                self.assertIn(path, str(caught.exception))

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

    def test_fallback_is_a_word(self):
        for spelling, action in (('"local"', 'local'), ('"refuse"', 'refuse')):
            job = load_text(MINIMAL + '\nfallback = %s\n' % spelling)['jobs']['suite']
            self.assertEqual(job['fallback']['action'], action)

    def test_an_undeclared_fallback_stays_undeclared(self):
        # Not a default of `local` and not a default of `refuse`: "nobody said"
        # is the third answer, and it is the one the size class decides.
        config = load_text(MINIMAL)
        self.assertIsNone(config['fallback'])
        self.assertIsNone(config['jobs']['suite']['fallback'])

    def test_the_repository_level_table_is_inherited_by_every_job(self):
        config = load_text(MINIMAL + '\n[fallback]\naction = "local"\non = ["queue-timeout"]\n')
        self.assertEqual(config['jobs']['suite']['fallback']['on'], ['queue-timeout'])

    def test_the_v01_spelling_fail_still_loads_as_refuse(self):
        config = load_text(MINIMAL + '\n[fallback]\naction = "fail"\n')
        self.assertEqual(config['fallback']['action'], 'refuse')

    def test_an_unknown_fallback_cause_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            load_text(MINIMAL + '\n[fallback]\naction = "local"\non = ["the-vibes"]\n')
        self.assertIn('the-vibes', str(caught.exception))

    def test_a_cancel_contract_has_a_signal_and_a_grace(self):
        job = load_text(MINIMAL + '\ncancel = { signal = "SIGINT", grace_ms = 240000 }\n'
                        )['jobs']['suite']
        self.assertEqual(job['cancel'], {'signal': 'SIGINT', 'grace_ms': 240000})

    def test_the_default_cancel_is_sigterm_and_fifteen_seconds(self):
        self.assertEqual(load_text(MINIMAL)['jobs']['suite']['cancel'],
                         {'signal': 'SIGTERM', 'grace_ms': 15000})

    def test_sigkill_is_not_offered_as_a_cancel_signal(self):
        # It is what the grace escalates to, not a thing a job may ask for.
        with self.assertRaises(ConfigError) as caught:
            load_text(MINIMAL + '\ncancel = { signal = "SIGKILL" }\n')
        self.assertIn('SIGTERM', str(caught.exception))

    def test_drift_belongs_to_the_job_when_the_job_says_so(self):
        local = MINIMAL + '\nwhere = "local"\ndrift = "off"\n'
        self.assertEqual(load_text(local)['jobs']['suite']['drift'], 'off')
        self.assertIsNone(load_text(MINIMAL)['jobs']['suite']['drift'])

    def test_drift_on_a_remote_job_warns_because_a_snapshot_cannot_drift(self):
        # A remote run executes a frozen tree; the knob only exists in the
        # local lane, so the key does nothing -- but live configs already set
        # it, so the load warns rather than fails. A future release will
        # refuse it. Should the job ever land in the local lane -- a fallback
        # or PANDORA_WHERE=local -- the machine's [local] drift applies.
        for spelling in ('warn', 'fail', 'off'):
            with self.subTest(drift=spelling):
                config = load_text(MINIMAL + '\ndrift = "%s"\n' % spelling)
                self.assertEqual(config['jobs']['suite']['drift'], spelling)
                self.assertEqual(len(config['warnings']), 1, config['warnings'])
                self.assertIn('drift', config['warnings'][0])
        self.assertEqual(load_text(MINIMAL)['warnings'], [])

    def test_worker_workdir_is_not_a_run_knob(self):
        base = 'base_image = "images:ubuntu/26.04"'
        self.assertEqual(load_text(MINIMAL)['worker']['workdir'], '/work')
        # The default spelled out claims nothing, so it still loads.
        self.assertEqual(load_text(MINIMAL.replace(base, base + '\nworkdir = "/work"'))
                         ['worker']['workdir'], '/work')
        with self.assertRaises(ConfigError) as caught:
            load_text(MINIMAL.replace(base, base + '\nworkdir = "/srv"'))
        self.assertIn('workdir', str(caught.exception))

    def test_a_local_job_declares_evidence_not_artifacts(self):
        local = MINIMAL + '\nwhere = "local"\n'
        job = load_text(local + 'outputs = [{ kind = "evidence", paths = ["tmp/marker"] }]\n'
                        )['jobs']['suite']
        self.assertEqual(job['outputs'][0]['kind'], 'evidence')
        with self.assertRaises(ConfigError) as caught:
            load_text(local + 'outputs = [{ kind = "artifacts", paths = ["tmp"] }]\n')
        self.assertIn('evidence', str(caught.exception))

    def test_a_remote_job_may_not_declare_evidence(self):
        with self.assertRaises(ConfigError) as caught:
            load_text(MINIMAL + '\noutputs = [{ kind = "evidence", paths = ["tmp"] }]\n')
        self.assertIn('artifacts', str(caught.exception))


class ResolveTest(unittest.TestCase):
    def test_the_repo_root_wins_over_the_enrollment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'repo'
            root.mkdir()
            (root / 'pandora.toml').write_text(MINIMAL)
            other = Path(tmp) / 'elsewhere.toml'
            other.write_text(MINIMAL)
            path, origin = loader.resolve(root, other)
            self.assertEqual(path, root / 'pandora.toml')
            self.assertEqual(origin, 'repo-root')

    def test_the_enrollment_is_used_when_the_repo_has_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'repo'
            root.mkdir()
            other = Path(tmp) / 'elsewhere.toml'
            other.write_text(MINIMAL)
            path, origin = loader.resolve(root, other)
            self.assertEqual(path, other)
            self.assertEqual(origin, 'enrollment')

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

    def test_every_reject_verdict_is_exit_64(self):
        # #124: a refused command used to reach the caller as exit 1, which is
        # indistinguishable from the command having run and failed.
        for argv, kwargs in (
            (['pnpm', 'journey'], {}),                          # args = "required", none given
            (['pnpm', 'journey', '../elsewhere'], {}),          # a path out of the worktree
            (['pnpm', 'journey', 'S0-01', '--fault'], {}),      # a value flag without a value
            (['pnpm', 'journey', 'S0-01', '--update', '--update'], {}),  # one option twice
            (['pnpm', 'check', '--focus'], {}),                 # args = "none", extras refused
            (['pnpm', 'dev:stack', 'stop'], {}),                # the job's own reject list
            (['pnpm', 'journey', 'S0-01'], {'env': {'JOURNEY_SHARD': '1/4'}}),
            (['pnpm', 'unit', 'src/x.test.ts'], {'cwd': 'apps/agent'}),
        ):
            with self.subTest(argv=argv):
                verdict = classify.classify(self.config, argv, **kwargs)
                self.assertEqual((verdict['decision'], verdict['exit']), ('reject', 64))

    def test_an_argument_may_not_escape_the_worktree(self):
        verdict = classify.classify(self.config, ['pnpm', 'journey', '../elsewhere'])
        self.assertEqual(verdict['decision'], 'reject')
        self.assertIn('inside the worktree', verdict['message'])

    def test_a_shard_variable_in_the_environment_is_refused_not_ignored(self):
        verdict = classify.classify(self.config, ['pnpm', 'journey', 'S0-01'],
                                    env={'JOURNEY_SHARD': '1/4'})
        self.assertEqual(verdict['decision'], 'reject')
        self.assertIn('JOURNEY_SHARD', verdict['message'])

    def test_a_subdirectory_invocation_is_re_rooted_when_nothing_names_a_path(self):
        # `S0-01` means the same thing in every directory, because the repository
        # resolves it against its own catalog.
        verdict = classify.classify(self.config, ['pnpm', 'journey', 'S0-01'], cwd='apps/agent')
        self.assertEqual(verdict['decision'], 'remote')
        self.assertEqual(verdict['rerooted'], 'apps/agent')

    def test_a_subdirectory_invocation_with_a_path_is_refused_with_64(self):
        # Never passed through: a claimed command run here unbounded is the
        # accident the fallback rule exists to prevent.
        verdict = classify.classify(self.config, ['pnpm', 'unit', 'src/x.test.ts'],
                                    cwd='apps/agent')
        self.assertEqual(verdict['decision'], 'reject')
        self.assertEqual((verdict['code'], verdict['exit']), ('subdirectory', 64))
        self.assertEqual(verdict['message'], 'run from the repo root to route')
        self.assertEqual(verdict['blocked_by'], 'src/x.test.ts')

    def test_a_bare_name_that_is_a_real_file_here_is_refused_too(self):
        # No slash, but it exists relative to where it was typed, so re-rooting
        # it would silently change which file the selector names.
        verdict = classify.classify(self.config, ['pnpm', 'unit', 'x.test.ts'],
                                    cwd='apps/agent', exists=lambda token: token == 'x.test.ts')
        self.assertEqual((verdict['decision'], verdict['exit']), ('reject', 64))

    def test_the_old_name_for_re_rooting_still_loads(self):
        text = EXAMPLE.read_text().replace('subdirectory = "reroot"', 'subdirectory = "local"')
        self.assertEqual(loader.validate(tomllib.loads(text))['matching']['subdirectory'],
                         'reroot')

    def test_the_root_invocation_is_never_re_rooted(self):
        verdict = classify.classify(self.config, ['pnpm', 'journey', 'S0-01'])
        self.assertIsNone(verdict['rerooted'])

    def test_a_subdirectory_can_be_refused_instead(self):
        config = loader.load(EXAMPLE)
        config['matching']['subdirectory'] = 'reject'
        verdict = classify.classify(config, ['pnpm', 'journey', 'S0-01'], cwd='apps/agent')
        self.assertEqual((verdict['decision'], verdict['exit']), ('reject', 64))

    def test_a_subdirectory_can_claim_nothing_instead(self):
        # `pnpm test` in a package is that package's test, not the root's job.
        config = loader.load(EXAMPLE)
        config['matching']['subdirectory'] = 'passthrough'
        verdict = classify.classify(config, ['pnpm', 'journey', 'S0-01'], cwd='apps/agent')
        self.assertEqual((verdict['decision'], verdict['plan']), ('local', None))
        self.assertIn('worktree root', verdict['reason'])
        self.assertIn('apps/agent', verdict['reason'])
        # A path in the argv is not refused either: nothing is claimed there.
        verdict = classify.classify(config, ['pnpm', 'unit', 'src/x.test.ts'], cwd='apps/agent')
        self.assertEqual(verdict['decision'], 'local')
        with self.assertRaises(NotClaimed):
            classify.claimed_or_raise(config, ['pnpm', 'journey', 'S0-01'], cwd='apps')
        self.assertEqual(classify.classify(config, ['pnpm', 'journey', 'S0-01'])['decision'],
                         'remote')

    def test_passthrough_is_a_subdirectory_mode_the_loader_accepts(self):
        text = EXAMPLE.read_text().replace('subdirectory = "reroot"',
                                           'subdirectory = "passthrough"')
        self.assertEqual(loader.validate(tomllib.loads(text))['matching']['subdirectory'],
                         'passthrough')
        text = EXAMPLE.read_text().replace('subdirectory = "reroot"', 'subdirectory = "root"')
        with self.assertRaises(ConfigError) as caught:
            loader.validate(tomllib.loads(text))
        self.assertIn('passthrough', str(caught.exception))

    def test_update_arms_the_writeback_output(self):
        plain = classify.classify(self.config, ['pnpm', 'journey', 'S0-01'])['plan']
        armed = classify.classify(self.config, ['pnpm', 'journey', 'S0-01', '--update'])['plan']
        self.assertEqual([item['kind'] for item in plain['outputs']], ['artifacts'])
        self.assertIn('writeback', [item['kind'] for item in armed['outputs']])
        self.assertTrue(armed['options']['update'])

    def test_an_unknown_flag_is_forwarded_not_refused(self):
        # Pandora does not know acme's flags. The repository's own validator
        # is what refuses them, one step later and in its own words.
        verdict = classify.classify(self.config, ['pnpm', 'journey', 'S0-01', '--nope'])
        self.assertEqual(verdict['decision'], 'remote')
        self.assertIn('--nope', verdict['forwarded'])

    def test_a_value_flag_keeps_its_value_unexamined(self):
        verdict = classify.classify(self.config, ['pnpm', 'journey', 'S0-01',
                                                  '--fault', '../weird'])
        self.assertEqual(verdict['decision'], 'remote')
        self.assertEqual(verdict['forwarded'][-2:], ['--fault', '../weird'])

    def test_the_policy_index_carries_size_and_fallback_to_the_marker(self):
        index = {tuple(item['prefix']): item
                 for item in classify.policy_index(self.config)}
        self.assertEqual(index[('journey',)]['size'], 'large')
        self.assertTrue(index[('journey',)]['writeback'])
        self.assertEqual(index[('node',)]['size'], 'small')
        self.assertFalse(index[('node',)]['writeback'])

    def test_the_claim_index_is_what_the_shim_reads(self):
        self.assertEqual(classify.claim_index(self.config),
                         [['check'], ['check:code'], ['check:docs'], ['dev:stack'],
                          ['journey'], ['native-unit'], ['node'],
                          ['test:native-unit'], ['test:unit'], ['unit']])


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
