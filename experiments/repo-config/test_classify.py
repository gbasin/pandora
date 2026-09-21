import json
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from classify import classify
from config import load

EICHLER = load(HERE / 'examples' / 'eichler.pandora.toml')
GENERIC = load(HERE / 'examples' / 'generic.pandora.toml')


def plan(config, argv, **kwargs):
    result = classify(config, argv, **kwargs)
    assert result['decision'] == 'remote', result
    return result['plan']


class PlanTests(unittest.TestCase):
    def test_journeys_fans_out_over_a_shard_env_matrix(self):
        resolved = plan(EICHLER, ['journeys', '--keep-going'], shards=3)
        self.assertEqual(resolved['shard_count'], 3)
        self.assertEqual([s['env']['JOURNEY_SHARD'] for s in resolved['shards']],
                         ['1/3', '2/3', '3/3'])
        self.assertTrue(all(s['argv'] == resolved['shards'][0]['argv'] for s in resolved['shards']))
        self.assertTrue(resolved['options']['keep_going'])

    def test_surfaces_fan_out_over_an_argv_template_after_one_build(self):
        resolved = plan(EICHLER, ['test:surface', 'desk', 'pipeline.spec.ts'], shards=2)
        self.assertEqual([s['argv'][-1] for s in resolved['shards']], ['--shard=1/2', '--shard=2/2'])
        self.assertEqual(resolved['plan_step']['argv'],
                         ['pnpm', '--filter', '@eichler/desk', 'run', 'plan:e2e'])
        self.assertEqual(resolved['plan_step']['emits'],
                         'apps/desk/e2e/dist/pandora-inventory.json')
        self.assertEqual(resolved['plan_step']['services'], [])
        self.assertIn({'kind': 'generated', 'paths': ['apps/desk/dist', 'apps/desk/e2e/dist']},
                      resolved['outputs'])

    def test_demand_matches_worker_config_units(self):
        # main 2000/6144 plus db 500/768, pool 500/256 and proxy 500/128.
        self.assertEqual(plan(EICHLER, ['journeys'])['resources'],
                         {'cpu_millis': 3500, 'memory_mib': 7296, 'exclusive': []})
        self.assertEqual(plan(EICHLER, ['test:unit'])['resources'],
                         {'cpu_millis': 2000, 'memory_mib': 6144, 'exclusive': []})

    def test_service_urls_reach_the_job_environment(self):
        environment = plan(EICHLER, ['test:postgres', 'api'])['shards'][0]['env']
        self.assertEqual(environment['DATABASE_OWNER_URL'],
                         'postgres://ike_owner:local-owner@127.0.0.1:5432/ike')
        self.assertEqual(environment['DATABASE_WS_PROXY'], 'localhost:5433')

    def test_writeback_appears_only_under_update(self):
        kinds = [o['kind'] for o in plan(EICHLER, ['journey', 'S0-01'])['outputs']]
        self.assertNotIn('writeback', kinds)
        resolved = plan(EICHLER, ['journey', 'S0-01', '--update'])
        writeback = [o for o in resolved['outputs'] if o['kind'] == 'writeback'][0]
        self.assertEqual(writeback['paths'], ['packages/scenarios/fixtures/S0-01.ledger.jsonl',
                                              'packages/scenarios/fixtures/write-routes.json'])
        self.assertIn('--update', resolved['shards'][0]['argv'])

    def test_conditional_flag_is_forwarded_only_where_it_applies(self):
        self.assertEqual(plan(EICHLER, ['test:postgres', 'api', '--foundation-only'])['shards'][0]['argv'],
                         ['node', 'tools/validate.mjs', 'postgres', 'api', '--foundation-only'])
        self.assertEqual(plan(EICHLER, ['test:postgres', 'scenarios'])['shards'][0]['argv'],
                         ['node', 'tools/validate.mjs', 'postgres', 'scenarios'])
        self.assertEqual(classify(EICHLER, ['test:postgres', 'scenarios', '--foundation-only'])['decision'],
                         'reject')

    def test_grep_pattern_is_never_mistaken_for_a_pandora_option(self):
        resolved = plan(EICHLER, ['test:surface', 'desk', '--grep', '--keep-going'])
        self.assertIn('--grep', resolved['params']['selectors'])
        self.assertEqual(resolved['params']['selectors'], ['--grep', '--keep-going'])
        self.assertFalse(resolved['options']['keep_going'])

    def test_fallback_policy_travels_with_the_plan(self):
        self.assertEqual(plan(EICHLER, ['test:unit'])['fallback'],
                         {'action': 'local', 'on': ['worker-unreachable', 'queue-timeout'],
                          'notice': EICHLER['fallback']['notice']})


class FeedbackTests(unittest.TestCase):
    def test_rejections_name_the_supported_form(self):
        result = classify(EICHLER, ['journey', 'S0-02', '--fault', 'other'])
        self.assertEqual(result['decision'], 'reject')
        self.assertIn('pnpm journey <id>', result['message'])
        self.assertTrue(result['message'].endswith('No validation started.'))

    def test_focused_forms_stay_local_per_spelling(self):
        self.assertEqual(classify(EICHLER, ['validate', 'tools', 'x.test.mjs'])['decision'], 'local')
        rejected = classify(EICHLER, ['test:tools', 'x.test.mjs'])
        self.assertEqual(rejected['decision'], 'reject')
        self.assertIn('pnpm validate tools <test files>', rejected['message'])

    def test_guarded_environment_stops_a_silently_different_run(self):
        result = classify(EICHLER, ['journeys'], env={'JOURNEY_SHARD': '1/2'})
        self.assertEqual(result['decision'], 'reject')
        self.assertIn('JOURNEY_SHARD', result['message'])
        self.assertEqual(classify(EICHLER, ['test:unit'], env={'JOURNEY_SHARD': '1/2'})['decision'],
                         'remote')

    def test_shard_count_is_bounded(self):
        self.assertEqual(classify(EICHLER, ['journeys'], shards=33)['decision'], 'reject')
        self.assertEqual(classify(EICHLER, ['test:unit'], shards=4)['decision'], 'reject')


class SubdirectoryTests(unittest.TestCase):
    def test_selectors_are_rerooted_against_the_repository_root(self):
        resolved = plan(EICHLER, ['test:surface', 'desk', 'e2e/pipeline.spec.ts'], cwd='apps/desk')
        self.assertEqual(resolved['params']['selectors'], ['apps/desk/e2e/pipeline.spec.ts'])
        self.assertIn('re-rooted', resolved['reroot'])

    def test_grep_pattern_is_not_rerooted(self):
        resolved = plan(EICHLER, ['test:surface', 'desk', '--grep', 'review'], cwd='apps/desk')
        self.assertEqual(resolved['params']['selectors'], ['--grep', 'review'])
        self.assertIsNone(resolved['reroot'])

    def test_argument_free_jobs_are_unaffected(self):
        self.assertEqual(plan(EICHLER, ['test:unit'], cwd='apps/api')['cwd'], 'apps/api')


class GenericRepositoryTests(unittest.TestCase):
    def test_a_repository_with_no_eichler_vocabulary_plans_the_same_way(self):
        resolved = plan(GENERIC, ['npm', 'test'])
        self.assertEqual(resolved['job'], 'node-tests')
        self.assertEqual(resolved['shards'][0]['argv'], ['npm', 'test'])
        self.assertEqual(resolved['shards'][0]['env']['REDIS_URL'], 'redis://127.0.0.1:6379/0')
        self.assertEqual(resolved['resources'], {'cpu_millis': 2250, 'memory_mib': 4352, 'exclusive': []})

    def test_short_option_flags_and_argv_sharding(self):
        resolved = plan(GENERIC, ['npm', 'run', 'pytest', 'tests/unit', '-k', 'slow'], shards=2)
        self.assertEqual(resolved['shards'][1]['argv'],
                         ['.venv/bin/pytest', '-p', 'no:cacheprovider', 'tests/unit', '-k', 'slow',
                          '--shard-id=2', '--num-shards=2'])

    def test_focused_node_tests_stay_local(self):
        self.assertEqual(classify(GENERIC, ['npm', 'test', 'one.test.js'])['decision'], 'local')


class CommandLineTests(unittest.TestCase):
    def run_cli(self, *args):
        process = subprocess.run([sys.executable, str(HERE / 'plan.py'), *args],
                                 capture_output=True, text=True, timeout=60)
        return process.returncode, process.stdout, process.stderr

    def test_dry_run_prints_a_plan(self):
        code, out, _ = self.run_cli('--config', str(HERE / 'examples/eichler.pandora.toml'),
                                    '--shards', '2', '--', 'pnpm', 'journeys', '--keep-going')
        self.assertEqual(code, 0)
        resolved = json.loads(out)['plan']
        self.assertEqual(resolved['job'], 'journeys')
        self.assertEqual(len(resolved['shards']), 2)

    def test_rejection_exits_64(self):
        code, out, _ = self.run_cli('--config', str(HERE / 'examples/eichler.pandora.toml'),
                                    '--', 'pnpm', 'journeys', '--fault', 'dropped')
        self.assertEqual(code, 64)
        self.assertEqual(json.loads(out)['decision'], 'reject')

    def test_unloadable_configuration_exits_78(self):
        code, _, err = self.run_cli('--config', str(HERE / 'examples/absent.toml'), '--', 'pnpm', 'test')
        self.assertEqual(code, 78)
        self.assertIn('cannot read', err)


if __name__ == '__main__':
    unittest.main()
