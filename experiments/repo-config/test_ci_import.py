"""What a real GitHub Actions workflow can and cannot lend a Pandora job.

The fixture under ``fixtures/acme`` is a verbatim trim of acme's
``.github/workflows/ci.yml`` -- the four jobs a worker would care about, copied
line for line.  Every assertion below is therefore a claim about the real file,
not about a workflow written to make the importer look good.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ci_import
from ci_import import CiImportError, drift, import_job, load_workflow, normalize

REAL = HERE / 'fixtures' / 'acme' / '.github' / 'workflows' / 'ci.yml'
BROKEN = HERE / 'fixtures' / 'unimportable.yml'
ANCHORS = HERE / 'fixtures' / 'anchors.yml'


def facts(job, **kwargs):
    document, _ = load_workflow(REAL)
    return import_job(document, job, **kwargs)


class ParserTests(unittest.TestCase):
    def test_a_parser_is_found_and_named(self):
        _document, parser = load_workflow(REAL)
        self.assertIn(parser, ('pyyaml', 'ruby'))

    def test_ruby_is_a_working_fallback_for_the_same_file(self):
        text = REAL.read_text()
        try:
            through_ruby = ci_import._ruby(text, 'ci.yml')
        except (CiImportError, OSError):
            self.skipTest('ruby is unavailable on this machine')
        if through_ruby is None:
            self.skipTest('ruby is unavailable on this machine')
        through_python = ci_import._pyyaml(text, 'ci.yml')
        if through_python is None:
            self.skipTest('PyYAML is unavailable on this machine')
        # GitHub's `on:` is YAML 1.1's boolean `true` in both parsers; compare the
        # part that matters.
        self.assertEqual(json.loads(json.dumps(through_ruby['jobs'])),
                         json.loads(json.dumps(through_python['jobs'])))

    def test_anchors_are_refused_by_name(self):
        with self.assertRaisesRegex(CiImportError, r'anchor &postgres'):
            load_workflow(ANCHORS)

    def test_a_committed_snapshot_loads_and_reports_its_freshness(self):
        import hashlib
        document, _ = load_workflow(REAL)
        snapshot = HERE / 'fixtures' / 'ci-snapshot.json'
        snapshot.write_text(json.dumps({
            'version': 1, 'source': '.github/workflows/ci.yml',
            'sha256': hashlib.sha256(REAL.read_bytes()).hexdigest(),
            'workflow': json.loads(json.dumps({'jobs': document['jobs']}))}))
        try:
            loaded, parser = load_workflow(snapshot)
            self.assertEqual(parser, 'snapshot')
            self.assertEqual(import_job(loaded, 'postgres')['timeout_minutes'], 15)
            self.assertTrue(ci_import.snapshot_is_fresh(snapshot, REAL))
            stale = HERE / 'fixtures' / 'ci-stale.json'
            value = json.loads(snapshot.read_text())
            value['sha256'] = '0' * 64
            stale.write_text(json.dumps(value))
            self.assertFalse(ci_import.snapshot_is_fresh(stale, REAL))
            stale.unlink()
        finally:
            snapshot.unlink(missing_ok=True)


class RealWorkflowTests(unittest.TestCase):
    def test_postgres_lends_two_services_with_health_and_ports(self):
        value = facts('postgres')
        self.assertEqual(sorted(value['services']), ['postgres', 'wsproxy'])
        db = value['services']['postgres']
        self.assertEqual(db['image'], 'postgres:16')
        self.assertEqual(db['env']['POSTGRES_PASSWORD'], 'ci-owner')
        self.assertEqual(db['ports'], [{'host': 5432, 'container': 5432}])
        self.assertEqual(db['health'], {'argv': ['pg_isready', '-U', 'app_owner', '-d', 'app'],
                                        'attempts': 10, 'interval_ms': 5000,
                                        'timeout_ms': 5000, 'start_period_ms': 0})
        # The published mapping is not an identity: 5433 on the host, 80 inside.
        self.assertEqual(value['services']['wsproxy']['ports'],
                         [{'host': 5433, 'container': 80}])

    def test_postgres_job_has_no_pooler_although_pandora_always_started_one(self):
        """A fact the welded adapter had wrong, recovered by reading the workflow."""
        value = facts('postgres')
        self.assertNotIn('pgbouncer', value['services'])
        self.assertEqual(value['services']['wsproxy']['env']['ALLOW_ADDR_REGEX'],
                         '^postgres:5432$')

    def test_journeys_lends_env_shards_timeout_and_node(self):
        value = facts('journeys')
        self.assertEqual(value['env']['DATABASE_OWNER_URL'],
                         'postgres://app_owner:ci-owner@localhost:5432/app')
        self.assertEqual(sorted(value['env']), ['DATABASE_OWNER_URL', 'VITE_API_URL',
                                                'VITE_DESK_API_URL', 'VITE_APP_API_URL'])
        self.assertEqual(value['shards'], {
            'dimension': 'shard', 'total': 4,
            'consumed': {'kind': 'env', 'name': 'JOURNEY_SHARD',
                         'where': 'ci.yml:journeys.steps[4].run'}})
        self.assertEqual(value['timeout_minutes'], 60)
        self.assertEqual(value['node'], '24')
        self.assertEqual(sorted(value['services']), ['pgbouncer', 'postgres', 'wsproxy'])

    def test_journeys_artifacts_include_a_path_outside_the_worktree(self):
        paths = facts('journeys')['artifacts'][0]['paths']
        self.assertIn('/tmp/app-stack.log', paths)
        self.assertIn('packages/scenarios/.journeys/results.json', paths)

    def test_surfaces_needs_its_agent_chosen_dimension_declared(self):
        with self.assertRaisesRegex(CiImportError, r'matrix\.app is a dimension pandora cannot'):
            facts('surfaces')
        value = facts('surfaces', matrix_params=('app',))
        self.assertEqual(value['shards']['total'], 2)
        self.assertEqual(value['shards']['consumed']['kind'], 'argv')
        self.assertEqual(value['shards']['consumed']['name'], '--shard')

    def test_surfaces_artifact_paths_keep_their_matrix_expression(self):
        entry = facts('surfaces', matrix_params=('app',))['artifacts'][0]
        self.assertEqual(entry['dimensions'], ['app'])
        self.assertEqual(entry['paths'][0], 'apps/${{ matrix.app }}/test-results/')

    def test_browser_integration_lends_only_a_timeout_and_an_artifact(self):
        value = facts('browser-integration')
        self.assertEqual(value['services'], {})
        self.assertEqual(value['shards'], None)
        self.assertEqual(value['timeout_minutes'], 15)
        self.assertEqual(value['artifacts'][0]['paths'], ['test-results/browser-integration/'])

    def test_provenance_names_the_file_job_and_field(self):
        value = facts('journeys')
        self.assertEqual(value['provenance']['services.postgres'],
                         'ci.yml:journeys.services.postgres')
        self.assertEqual(value['provenance']['env.VITE_API_URL'], 'ci.yml:journeys.env.VITE_API_URL')
        self.assertEqual(value['provenance']['shards'], 'ci.yml:journeys.strategy.matrix.shard')

    def test_an_unknown_job_lists_the_ones_that_exist(self):
        document, _ = load_workflow(REAL)
        with self.assertRaisesRegex(CiImportError, 'browser-integration, journeys'):
            import_job(document, 'journey')


class RefusalTests(unittest.TestCase):
    """Every shape a CI maintainer may reach for that ends the import."""

    def refuse(self, job, fragment, **kwargs):
        document, _ = load_workflow(BROKEN)
        with self.assertRaises(CiImportError) as caught:
            import_job(document, job, **kwargs)
        self.assertIn(fragment, str(caught.exception))

    def test_a_container_job_is_refused(self):
        self.refuse('in-container', 'container cannot be imported')

    def test_a_reusable_workflow_call_is_refused(self):
        self.refuse('reusable', 'reusable-workflow call')

    def test_service_credentials_and_volumes_are_refused(self):
        self.refuse('private-registry', 'credentials cannot be imported')
        self.refuse('bind-mount', 'volumes cannot be imported')

    def test_a_non_matrix_expression_is_refused(self):
        self.refuse('github-expression', 'only ${{ matrix.<name> }} can be imported')

    def test_a_shard_that_disappears_into_a_composite_action_is_refused(self):
        self.refuse('hidden-shard', 'never consumes matrix.shard')

    def test_an_incomplete_shard_list_is_refused(self):
        self.refuse('ragged-shard', 'not a complete 1..n shard list')

    def test_an_isolation_changing_docker_option_is_refused(self):
        self.refuse('exotic-option', 'uses the docker option --cpus')

    def test_an_unknown_upload_artifact_input_is_refused(self):
        self.refuse('odd-artifact', 'unknown key tags')

    def test_a_node_version_file_is_refused(self):
        self.refuse('floating-node', 'node-version-file points outside the workflow')


class NormalizeTests(unittest.TestCase):
    def test_pins_and_roles_are_applied_before_comparison(self):
        flat = normalize(facts('postgres'),
                         pins={'postgres:16': 'postgres@sha256:' + 'a' * 64},
                         roles={'postgres': 'db', 'wsproxy': 'proxy'})
        self.assertEqual(flat['services.db.image'], 'postgres@sha256:' + 'a' * 64)
        self.assertEqual(flat['services.proxy.ports'], [80])
        self.assertEqual(flat['timeout_minutes'], 15)

    def test_drift_separates_disagreement_from_absence(self):
        findings = drift({'a': 1, 'b': 2, 'c': 3}, {'a': 1, 'b': 9, 'd': 4})
        self.assertEqual(findings, [
            {'field': 'b', 'kind': 'differs', 'ci': 2, 'pandora': 9},
            {'field': 'c', 'kind': 'only-in-ci', 'ci': 3, 'pandora': None},
            {'field': 'd', 'kind': 'only-in-pandora', 'ci': None, 'pandora': 4},
        ])

    def test_exceptions_accept_a_glob(self):
        self.assertEqual(drift({'services.db.image': 'x'}, {'services.db.image': 'y'},
                               ['services.*']), [])


class CommandLineTests(unittest.TestCase):
    def test_lint_reports_drift_and_exits_65(self):
        process = subprocess.run(
            [sys.executable, str(HERE / 'plan.py'), 'lint',
             '--config', str(HERE / 'examples' / 'acme.pandora.toml'),
             '--repo-root', str(HERE / 'fixtures' / 'acme'), '--json'],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(process.returncode, 65)
        reports = {report['job']: report for report in json.loads(process.stdout)}
        # The shard count is excepted: the worker legitimately runs four where CI
        # runs two.  The artifact paths are not, and the finding is the real one:
        # CI names the app with a matrix expression, Pandora with a glob, so
        # nothing checks that they still cover the same directories.
        self.assertEqual(reports['surface']['exceptions'], ['shards.total'])
        self.assertEqual(reports['surface']['findings'],
                         [{'field': 'artifacts', 'kind': 'differs',
                           'ci': ['apps/${{ matrix.app }}/playwright-report',
                                  'apps/${{ matrix.app }}/test-results'],
                           'pandora': ['apps/*/playwright-report', 'apps/*/test-results']}])


if __name__ == '__main__':
    unittest.main()
