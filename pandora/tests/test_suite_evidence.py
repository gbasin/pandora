"""Aggregate provenance, real signatures and separation from skip verdicts."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pandora.engine import suite, verdict, runner
from pandora.engine.ledger import Ledger
from pandora.client import verdicts
from pandora.tests.test_engine import claim
from pandora.tests.test_verdicts import make_repo, sh
from pandora.tests.test_shards import FanoutHarness

TREE = 'ab' * 20
PINS = {'image': 'c' * 64, 'lockfiles': {'pnpm-lock.yaml': 'd' * 64}}


def fixture():
    parent = {'role': 'parent', 'run_id': 'rparent', 'repo': 'demo', 'job': 'surface',
              'input_id': 'a' * 64, 'argv': ['pnpm', 'test:surface', 'desk'], 'cwd': '.', 'env': {}}
    plan_result = dict(parent, role='plan', parent='rparent', run_id='rplan',
                       tree=TREE, outcome='passed', cli_exit=0)
    build = {'sha256': 'b' * 64, 'files': [{'path': 'apps/desk/dist/app.js', 'sha256': 'e' * 64}]}
    selection = {'app': 'desk', 'selectors': [], 'grep': None}
    plan_report = dict(selection, kind='pandora-playwright-plan', version=1, build=build,
                       tests=[{'id': 'a'}, {'id': 'b'}],
                       inventory=[{'shard': 1, 'testIds': ['a']}, {'shard': 2, 'testIds': ['b']}])
    children = []
    for index, identity in enumerate(('a', 'b'), 1):
        result = dict(parent, role='shard', parent='rparent', run_id='rchild%d' % index,
                      tree=TREE, outcome='passed', cli_exit=0, shard='%d/2' % index)
        report = dict(selection, kind='pandora-playwright-report', version=1,
                      build=copy.deepcopy(build), status='passed', exitCode=0, errors=0,
                      shard='%d/2' % index, observed=[{'id': identity}],
                      profile={'version': 1, 'workers': 2, 'runtime_complete': False},
                      outcomes=[{'id': identity, 'status': 'passed', 'attempts': [
                          {'retry': 0, 'status': 'passed', 'expected_status': 'passed'}]}])
        children.append((result, report, 'f' * 64, copy.deepcopy(PINS)))
    return parent, plan_result, plan_report, children


def assemble(values=None):
    return suite.assemble(*(values or fixture()), plan_digest='a' * 64)


class Aggregate(unittest.TestCase):
    def test_source_build_coverage_and_actual_child_profile_are_separate(self):
        report = assemble()
        self.assertEqual(report['source_tree'], TREE)
        self.assertEqual(report['planned'], ['a', 'b'])
        self.assertEqual([case['id'] for case in report['tests']], ['a', 'b'])
        self.assertFalse(report['profile']['execution']['runtime_complete'])
        self.assertEqual(report['profile']['golden_pins'], PINS)
        self.assertEqual(len(report['children']), 2)
        self.assertEqual(report['kind'], 'pandora-suite-observation')

    def test_missing_duplicate_or_wrong_shard_never_certifies_coverage(self):
        for mutate in (lambda v: v[3].pop(), lambda v: v[3].append(v[3][0]),
                       lambda v: v[3][0][0].update(shard='1/3'),
                       lambda v: v[3][0][1].update(observed=[{'id': 'b'}])):
            values = fixture()
            mutate(values)
            with self.assertRaises(ValueError):
                assemble(values)

    def test_mixed_source_context_runtime_build_and_selection_are_rejected(self):
        for target, field, value in (
                ('result', 'tree', '0' * 40), ('result', 'input_id', 'other'),
                ('result', 'parent', 'other'), ('result', 'repo', 'other'),
                ('result', 'job', 'other'), ('result', 'role', 'single'),
                ('result', 'cli_exit', 75), ('report', 'errors', 1),
                ('report', 'profile', {'workers': 4}), ('report', 'build', {'sha256': '0' * 64}),
                ('report', 'selectors', ['other.spec.ts']), ('report', 'shard', '2/2')):
            with self.subTest(field=field, target=target):
                values = fixture()
                values[3][0][0 if target == 'result' else 1][field] = value
                with self.assertRaises(ValueError):
                    assemble(values)
        values = fixture()
        values[3][0][3]['image'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'profile_mismatch'):
            assemble(values)

    def test_selected_cases_need_exact_outcomes_and_retry_history(self):
        for outcomes in ([], [{'id': 'a', 'status': 'passed'}],
                         [{'id': 'a', 'status': 'passed', 'attempts': []}],
                         [{'id': 'a', 'status': 'passed', 'attempts': [
                             {'retry': 1, 'status': 'passed', 'expected_status': 'passed'}]}],
                         [{'id': 'a', 'status': 'passed', 'attempts': [
                             {'retry': 0, 'status': 'failed', 'expected_status': 'passed'}]}]):
            values = fixture()
            values[3][0][1]['outcomes'] = outcomes
            with self.assertRaises(ValueError):
                assemble(values)

    def test_full_listing_cannot_be_narrowed_and_malformed_ids_never_disappear(self):
        for mutate in (lambda v: v[2]['tests'].append({'id': 'missing'}),
                       lambda v: v[2]['inventory'][0]['testIds'].append({}),
                       lambda v: v[2]['tests'].append({}),
                       lambda v: v[3][0][1]['observed'].append({}),
                       lambda v: v[3][0][1]['observed'].append({'id': 'a'})):
            values = fixture()
            mutate(values)
            with self.assertRaises(ValueError):
                assemble(values)

    def test_skips_and_test_retries_remain_explicit_observations_never_skip_authority(self):
        values = fixture()
        case = values[3][0][1]['outcomes'][0]
        case['attempts'] = [{'retry': 0, 'status': 'failed', 'expected_status': 'passed'},
                            {'retry': 1, 'status': 'passed', 'expected_status': 'passed'}]
        skipped = values[3][1][1]['outcomes'][0]
        skipped['status'] = skipped['attempts'][0]['status'] = 'skipped'
        report = assemble(values)
        self.assertEqual(len(report['tests'][0]['attempts']), 2)
        self.assertEqual(report['tests'][1]['status'], 'skipped')
        self.assertEqual(report['kind'], suite.KIND)

    def test_report_bytes_are_bounded_and_cannot_escape_collected_outputs(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'report.json'
            path.write_bytes(b'{"kind":"example"}\n')
            report, digest = suite.read(path, folder)
            self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(report['kind'], 'example')
            path.write_bytes(b'x' * (suite.MAX_BYTES + 1))
            with self.assertRaisesRegex(ValueError, 'too_large'):
                suite.read(path, folder)
            path.unlink()
            path.symlink_to('/etc/passwd')
            with self.assertRaisesRegex(ValueError, 'unsafe_suite_report'):
                suite.read(path, folder)


class Signing(unittest.TestCase):
    def test_real_signature_binds_report_with_a_distinct_non_skip_envelope(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(verdict, 'ready_state', return_value='ready'), \
                mock.patch.object(verdict, 'worker_drift', return_value=''):
            signature, artifact = suite.sign(folder, fixture()[0], assemble(), finished=1,
                                             golden='golden', golden_pins=PINS)
            body = json.loads(signature['payload'])
            self.assertEqual(body['kind'], suite.VERDICT_KIND)
            self.assertEqual(body['test_evidence']['sha256'], artifact['sha256'])
            self.assertTrue(verdict.verify(signature['payload'].encode(), signature['signature'],
                                           signature['signer']))
            self.assertNotEqual(body['kind'], verdict.KIND)
            with mock.patch.object(verdicts, 'has_remote', return_value=True):
                self.assertIsNone(verdicts.publish(folder, 'origin',
                    {'tree': TREE, 'verdict': signature, 'test_evidence': artifact}))

    def test_unready_or_drifted_worker_cannot_sign_observations(self):
        for ready, drift in [('unproven', ''), ('ready', 'image changed')]:
            with mock.patch.object(verdict, 'ready_state', return_value=ready), \
                    mock.patch.object(verdict, 'worker_drift', return_value=drift):
                with self.assertRaisesRegex(ValueError, 'worker_unproven'):
                    suite.sign('/unused', fixture()[0], assemble(), finished=1,
                               golden='golden', golden_pins=PINS)

    def test_existing_immutable_evidence_transport_carries_typed_suite_bytes(self):
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(verdict, 'ready_state', return_value='ready'), \
                mock.patch.object(verdict, 'worker_drift', return_value=''):
            root = Path(folder)
            repo = make_repo(root)
            remote = root / 'origin.git'
            signature, artifact = suite.sign(root / 'engine', fixture()[0], assemble(),
                                             finished=1, golden='golden', golden_pins=PINS)
            result = {'tree': TREE, 'verdict': signature, 'test_evidence': artifact}
            published = verdicts.publish_evidence(repo, 'origin', result)
            self.assertEqual(published['state'], 'published', published)
            payload = json.loads(sh('git', '-C', str(remote), 'show', published['ref'] + ':payload.json'))
            self.assertEqual(payload['kind'], suite.VERDICT_KIND)
            self.assertEqual(json.loads(sh('git', '-C', str(remote), 'show', published['ref'] + ':report.json'))['kind'],
                             suite.KIND)

    def test_result_signing_isolated_from_whole_run_verdicts_and_nonfatal(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = runner.Paths(folder).ensure()
            ledger = Ledger(paths.ledger)
            self.addCleanup(ledger.close)
            claim(ledger, run_id='rparent', role='parent', job='surface', repo='demo', input_id='a' * 64)
            with mock.patch.object(verdict, 'ready_state', return_value='ready'), \
                    mock.patch.object(verdict, 'worker_drift', return_value=''), \
                    mock.patch.object(runner, 'golden_and_pins', return_value=('golden', PINS)):
                result = runner.write_result(paths, ledger, 'rparent', outcome='passed',
                    layer='command', exit_code=0, peak_mib=0, durations={}, evidence={},
                    receipt={}, suite_report=assemble())
            self.assertEqual(json.loads(result['verdict']['payload'])['kind'], suite.VERDICT_KIND)
            self.assertIn('test_evidence', result)


class FullFanout(FanoutHarness):
    def typed_reports(self, *, collision=False):
        _, _, plan_report, _ = fixture()
        self.arrange(planned=plan_report)
        previous = self.driver.clone

        def clone(golden, run_id, limits=None):
            instance = previous(golden, run_id, limits=limits)
            row = self.role_of(run_id)
            if row['role'] == 'shard':
                index = row['shard_index']
                report = fixture()[3][index - 1][1]
                path = 'apps/desk/test-results/run-%d-of-%d.json' % (index, row['shard_total'])
                self.trees[run_id][path] = json.dumps(report)
                if collision:
                    self.trees[run_id]['apps/desk/test-results/common.txt'] = str(index)
            return instance

        self.driver.clone = clone
        self.driver.synthetic_git = lambda *args, **kwargs: (0.1, TREE, None)

    def test_complete_engine_fanout_signs_and_collisions_never_receive_a_signature(self):
        self.typed_reports()
        with mock.patch.object(verdict, 'ready_state', return_value='ready'), \
                mock.patch.object(verdict, 'worker_drift', return_value=''), \
                mock.patch.object(runner, 'golden_and_pins', return_value=('golden', PINS)):
            result = self.parent(want=2, synthetic_git=True)
        self.assertEqual(result['cli_exit'], 0)
        self.assertIsNotNone(result['verdict'], result)
        self.assertEqual(json.loads(result['verdict']['payload'])['kind'], suite.VERDICT_KIND)
        self.assertEqual(json.loads(result['test_evidence']['report'])['planned'], ['a', 'b'])

    def test_collision_before_signing_and_unknown_source_remain_unsigned(self):
        self.typed_reports(collision=True)
        with mock.patch.object(suite, 'sign', side_effect=AssertionError('must not sign')):
            result = self.parent(want=2, synthetic_git=True)
        self.assertEqual(result['cli_exit'], 75)
        self.assertIsNone(result['verdict'])


if __name__ == '__main__':
    unittest.main()
