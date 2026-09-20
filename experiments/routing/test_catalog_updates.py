import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import catalog_updates
from suite_evidence import plan_digest


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')


def artifacts(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob('*') if path.is_file() and path.name != 'artifacts.json'}


class RoutingSuiteUpdates(unittest.TestCase):
    def output(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        parent_attempt = 'f' * 32; child_attempt = '1' * 32
        write(root / 'submission.json', {'attempt': parent_attempt, 'workflow': 'suite-run', 'source_digest': 'a' * 64,
                                         'suite': {'action': 'run', 'update': True}})
        write(root / 'source/packages/scenarios/fixtures/write-routes.json', {'S0-01': ['old'], 'S0-02': ['keep']})
        source = root / 'source/packages/scenarios/fixtures/write-routes.json'
        write(root / 'manifest.json', [{'path': 'packages/scenarios/fixtures/write-routes.json', 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'executable': False}])
        plan = {'version': 1, 'source_digest': 'a' * 64, 'selection': ['S0-01'],
                'catalog': [{'id': 'S0-01', 'consequential': True}], 'replay_ids': [],
                'shards': [{'index': 1, 'ids': ['S0-01']}]}
        plan['plan_id'] = plan_digest(plan)
        write(root / 'results/suite-plan.json', plan)
        write(root / 'results/suite-run.json', {'version': 1, 'parent_attempt': parent_attempt, 'source_digest': 'a' * 64,
              'plan_id': plan['plan_id'], 'plan_attempt': '0' * 32, 'shard_attempts': [child_attempt], 'keep_going': False,
              'stop_reason': None, 'completed_shards': [1], 'unrun_shards': [], 'unrun_journeys': [], 'results': [], 'exit_code': 0, 'status': 'pass'})
        child = root / 'results/attempts' / child_attempt
        write(child / 'submission.json', {'attempt': child_attempt, 'workflow': 'suite', 'parent_attempt': parent_attempt,
                                           'suite_update': True, 'source_digest': 'a' * 64,
                                           'suite': {'action': 'shard', 'plan': plan, 'shard': 1}})
        write(child / 'terminal.json', {'attempt': child_attempt, 'workflow': 'suite', 'exit_code': 0, 'cleanup_verified': True})
        path = 'packages/scenarios/fixtures/S0-01.ledger.jsonl'
        ledger = b'new ledger\n'; target = child / 'results/updates' / path; target.parent.mkdir(parents=True); target.write_bytes(ledger)
        write(child / 'results/suite-update.json', {'version': 1, 'plan_id': plan['plan_id'], 'source_digest': 'a' * 64,
              'shard': 1, 'planned_ids': ['S0-01'], 'routes': {'S0-01': ['new']}, 'ledger_expected': ['S0-01'],
              'ledgers': [{'id': 'S0-01', 'path': path, 'sha256': hashlib.sha256(ledger).hexdigest()}]})
        write(child / 'artifacts.json', artifacts(child))
        parent_target = root / 'results/updates' / path; parent_target.parent.mkdir(parents=True); parent_target.write_bytes(ledger)
        routes = b'{\n  "S0-01": [\n    "new"\n  ],\n  "S0-02": [\n    "keep"\n  ]\n}\n'
        route_target = root / 'results/updates/packages/scenarios/fixtures/write-routes.json'; route_target.parent.mkdir(parents=True, exist_ok=True); route_target.write_bytes(routes)
        write(root / 'results/suite-updates.json', {'version': 1, 'plan_id': plan['plan_id'], 'source_digest': 'a' * 64,
              'shards': [{'shard': 1, 'attempt': child_attempt}], 'routes': {'S0-01': ['new']},
              'ledgers': [{'id': 'S0-01', 'path': path, 'sha256': hashlib.sha256(ledger).hexdigest()}]})
        write(root / 'artifacts.json', artifacts(root))
        return root

    def test_declarations_reconstructs_only_parent_targets(self):
        root = self.output()
        changes = catalog_updates.declarations(root)
        self.assertEqual(set(changes), {'packages/scenarios/fixtures/S0-01.ledger.jsonl', 'packages/scenarios/fixtures/write-routes.json'})
        self.assertEqual(changes['packages/scenarios/fixtures/write-routes.json']['base'], b'{\n  "S0-01": [\n    "old"\n  ],\n  "S0-02": [\n    "keep"\n  ]\n}\n')

    def test_rejects_parent_route_that_disagrees_with_child(self):
        root = self.output()
        receipt = json.loads((root / 'results/suite-updates.json').read_text()); receipt['routes']['S0-01'] = ['forged']
        write(root / 'results/suite-updates.json', receipt)
        write(root / 'artifacts.json', artifacts(root))
        with self.assertRaisesRegex(ValueError, 'differs'):
            catalog_updates.declarations(root)

    def test_rejects_child_attempt_outside_the_successful_suite_summary(self):
        root = self.output()
        receipt = json.loads((root / 'results/suite-updates.json').read_text()); receipt['shards'][0]['attempt'] = '2' * 32
        write(root / 'results/suite-updates.json', receipt)
        write(root / 'artifacts.json', artifacts(root))
        with self.assertRaisesRegex(ValueError, 'summary'):
            catalog_updates.declarations(root)

    def test_update_gate_is_suite_run_only(self):
        self.assertTrue(catalog_updates.is_update({'workflow': 'suite-run', 'suite': {'update': True}}))
        self.assertFalse(catalog_updates.is_update({'workflow': 'journey', 'suite': {'update': True}}))


if __name__ == '__main__': unittest.main()
