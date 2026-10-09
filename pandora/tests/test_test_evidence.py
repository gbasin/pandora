"""Real signing, Git publication and conservative observation comparison."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pandora.client import observations as shadow, verdicts
from pandora.engine import inventory as test_evidence, verdict, runner
from pandora.engine.ledger import Ledger
from pandora.tests.test_engine import claim, PLAN
from pandora.tests.test_verdict import fields
from pandora.tests.test_verdicts import make_repo, sh


def report(run='r1', *, pattern=None, selected=None):
    tests = [{'project': 'alpha', 'file': 'test/example.test.ts', 'name': name,
              'location': {'line': index + 1, 'column': 1}, 'collection_index': index, 'mode': 'run',
              'status': 'passed' if selected is None or name in selected else 'skipped',
              'duration_ms': 1.5} for index, name in enumerate(('a', 'b'))]
    return {'kind': test_evidence.KIND, 'v': 1, 'runner': 'vitest', 'runner_version': '4.1.11',
            'worker_run': run, 'complete': True, 'outcome': 'passed', 'errors': [],
            'profile': {'node': 'v24', 'ci': 'true', 'projects': ['alpha']},
            'selection': {'name_pattern': pattern}, 'tests': tests,
            'modules': [{'project': 'alpha', 'file': 'test/example.test.ts', 'state': 'passed'}]}


class Evidence(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.root = Path(home.name)
        self.outputs = [{'kind': 'artifacts', 'paths': [test_evidence.PATH]}]
        self.file = self.root / test_evidence.PATH
        self.file.parent.mkdir()

    def save(self, value):
        self.file.write_text(json.dumps(value) + '\n')
        return test_evidence.load(self.root, self.outputs, 'r1')

    def test_opt_in_missing_wrong_run_and_incomplete_fail_closed(self):
        self.assertEqual(test_evidence.load(self.root, [], 'r1'), (None, None))
        self.assertEqual(test_evidence.load(self.root, self.outputs, 'r1')[1], 'report_missing')
        self.assertEqual(self.save(report('old'))[1], 'report_run_mismatch')
        value = report()
        value['complete'] = False
        self.assertEqual(self.save(value)[1], 'report_not_passed')
        value = report()
        value['tests'][0]['mode'] = 'only'
        self.assertEqual(self.save(value)[1], 'report_not_passed')

    def test_raw_bytes_are_bound_and_limits_and_symlinks_are_refused(self):
        observed, reason = self.save(report())
        self.assertIsNone(reason)
        self.assertEqual(observed['sha256'], hashlib.sha256(self.file.read_bytes()).hexdigest())
        self.file.write_bytes(b'x' * (test_evidence.MAX_BYTES + 1))
        self.assertEqual(test_evidence.load(self.root, self.outputs, 'r1')[1], 'report_too_large')
        self.file.unlink()
        self.file.symlink_to('/etc/passwd')
        self.assertEqual(test_evidence.load(self.root, self.outputs, 'r1')[1], 'unsafe_report_path')

    def test_duplicate_or_incomplete_identity_is_not_attested(self):
        value = report()
        value['tests'].append(value['tests'][0])
        self.assertEqual(self.save(value)[1], 'invalid_report')
        value = report()
        value['tests'][0].pop('location')
        self.assertEqual(self.save(value)[1], 'invalid_report')

    def test_focused_union_never_becomes_a_complete_file_observation(self):
        tree = 'a' * 40
        body = {'tree': tree, 'run_id': 'r1', 'test_evidence': {'execution_seconds': 1}}
        result = shadow.compare(report(), [(body, report(pattern='a', selected=['a'])),
                                          (body, report(pattern='b', selected=['b']))], ci_tree=tree)
        self.assertEqual(result['observed_complete_files'], 0)
        self.assertEqual(sum(r['matching_passed_cases'] for r in result['observations']), 2)
        whole = shadow.compare(report(), [(body, report()), (body, report())], ci_tree=tree)
        self.assertEqual(whole['observed_complete_files'], 1)
        self.assertEqual(whole['matching_profile_and_tree_files'], 1)
        self.assertEqual(whole['ci_case_duration_ms_in_observed_files'], 3)

    def test_missing_locations_exclude_whole_files_on_both_sides(self):
        whole = report()
        healthy = copy.deepcopy(whole)
        for test in healthy['tests']:
            test['file'] = 'test/healthy.test.ts'
        healthy['modules'][0]['file'] = 'test/healthy.test.ts'
        whole['tests'].extend(healthy['tests'])
        whole['modules'].extend(healthy['modules'])
        partial = copy.deepcopy(whole)
        partial['tests'][0]['location'] = None
        observed, reason = self.save(partial)
        self.assertIsNone(reason)
        self.assertEqual(observed['report'].encode(), self.file.read_bytes())
        tree = 'a' * 40
        body = {'tree': tree, 'run_id': 'r1', 'test_evidence': {}}
        for ci, worker in [(partial, whole), (whole, partial), (partial, partial)]:
            with self.subTest(ci_partial=ci is partial, worker_partial=worker is partial):
                measured = shadow.compare(ci, [(body, worker)], ci_tree=tree)
                self.assertEqual(measured['required_files'], 2)
                self.assertEqual(measured['required_cases'], 4)
                self.assertEqual(measured['observed_complete_files'], 1)
                self.assertEqual(measured['observations'][0]['matching_passed_cases'], 2)
                self.assertEqual(measured['ci_case_duration_ms_in_observed_files'], 3)
                if ci is partial:
                    self.assertEqual(measured['identifiable_files'], 1)
                    self.assertEqual(measured['identifiable_cases'], 2)
                    self.assertEqual(measured['excluded_ci_files'][0], {
                        'project': 'alpha', 'file': 'test/example.test.ts',
                        'reason': 'missing_location', 'total_cases': 2, 'missing_location_cases': 1})
                if worker is partial:
                    self.assertEqual(len(measured['observations'][0]['excluded_worker_files']), 1)

    def test_null_locations_do_not_relax_other_validation_or_execution_rules(self):
        value = report()
        value['tests'][0]['location'] = None
        for key, bad in [('location', {}), ('collection_index', -1), ('file', '../bad.ts'),
                         ('status', 'unknown'), ('duration_ms', -1)]:
            with self.subTest(key=key):
                invalid = copy.deepcopy(value)
                invalid['tests'][0][key] = bad
                self.assertEqual(self.save(invalid)[1], 'invalid_report')
        for key, bad in [('status', 'failed'), ('status', 'pending'), ('mode', 'only')]:
            invalid = copy.deepcopy(value)
            invalid['tests'][0][key] = bad
            self.assertEqual(self.save(invalid)[1], 'report_not_passed')
        value['tests'][0]['mode'] = 'skip'
        value['tests'][0]['status'] = 'skipped'
        body = {'tree': 'a' * 40, 'run_id': 'r1', 'test_evidence': {}}
        measured = shadow.compare(report(), [(body, value)], ci_tree='a' * 40)
        self.assertEqual(measured['observed_complete_files'], 0)
        self.assertEqual(measured['observations'][0]['matching_passed_cases'], 0)

    def test_exclusion_is_scoped_to_project_and_file(self):
        value = report()
        other = copy.deepcopy(value)
        for test in other['tests']:
            test['project'] = 'beta'
        other['modules'][0]['project'] = 'beta'
        value['tests'].extend(other['tests'])
        value['modules'].extend(other['modules'])
        value['tests'][0]['location'] = None
        body = {'tree': 'a' * 40, 'run_id': 'r1', 'test_evidence': {}}
        measured = shadow.compare(value, [(body, value)], ci_tree='a' * 40)
        self.assertEqual(measured['required_files'], 2)
        self.assertEqual(measured['identifiable_files'], 1)
        self.assertEqual(measured['observed_complete_files'], 1)
        self.assertEqual(measured['excluded_ci_files'][0]['project'], 'alpha')

    def test_all_locationless_ci_files_report_exclusions_without_fetching(self):
        value = report()
        value['tests'][0]['location'] = None
        ci = self.root / 'partial-ci.json'
        ci.write_text(json.dumps(value))
        with mock.patch.object(verdicts, 'git', side_effect=AssertionError('must not fetch')):
            measured = shadow.measure(self.root, ci, repo='demo')
        self.assertEqual(measured['reason'], 'ci_no_identifiable_files')
        self.assertEqual(measured['required_files'], 1)
        self.assertEqual(measured['required_cases'], 2)
        self.assertEqual(measured['identifiable_files'], 0)
        self.assertEqual(measured['observed_complete_files'], 0)
        self.assertEqual(len(measured['excluded_ci_files']), 1)
        self.assertFalse(measured['skip_enabled'])

    def test_tree_profile_version_and_ci_failure_are_reported_separately(self):
        body = {'tree': 'b' * 40, 'run_id': 'r1', 'test_evidence': {}}
        value = report()
        value['profile']['node'] = 'v26'
        value['runner_version'] = 'different'
        ci = report()
        ci['outcome'] = 'failed'
        result = shadow.compare(ci, [(body, value)], ci_tree='a' * 40)
        self.assertEqual(result['observed_complete_files'], 1)
        self.assertEqual(result['matching_profile_and_tree_files'], 0)
        self.assertEqual(result['observations'][0]['incompatibilities'],
                         ['ci_tree_mismatch', 'runner_version_mismatch', 'profile_mismatch', 'ci_not_passed'])

    def test_real_signed_publication_keeps_runs_and_rejects_tampering(self):
        repo = make_repo(self.root / 'git')
        engine = self.root / 'engine'
        _, public = verdict.ensure_key(engine)
        signers = repo / '.github/pandora/allowed_signers'
        signers.parent.mkdir(parents=True)
        signers.write_text('pandora-verdict namespaces="pandora-verdict" ' + public + '\n')
        sh('git', '-C', str(repo), 'add', '.')
        sh('git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t',
           '-c', 'commit.gpgsign=false', 'commit', '-qm', 'trust')
        sh('git', '-C', str(repo), 'push', '-q', 'origin', 'main')
        tree = sh('git', '-C', str(repo), 'rev-parse', 'HEAD^{tree}')
        for run in ('r1', 'r2'):
            inventory = report(run)
            if run == 'r2':
                inventory['tests'][0]['location'] = None
            raw = json.dumps(inventory, indent=2) + '\n'
            digest = {'sha256': hashlib.sha256(raw.encode()).hexdigest(), 'bytes': len(raw.encode()),
                      'execution_seconds': 2.5}
            payload = verdict.payload(**fields(tree=tree, run_id=run, repo='demo', job='unit',
                                                test_evidence=digest))
            result = {'tree': tree, 'verdict': verdict.sign(engine, payload),
                      'test_evidence': {'report': raw}}
            record = verdicts.publish_evidence(repo, 'origin', result)
            self.assertEqual(record['state'], 'published', record)
            self.assertEqual(verdicts.publish_evidence(repo, 'origin', result)['state'], 'present')
        ci = self.root / 'ci.json'
        ci.write_text(json.dumps(report()))
        measured = shadow.measure(repo, ci, repo='demo')
        self.assertEqual(measured['reason'], 'observed', measured)
        self.assertEqual(len(measured['observations']), 2)
        self.assertEqual(measured['observed_complete_files'], 1)
        excluded = [entry for entry in measured['observations'] if entry['run_id'] == 'r2'][0]
        self.assertEqual(excluded['matching_passed_cases'], 0)
        self.assertEqual(len(excluded['excluded_worker_files']), 1)
        self.assertFalse(measured['skip_enabled'])
        result['test_evidence']['report'] += ' '
        self.assertEqual(verdicts.publish_evidence(repo, 'origin', result)['state'], 'failed')
        # Signed digest cannot rescue substituted bytes in a hosted artifact.
        bad = copy.deepcopy(result)
        bad['test_evidence']['report'] = '{}'
        ref = '%s/%s/unit/r2' % (verdicts.EVIDENCE_PREFIX, tree)
        commit = verdicts.build(repo, tree, 'unit', bad['verdict']['payload'],
                                bad['verdict']['signature'], bad['verdict']['signer'], report='{}')
        sh('git', '-C', str(repo), 'push', '-q', '--force', 'origin', commit + ':' + ref)
        measured = shadow.measure(repo, ci, repo='demo')
        self.assertEqual(len(measured['observations']), 1)
        self.assertEqual(measured['rejected'][0]['reason'], 'report_digest_mismatch')
        # A key added only to the worktree cannot establish trust.
        _, unknown = verdict.ensure_key(self.root / 'foreign-engine')
        signers.write_text('pandora-verdict ' + unknown + '\n')
        measured = shadow.measure(repo, ci, repo='demo')
        self.assertEqual(len(measured['observations']), 1)

    def test_worker_result_binds_collected_report_and_supervisor_timing(self):
        paths = runner.Paths(self.root / 'worker-engine').ensure()
        ledger = Ledger(paths.ledger)
        self.addCleanup(ledger.close)
        claim(ledger, outputs=self.outputs)
        attempt = paths.attempt('r1')
        attempt.mkdir(parents=True, exist_ok=True)
        (attempt / 'toolchain.json').write_text(json.dumps(PLAN['worker']))
        output = paths.outputs('r1') / test_evidence.PATH
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report()) + '\n')
        with mock.patch.object(verdict, 'ready_state', return_value='ready'), \
                mock.patch.object(verdict, 'worker_drift', return_value=''):
            result = runner.write_result(paths, ledger, 'r1', outcome='passed', layer='command',
                                         exit_code=0, peak_mib=1, durations={'execute': 7.25},
                                         evidence={}, receipt=None, tree='a' * 40)
        body = json.loads(result['verdict']['payload'])
        self.assertEqual(body['test_evidence']['sha256'], hashlib.sha256(output.read_bytes()).hexdigest())
        self.assertEqual(body['test_evidence']['execution_seconds'], 7.25)
        self.assertTrue(verdict.verify(result['verdict']['payload'], result['verdict']['signature'],
                                       result['verdict']['signer']))
        self.assertEqual(result['test_evidence']['report'].encode(), output.read_bytes())

    def test_identical_parameterized_titles_keep_distinct_collection_positions(self):
        value = report()
        value['tests'][1]['name'] = value['tests'][0]['name']
        value['tests'][1]['location'] = value['tests'][0]['location']
        observed, reason = self.save(value)
        self.assertIsNone(reason)
        body = {'tree': 'a' * 40, 'run_id': 'r1', 'test_evidence': {}}
        measured = shadow.compare(value, [(body, value)], ci_tree='a' * 40)
        self.assertEqual(measured['required_cases'], 2)
        self.assertEqual(measured['observed_complete_files'], 1)
        self.assertEqual(measured['observations'][0]['matching_passed_cases'], 2)

    def test_pathological_json_and_durations_cannot_fail_the_run(self):
        self.file.write_text('[' * 2000 + '0' + ']' * 2000)
        self.assertEqual(test_evidence.load(self.root, self.outputs, 'r1')[1], 'invalid_report')
        value = report()
        value['tests'][0]['duration_ms'] = 10 ** 1000
        self.assertEqual(self.save(value)[1], 'invalid_report')

    def test_zero_test_ci_selection_is_an_observation_not_a_transport_failure(self):
        value = report()
        value['tests'] = []
        value['modules'] = []
        ci = self.root / 'empty-ci.json'
        ci.write_text(json.dumps(value))
        observed = shadow.measure(self.root, ci, repo='demo')
        self.assertEqual(observed['reason'], 'ci_no_tests')
        self.assertEqual(observed['required_files'], 0)
        self.assertFalse(observed['skip_enabled'])
        self.assertEqual(self.save(value)[1], 'invalid_report')
