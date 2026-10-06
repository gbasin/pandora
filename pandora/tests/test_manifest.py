"""Offline previews share freeze semantics without cache or daemon writes."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pandora.client import manifest
from pandora.errors import SnapshotError
from pandora.snapshot import freeze as snapshot
from pandora.tests.test_snapshot import git, make_repo


class ManifestTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = make_repo(self.root / 'repo', {
            'README': 'abc', 'src/a': '12345', 'docs/a': 'hello',
            '.env': 'SECRET=1', 'private/key.txt': 'secret',
            'private/db.example': 'USER=', '.gitignore': 'ignored\n'})
        self.config = {'repo': {'name': 'acme'}, 'secrets': {'exclude_globs': ['private/*']}}

    def build(self, **kwargs):
        return manifest.build(self.repo, config=self.config, **kwargs)

    def test_counts_identity_and_exclusions_match_actual_freeze(self):
        (self.repo / 'ignored').write_text('not source')
        (self.repo / 'link').symlink_to('README')
        report = self.build()
        records, dropped, identity = snapshot.freeze(self.repo, exclude_globs=['private/*'])
        self.assertEqual(report['input_id'], identity)
        self.assertEqual(report['files'], len(records))
        self.assertEqual(report['bytes'], 3 + 5 + 5 + 5 + len('ignored\n'))
        self.assertEqual(report['excluded'], dropped)
        self.assertEqual(set(dropped), {'.env', 'private/key.txt'})
        self.assertNotIn('ignored', dropped)
        self.assertEqual(report['directories'], [
            {'path': '.', 'files': 3, 'bytes': 11},
            {'path': 'docs', 'files': 1, 'bytes': 5},
            {'path': 'private', 'files': 1, 'bytes': 5},
            {'path': 'src', 'files': 1, 'bytes': 5}])

    def test_preview_does_not_write_git_index_or_state(self):
        state = self.root / 'absent-state'
        before = (self.repo / '.git' / 'index').read_bytes()
        self.build(state=state)
        self.assertFalse(state.exists())
        self.assertEqual((self.repo / '.git' / 'index').read_bytes(), before)

    def test_missing_files_report_remote_partial_tree_gate(self):
        (self.repo / 'README').unlink()
        report = self.build()
        self.assertEqual(report['missing'], ['README'])
        self.assertTrue(report['submission_allowed'])
        for name in ('src/a', 'docs/a', 'private/db.example', '.gitignore'):
            (self.repo / name).unlink()
        report = self.build()
        self.assertFalse(report['submission_allowed'])
        self.assertIn('would refuse this partial tree', manifest.render(report))

    def test_more_than_fifty_missing_refuses_even_with_many_remaining(self):
        for i in range(110):
            (self.repo / ('file%d' % i)).write_text('x')
        git(self.repo, 'add', '-A')
        for i in range(51):
            (self.repo / ('file%d' % i)).unlink()
        report = self.build()
        self.assertGreater(report['files'], len(report['missing']))
        self.assertFalse(report['submission_allowed'])

    def test_matching_history_uses_alias_and_skips_malformed_rows(self):
        identity = self.build()['input_id']
        state = self.root / 'state'
        entries = [
            ('valid', {'repo': 'enrolled-alias', 'id': ['malformed']}, {'input_id': identity}),
            ('other', {'repo': 'other'}, {'input_id': identity}),
            ('bad-result', {'repo': 'enrolled-alias'}, []),
            ('bad-meta', [], {'input_id': identity}),
            ('old-input', {'repo': 'enrolled-alias'}, {'input_id': 'different'})]
        for run_id, meta, result in entries:
            directory = state / 'runs' / run_id
            directory.mkdir(parents=True)
            (directory / 'meta.json').write_text(json.dumps(meta))
            (directory / 'result.json').write_text(json.dumps(result))
        report = self.build(state=state, history_repo='enrolled-alias')
        self.assertEqual(report['same_input_run'], 'valid')
        self.assertIsNone(self.build(state=state)['same_input_run'])
        self.assertIn('source identity only', manifest.render(report))

    def test_nested_worktree_is_reported_as_an_excluded_prefix(self):
        git(self.repo, 'worktree', 'add', '-q', '-b', 'nested-test', str(self.repo / 'nested'))
        self.addCleanup(git, self.repo, 'worktree', 'remove', '--force', str(self.repo / 'nested'))
        report = self.build()
        self.assertIn('nested/', report['excluded'])
        self.assertFalse(any(row['path'] == 'nested' for row in report['directories']))

    def test_worker_cache_render_reports_grace_and_unknown_without_wire_estimate(self):
        report = self.build()
        report['worker_cache'] = {'status': 'present', 'grace_refreshed': True}
        self.assertIn('retention grace refreshed', manifest.render(report))
        report['worker_cache'] = {'status': 'unknown', 'error': 'worker unavailable'}
        self.assertIn('worker unavailable', manifest.render(report))
        report['worker_cache'] = {'status': 'absent'}
        self.assertIn('wire bytes depend on rsync reuse', manifest.render(report))

    def test_newest_matching_saved_run_selected(self):
        identity = self.build()['input_id']
        state = self.root / 'state'
        for run_id in ('first', 'second'):
            directory = state / 'runs' / run_id
            directory.mkdir(parents=True)
            (directory / 'meta.json').write_text(json.dumps({'repo': 'acme'}))
            (directory / 'result.json').write_text(json.dumps({'input_id': identity}))
        self.assertEqual(self.build(state=state)['same_input_run'], 'second')

    def test_unreadable_size_is_a_refusal_instead_of_an_undercount(self):
        frozen = snapshot.freeze(self.repo, exclude_globs=['private/*'])
        (self.repo / 'README').unlink()
        with mock.patch.object(snapshot, 'freeze', return_value=frozen):
            with self.assertRaisesRegex(SnapshotError, 'cannot measure source file README'):
                self.build()
