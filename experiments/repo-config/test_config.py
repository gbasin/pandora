import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import ConfigError, load, validate

EXAMPLES = Path(__file__).resolve().parent / 'examples'

MINIMAL = {
    'version': 1,
    'repo': {'name': 'demo', 'entrypoints': ['pnpm']},
    'runtime': {'base_image': 'node@sha256:' + 'a' * 64},
    'prepare': {'argv': ['pnpm', 'install'], 'cache_key_paths': ['pnpm-lock.yaml']},
    'jobs': [{'id': 'unit', 'cpu_millis': 1000, 'memory_mib': 1024,
              'forms': [{'prefix': ['test:unit']}],
              'run': {'argv': ['node', '--test']}}],
}


def with_job(**patch):
    value = copy.deepcopy(MINIMAL)
    value['jobs'][0].update(patch)
    return value


class LoadTests(unittest.TestCase):
    def test_both_examples_load(self):
        for name in ('eichler.pandora.toml', 'generic.pandora.toml'):
            with self.subTest(name=name):
                config = load(EXAMPLES / name)
                self.assertEqual(config['version'], 1)
                self.assertTrue(config['jobs'])

    def test_eichler_example_covers_the_v0_1_1_boundary_and_two_new_jobs(self):
        config = load(EXAMPLES / 'eichler.pandora.toml')
        self.assertEqual(set(config['jobs']), {
            'unit', 'tools', 'full', 'agent-web', 'employee-browser', 'browser-integration',
            'mockup-browser', 'postgres', 'journey', 'journeys', 'surface', 'check', 'native-unit'})
        self.assertEqual(set(config['services']), {'db', 'pool', 'proxy'})

    def test_minimal_configuration_normalizes_defaults(self):
        config = validate(copy.deepcopy(MINIMAL))
        job = config['jobs']['unit']
        self.assertEqual(job['tool'], 'pnpm')
        self.assertEqual(job['shards'], None)
        self.assertEqual(job['fallback']['action'], 'fail')
        self.assertEqual(job['run']['cwd'], '.')

    def test_unreadable_or_invalid_file_names_the_path(self):
        with self.assertRaisesRegex(ConfigError, 'cannot read'):
            load(EXAMPLES / 'absent.toml')


class StrictnessTests(unittest.TestCase):
    def assertRefused(self, value, fragment):
        with self.assertRaises(ConfigError) as caught:
            validate(value)
        self.assertIn(fragment, str(caught.exception))

    def test_unknown_key_is_refused_with_the_allowed_set(self):
        value = copy.deepcopy(MINIMAL)
        value['jbos'] = []
        self.assertRefused(value, 'configuration has unknown key jbos; allowed:')
        self.assertRefused(with_job(sharding={}), 'jobs[0] has unknown key sharding')

    def test_missing_key_is_named(self):
        value = copy.deepcopy(MINIMAL)
        del value['prepare']
        self.assertRefused(value, 'configuration is missing prepare')

    def test_images_must_be_digest_pinned(self):
        value = copy.deepcopy(MINIMAL)
        value['runtime']['base_image'] = 'node:24'
        self.assertRefused(value, 'runtime.base_image must be digest-pinned')
        value = copy.deepcopy(MINIMAL)
        value['services'] = [{'id': 'db', 'image': 'postgres:16', 'cpu_millis': 1, 'memory_mib': 1}]
        self.assertRefused(value, 'services[0].image must be digest-pinned')

    def test_unknown_template_value_names_the_known_ones(self):
        self.assertRefused(with_job(run={'argv': ['node', '{p.app}']}),
                           'jobs.unit.run.argv uses unknown template value {p.app}')

    def test_rest_parameter_must_come_last_and_be_unique(self):
        rest = {'name': 'files', 'kind': 'rest'}
        self.assertRefused(with_job(params=[rest, {'name': 'app', 'kind': 'enum', 'values': ['a']}]),
                           'at most one rest parameter')

    def test_list_parameter_is_only_usable_as_a_whole_argv_element(self):
        job = with_job(params=[{'name': 'files', 'kind': 'rest'}],
                       run={'argv': ['node', 'prefix{p.files[]}']})
        self.assertRefused(job, 'uses unknown template value {p.files[]}')

    def test_writeback_output_requires_an_option_that_a_flag_sets(self):
        self.assertRefused(
            with_job(outputs=[{'kind': 'writeback', 'paths': ['fixtures/x.json'],
                               'requires_option': 'update'}]),
            "requires option 'update', which no flag of this job sets")
        self.assertRefused(
            with_job(flags=[{'name': '--update', 'kind': 'pandora'}],
                     outputs=[{'kind': 'writeback', 'paths': ['fixtures/x.json']}]),
            'writeback must name a requires_option')

    def test_outputs_stay_inside_the_worktree(self):
        self.assertRefused(with_job(outputs=[{'kind': 'artifacts', 'paths': ['../elsewhere']}]),
                           'must stay inside the worktree')

    def test_services_must_be_declared_before_use(self):
        self.assertRefused(with_job(services=['db']), 'names undeclared service(s): db')

    def test_shard_strategy_and_payload_must_agree(self):
        self.assertRefused(with_job(shards={'strategy': 'env'}),
                           "strategy 'env' requires env")
        self.assertRefused(with_job(shards={'strategy': 'argv', 'argv_append': ['--shard={shard.index}'],
                                            'env': {'X': '1'}}),
                           "strategy 'env' requires env and forbids it otherwise")

    def test_two_jobs_cannot_claim_the_same_form(self):
        value = copy.deepcopy(MINIMAL)
        value['jobs'].append({'id': 'other', 'cpu_millis': 1, 'memory_mib': 1,
                              'forms': [{'prefix': ['test:unit']}], 'run': {'argv': ['node']}})
        self.assertRefused(value, 'is claimed by both unit and other')

    def test_conditional_flag_must_name_a_real_parameter(self):
        self.assertRefused(
            with_job(flags=[{'name': '--foundation-only', 'kind': 'forward',
                             'requires': {'param': 'target', 'equals': 'api'}}]),
            'requires.param is not a parameter of this job: target')

    def test_pandora_flag_cannot_take_a_value(self):
        self.assertRefused(with_job(flags=[{'name': '--update', 'kind': 'pandora', 'arity': 1}]),
                           'of kind pandora must not take a value')

    def test_job_tool_must_be_a_declared_entrypoint(self):
        self.assertRefused(with_job(tool='npm'), 'is not a declared entrypoint: npm')


if __name__ == '__main__':
    unittest.main()
