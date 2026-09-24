"""`--update` write-back: the proposal on the worker, the publication here.

The properties worth pinning are all refusals. A write-back that lands when it
should is one assertion; the ones that must *not* land are the design: a failed
run, a failed or missing shard, a tree edited outside the declared files while
the run was away, a declared file edited here, bytes that arrived wrong. Each
of those leaves the worktree exactly as the agent left it.
"""
import base64
import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import unittest
from pathlib import Path

from pandora import cli
from pandora.client import daemon as daemon_module
from pandora.client import writeback as publication
from pandora.client.protocol import Reader, VERSION, dump
from pandora.engine import runner
from pandora.engine import writeback as proposals
from pandora.engine.ledger import Ledger
from pandora.errors import WorkerUnreachable
from pandora.snapshot import freeze as snapshot
from pandora.tests.test_cli import capture
from pandora.tests.test_shards import FanoutHarness, WritingDriver
from pandora.tests.test_snapshot import make_repo

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

    def test_a_serialization_the_merge_cannot_reproduce_is_a_collision(self):
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


# --- publication, on this Mac --------------------------------------------------

class Worktree(unittest.TestCase):
    """A real git worktree, frozen for real, and a proposal as the engine made it."""

    FILES = {'fixtures/S0-01.ledger.jsonl': 'old\n', ROUTES: '{}\n', 'src/app.js': 'app\n'}
    PLAN = {'outputs': [{'kind': 'artifacts', 'paths': ['reports']}] + WRITEBACK,
            'secrets_exclude_globs': []}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = make_repo(self.root / 'repo', self.FILES)
        self.run_dir = self.root / 'runs' / 'run1'
        self.run_dir.mkdir(parents=True)
        self.worker_side = self.root / 'worker'
        self.fetched = 0
        self.freeze_now()

    def freeze_now(self):
        manifest, _, input_id = snapshot.freeze(self.repo)
        publication.save(self.run_dir, publication.context(
            manifest, self.PLAN, worktree=self.repo, input_id=input_id))

    def propose(self, files):
        tree(self.worker_side, files)
        return {'complete': True, 'why': None, 'exit': None, 'removed': [],
                'changes': {path: sha(text) for path, text in files.items()}}

    def fetch(self, into):
        self.fetched += 1
        shutil.copytree(self.worker_side, into, dirs_exist_ok=True)

    def settle(self, proposal, *, outcome='passed', cli_exit=0):
        return publication.settle(
            self.run_dir, {'outcome': outcome, 'cli_exit': cli_exit, 'writeback': proposal},
            run_id='run1', fetch=self.fetch,
            freeze=lambda worktree, globs: snapshot.freeze(worktree, exclude_globs=globs)[0])

    def read(self, path):
        return (self.repo / path).read_text()


class Publication(Worktree):
    def test_unchanged_declared_files_take_the_workers_version(self):
        record = self.settle(self.propose({'fixtures/S0-01.ledger.jsonl': 'new\n',
                                           'fixtures/S0-02.ledger.jsonl': 'fresh\n'}))
        self.assertEqual(record['state'], 'published')
        self.assertIsNone(record['exit'])
        self.assertEqual(record['written'], ['fixtures/S0-01.ledger.jsonl',
                                             'fixtures/S0-02.ledger.jsonl'])
        self.assertEqual(self.read('fixtures/S0-01.ledger.jsonl'), 'new\n')
        self.assertEqual(self.read('fixtures/S0-02.ledger.jsonl'), 'fresh\n')
        self.assertEqual(list(self.repo.glob('fixtures/.*.tmp')), [])

    def test_a_locally_edited_declared_file_is_kept_and_nothing_is_written(self):
        (self.repo / ROUTES).write_text('{"mine": 1}\n')
        record = self.settle(self.propose({'fixtures/S0-01.ledger.jsonl': 'new\n',
                                           ROUTES: '{"theirs": 1}\n'}))
        self.assertEqual(record['state'], 'conflicted')
        self.assertEqual(record['exit'], 75)
        self.assertEqual([item['path'] for item in record['conflicts']], [ROUTES])
        self.assertEqual(self.read(ROUTES), '{"mine": 1}\n')
        # All or nothing: the untouched ledger is not published on its own.
        self.assertEqual(self.read('fixtures/S0-01.ledger.jsonl'), 'old\n')
        self.assertEqual((Path(record['proposed']) / ROUTES).read_text(), '{"theirs": 1}\n')
        self.assertIn('pandora resolve run1 --keep-local', record['resolve'])

    def test_a_file_absent_at_freeze_and_created_here_meanwhile_is_a_conflict(self):
        (self.repo / 'fixtures/S0-02.ledger.jsonl').write_text('mine\n')
        record = self.settle(self.propose({'fixtures/S0-02.ledger.jsonl': 'theirs\n'}))
        self.assertEqual(record['state'], 'conflicted')
        self.assertIsNone(record['conflicts'][0]['frozen'])
        self.assertEqual(self.read('fixtures/S0-02.ledger.jsonl'), 'mine\n')

    def test_a_local_file_already_equal_to_the_proposal_is_not_a_conflict(self):
        (self.repo / ROUTES).write_text('{"same": 1}\n')
        record = self.settle(self.propose({ROUTES: '{"same": 1}\n'}))
        self.assertEqual(record['state'], 'unchanged')

    def test_an_edit_outside_the_declared_files_makes_the_run_stale(self):
        (self.repo / 'src/app.js').write_text('edited\n')
        record = self.settle(self.propose({'fixtures/S0-01.ledger.jsonl': 'new\n'}))
        self.assertEqual(record['state'], 'stale')
        self.assertEqual(record['exit'], 75)
        self.assertEqual(record['stale'], ['src/app.js'])
        self.assertEqual(self.read('fixtures/S0-01.ledger.jsonl'), 'old\n')

    def test_artifacts_arriving_in_the_worktree_do_not_make_it_stale(self):
        # Not gitignored, so a fresh freeze sees them: they are excluded because
        # they are declared artifacts, which `deliver` rsyncs in before this runs.
        (self.repo / 'reports').mkdir()
        (self.repo / 'reports/run.json').write_text('{}')
        record = self.settle(self.propose({'fixtures/S0-01.ledger.jsonl': 'new\n'}))
        self.assertEqual(record['state'], 'published')

    def test_a_failed_run_writes_nothing_and_keeps_its_own_exit(self):
        record = self.settle(self.propose({'fixtures/S0-01.ledger.jsonl': 'new\n'}),
                             outcome='command_failed', cli_exit=1)
        self.assertEqual(record['state'], 'not-run')
        self.assertIsNone(record['exit'])
        self.assertEqual(self.fetched, 0)
        self.assertEqual(self.read('fixtures/S0-01.ledger.jsonl'), 'old\n')

    def test_an_incomplete_proposal_writes_nothing_and_exits_with_its_code(self):
        record = self.settle({'complete': False, 'why': 'shard 3 did not pass', 'exit': 75,
                              'changes': {}, 'removed': []})
        self.assertEqual(record['state'], 'incomplete')
        self.assertEqual(record['exit'], 75)
        self.assertEqual(record['why'], 'shard 3 did not pass')

    def test_bytes_that_arrive_wrong_are_never_written(self):
        proposal = self.propose({'fixtures/S0-01.ledger.jsonl': 'new\n'})
        (self.worker_side / 'fixtures/S0-01.ledger.jsonl').write_text('corrupt\n')
        record = self.settle(proposal)
        self.assertEqual(record['state'], 'incomplete')
        self.assertEqual(record['exit'], 70)
        self.assertEqual(self.read('fixtures/S0-01.ledger.jsonl'), 'old\n')

    def test_a_replaced_file_keeps_its_mode(self):
        path = self.repo / 'fixtures/S0-01.ledger.jsonl'
        os.chmod(path, 0o755)
        self.freeze_now()
        self.settle(self.propose({'fixtures/S0-01.ledger.jsonl': 'new\n'}))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)

    def test_a_symlinked_directory_is_never_written_through(self):
        shutil.rmtree(self.repo / 'fixtures')
        (self.root / 'elsewhere').mkdir()
        os.symlink(self.root / 'elsewhere', self.repo / 'fixtures')
        record = self.settle(self.propose({'fixtures/S0-09.ledger.jsonl': 'x\n'}))
        self.assertIn(record['state'], ('conflicted', 'stale'))
        self.assertFalse((self.root / 'elsewhere/S0-09.ledger.jsonl').exists())


class Resolve(Worktree):
    def conflicted(self):
        (self.repo / ROUTES).write_text('{"mine": 1}\n')
        record = self.settle(self.propose({'fixtures/S0-01.ledger.jsonl': 'new\n',
                                           ROUTES: '{"theirs": 1}\n'}))
        self.assertEqual(record['state'], 'conflicted')
        return {'outcome': 'passed', 'cli_exit': 75, 'writeback': record}

    def test_keep_local_records_the_choice_and_writes_nothing(self):
        result = self.conflicted()
        code, lines = publication.resolve(self.run_dir, result, keep_local=True)
        self.assertEqual(code, 0)
        self.assertEqual(result['writeback']['state'], 'resolved')
        self.assertEqual(result['writeback']['resolution'], 'keep-local')
        self.assertEqual(self.read(ROUTES), '{"mine": 1}\n')
        self.assertIn('not validated', ' '.join(lines))

    def test_take_worker_publishes_the_whole_proposal(self):
        result = self.conflicted()
        code, _ = publication.resolve(self.run_dir, result, keep_local=False)
        self.assertEqual(code, 0)
        self.assertEqual(self.read(ROUTES), '{"theirs": 1}\n')
        self.assertEqual(self.read('fixtures/S0-01.ledger.jsonl'), 'new\n')

    def test_take_worker_refuses_a_file_edited_again_after_the_report(self):
        result = self.conflicted()
        (self.repo / ROUTES).write_text('{"mine": 2}\n')
        code, lines = publication.resolve(self.run_dir, result, keep_local=False)
        self.assertEqual(code, 75)
        self.assertEqual(self.read(ROUTES), '{"mine": 2}\n')
        self.assertEqual(self.read('fixtures/S0-01.ledger.jsonl'), 'old\n')

    def test_only_a_conflicted_write_back_can_be_resolved(self):
        code, _ = publication.resolve(self.run_dir, {'writeback': {'state': 'stale'}},
                                      keep_local=True)
        self.assertEqual(code, 64)

    def test_the_cli_verb_rewrites_the_result(self):
        result = self.conflicted()
        state = self.root
        (self.run_dir / 'result.json').write_text(json.dumps(result))
        config = self.root / 'config.toml'
        config.write_text('[client]\nstate = "%s"\n' % state)
        code, _, err = capture(lambda: cli.main(['--config', str(config), 'resolve', 'run1',
                                                 '--keep-local']))
        self.assertEqual(code, 0, err)
        saved = json.loads((self.run_dir / 'result.json').read_text())
        self.assertEqual(saved['writeback']['state'], 'resolved')
        code, out, _ = capture(lambda: cli.main(['--config', str(config), 'result', 'run1']))
        self.assertIn('write-back: resolved', out)


# --- the daemon, whole --------------------------------------------------------

CONFIG = '''
version = 1
[repo]
name = "demo"
entrypoints = ["pnpm"]
[[jobs]]
id = "journey"
size = "small"
args = "required"
forms = [{ prefix = ["journey"] }]
options = [{ name = "--update", sets = "update", forward = true, writeback = true }]
outputs = [
  { kind = "writeback", requires_option = "update", paths = ["fixtures/*.ledger.jsonl", "fixtures/routes.json"] },
]
run = { argv = ["sh", "-c", "echo ran-here > %(marker)s", "--", "{args}"] }
[worker]
base_image = "images:ubuntu/26.04"
'''


class Submission:
    def __init__(self, writeback):
        self.run_id, self.input_id, self.same_tree_as = 'r1', 'i1', None
        self.admission = {'reservation_mib': 100, 'cpus_hint': 1}
        self.source, self.durations, self.shipped = {'reused': False}, {}, frozenset()
        self.writeback = writeback


class RemoteWorker:
    """Freezes the real worktree like the real one, then "runs" by proposing files."""

    proposal = {}
    follow_raises = None
    before_result = None
    submitted = []

    def __init__(self, host, **kwargs):
        self.host = host

    def submit(self, *, plan, worktree, **kwargs):
        RemoteWorker.submitted.append(plan)
        manifest, _, input_id = snapshot.freeze(worktree)
        return Submission(publication.context(manifest, plan, worktree=worktree,
                                              input_id=input_id)
                          if plan.get('writeback') else None)

    def follow(self, run_id, **kwargs):
        if RemoteWorker.follow_raises is not None:
            raise RemoteWorker.follow_raises
        if RemoteWorker.before_result is not None:
            RemoteWorker.before_result()
        changes = {path: sha(text) for path, text in RemoteWorker.proposal.items()}
        return {'outcome': 'passed', 'cli_exit': 0, 'hint': None,
                'writeback': {'complete': True, 'why': None, 'exit': None,
                              'changes': changes, 'removed': []}}, 0

    def collect(self, *a, **k):
        return {'fetched': False, 'missing': []}

    def fetch_writeback(self, run_id, into):
        tree(into, RemoteWorker.proposal)

    def health(self, **kwargs):
        return {'ok': True, 'capacity': {'ok': True}, 'goldens': [], 'state': 'ready',
                'canary': {'ok': True}, 'kernel_drift': False}

    def close(self):
        pass


class DaemonWriteBack(unittest.TestCase):
    def setUp(self):
        RemoteWorker.proposal, RemoteWorker.follow_raises = {}, None
        RemoteWorker.before_result, RemoteWorker.submitted = None, []
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.root = Path(self.home.name)
        self.marker = self.root / 'marker'
        self.repo = make_repo(self.root / 'repo', {
            'pandora.toml': CONFIG % {'marker': self.marker},
            'fixtures/S0-01.ledger.jsonl': 'old\n', ROUTES: '{}\n', 'src/app.js': 'app\n'})
        self.state = self.root / 'state'
        config = self.root / 'config.toml'
        config.write_text(
            '[client]\nstate = "%s"\n[worker]\nhost = "fake@nowhere"\n'
            '[notify]\nenabled = false\n'
            '[local]\nbudget_mib = 16384\nqueue_timeout_seconds = 20\ndrift = "off"\n'
            '[local.pause]\nenabled = false\n'
            '[[repos]]\nname = "demo"\nroot = "%s"\n' % (self.state, self.repo))
        self.daemon = daemon_module.Daemon(config_path=str(config))
        self.daemon.worker_factory = RemoteWorker
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        threading.Thread(target=self.daemon.serve, daemon=True).start()

    def call(self, argv):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(60)
        sock.connect(str(self.daemon.socket_path))
        sock.sendall(dump({'v': VERSION, 'op': 'run', 'cwd': str(self.repo), 'argv': argv,
                           'env': {}, 'tty': False}))
        reader, err, answer = Reader(sock), b'', {}
        try:
            while True:
                frame = reader.line()
                if frame is None:
                    return answer
                if frame.get('t') == 'accepted':
                    answer['run'] = frame['run']
                elif frame.get('t') == 'log' and frame.get('s') == 'err':
                    err += base64.b64decode(frame['b64'])
                    answer['err'] = err.decode()
                elif frame.get('t') in ('exit', 'error'):
                    answer['exit'] = frame.get('code') if frame['t'] == 'exit' else frame['exit']
                    answer['error'] = frame if frame['t'] == 'error' else None
                    return answer
        finally:
            sock.close()

    def read(self, path):
        return (self.repo / path).read_text()

    def test_update_runs_remotely_and_its_files_come_home_with_the_review_hint(self):
        RemoteWorker.proposal = {'fixtures/S0-01.ledger.jsonl': 'new\n'}
        answer = self.call(['pnpm', 'journey', 'S0-01', '--update'])
        self.assertEqual(answer['exit'], 0, answer)
        self.assertTrue(RemoteWorker.submitted[0]['writeback'])
        self.assertEqual(self.read('fixtures/S0-01.ledger.jsonl'), 'new\n')
        self.assertIn('wrote back 1 file(s): fixtures/S0-01.ledger.jsonl', answer['err'])
        self.assertIn('pandora: hint: review `git diff` of 1 updated file, then validate '
                      'without --update', answer['err'])
        self.assertFalse(self.marker.exists())

    def test_a_fixture_edited_during_the_run_is_kept_and_the_run_exits_75(self):
        RemoteWorker.proposal = {ROUTES: '{"theirs": 1}\n'}
        RemoteWorker.before_result = lambda: (self.repo / ROUTES).write_text('{"mine": 1}\n')
        answer = self.call(['pnpm', 'journey', 'S0-01', '--update'])
        self.assertEqual(answer['exit'], 75)
        self.assertEqual(self.read(ROUTES), '{"mine": 1}\n')
        self.assertIn('pandora resolve %s --keep-local' % answer['run'], answer['err'])
        result = json.loads((self.state / 'runs' / answer['run'] / 'result.json').read_text())
        self.assertEqual(result['writeback']['state'], 'conflicted')

    def test_a_source_edit_during_the_run_makes_it_stale_and_writes_nothing(self):
        RemoteWorker.proposal = {'fixtures/S0-01.ledger.jsonl': 'new\n'}
        RemoteWorker.before_result = lambda: (self.repo / 'src/app.js').write_text('edit\n')
        answer = self.call(['pnpm', 'journey', 'S0-01', '--update'])
        self.assertEqual(answer['exit'], 75)
        self.assertEqual(self.read('fixtures/S0-01.ledger.jsonl'), 'old\n')
        self.assertIn('src/app.js', answer['err'])

    def test_a_worker_lost_after_acceptance_exits_70_and_never_runs_here(self):
        # "Never falls back", after `accepted`: the command may be running on
        # the worker, so running it here too would write the fixtures twice.
        self.daemon.ATTEMPTS, self.daemon.BACKOFF = 2, 0.01
        RemoteWorker.follow_raises = WorkerUnreachable('the worker went away')
        answer = self.call(['pnpm', 'journey', 'S0-01', '--update'])
        self.assertIn('run', answer)
        self.assertEqual(answer['exit'], 70)
        self.assertFalse(self.marker.exists(), 'an --update ran on this Mac')
        self.assertEqual(self.read('fixtures/S0-01.ledger.jsonl'), 'old\n')

    def test_a_worker_lost_before_acceptance_exits_70_and_never_runs_here(self):
        # The job is `small`, which would otherwise earn the local lane.
        original = RemoteWorker.submit
        RemoteWorker.submit = lambda self, **kwargs: (_ for _ in ()).throw(
            WorkerUnreachable('no route'))
        self.addCleanup(setattr, RemoteWorker, 'submit', original)
        answer = self.call(['pnpm', 'journey', 'S0-01', '--update'])
        self.assertEqual(answer['exit'], 70)
        self.assertEqual(answer['error']['code'], 'fallback-refused')
        self.assertIn('write-back', answer['error']['msg'])
        self.assertFalse(self.marker.exists(), 'an --update ran on this Mac')

    def test_the_same_job_without_update_still_falls_back_by_its_size(self):
        original = RemoteWorker.submit
        RemoteWorker.submit = lambda self, **kwargs: (_ for _ in ()).throw(
            WorkerUnreachable('no route'))
        self.addCleanup(setattr, RemoteWorker, 'submit', original)
        answer = self.call(['pnpm', 'journey', 'S0-01'])
        self.assertEqual(answer['exit'], 0, answer)
        self.assertEqual(self.marker.read_text().strip(), 'ran-here')


if __name__ == '__main__':
    unittest.main()
