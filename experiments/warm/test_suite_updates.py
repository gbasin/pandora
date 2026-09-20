import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from suite_evidence import plan_digest
from suite_updates import merge


def plan():
    value = {'version': 1, 'source_digest': 'a' * 64, 'selection': ['S0-01', 'S1-02'],
             'catalog': [{'id': 'S0-01', 'consequential': True}, {'id': 'S1-02', 'consequential': False}],
             'replay_ids': [], 'shards': [{'index': 1, 'ids': ['S0-01']}, {'index': 2, 'ids': ['S1-02']}]}
    value['plan_id'] = plan_digest(value)
    return value


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')


def child(root, p, shard, *, route=None, ledger=True):
    attempt = f'{shard:032x}'
    path = root / attempt
    request = {'action': 'shard', 'plan': p, 'shard': shard}
    write_json(path / 'submission.json', {'attempt': attempt, 'workflow': 'suite', 'parent_attempt': root.parent.name,
                                           'suite_update': True, 'source_digest': p['source_digest'], 'suite': request})
    write_json(path / 'terminal.json', {'attempt': attempt, 'workflow': 'suite', 'exit_code': 0, 'cleanup_verified': True})
    identifier = p['shards'][shard - 1]['ids'][0]
    report = {'version': 1, 'plan_id': p['plan_id'], 'source_digest': p['source_digest'], 'shard': shard,
              'planned_ids': [identifier], 'exit_code': 0, 'results': [{'id': identifier, 'stage': identifier.split('-')[0], 'status': 'pass'}],
              'errors': {'infrastructureFailures': 0, 'unrunJourneys': []},
              'coverage': {'mode': 'cover', 'journeys': {'total': 1, 'consequential': int(shard == 1), 'selected': [], 'completed': [], 'passed': [], 'withoutReplayResult': []}, 'stageRoutes': {key: [] for key in ('observed', 'selected', 'replayed', 'passed', 'uncovered')}}, 'detail': ''}
    write_json(path / 'results/suite-shard.json', report)
    ledgers = []
    if ledger:
        contents = f'{identifier} updated\n'.encode(); target = path / 'results/updates/packages/scenarios/fixtures' / f'{identifier}.ledger.jsonl'
        target.parent.mkdir(parents=True); target.write_bytes(contents)
        ledgers = [{'id': identifier, 'path': f'packages/scenarios/fixtures/{identifier}.ledger.jsonl', 'sha256': hashlib.sha256(contents).hexdigest()}]
    proposal = {'version': 1, 'plan_id': p['plan_id'], 'source_digest': p['source_digest'], 'shard': shard,
                'planned_ids': [identifier], 'routes': {identifier: route if route is not None else [identifier + '/new']},
                'ledger_expected': [identifier] if ledger else [], 'ledgers': ledgers}
    write_json(path / 'results/suite-update.json', proposal)
    artifacts = {}
    for item in (path / 'results').rglob('*'):
        if item.is_file(): artifacts[str(item.relative_to(path))] = hashlib.sha256(item.read_bytes()).hexdigest()
    write_json(path / 'artifacts.json', artifacts)
    return path


class SuiteUpdatesTests(unittest.TestCase):
    def test_merge_writes_one_parent_manifest_and_owned_ledgers(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp) / 'parent'; p = plan()
            write_json(parent / 'source/packages/scenarios/fixtures/write-routes.json', {'S0-01': ['old'], 'S9-99': ['keep']})
            first, second = child(parent / 'children', p, 1), child(parent / 'children', p, 2, ledger=False)
            receipt = merge(parent, p, [first, second])
            self.assertEqual(receipt['routes'], {'S0-01': ['S0-01/new'], 'S1-02': ['S1-02/new']})
            merged = json.loads((parent / 'results/updates/packages/scenarios/fixtures/write-routes.json').read_text())
            self.assertEqual(merged['S9-99'], ['keep'])
            self.assertTrue((parent / 'results/updates/packages/scenarios/fixtures/S0-01.ledger.jsonl').exists())

    def test_merge_rejects_unverified_or_failed_child(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp) / 'parent'; p = plan()
            write_json(parent / 'source/packages/scenarios/fixtures/write-routes.json', {})
            first, second = child(parent / 'children', p, 1), child(parent / 'children', p, 2)
            write_json(second / 'terminal.json', {'attempt': second.name, 'workflow': 'suite', 'exit_code': 1, 'cleanup_verified': True})
            with self.assertRaisesRegex(ValueError, 'complete successfully'):
                merge(parent, p, [first, second])

    def test_merge_rejects_unowned_route_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp) / 'parent'; p = plan()
            write_json(parent / 'source/packages/scenarios/fixtures/write-routes.json', {})
            first, second = child(parent / 'children', p, 1), child(parent / 'children', p, 2)
            proposal = json.loads((first / 'results/suite-update.json').read_text())
            proposal['routes']['S1-02'] = ['leak']
            write_json(first / 'results/suite-update.json', proposal)
            artifacts = json.loads((first / 'artifacts.json').read_text())
            artifacts['results/suite-update.json'] = hashlib.sha256((first / 'results/suite-update.json').read_bytes()).hexdigest()
            write_json(first / 'artifacts.json', artifacts)
            with self.assertRaisesRegex(ValueError, 'ownership'):
                merge(parent, p, [first, second])

    def test_merge_requires_the_parent_bound_update_submission(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp) / 'parent'; p = plan()
            write_json(parent / 'source/packages/scenarios/fixtures/write-routes.json', {})
            first, second = child(parent / 'children', p, 1), child(parent / 'children', p, 2)
            submitted = json.loads((first / 'submission.json').read_text()); submitted['suite_update'] = False
            write_json(first / 'submission.json', submitted)
            with self.assertRaisesRegex(ValueError, 'identity'):
                merge(parent, p, [first, second])


if __name__ == '__main__': unittest.main()
