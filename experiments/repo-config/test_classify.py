import json
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from classify import WORKER, classify
from config import load

ACME_ROOT = HERE / 'fixtures' / 'acme'
ACME = load(HERE / 'examples' / 'acme.pandora.toml', root=ACME_ROOT)
GENERIC = load(HERE / 'examples' / 'generic.pandora.toml')


def plan(config, argv, **kwargs):
    result = classify(config, argv, **kwargs)
    assert result['decision'] == 'remote', result
    return result['plan']


class PlanTests(unittest.TestCase):
    def test_journeys_fans_out_over_the_shard_pattern_ci_states(self):
        resolved = plan(ACME, ['journeys', '--keep-going'], shards=3)
        self.assertEqual(resolved['shard_count'], 3)
        self.assertEqual([s['env']['JOURNEY_SHARD'] for s in resolved['shards']],
                         ['1/3', '2/3', '3/3'])
        self.assertTrue(all(s['argv'] == resolved['shards'][0]['argv'] for s in resolved['shards']))
        self.assertTrue(resolved['options']['keep_going'])
        self.assertEqual(resolved['provenance']['shards'],
                         'ci.yml:journeys.strategy.matrix.shard')

    def test_surfaces_fan_out_over_an_argv_template_after_one_build(self):
        resolved = plan(ACME, ['test:surface', 'desk', 'pipeline.spec.ts'], shards=2)
        self.assertEqual([s['argv'][-1] for s in resolved['shards']], ['--shard=1/2', '--shard=2/2'])
        self.assertEqual(resolved['plan_step']['argv'],
                         ['node', 'tools/pandora-run.mjs', 'surface-plan', 'desk', 'pipeline.spec.ts'])
        self.assertEqual(resolved['plan_step']['emits'], 'apps/e2e-dist/pandora-inventory.json')
        self.assertIn({'kind': 'generated', 'paths': ['apps/*/dist', 'apps/*/e2e/dist']},
                      resolved['outputs'])

    def test_a_size_class_and_service_roles_become_worker_limits(self):
        """Review item 4: the repo asks for a class; the worker owns the numbers."""
        journeys = plan(ACME, ['journeys'])['resources']
        self.assertEqual(journeys['size'], 'medium')
        self.assertEqual(journeys['main'], WORKER['sizes']['medium'])
        # medium 1000/4096 plus db 500/768, pool 500/256 and proxy 500/128.
        self.assertEqual((journeys['cpu_millis'], journeys['memory_mib']), (2500, 5248))
        self.assertEqual(plan(ACME, ['test:unit'])['resources']['cpu_millis'], 1000)
        self.assertEqual(plan(ACME, ['check'])['resources']['memory_mib'], 2048)

    def test_a_class_this_worker_cannot_offer_is_reported_as_clamped(self):
        large = plan(ACME, ['test'])['resources']
        self.assertEqual(large['size'], 'large')
        self.assertEqual(large['clamped_to'], 'medium')
        self.assertIsNone(plan(ACME, ['test:unit'])['resources']['clamped_to'])

    def test_an_unknown_service_role_is_refused_with_the_roles_the_worker_has(self):
        worker = {'source': 'test', 'sizes': WORKER['sizes'], 'roles': {'db': WORKER['roles']['db']}}
        result = classify(ACME, ['journeys'], worker=worker)
        self.assertEqual(result['decision'], 'reject')
        self.assertIn("no limits for the service role 'pool'", result['message'])

    def test_service_urls_reach_the_job_environment_from_ci(self):
        resolved = plan(ACME, ['test:postgres', 'api'])
        environment = resolved['shards'][0]['env']
        self.assertEqual(environment['DATABASE_OWNER_URL'],
                         'postgres://app_owner:ci-owner@localhost:5432/app')
        self.assertEqual(resolved['provenance']['env.DATABASE_OWNER_URL'],
                         'ci.yml:postgres.env.DATABASE_OWNER_URL')

    def test_unset_is_explicit_rather_than_an_empty_string(self):
        """Review item 5: CI = "" only worked because Node treats '' as falsy."""
        resolved = plan(ACME, ['journeys'])
        self.assertEqual(resolved['env_unset'], ['CI'])
        self.assertNotIn('CI', resolved['shards'][0]['env'])
        self.assertEqual(plan(ACME, ['test:unit'])['shards'][0]['env']['CI'], 'true')

    def test_writeback_appears_only_under_update(self):
        kinds = [o['kind'] for o in plan(ACME, ['journey', 'S0-01'])['outputs']]
        self.assertNotIn('writeback', kinds)
        resolved = plan(ACME, ['journey', 'S0-01', '--update'])
        writeback = [o for o in resolved['outputs'] if o['kind'] == 'writeback'][0]
        self.assertEqual(writeback['paths'], ['packages/scenarios/fixtures/*.ledger.jsonl',
                                              'packages/scenarios/fixtures/write-routes.json'])
        self.assertIn('--update', resolved['shards'][0]['argv'])

    def test_arguments_are_forwarded_verbatim_to_the_repository_runner(self):
        """Review item 3: Pandora claims the form, not the argument grammar."""
        self.assertEqual(plan(ACME, ['test:postgres', 'api', '--foundation-only'])['shards'][0]['argv'],
                         ['node', 'tools/validate.mjs', 'postgres', 'api', '--foundation-only'])
        self.assertEqual(plan(ACME, ['test:postgres', 'scenarios'])['args'], ['scenarios'])
        # The repository's runner, not Pandora, decides this is nonsense.
        self.assertEqual(classify(ACME, ['test:postgres', 'scenarios', '--foundation-only'])['decision'],
                         'remote')

    def test_grep_pattern_is_never_mistaken_for_a_pandora_option(self):
        resolved = plan(ACME, ['test:surface', 'desk', '--grep', '--keep-going'])
        self.assertEqual(resolved['args'], ['desk', '--grep', '--keep-going'])
        self.assertFalse(resolved['options']['keep_going'])
        trailing = plan(ACME, ['test:surface', 'desk', '--grep', 'review', '--keep-going'])
        self.assertTrue(trailing['options']['keep_going'])
        self.assertEqual(trailing['args'], ['desk', '--grep', 'review'])

    def test_an_explicit_refusal_list_keeps_the_cheap_local_no(self):
        result = classify(ACME, ['test:surface', 'desk', '--ui'])
        self.assertEqual(result['decision'], 'reject')
        self.assertIn('interactive modes', result['message'])

    def test_fallback_policy_travels_with_the_plan(self):
        self.assertEqual(plan(ACME, ['test:unit'])['fallback'],
                         {'action': 'local', 'on': ['worker-unreachable', 'queue-timeout'],
                          'notice': ACME['fallback']['notice']})

    def test_timeout_comes_from_the_workflow_when_the_config_is_silent(self):
        self.assertEqual(plan(ACME, ['journeys'])['timeout_minutes'], 60)
        self.assertEqual(plan(ACME, ['test:postgres', 'api'])['timeout_minutes'], 15)
        self.assertEqual(plan(ACME, ['test:surface', 'desk'])['timeout_minutes'], 30)
        self.assertIsNone(plan(ACME, ['test:unit'])['timeout_minutes'])


class NetworkTests(unittest.TestCase):
    def test_one_namespace_per_run_with_explicit_service_name_aliases(self):
        network = plan(ACME, ['journeys'])['network']
        self.assertEqual(network['mode'], 'pod')
        self.assertEqual(network['published_ports'], [])
        for name in ('postgres', 'pgbouncer', 'wsproxy'):
            self.assertIn('%s:127.0.0.1' % name, network['add_host'])

    def test_a_non_identity_port_mapping_does_not_survive_a_shared_namespace(self):
        network = plan(ACME, ['journeys'])['network']
        self.assertEqual(network['port_forwards'],
                         [{'service': 'proxy', 'listen': 5433, 'target': 80}])

    def test_services_sharing_a_port_are_reported_rather_than_silently_broken(self):
        network = plan(ACME, ['test:postgres', 'api'])['network']
        self.assertEqual(network['port_conflicts'], [])
        self.assertEqual(network['port_forwards'],
                         [{'service': 'proxy', 'listen': 5433, 'target': 80}])


class FeedbackTests(unittest.TestCase):
    def test_rejections_name_the_supported_form(self):
        result = classify(ACME, ['journey', '--fault'])
        self.assertEqual(result['decision'], 'reject')
        self.assertIn('pnpm journey <id>', result['message'])
        self.assertTrue(result['message'].endswith('No validation started.'))

    def test_focused_forms_stay_local_per_spelling(self):
        self.assertEqual(classify(ACME, ['validate', 'tools', 'x.test.mjs'])['decision'], 'local')
        rejected = classify(ACME, ['test:tools', 'x.test.mjs'])
        self.assertEqual(rejected['decision'], 'reject')
        self.assertIn('pnpm validate tools <test files>', rejected['message'])

    def test_a_required_argument_is_part_of_the_claim(self):
        for argv in (['test:surface'], ['test:postgres'], ['journey']):
            with self.subTest(argv=argv):
                result = classify(ACME, argv)
                self.assertEqual(result['decision'], 'reject')
                self.assertTrue(result['message'].endswith('No validation started.'))

    def test_guarded_environment_stops_a_silently_different_run(self):
        result = classify(ACME, ['journeys'], env={'JOURNEY_SHARD': '1/2'})
        self.assertEqual(result['decision'], 'reject')
        self.assertIn('JOURNEY_SHARD', result['message'])
        self.assertEqual(classify(ACME, ['test:unit'], env={'JOURNEY_SHARD': '1/2'})['decision'],
                         'remote')

    def test_shard_count_is_bounded(self):
        self.assertEqual(classify(ACME, ['journeys'], shards=33)['decision'], 'reject')
        self.assertEqual(classify(ACME, ['test:unit'], shards=4)['decision'], 'reject')
        self.assertEqual(classify(ACME, ['journey', 'S0-01'], shards=2)['decision'], 'reject')


class SubdirectoryTests(unittest.TestCase):
    def test_file_arguments_are_rerooted_against_the_repository_root(self):
        resolved = plan(ACME, ['test:surface', 'desk', 'e2e/pipeline.spec.ts'], cwd='apps/desk')
        self.assertEqual(resolved['args'], ['desk', 'apps/desk/e2e/pipeline.spec.ts'])
        self.assertIn('re-rooted', resolved['reroot'])

    def test_a_bare_selector_word_is_not_treated_as_a_path(self):
        resolved = plan(ACME, ['test:postgres', 'api'], cwd='apps/api')
        self.assertEqual(resolved['args'], ['api'])
        self.assertIsNone(resolved['reroot'])

    def test_grep_pattern_is_not_rerooted(self):
        resolved = plan(ACME, ['test:surface', 'desk', '--grep', 'review'], cwd='apps/desk')
        self.assertEqual(resolved['args'], ['desk', '--grep', 'review'])
        self.assertIsNone(resolved['reroot'])

    def test_argument_free_jobs_are_unaffected(self):
        self.assertEqual(plan(ACME, ['test:unit'], cwd='apps/api')['cwd'], 'apps/api')


class GenericRepositoryTests(unittest.TestCase):
    def test_a_repository_with_no_acme_vocabulary_plans_the_same_way(self):
        resolved = plan(GENERIC, ['npm', 'test'])
        self.assertEqual(resolved['job'], 'node-tests')
        self.assertEqual(resolved['shards'][0]['argv'], ['npm', 'test'])
        self.assertEqual(resolved['shards'][0]['env']['REDIS_URL'], 'redis://127.0.0.1:6379/0')
        self.assertEqual((resolved['resources']['cpu_millis'], resolved['resources']['memory_mib']),
                         (1250, 4352))
        self.assertEqual(resolved['provenance']['size'], 'pandora.toml')

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

    def test_dry_run_prints_a_plan_with_provenance(self):
        code, out, _ = self.run_cli('--config', str(HERE / 'examples/acme.pandora.toml'),
                                    '--repo-root', str(ACME_ROOT),
                                    '--shards', '2', '--', 'pnpm', 'journeys', '--keep-going')
        self.assertEqual(code, 0)
        resolved = json.loads(out)['plan']
        self.assertEqual(resolved['job'], 'journeys')
        self.assertEqual(len(resolved['shards']), 2)
        self.assertEqual(resolved['provenance']['services.db'],
                         'ci.yml:journeys.services.postgres')

    def test_rejection_exits_64(self):
        code, out, _ = self.run_cli('--config', str(HERE / 'examples/acme.pandora.toml'),
                                    '--repo-root', str(ACME_ROOT),
                                    '--', 'pnpm', 'journeys', '--update', '--update')
        self.assertEqual(code, 64)
        self.assertEqual(json.loads(out)['decision'], 'reject')

    def test_unloadable_configuration_exits_78(self):
        code, _, err = self.run_cli('--config', str(HERE / 'examples/absent.toml'), '--', 'pnpm', 'test')
        self.assertEqual(code, 78)
        self.assertIn('cannot read', err)


if __name__ == '__main__':
    unittest.main()
