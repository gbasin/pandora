"""`--update` write-back: the proposal on the worker, the publication here.

The properties worth pinning are all refusals. A write-back that lands when it
should is one assertion; the ones that must *not* land are the design: a failed
run, a failed or missing shard, a tree edited outside the declared files while
the run was away, a declared file edited here, bytes that arrived wrong. Each
of those leaves the worktree exactly as the agent left it.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from pandora.engine import runner
from pandora.engine import writeback as proposals
from pandora.engine.ledger import Ledger
from pandora.tests.test_shards import FanoutHarness, WritingDriver

LEDGER = 'fixtures/*.ledger.jsonl'
ROUTES = 'fixtures/routes.json'
PATTERNS = [LEDGER, ROUTES]
WRITEBACK = [{'kind': 'writeback', 'paths': PATTERNS}]


def tree(root, files):
    root = Path(root)
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def sha(text):
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


# --- what a declared path means ---------------------------------------------------

class DeclaredPaths(unittest.TestCase):
    def test_a_glob_matches_file_names_in_its_own_directory_only(self):
        self.assertTrue(proposals.matches('fixtures/S0-01.ledger.jsonl', PATTERNS))
        self.assertFalse(proposals.matches('fixtures/deep/S0-01.ledger.jsonl', PATTERNS))
        self.assertFalse(proposals.matches('fixtures/S0-01.json', PATTERNS))
        self.assertTrue(proposals.matches(ROUTES, PATTERNS))

    def test_a_literal_directory_declares_everything_below_it(self):
        self.assertTrue(proposals.matches('snap/a/b.txt', ['snap']))
        self.assertFalse(proposals.matches('snapshot.txt', ['snap']))

    def test_expansion_finds_regular_files_and_skips_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = tree(tmp, {'fixtures/a.ledger.jsonl': '1', 'fixtures/b.txt': '2',
                              'snap/x/y.txt': '3'})
            os.symlink('a.ledger.jsonl', root / 'fixtures/c.ledger.jsonl')
            self.assertEqual(proposals.expand(root, PATTERNS + ['snap']),
                             ['fixtures/a.ledger.jsonl', 'snap/x/y.txt'])


# --- the engine's proposal -------------------------------------------------------

class Proposal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = tree(self.root / 'src', {'fixtures/a.ledger.jsonl': 'a0',
                                               ROUTES: '{}\n', 'other.txt': 'x'})

    def test_only_changed_and_new_files_are_proposed(self):
        pulled = tree(self.root / 'pulled', {'fixtures/a.ledger.jsonl': 'a0',
                                             'fixtures/b.ledger.jsonl': 'b1',
                                             ROUTES: '{"b": 1}\n'})
        record = proposals.propose(self.source, pulled, self.root / 'into', PATTERNS)
        self.assertTrue(record['complete'])
        self.assertEqual(sorted(record['changes']), ['fixtures/b.ledger.jsonl', ROUTES])
        self.assertEqual((self.root / 'into/fixtures/b.ledger.jsonl').read_text(), 'b1')
        self.assertFalse((self.root / 'into/fixtures/a.ledger.jsonl').exists())

    def test_a_declared_file_that_did_not_come_back_proposes_nothing(self):
        pulled = tree(self.root / 'pulled', {ROUTES: '{"b": 1}\n'})
        record = proposals.propose(self.source, pulled, self.root / 'into', PATTERNS)
        self.assertFalse(record['complete'])
        self.assertEqual(record['removed'], ['fixtures/a.ledger.jsonl'])
        self.assertEqual(record['exit'], 70)
        self.assertIn('never deletes', record['why'])

    def test_a_gone_source_is_incomplete_rather_than_everything_new(self):
        record = proposals.propose(self.root / 'nowhere', self.source, self.root / 'into',
                                   PATTERNS)
        self.assertFalse(record['complete'])


class Merge(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = tree(self.root / 'src', {ROUTES: json.dumps(
            {'S0-01': ['a'], 'S0-02': ['b']}, indent=2, sort_keys=True) + '\n'})

    def shard(self, index, files, complete=True):
        directory = tree(self.root / ('shard-%d' % index), files)
        record = {'complete': complete, 'why': None if complete else 'boom', 'exit': None,
                  'changes': {path: sha(text) for path, text in files.items()}, 'removed': []}
        return record, directory

    def routes(self, value):
        return json.dumps(value, indent=2, sort_keys=True) + '\n'

    def test_shards_that_changed_different_files_are_unioned(self):
        merged = proposals.merge(self.source, {
            1: self.shard(1, {'fixtures/S0-01.ledger.jsonl': 'one'}),
            2: self.shard(2, {'fixtures/S0-02.ledger.jsonl': 'two'})}, self.root / 'into')
        self.assertTrue(merged['complete'])
        self.assertEqual(sorted(merged['changes']),
                         ['fixtures/S0-01.ledger.jsonl', 'fixtures/S0-02.ledger.jsonl'])

    def test_disjoint_keys_of_one_json_manifest_merge_like_the_catalog_update(self):
        merged = proposals.merge(self.source, {
            1: self.shard(1, {ROUTES: self.routes({'S0-01': ['a', 'x'], 'S0-02': ['b']})}),
            2: self.shard(2, {ROUTES: self.routes({'S0-01': ['a'], 'S0-02': ['b'],
                                                   'S0-03': ['c']})})},
            self.root / 'into')
        self.assertTrue(merged['complete'], merged['why'])
        text = (self.root / 'into' / ROUTES).read_text()
        self.assertEqual(text, self.routes({'S0-01': ['a', 'x'], 'S0-02': ['b'],
                                            'S0-03': ['c']}))
        self.assertEqual(merged['changes'][ROUTES], sha(text))

    def test_two_shards_changing_one_key_differently_collide_and_publish_nothing(self):
        merged = proposals.merge(self.source, {
            1: self.shard(1, {ROUTES: self.routes({'S0-01': ['x'], 'S0-02': ['b']})}),
            2: self.shard(2, {ROUTES: self.routes({'S0-01': ['y'], 'S0-02': ['b']})})},
            self.root / 'into')
        self.assertFalse(merged['complete'])
        self.assertEqual(merged['exit'], 75)
        self.assertEqual(merged['changes'], {})
        self.assertEqual(merged['collisions'], [{'path': ROUTES, 'shards': [1, 2]}])

    def test_a_serialisation_the_merge_cannot_reproduce_is_a_collision(self):
        odd = '{ "S0-01": ["x"],\n  "S0-02": ["b"] }\n'
        merged = proposals.merge(self.source, {
            1: self.shard(1, {ROUTES: odd}),
            2: self.shard(2, {ROUTES: self.routes({'S0-01': ['a'], 'S0-02': ['c']})})},
            self.root / 'into')
        self.assertFalse(merged['complete'])

    def test_one_shard_without_a_complete_proposal_publishes_nobody(self):
        merged = proposals.merge(self.source, {
            1: self.shard(1, {'fixtures/S0-01.ledger.jsonl': 'one'}),
            2: self.shard(2, {}, complete=False)}, self.root / 'into')
        self.assertFalse(merged['complete'])
        self.assertIn('shard 2', merged['why'])
        self.assertEqual(merged['changes'], {})


# --- the engine, whole ----------------------------------------------------------

class SingleRun(unittest.TestCase):
    """`supervise` with a driver whose instance holds real files."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = runner.Paths(self.root / 'engine').ensure()
        self.source = tree(self.root / 'src', {'fixtures/S0-01.ledger.jsonl': 'old',
                                               ROUTES: '{}\n'})
        self.budget = os.environ.get('PANDORA_BUDGET_MIB')
        os.environ['PANDORA_BUDGET_MIB'] = '8192'
        self.addCleanup(self.restore)

    def restore(self):
        if self.budget is None:
            os.environ.pop('PANDORA_BUDGET_MIB', None)
        else:
            os.environ['PANDORA_BUDGET_MIB'] = self.budget

    def run_with(self, files, outcome='ok', outputs=WRITEBACK):
        ledger = Ledger(self.paths.ledger)
        ledger.claim('req', 'r1', repo='demo', job='journey', input_id='i',
                     source_path=str(self.source), argv=['node', 'run.mjs'], env={},
                     cwd='.', outputs=outputs, size_class='medium')
        attempt = self.paths.attempt('r1')
        attempt.mkdir(parents=True, exist_ok=True)
        (attempt / 'toolchain.json').write_text(json.dumps(
            {'base_image': 'i', 'packages': [], 'node_version': '', 'pnpm_version': '',
             'service_images': [], 'install_command': '', 'source_id': 'x', 'env': {}}))
        ledger.update('r1', state='admitted', reservation_mib=1024, ceiling_mib=2048,
                      cpus_hint=1)
        ledger.close()
        driver = WritingDriver({'r1': files}, {'r1': outcome})
        return runner.supervise(self.paths.root, 'r1', driver=driver)

    def test_a_passing_run_proposes_what_it_changed_and_nothing_else(self):
        result = self.run_with({'fixtures/S0-01.ledger.jsonl': 'new', ROUTES: '{}\n'})
        self.assertEqual(result['outcome'], 'passed')
        self.assertTrue(result['writeback']['complete'])
        self.assertEqual(list(result['writeback']['changes']), ['fixtures/S0-01.ledger.jsonl'])
        proposed = self.paths.attempt('r1') / proposals.PROPOSAL
        self.assertEqual((proposed / 'fixtures/S0-01.ledger.jsonl').read_text(), 'new')
        self.assertFalse((self.paths.attempt('r1') / proposals.PULLED).exists())

    def test_a_failing_run_proposes_nothing(self):
        result = self.run_with({'fixtures/S0-01.ledger.jsonl': 'new', ROUTES: '{}\n'},
                               outcome='failed')
        self.assertEqual(result['outcome'], 'command_failed')
        self.assertFalse(result['writeback']['complete'])
        self.assertEqual(result['writeback']['changes'], {})

    def test_a_run_that_arms_no_write_back_carries_no_record(self):
        result = self.run_with({}, outputs=[{'kind': 'artifacts', 'paths': ['reports']}])
        self.assertNotIn('writeback', result)


class CatalogFanout(FanoutHarness):
    """`pnpm journeys --update`: every shard, or none of them."""

    def setUp(self):
        super().setUp()
        self.source = tree(self.root / 'src', {
            'fixtures/S0-01.ledger.jsonl': 'one-old', 'fixtures/S0-02.ledger.jsonl': 'two-old',
            ROUTES: '{}\n'})
        self.outputs = self.OUTPUTS + WRITEBACK

    def shard_files(self, one='one-old', two='two-old'):
        return {'fixtures/S0-01.ledger.jsonl': one, 'fixtures/S0-02.ledger.jsonl': two,
                ROUTES: '{}\n'}

    def test_every_shard_passing_publishes_one_merged_proposal(self):
        self.arrange(extra={1: self.shard_files(one='one-new'),
                            2: self.shard_files(two='two-new')})
        result = self.parent(want=2, outputs=self.outputs, source_path=str(self.source))
        self.assertEqual(result['outcome'], 'passed')
        self.assertTrue(result['writeback']['complete'], result['writeback']['why'])
        self.assertEqual(sorted(result['writeback']['changes']),
                         ['fixtures/S0-01.ledger.jsonl', 'fixtures/S0-02.ledger.jsonl'])
        merged = self.paths.attempt('p1') / proposals.PROPOSAL
        self.assertEqual((merged / 'fixtures/S0-02.ledger.jsonl').read_text(), 'two-new')

    def test_a_failed_shard_publishes_nothing_and_names_itself(self):
        self.arrange(extra={1: self.shard_files(one='one-new'),
                            2: self.shard_files(two='two-new')})
        original = self.driver.clone

        def clone(golden, run_id, limits=None):
            instance = original(golden, run_id, limits=limits)
            if self.role_of(run_id)['shard_index'] == 2:
                self.outcomes[run_id] = 'failed'
            return instance

        self.driver.clone = clone
        result = self.parent(want=2, outputs=self.outputs, source_path=str(self.source),
                             keep_going=True)
        self.assertEqual(result['outcome'], 'command_failed')
        self.assertFalse(result['writeback']['complete'])
        self.assertEqual(result['writeback']['changes'], {})
        self.assertIn('shard 2 did not pass', result['writeback']['why'])
        merged = self.paths.attempt('p1') / proposals.PROPOSAL
        self.assertFalse(merged.exists() and any(merged.rglob('*')))

    def test_a_shard_missing_its_report_publishes_nothing(self):
        self.arrange(reports={2: None}, extra={1: self.shard_files(one='one-new'),
                                               2: self.shard_files(two='two-new')})
        result = self.parent(want=2, outputs=self.outputs, source_path=str(self.source))
        self.assertNotEqual(result['outcome'], 'passed')
        self.assertFalse(result['writeback']['complete'])
        self.assertEqual(result['writeback']['changes'], {})


if __name__ == '__main__':
    unittest.main()
