"""The invoking checkout owns routing, even when enrollment spans worktrees."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pandora.client import daemon, shim
from pandora.client import settings
from pandora.errors import NotClaimed
from pandora.exits import INFRA


def config(script, *, markers=True, writeback=False):
    marker_line = 'root_markers = ["package.json"]\n' if markers else ''
    output_line = ('options = [{ name = "--update", sets = "update", writeback = true }]\n'
                   'outputs = [{ kind = "writeback", requires_option = "update", '
                   'paths = ["fixtures"] }]\n'
                   if writeback else '')
    return ('''version = 1
[repo]
name = "demo"
entrypoints = ["pnpm"]
%s[worker]
base_image = "image"
[[jobs]]
id = "journey"
args = "optional"
forms = [{ prefix = ["journey"] }]
%s
run = { argv = ["node", "%s", "run", "{args}"] }
validate = { argv = ["node", "%s", "validate", "{args}"] }
''' % (marker_line, output_line, script, script))


class WorktreeConfig(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        root = Path(home.name)
        self.enrolled = root / 'enrolled'
        self.enrolled.mkdir()
        common = self.enrolled / '.git'
        (common / 'worktrees' / 'old').mkdir(parents=True)
        (common / 'worktrees' / 'new').mkdir(parents=True)
        self.old, self.new = root / 'old', root / 'new'
        for name, path in (('old', self.old), ('new', self.new)):
            path.mkdir()
            (path / '.git').write_text('gitdir: %s\n' % (common / 'worktrees' / name))
            (path / 'package.json').write_text('{}')
        self.external = root / 'external.toml'
        self.external.write_text(config('tools/validation/journey-runner.mjs'))
        self.client = daemon.Daemon.__new__(daemon.Daemon)
        self.client.config = settings.normalize({'repos': [
            {'name': 'demo', 'root': str(self.enrolled), 'config': str(self.external)}]})
        self.client.repo_configs = {}
        self.client.repo_stamps = {}

    def plan(self, root):
        return self.client.plan_for({'cwd': str(root), 'argv': ['pnpm', 'journey', 'S0-01']})

    def test_old_worktree_without_runner_passes_through(self):
        with self.assertRaisesRegex(NotClaimed, 'runner is unavailable'):
            self.plan(self.old)

    def test_own_config_with_missing_direct_runner_passes_through(self):
        (self.old / 'pandora.toml').write_text(config('tools/missing.mjs'))
        with self.assertRaisesRegex(NotClaimed, 'runner is unavailable'):
            self.plan(self.old)

    def test_own_config_wins_over_external_and_cache_is_per_path(self):
        (self.old / 'pandora.toml').write_text(config('tools/old.mjs'))
        (self.new / 'pandora.toml').write_text(config('tools/new.mjs'))
        for root, script in ((self.old, 'old.mjs'), (self.new, 'new.mjs')):
            path = root / 'tools' / script
            path.parent.mkdir(exist_ok=True)
            path.write_text('')
        for root, expected in ((self.old, 'old.mjs'), (self.new, 'new.mjs'),
                               (self.old, 'old.mjs')):
            _repo, loaded, verdict = self.plan(root)
            self.assertEqual(loaded['origin'], 'repo-root')
            self.assertEqual(verdict['plan']['argv'][1], 'tools/' + expected)
            self.assertEqual(verdict['worktree'], str(root.resolve()))

    def test_external_config_can_route_compatible_sibling(self):
        path = self.new / 'tools/validation/journey-runner.mjs'
        path.parent.mkdir(parents=True)
        path.write_text('')
        _repo, loaded, verdict = self.plan(self.new)
        self.assertEqual(loaded['origin'], 'enrollment')
        self.assertEqual(verdict['worktree'], str(self.new.resolve()))

    def test_external_config_needs_positive_compatibility_evidence(self):
        self.external.write_text(config('tools/validation/journey-runner.mjs', markers=False))
        path = self.new / 'tools/validation/journey-runner.mjs'
        path.parent.mkdir(parents=True)
        path.write_text('')
        with self.assertRaises(NotClaimed):
            self.plan(self.new)

    def test_missing_config_passes_through(self):
        self.external.unlink()
        with self.assertRaisesRegex(NotClaimed, 'no usable config'):
            self.plan(self.old)

    def test_explicit_external_config_still_applies_to_enrolled_checkout(self):
        # Legacy external configs can use package commands, whose implementation
        # is not inferable from a literal argv. The enrolled root opted into it.
        self.external.write_text(config('tools/unknown.mjs', markers=False).replace(
            '["node", "tools/unknown.mjs", "run", "{args}"]',
            '["pnpm", "validate", "journey", "{args}"]').replace(
            '["node", "tools/unknown.mjs", "validate", "{args}"]',
            '["pnpm", "validate", "journey", "--dry-run", "{args}"]'))
        _repo, loaded, verdict = self.plan(self.enrolled)
        self.assertEqual(loaded['origin'], 'enrollment')
        self.assertEqual(verdict['plan']['argv'][0], 'pnpm')

    def test_incompatible_writeback_is_marked_for_refusal_by_the_shim(self):
        self.external.write_text(config('tools/missing.mjs', writeback=True))
        with self.assertRaises(NotClaimed) as caught:
            self.client.plan_for({'cwd': str(self.old),
                                  'argv': ['pnpm', 'journey', 'S0-01', '--update']})
        self.assertTrue(caught.exception.writeback)


class PassthroughOverrides(unittest.TestCase):
    def invoke(self, command, *, where=None, policy=None, frame_writeback=False):
        sock = mock.Mock()
        with (mock.patch.object(shim, 'connect', return_value=sock),
              mock.patch.object(shim, 'handshake', return_value=(None, {
                  't': 'error', 'code': 'passthrough', 'msg': 'runner unavailable',
                  'writeback': frame_writeback})),
              mock.patch.object(shim, 'marker_policy', return_value=policy),
              mock.patch.object(shim, 'run_local', return_value=0) as local):
            args = ['--sock', '/tmp/unused.sock', '--real', '/tmp/pnpm']
            if where:
                args += ['--where', where]
            result = shim.main([*args, '--', *command])
        return result, local

    def test_remote_refuses_local_passthrough(self):
        code, local = self.invoke(['journey'], where='remote')
        self.assertEqual(code, INFRA)
        local.assert_not_called()

    def test_update_refuses_local_passthrough(self):
        code, local = self.invoke(['journey', '--update'])
        self.assertEqual(code, INFRA)
        local.assert_not_called()

    def test_writeback_capability_does_not_block_an_ordinary_passthrough(self):
        code, local = self.invoke(['journey'], policy={'writeback': True})
        self.assertEqual(code, 0)
        local.assert_called_once()

    def test_daemon_writeback_refuses_local_passthrough(self):
        code, local = self.invoke(['journey'], frame_writeback=True)
        self.assertEqual(code, INFRA)
        local.assert_not_called()

    def test_connection_lost_after_submission_never_replays(self):
        for response in (OSError('lost'), (None, None)):
            with self.subTest(response=response):
                with (mock.patch.object(shim, 'connect', return_value=mock.Mock()),
                      mock.patch.object(shim, 'handshake') as handshake,
                      mock.patch.object(shim, 'run_local') as local):
                    if isinstance(response, Exception):
                        handshake.side_effect = response
                    else:
                        handshake.return_value = response
                    code = shim.main(['--sock', '/tmp/unused.sock', '--real', '/tmp/pnpm',
                                      '--', 'unit'])
                self.assertEqual(code, INFRA)
                local.assert_not_called()

    def test_regular_passthrough_still_runs_locally(self):
        code, local = self.invoke(['journey'])
        self.assertEqual(code, 0)
        local.assert_called_once()


if __name__ == '__main__':
    unittest.main()
