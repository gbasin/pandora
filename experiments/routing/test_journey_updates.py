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
