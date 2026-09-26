import copy
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from config import ConfigError, load, validate

EXAMPLES = HERE / 'examples'
ACME_ROOT = HERE / 'fixtures' / 'acme'
DIGEST = 'sha256:' + 'a' * 64

MINIMAL = {
    'version': 1,
    'repo': {'name': 'demo', 'entrypoints': ['pnpm']},
    'runtime': {'base_image': 'node@' + DIGEST},
    'prepare': {'argv': ['pnpm', 'install'], 'cache_key_paths': ['pnpm-lock.yaml']},
    'jobs': [{'id': 'unit', 'forms': [{'prefix': ['test:unit']}],
              'run': {'argv': ['node', '--test']}}],
}


def with_job(**patch):
    value = copy.deepcopy(MINIMAL)
    value['jobs'][0].update(patch)
    return value


class LoadTests(unittest.TestCase):
    def test_both_examples_load(self):
        for name, root in (('acme.pandora.toml', ACME_ROOT), ('generic.pandora.toml', None)):
            with self.subTest(name=name):
                config = load(EXAMPLES / name, root=root)
                self.assertEqual(config['version'], 1)
                self.assertTrue(config['jobs'])

    def test_acme_example_covers_the_v0_1_1_boundary_and_two_new_jobs(self):
        config = load(EXAMPLES / 'acme.pandora.toml', root=ACME_ROOT)
        self.assertEqual(set(config['jobs']), {
            'unit', 'tools', 'full', 'agent-web', 'employee-browser', 'browser-integration',
            'mockup-browser', 'postgres', 'journey', 'journeys', 'surface', 'check', 'native-unit'})

    def test_minimal_configuration_normalizes_defaults(self):
        config = validate(copy.deepcopy(MINIMAL))
        job = config['jobs']['unit']
        self.assertEqual(job['tool'], 'pnpm')
        self.assertEqual(job['shards'], None)
        self.assertEqual(job['size'], 'medium')
        self.assertEqual(job['args'], 'none')
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

    def test_resource_numbers_are_no_longer_a_repository_concern(self):
        """Review item 4: a repo declares a size class, not the worker's millis."""
        self.assertRefused(with_job(cpu_millis=2000), 'unknown key cpu_millis')
        self.assertRefused(with_job(size='huge'), 'size must be one of small, medium, large')

    def test_a_discarded_key_is_now_a_refusal(self):
        """Review item 5: services[].memory_swap used to be accepted and dropped."""
        value = copy.deepcopy(MINIMAL)
        value['services'] = [{'id': 'db', 'image': 'postgres@' + DIGEST, 'memory_swap': 1}]
        self.assertRefused(value, 'services[0] has unknown key memory_swap')

    def test_images_must_be_whole_digest_pinned_references(self):
        value = copy.deepcopy(MINIMAL)
        value['runtime']['base_image'] = 'node:24'
        self.assertRefused(value, 'runtime.base_image must be digest-pinned')
        for image in ('postgres:16', 'postgres@sha256:abc', 'postgres:16 @' + DIGEST,
                      'evil.example.com/x@sha256:' + 'z' * 64):
            with self.subTest(image=image):
                value = copy.deepcopy(MINIMAL)
                value['services'] = [{'id': 'db', 'image': image}]
                with self.assertRaises(ConfigError) as caught:
                    validate(value)
                self.assertIn('services[0].image', str(caught.exception))

    def test_a_pin_turns_a_floating_ci_tag_into_a_digest(self):
        value = copy.deepcopy(MINIMAL)
        value['pins'] = {'postgres:16': 'postgres@' + DIGEST}
        value['services'] = [{'id': 'db', 'image': 'postgres:16'}]
        config = validate(value)
        self.assertEqual(config['services']['db']['image'], 'postgres@' + DIGEST)
        value['pins'] = {'postgres:16': 'postgres:17'}
        self.assertRefused(value, 'pins.postgres:16 must map to a digest')

    def test_unsetting_a_variable_is_explicit(self):
        """Review item 5: CI = "" is not the same as no CI in the environment."""
        value = copy.deepcopy(MINIMAL)
        value['env'] = {'set': {'CI': 'true'}}
        value['jobs'][0]['run'] = {'argv': ['node'], 'unset': ['CI']}
        config = validate(value)
        self.assertEqual(config['jobs']['unit']['run']['unset'], ['CI'])
        value['jobs'][0]['run'] = {'argv': ['node'], 'env': {'CI': ''}, 'unset': ['CI']}
        self.assertRefused(value, 'both sets and unsets CI')

    def test_setup_text_and_platform_key_the_dependency_image(self):
        """Review item 5: apt-get makes a digest-pinned base non-reproducible."""
        first = validate(copy.deepcopy(MINIMAL))['dependency_cache']
        value = copy.deepcopy(MINIMAL)
        value['runtime']['setup'] = ['apt-get install -y git']
        second = validate(value)['dependency_cache']
        value['runtime']['platform'] = 'linux/arm64'
        third = validate(value)['dependency_cache']
        self.assertNotEqual(first['setup_sha256'], second['setup_sha256'])
        self.assertNotEqual(second['setup_sha256'], third['setup_sha256'])

    def test_the_argv_language_no_longer_re_implements_a_cli_parser(self):
        """Review item 3: params, flag arity and argv templates are gone."""
        self.assertRefused(with_job(params=[{'name': 'app', 'kind': 'enum', 'values': ['a']}]),
                           'unknown key params')
        self.assertRefused(with_job(flags=[{'name': '--update', 'kind': 'pandora'}]),
                           'unknown key flags')
        self.assertRefused(with_job(run={'argv': ['node', '{p.app}']}),
                           'uses unknown template value {p.app}')

    def test_forwarded_arguments_need_a_place_in_the_argv(self):
        self.assertRefused(with_job(args='required', run={'argv': ['node']}),
                           'must place {args}')
        self.assertRefused(with_job(run={'argv': ['node', '{args}']}),
                           "uses {args} but the job declares args = 'none'")
        self.assertRefused(with_job(args='optional', run={'argv': ['node', '{args}', '{args}']}),
                           'uses {args} more than once')

    def test_a_refusal_list_cannot_name_something_the_job_accepts(self):
        self.assertRefused(
            with_job(args='optional', run={'argv': ['node', '{args}']},
                     value_flags=['--grep'],
                     reject=[{'args': ['--grep'], 'message': 'no'}]),
            'reject names --grep, which this job also accepts')

    def test_outputs_stay_inside_the_worktree(self):
        self.assertRefused(with_job(outputs=[{'kind': 'artifacts', 'paths': ['../elsewhere']}]),
                           'must stay inside the worktree')

    def test_writeback_output_requires_an_option_that_arms_it(self):
        self.assertRefused(
            with_job(outputs=[{'kind': 'writeback', 'paths': ['fixtures/x.json'],
                               'requires_option': 'update'}]),
            "requires option 'update', which no writeback option of this job sets")
        self.assertRefused(
            with_job(options=[{'name': '--update'}],
                     outputs=[{'kind': 'writeback', 'paths': ['fixtures/x.json'],
                               'requires_option': 'update'}]),
            "no writeback option of this job sets")

    def test_services_must_be_declared_before_use(self):
        self.assertRefused(with_job(services=['db']), 'names an undeclared service: db')

    def test_shard_strategy_and_payload_must_agree(self):
        self.assertRefused(with_job(shards={'strategy': 'env'}), "strategy 'env' requires env")
        self.assertRefused(with_job(shards={'strategy': 'argv', 'argv_append': ['--shard={shard.index}'],
                                            'env': {'X': '1'}}),
                           "strategy 'env' requires env and forbids it otherwise")

    def test_two_jobs_cannot_claim_the_same_form(self):
        value = copy.deepcopy(MINIMAL)
        value['jobs'].append({'id': 'other', 'forms': [{'prefix': ['test:unit']}],
                              'run': {'argv': ['node']}})
        self.assertRefused(value, 'is claimed by both unit and other')

    def test_job_tool_must_be_a_declared_entrypoint(self):
        self.assertRefused(with_job(tool='npm'), 'is not a declared entrypoint: npm')


class ImportTests(unittest.TestCase):
    def test_an_imported_job_carries_provenance_for_every_inherited_field(self):
        config = load(EXAMPLES / 'acme.pandora.toml', root=ACME_ROOT)
        origin = config['jobs']['journeys']['provenance']
        self.assertEqual(origin['services.db'], 'ci.yml:journeys.services.postgres')
        self.assertEqual(origin['services.pool'], 'ci.yml:journeys.services.pgbouncer')
        self.assertEqual(origin['shards'], 'ci.yml:journeys.strategy.matrix.shard')
        self.assertEqual(origin['timeout_minutes'], 'ci.yml:journeys.timeout-minutes')
        self.assertEqual(origin['env.DATABASE_OWNER_URL'], 'ci.yml:journeys.env.DATABASE_OWNER_URL')
        self.assertEqual(origin['env.JOURNEY_REPLAY'], 'pandora.toml')
        self.assertEqual(origin['size'], 'pandora.toml')
        self.assertEqual(origin['outputs.artifacts'], 'pandora.toml')

    def test_the_pin_table_is_what_makes_a_floating_ci_tag_runnable(self):
        config = load(EXAMPLES / 'acme.pandora.toml', root=ACME_ROOT)
        images = {s['role']: s['image'] for s in config['jobs']['journeys']['services']}
        self.assertTrue(all('@sha256:' in image for image in images.values()))
        self.assertTrue(images['pool'].startswith('edoburu/pgbouncer@sha256:'))

    def test_the_import_corrects_a_service_list_pandora_had_wrong(self):
        config = load(EXAMPLES / 'acme.pandora.toml', root=ACME_ROOT)
        roles = sorted(s['role'] for s in config['jobs']['postgres']['services'])
        self.assertEqual(roles, ['db', 'proxy'])
        proxy = next(s for s in config['jobs']['postgres']['services'] if s['role'] == 'proxy')
        self.assertEqual(proxy['env']['ALLOW_ADDR_REGEX'], '^postgres:5432$')

    def test_a_job_pinned_to_one_shard_inherits_the_world_but_not_the_marker(self):
        config = load(EXAMPLES / 'acme.pandora.toml', root=ACME_ROOT)
        self.assertEqual(config['jobs']['journey']['shards']['env'], {})
        self.assertEqual(config['jobs']['journeys']['shards']['env'],
                         {'JOURNEY_SHARD': '{shard.index}/{shard.total}'})

    def test_an_artifact_path_outside_the_worktree_is_refused_not_dropped(self):
        text = (EXAMPLES / 'acme.pandora.toml').read_text()
        # Remove the journeys job's declared outputs so the CI list is inherited.
        start = text.index('# Declared, not inherited')
        end = text.index('# ----', start)
        trimmed = HERE / 'fixtures' / 'inherit-artifacts.pandora.toml'
        trimmed.write_text(text[:start] + text[end:])
        try:
            with self.assertRaises(ConfigError) as caught:
                load(trimmed, root=ACME_ROOT)
            self.assertIn('/tmp/app-stack.log', str(caught.exception))
            self.assertIn('outside the worktree', str(caught.exception))
        finally:
            trimmed.unlink(missing_ok=True)

    def test_ci_job_without_a_workflow_is_refused(self):
        self.assertRaises(ConfigError, validate, with_job(ci_job='journeys'))

    def test_a_job_cannot_both_import_and_lint(self):
        value = with_job(ci_job='journeys', ci_lint={'job': 'journeys'})
        value['ci_workflow'] = '.github/workflows/ci.yml'
        value['pins'] = {'postgres:16': 'postgres@' + DIGEST,
                         'edoburu/pgbouncer:latest': 'edoburu/pgbouncer@' + DIGEST,
                         'ghcr.io/neondatabase/wsproxy:latest': 'ghcr.io/neondatabase/wsproxy@' + DIGEST}
        with self.assertRaises(ConfigError) as caught:
            validate(value, root=ACME_ROOT)
        self.assertIn('cannot both import ci_job and lint', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
