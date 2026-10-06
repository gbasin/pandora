"""Manifest CLI uses the selected worktree's configuration without a daemon."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pandora import cli
from pandora.client import settings
from pandora.tests.test_snapshot import make_repo

RECIPE = '''version = 1
[repo]
name = "acme"
entrypoints = ["pnpm"]
[worker]
base_image = "images:ubuntu/26.04"
[secrets]
exclude_globs = ["private/*"]
[[jobs]]
id = "unit"
forms = [{prefix = ["unit"]}]
run = {argv = ["true"]}
'''


class ManifestCLI(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = make_repo(self.base / 'repo', {'src/main': 'code', 'private/key': 'secret'})
        self.fallback = self.base / 'fallback.toml'
        self.fallback.write_text(RECIPE)
        self.state = self.base / 'state'
        self.config = settings.normalize({'repos': [{'name': 'enrolled-alias',
            'root': str(self.repo), 'config': str(self.fallback)}],
            'client': {'state': str(self.state)}})

    def command(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(settings, 'load', return_value=self.config), \
                mock.patch.object(cli, 'ask', side_effect=AssertionError('daemon called')), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(['manifest', *args])
        return code, out.getvalue(), err.getvalue()

    def test_subdirectory_previews_whole_worktree_using_fallback_without_state_writes(self):
        code, out, err = self.command(str(self.repo / 'src'), '--json')
        self.assertEqual((code, err), (0, ''))
        report = json.loads(out)
        self.assertEqual(report['worktree'], str(self.repo.resolve()))
        self.assertEqual(report['repo'], 'acme')
        self.assertEqual(report['excluded'], ['private/key'])
        self.assertEqual(report['files'], 1)
        self.assertFalse(self.state.exists())
        self.assertNotIn('worker_cache', report)

    def test_worktree_config_takes_precedence_over_enrollment_fallback(self):
        (self.repo / 'pandora.toml').write_text(RECIPE.replace('private/*', 'src/*'))
        code, out, _ = self.command(str(self.repo), '--json')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['excluded'], ['src/main'])

    def test_no_path_uses_current_directory(self):
        before = os.getcwd()
        try:
            os.chdir(self.repo / 'src')
            code, out, _ = self.command('--json')
        finally:
            os.chdir(before)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['worktree'], str(self.repo.resolve()))

    def test_bad_path_is_a_refusal(self):
        code, out, err = self.command(str(self.base))
        self.assertEqual((code, out), (64, ''))
        self.assertIn('Git worktree', err)
        code, out, err = self.command(str(self.base / 'gone'))
        self.assertEqual((code, out), (64, ''))
        self.assertIn('not a directory', err)

    def test_failed_requested_probe_preserves_json_preview_and_exits_70(self):
        from pandora.client import manifest
        with mock.patch.object(manifest, 'worker_cache', return_value={
                'status': 'unknown', 'error': 'worker unreachable', 'grace_refreshed': False}):
            code, out, err = self.command(str(self.repo), '--worker-cache', '--json')
        self.assertEqual((code, err), (70, ''))
        report = json.loads(out)
        self.assertEqual(report['files'], 1)
        self.assertEqual(report['worker_cache']['status'], 'unknown')


class WorkerCache(unittest.TestCase):
    def probe(self, output=None, error=None):
        from pandora.client import manifest, worker
        from pandora.snapshot import transfer
        config = settings.normalize({'worker': {'host': 'worker', 'engine_root': '/engine'}})
        with mock.patch.object(worker, 'Worker') as constructor:
            remote = constructor.return_value
            remote.root.return_value = '/engine'
            remote.link.feed.return_value = (0, output, '')
            remote.link.feed.side_effect = error
            result = manifest.worker_cache({'repo': 'acme', 'input_id': 'digest'}, config=config)
            remote.link.feed.assert_called_once_with(transfer.FEEDS['probe'],
                ('/engine', '/engine/src/acme/digest'), timeout=60)
            remote.link.close.assert_called_once()
            remote.bundle_path.assert_not_called()
            remote.submit.assert_not_called()
            temporary = Path(constructor.call_args.kwargs['state'])
            self.assertFalse(temporary.exists())
        return result

    def test_present_probe_discloses_retention_refresh(self):
        self.assertEqual(self.probe('present\n'), {'status': 'present', 'grace_refreshed': True})

    def test_absent_probe_does_not_invent_transfer_bytes(self):
        self.assertEqual(self.probe('absent\n'), {'status': 'absent', 'grace_refreshed': False})

    def test_probe_failure_or_invalid_response_is_unknown_and_closes_link(self):
        from pandora.errors import WorkerUnreachable
        for error, output in ((WorkerUnreachable('offline'), None), (None, 'nonsense')):
            with self.subTest(error=error, output=output):
                report = self.probe(output, error)
                self.assertEqual(report['status'], 'unknown')
                self.assertFalse(report['grace_refreshed'])
                self.assertTrue(report['error'])
