import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from suite import suite_request, suite_config, validate_result
from suite_evidence import plan_digest
import warm


def plan():
    value = {'version': 1, 'source_digest': 'a' * 64, 'selection': None,
             'catalog': [{'id': 'S0-01', 'consequential': False}], 'replay_ids': [],
             'shards': [{'index': 1, 'ids': ['S0-01']}]}
    value['plan_id'] = plan_digest(value)
    return value


class SuiteRequestTests(unittest.TestCase):
    def test_request_rejects_updates_invalid_shards_and_changed_source(self):
        for request in ({'action':'update'}, {'action':'plan','selection':None,'shard_count':True},
                        {'action':'plan','selection':['S0-01','S0-01'],'shard_count':2},
                        {'action':'shard','plan':plan(),'shard':2}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                suite_request(request)
        with self.assertRaisesRegex(ValueError, 'Source differs'):
            suite_config({'suite':{'action':'shard','plan':plan(),'shard':1},'source_digest':'b'*64})

    def test_plan_return_must_match_request_not_only_checksum(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / 'results').mkdir()
            name = 'results/suite-plan.json'
            (root / name).write_text(json.dumps(plan()))
            submitted = {'suite':{'action':'plan','selection':None,'shard_count':1}, 'source_digest':'a'*64}
            self.assertEqual(validate_result(root,submitted,{'exit_code':0},{name:'unused'}),plan())
            submitted['suite']['shard_count'] = 2
            with self.assertRaisesRegex(ValueError, 'differs'):
                validate_result(root,submitted,{'exit_code':0},{name:'unused'})

    def test_warm_captures_private_suite_request_without_routing_selectors(self):
        class Captured(Exception): pass
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); request = root/'request.json'
            request.write_text(json.dumps({'action':'plan','shard_count':2,'selection':['S0-01','S0-02']}))
            captured=[]
            def save(path, metadata): captured.append(metadata); raise Captured
            argv=['warm.py','--host','unused','--repo',str(root),'--output',str(root/'out'),
                  '--workflow','suite','--suite-request',str(request)]
            with patch.object(sys,'argv',argv), patch.object(warm,'repository_key',return_value='key'), \
                 patch.object(warm,'freeze',return_value=([],[])), patch.object(warm,'write_metadata',side_effect=save):
                with self.assertRaises(Captured): warm.main()
            self.assertEqual(captured[0]['suite'],json.loads(request.read_text()))
            self.assertEqual(captured[0]['selectors'],[])


if __name__ == '__main__': unittest.main()
