import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import subprocess

from surface_suite import aggregate, outputs_manifest, plan_digest, surface_request, validate_plan, validate_shard


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def plan():
    tests = [
        {'id': 'a', 'project': 'chromium', 'file': 'e2e/a.spec.ts', 'title': 'a'},
        {'id': 'b', 'project': 'chromium', 'file': 'e2e/b.spec.ts', 'title': 'b'},
    ]
    value = {'version': 1, 'parent_attempt': 'a' * 32, 'source_digest': 'b' * 64,
             'app': 'web', 'selectors': ['e2e/a.spec.ts', '--grep', 'loan'],
             'shard_count': 3, 'keep_going': False,
             'build': {'app': 'web', 'files': [{'path': 'apps/web/dist/x', 'sha256': 'c' * 64}],
                       'sha256': digest({'app': 'web', 'files': [{'path': 'apps/web/dist/x', 'sha256': 'c' * 64}]})},
             'tests': tests,
             'shards': [
                 {'index': 1, 'test_ids': ['a'], 'inventory_sha256': digest(['a'])},
                 {'index': 2, 'test_ids': ['b'], 'inventory_sha256': digest(['b'])},
                 {'index': 3, 'test_ids': [], 'inventory_sha256': digest([])},
             ]}
    value['plan_id'] = plan_digest(value)
    return value


def report(value, index, code=0):
    ids = value['shards'][index - 1]['test_ids']
    return {'version': 1, 'plan_id': value['plan_id'], 'parent_attempt': value['parent_attempt'],
            'source_digest': value['source_digest'], 'app': value['app'], 'shard': index,
            'planned_ids': ids, 'observed_ids': ids,
            'outcomes': [{'id': item, 'status': 'passed'} for item in ids], 'exit_code': code, 'detail': ''}


class SurfaceSuiteTests(unittest.TestCase):
    def test_request_accepts_private_keep_going_and_rejects_public_unknowns(self):
        self.assertEqual(surface_request({'action': 'run', 'app': 'desk', 'selectors': [], 'shard_count': 2, 'keep_going': True})['app'], 'desk')
        with self.assertRaises(ValueError): surface_request({'action': 'run', 'app': 'desk', 'selectors': ['--shard=1/2'], 'shard_count': 2, 'keep_going': False})

    def test_plan_requires_nonempty_exact_partition_including_empty_shards(self):
        value = plan()
        self.assertEqual(validate_plan(value)['shards'][2]['test_ids'], [])
        value['shards'][1]['test_ids'] = ['a']; value['shards'][1]['inventory_sha256'] = digest(['a']); value['plan_id'] = plan_digest(value)
        with self.assertRaises(ValueError): validate_plan(value)
        value = plan(); value['tests'] = []; value['plan_id'] = plan_digest(value)
        with self.assertRaises(ValueError): validate_plan(value)

    def test_shard_binds_exact_ids_and_aggregate_needs_every_index(self):
        value = plan()
        reports = [report(value, index) for index in range(1, 4)]
        self.assertEqual(aggregate(value, reports)['status'], 'pass')
        reports[1]['observed_ids'] = []
        with self.assertRaises(ValueError): validate_shard(reports[1], value, 2)
        with self.assertRaises(ValueError): aggregate(value, reports[:2])

    def test_success_allows_native_skips_and_expected_failures_but_not_real_failures(self):
        value = plan(); item = report(value, 1)
        item['outcomes'][0]['status'] = 'skipped'
        self.assertEqual(validate_shard(item, value, 1)['exit_code'], 0)
        item['outcomes'][0]['status'] = 'expected'
        self.assertEqual(validate_shard(item, value, 1)['exit_code'], 0)
        item['outcomes'][0]['status'] = 'failed'
        with self.assertRaises(ValueError): validate_shard(item, value, 1)
        item = report(value, 1); item['shard'] = True
        with self.assertRaises(ValueError): validate_shard(item, value, 1)
        with self.assertRaises(ValueError): validate_shard(report(value, 1), value, 4)

    def test_outputs_manifest_hashes_only_regular_generated_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for output in ('apps/desk/dist/x.js', 'apps/desk/e2e/dist/y.js'):
                path = root / output; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(output)
            manifest = outputs_manifest(root, 'desk')
            self.assertEqual([row['path'] for row in manifest['files']], ['apps/desk/dist/x.js', 'apps/desk/e2e/dist/y.js'])
            link = root / 'apps/desk/dist/link'; link.symlink_to(root / 'apps/desk/dist/x.js')
            with self.assertRaises(ValueError): outputs_manifest(root, 'desk')

    def test_unicode_canonical_digest_matches_node_json_escaping(self):
        value = {'title': 'a › 😀', 'files': ['A.js', 'Z.js', 'é.js']}
        expected = digest(value)
        script = """const c=require('node:crypto'); const v=JSON.parse(process.argv[1]); const j=x=>JSON.stringify(x).replace(/[^\\x00-\\x7f]/g,q=>'\\\\u'+q.charCodeAt(0).toString(16).padStart(4,'0')); const f=x=>Array.isArray(x)?'['+x.map(f).join(',')+']':x&&typeof x==='object'?'{'+Object.keys(x).sort().map(k=>j(k)+':'+f(x[k])).join(',')+'}':j(x); process.stdout.write(c.createHash('sha256').update(f(v)).digest('hex'));"""
        actual = subprocess.check_output(['node', '-e', script, json.dumps(value)]).decode()
        self.assertEqual(actual, expected)

    def test_reporter_converts_expected_failure_and_keeps_last_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'report.json'
            script = """const Reporter=require(process.argv[1]); const r=new Reporter(); const t={id:'x',expectedStatus:'failed',parent:{project:()=>({name:'p'})},location:{file:'x.spec.ts'},titlePath:()=>['x']}; r.onBegin({}, {allTests:()=>[t]}); r.onTestEnd(t,{status:'failed'}); r.onTestEnd(t,{status:'passed'}); r.onEnd({status:'failed'});"""
            subprocess.run(['node', '-e', script, str(Path(__file__).with_name('surface-reporter.cjs'))],
                           env={'PATH': __import__('os').environ['PATH'], 'PANDORA_SURFACE_REPORT': str(output), 'PANDORA_SURFACE_REPORT_MODE': 'run'}, check=True)
            self.assertEqual(json.loads(output.read_text())['outcomes'], [{'id': 'x', 'status': 'unexpected'}])

    def test_plan_rejects_unsafe_duplicate_and_empty_build_manifests(self):
        value = plan(); value['build']['files'] = []; value['build']['sha256'] = digest({'app': 'web', 'files': []}); value['plan_id'] = plan_digest(value)
        with self.assertRaises(ValueError): validate_plan(value)
        value = plan(); value['build']['files'][0]['path'] = '../outside'; value['build']['sha256'] = digest({'app': 'web', 'files': value['build']['files']}); value['plan_id'] = plan_digest(value)
        with self.assertRaises(ValueError): validate_plan(value)


if __name__ == '__main__':
    unittest.main()
