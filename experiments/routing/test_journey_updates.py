import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import route
import journey_updates as updates
from snapshot import freeze, encode


class JourneyUpdates(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.repo = root / 'repo'; self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (self.repo / 'app.txt').write_text('original')
        for name, data in zip(updates.PATHS, [b'old ledger\n', b'{"S0-01":["old"],"S0-02":["keep"]}\n']):
            path = self.repo / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
        self.state = root / 'state'
        self.output = self.state / hashlib.sha256(str(self.repo).encode()).hexdigest() / ('a'*32)
        self.output.mkdir(parents=True)
        manifest, _ = freeze(self.repo, self.output / 'source')
        (self.output / 'manifest.json').write_bytes(encode(manifest))
        self.submitted = {'workflow':'journey', 'selectors':['S0-01','--update'],
                          'source_digest':hashlib.sha256(encode(manifest)).hexdigest()}
        (self.output / 'submission.json').write_text(json.dumps(self.submitted))
        target = [b'new ledger\n', b'{"S0-01":["new"],"S0-02":["keep"]}\n']
        for name, data in zip(updates.PATHS, target):
            path = self.output / 'results/updates' / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
        (self.output / 'results/journey.json').write_text(json.dumps({'journey':'S0-01','status':'pass','update':True}))
        (self.output / 'results/exit-code').write_text('0\n')
        self.hash_artifacts()
        (self.output / 'terminal.json').write_text(json.dumps({'attempt':'a'*32,'workflow':'journey','exit_code':0,'cleanup_verified':True}))

    def hash_artifacts(self):
        (self.output / 'artifacts.json').write_text(json.dumps({str(p.relative_to(self.output)):hashlib.sha256(p.read_bytes()).hexdigest()
                                                            for p in (self.output/'results').rglob('*') if p.is_file()}))

    def test_reject_unrelated_manifest_changes_even_with_valid_checksum(self):
        p = self.output/'results/updates'/updates.PATHS[1]
        p.write_text('{"S0-01":["new"]}')
        self.hash_artifacts()
        with self.assertRaisesRegex(ValueError, 'unrelated'):
            updates.declarations(self.output)

    def test_other_journey_uses_its_own_ledger_and_preserves_other_routes(self):
        # Rename only the selected ledger; the other route entry must survive.
        new_id = 'S2-03'
        old_name = updates.PATHS[0]
        new_name = updates.FIXTURES + new_id + '.ledger.jsonl'
        for root in (self.output / 'source', self.output / 'results/updates'):
            (root / old_name).rename(root / new_name)
        manifest = json.loads((self.output / 'manifest.json').read_text())
        for record in manifest:
            if record['path'] == old_name:
                record['path'] = new_name
        (self.output / 'manifest.json').write_text(json.dumps(manifest))
        self.submitted['selectors'] = [new_id, '--update']
        (self.output / 'submission.json').write_text(json.dumps(self.submitted))
        (self.output / 'results/journey.json').write_text(json.dumps({'journey':new_id,'status':'pass','update':True}))
        target = self.output / 'results/updates' / updates.PATHS[1]
        target.write_text(json.dumps({'S0-01':['old'], 'S0-02':['keep'], new_id:['new']}))
        self.hash_artifacts()
        changes = updates.declarations(self.output)
        self.assertEqual(set(changes), {new_name, updates.PATHS[1]})
        target.write_text(json.dumps({'S0-01':['changed'], 'S0-02':['keep'], new_id:['new']}))
        self.hash_artifacts()
        with self.assertRaisesRegex(ValueError, 'unrelated'):
            updates.declarations(self.output)

    def test_api_less_update_returns_routes_only_but_cannot_omit_existing_ledger(self):
        report_path = self.output / 'results/journey.json'
        report = json.loads(report_path.read_text()) | {'ledger_expected': False}
        report_path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, 'omitted an existing ledger'):
            updates.declarations(self.output)
        ledger = updates.PATHS[0]
        manifest = [e for e in json.loads((self.output / 'manifest.json').read_text()) if e['path'] != ledger]
        (self.output / 'manifest.json').write_text(json.dumps(manifest))
        (self.output / 'source' / ledger).unlink()
        (self.output / 'results/updates' / ledger).unlink()
        self.hash_artifacts()
        self.assertEqual(set(updates.declarations(self.output)), {updates.PATHS[1]})

    def test_non_output_source_still_invalidates_update(self):
        changes = updates.declarations(self.output)
        self.assertTrue(updates.source_is_current(self.repo,self.output,self.submitted,changes))
        (self.repo / 'app.txt').write_text('edited during run')
        self.assertFalse(updates.source_is_current(self.repo,self.output,self.submitted,changes))

    def test_partial_return_keeps_same_attempt_recoverable(self):
        command = ['journey','S0-01','--update']
        active = self.output.parent/'active.json'
        route.write(active, {'state':'active','output':str(self.output),'command':command,'host':'unused','attempt':'a'*32})
        calls = []
        original_delivery = updates.deliver
        def interrupted_delivery(repo, output, changes):
            calls.append(True)
            def fault(point):
                if len(calls) == 1 and point == 'after_write':
                    raise OSError('injected publication interruption')
            return original_delivery(repo, output, changes, fault=fault)
        with patch.dict(os.environ, {'PANDORA_STATE':str(self.state),'PANDORA_HOST':'unused'}), \
             patch('sys.argv',['route.py',*command]), \
             patch('route.subprocess.check_output',return_value=str(self.repo)), \
             patch('route.Path.cwd',return_value=self.repo), \
             patch('route.subprocess.Popen',side_effect=AssertionError('must not rerun')), \
             patch('route.control',return_value={'cleanup_verified':True}), \
             patch('journey_updates.names',return_value=['app.txt',*updates.PATHS]), \
             patch('journey_updates.deliver',side_effect=interrupted_delivery):
            self.assertEqual(route.main(),75)
            self.assertEqual(json.loads(active.read_text())['state'],'active')
            self.assertEqual(route.main(),0)
            self.assertEqual(len(calls),2)


if __name__=='__main__': unittest.main()
