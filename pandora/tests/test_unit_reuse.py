"""Pre-test reuse must fail open for unsafe inputs, drift, or untrusted evidence."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from pandora.client import reuse, verdicts
from pandora.engine import verdict
from pandora.tests.test_test_evidence import report
from pandora.tests.test_verdict import fields
from pandora.tests.test_verdicts import make_repo, sh


def reusable(run='r1'):
    value = report(run)
    value['selection']['reuse_safe_args'] = True
    for test in value['tests']:
        test.update(retry_count=0, repeat_count=0)
    value['profile'] = {'node': 'v24.19.0', 'ci': 'true', 'platform': 'linux',
        'os_release': 'worker-kernel', 'projects': [{'name': 'alpha', 'environment': 'node',
        'isolate': True, 'setup_files': [], 'global_setup': []}],
        'reuse': {'version': 1, 'root_global_setup': [], 'projects': [{'name': 'alpha', 'globals': False}]}}
    return value


def collection(tree='a' * 40):
    value = reusable()
    value.update(kind=reuse.COLLECTION, tree=tree, outcome='collected')
    value['profile']['os_release'] = 'ci-kernel'
    for test in value['tests']:
        test['status'] = 'pending'
    return value


class Selection(unittest.TestCase):
    def select(self, worker=None, ci=None, tree='a' * 40):
        body = {'tree': tree, 'run_id': 'r1'}
        return reuse.select(ci or collection(), [(body, worker or reusable())],
                            {('alpha', 'test/example.test.ts')}, tree='a' * 40)

    def test_portable_kernel_and_unused_project_differences_are_scoped(self):
        worker = reusable()
        worker['profile']['projects'].append({'name': 'unused'})
        worker['profile']['reuse']['projects'].append({'name': 'unused'})
        self.assertEqual(self.select(worker)[0]['file'], 'test/example.test.ts')
        self.assertEqual(self.select(tree='b' * 40), [])

    def test_incomplete_filtered_skipped_retried_or_ambiguous_files_do_not_qualify(self):
        for field, bad in [('name_pattern', 'a'), ('reuse_safe_args', False)]:
            worker = reusable()
            worker['selection'][field] = bad
            self.assertEqual(self.select(worker), [])
        for field, bad in [('status', 'skipped'), ('mode', 'skip'), ('location', None), ('retry_count', 1), ('repeat_count', 1)]:
            worker = reusable()
            worker['tests'][0][field] = bad
            self.assertEqual(self.select(worker), [])
        worker = reusable()
        worker['tests'].pop()
        self.assertEqual(self.select(worker), [])
        worker = reusable()
        worker['tests'][0]['name'] = 'different collection'
        self.assertEqual(self.select(worker), [])
        worker = reusable()
        worker['modules'][0]['state'] = 'failed'
        self.assertEqual(self.select(worker), [])
        for key, bad in [('complete', False), ('outcome', 'failed'), ('errors', ['error'])]:
            worker = reusable()
            worker[key] = bad
            with self.assertRaises(ValueError):
                self.select(worker)

    def test_runtime_and_effective_configuration_mismatches_remain_misses(self):
        for key, bad in [('node', 'v26'), ('ci', None), ('platform', 'darwin')]:
            worker = reusable()
            worker['profile'][key] = bad
            try:
                self.assertEqual(self.select(worker), [])
            except ValueError:
                pass
        worker = reusable()
        worker['profile']['reuse']['projects'][0]['globals'] = True
        self.assertEqual(self.select(worker), [])
        worker = reusable()
        worker['profile'].pop('reuse')
        self.assertEqual(self.select(worker), [])
        body = {'tree': 'a' * 40, 'run_id': 'r1'}
        self.assertEqual(len(reuse.select(collection(), [(body, worker), (body, reusable())],
            {('alpha', 'test/example.test.ts')}, tree='a' * 40)), 1)
        worker = reusable()
        worker['runner_version'] = '4.2'
        self.assertEqual(self.select(worker), [])

    def test_collection_is_not_a_pass_and_cross_project_exclusions_are_refused(self):
        ci = collection()
        other = copy.deepcopy(ci['tests'][0])
        other['project'] = 'beta'
        ci['tests'].append(other)
        self.assertEqual(self.select(ci=ci), [])
        ci = collection()
        ci['complete'] = False
        with self.assertRaises(ValueError):
            self.select(ci=ci)
        ci = reusable()
        with self.assertRaises(ValueError):
            self.select(ci=ci)
        ci = collection()
        ci['tests'][0]['status'] = 'failed'
        with self.assertRaises(ValueError):
            self.select(ci=ci)


class SignedPlan(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.repo = make_repo(self.root)
        self.engine = self.root / 'engine'
        _, public = verdict.ensure_key(self.engine)
        policy_dir = self.repo / '.github/pandora'
        policy_dir.mkdir(parents=True)
        (policy_dir / 'allowed_signers').write_text('pandora-verdict namespaces="pandora-verdict" ' + public + '\n')
        self.policy = policy_dir / 'unit-reuse.json'
        self.policy.write_text(json.dumps({'schema': 1, 'enabled': True, 'portable_files': [{
            'project': 'alpha', 'file': 'test/example.test.ts', 'portability': 'linux-node-isolated-v1',
            'guard_paths': ['test/example.test.ts', 'file.txt']}]}))
        file = self.repo / 'test/example.test.ts'
        file.parent.mkdir()
        file.write_text('audited deterministic test\n')
        self.git('add', '.')
        staged = self.git('write-tree')
        policy = json.loads(self.policy.read_text())
        policy['portable_files'][0]['guard_objects'] = {path: self.git('rev-parse', staged + ':' + path)
            for path in policy['portable_files'][0]['guard_paths']}
        self.policy.write_text(json.dumps(policy))
        self.commit(push=True)
        self.tree = self.git('rev-parse', 'HEAD^{tree}')
        self.manifest = self.root / 'collection.json'
        self.manifest.write_text(json.dumps(collection(self.tree)))
        self.publish()

    def git(self, *args):
        return sh('git', '-C', str(self.repo), *args)

    def commit(self, push=False):
        self.git('add', '.')
        self.git('-c', 'user.name=t', '-c', 'user.email=t@t', '-c', 'commit.gpgsign=false',
                 'commit', '-qm', 'change')
        if push:
            self.git('push', '-q', 'origin', 'main')

    def publish(self, tree=None):
        tree = tree or self.tree
        raw = json.dumps(reusable()) + '\n'
        binding = {'bytes': len(raw.encode()), 'sha256': hashlib.sha256(raw.encode()).hexdigest()}
        payload = verdict.payload(**fields(tree=tree, run_id='r1', repo='demo', job='unit', test_evidence=binding))
        signed = verdict.sign(self.engine, payload)
        result = verdicts.publish_evidence(self.repo, 'origin', {'tree': tree, 'verdict': signed,
                                                                'test_evidence': {'report': raw}})
        self.assertEqual(result['state'], 'published', result)

    def plan(self, **kwargs):
        return reuse.plan(self.repo, self.manifest, repo='demo', event='pull_request', **kwargs)

    def test_signed_evidence_canary_and_disabled_default_policy(self):
        result = self.plan(run_number=11)
        self.assertEqual(result['reason'], 'verified', result)
        self.assertEqual(result['skip_files'], ['test/example.test.ts'])
        canary = self.plan(run_number=20)
        self.assertEqual(canary['skip_files'], [])
        self.assertEqual(len(canary['eligible_files']), 1)
        self.assertTrue(canary['canary'])
        self.policy.write_text(self.policy.read_text().replace('true', 'false'))
        self.commit(push=True)
        new_tree = self.git('rev-parse', 'HEAD^{tree}')
        self.manifest.write_text(json.dumps(collection(new_tree)))
        self.publish(new_tree)
        self.assertEqual(self.plan()['reason'], 'policy_disabled')

    def test_pr_guard_change_cannot_be_overridden_by_pr_policy(self):
        (self.repo / 'file.txt').write_text('new ambient dependency\n')
        value = json.loads(self.policy.read_text())
        value['portable_files'][0]['guard_paths'] = ['test/example.test.ts']
        self.policy.write_text(json.dumps(value))
        self.commit()
        tree = self.git('rev-parse', 'HEAD^{tree}')
        self.manifest.write_text(json.dumps(collection(tree)))
        self.publish(tree)
        self.assertEqual(self.plan()['skip_files'], [])

    def test_default_branch_guard_changes_require_a_new_audited_object(self):
        (self.repo / 'file.txt').write_text('changed on main without policy review\n')
        self.commit(push=True)
        tree = self.git('rev-parse', 'HEAD^{tree}')
        self.manifest.write_text(json.dumps(collection(tree)))
        self.publish(tree)
        self.assertEqual(self.plan()['skip_files'], [])
        policy = json.loads(self.policy.read_text())
        policy['portable_files'][0]['guard_objects']['file.txt'] = self.git('rev-parse', 'HEAD:file.txt')
        self.policy.write_text(json.dumps(policy))
        self.commit(push=True)
        tree = self.git('rev-parse', 'HEAD^{tree}')
        self.manifest.write_text(json.dumps(collection(tree)))
        self.publish(tree)
        self.assertEqual(self.plan()['skip_files'], ['test/example.test.ts'])

    def test_tree_drift_missing_or_bad_collection_and_non_pr_run_normally(self):
        self.manifest.write_text('{broken')
        self.assertEqual(self.plan()['skip_files'], [])
        self.manifest.unlink()
        self.assertEqual(self.plan()['skip_files'], [])
        self.manifest.write_text(json.dumps(collection('b' * 40)))
        self.assertEqual(self.plan()['skip_files'], [])
        self.assertEqual(reuse.plan(self.repo, self.manifest, repo='demo', event='merge_group')['reason'], 'pr_only')
