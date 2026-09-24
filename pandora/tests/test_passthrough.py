"""Unclaimed heavy-looking commands run unchanged here and leave one log line.

Through the real POSIX shim, so the `heavy` marker key, the hand-off and the
exit code are all the shipped ones.
"""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from pandora.client import enrollment

HERE = Path(__file__).resolve().parents[2]
SHELLS = ['sh'] + [shell for shell in ('/bin/dash', '/usr/bin/dash') if os.path.exists(shell)][:1]


class HeavyForms(unittest.TestCase):
    def test_a_claimed_form_is_never_also_heavy(self):
        forms = enrollment.heavy_forms([['build'], ['journey']])
        self.assertNotIn(['build'], forms)
        self.assertIn(['lint'], forms)


class ThroughTheShim(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        root = Path(home.name)
        self.repo, self.state, fake = root / 'repo', root / 'state', root / 'fake'
        for directory in (self.repo, self.state, fake):
            directory.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (fake / 'pnpm').write_text('#!/bin/sh\necho "real $*"\nexit 3\n')
        (fake / 'pnpm').chmod(0o755)
        (self.repo / '.git' / 'pandora-enrolled').write_text(enrollment.render(
            socket_path=str(self.state / 'client.sock'), repo='demo',
            claims=[['journey']], heavy=enrollment.heavy_forms([['journey']])))
        self.env = dict(os.environ, PATH='%s:%s' % (HERE / 'bin', fake) + ':/usr/bin:/bin')
        for name in ('PANDORA_OFF', 'PANDORA_ROUTE_DEPTH'):
            self.env.pop(name, None)

    def pnpm(self, *argv):
        return subprocess.run(['sh', str(HERE / 'bin' / 'pnpm'), *argv], cwd=self.repo,
                              env=self.env, capture_output=True, text=True, timeout=30)

    def rows(self):
        path = self.state / 'passthrough.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] \
            if path.exists() else []

    def test_a_heavy_command_runs_unchanged_and_is_logged(self):
        proc = self.pnpm('build', '--filter', 'x')
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(proc.stdout, 'real build --filter x\n')
        self.assertEqual(proc.stderr, '')
        [row] = self.rows()
        self.assertEqual((row['kind'], row['reason'], row['argv'], row['exit']),
                         ('passthrough', 'unclaimed', ['build', '--filter', 'x'], 3))
        self.assertIn('duration_ms', row)

    def test_a_light_command_is_not_logged(self):
        self.assertEqual(self.pnpm('why', 'react').returncode, 3)
        self.assertEqual(self.rows(), [])

    def test_pandora_off_on_a_claimed_command_is_logged_then_run(self):
        self.env['PANDORA_OFF'] = '1'
        for shell in SHELLS:
            with self.subTest(shell=shell):
                (self.state / 'passthrough.jsonl').unlink(missing_ok=True)
                proc = subprocess.run([shell, str(HERE / 'bin' / 'pnpm'), 'journey',
                                       'a"b\\c', 'tab\there'], cwd=self.repo, env=self.env,
                                      capture_output=True, text=True, timeout=30)
                self.assertEqual((proc.returncode, proc.stderr), (3, ''))
                self.assertTrue(proc.stdout.startswith('real journey'))
                [row] = self.rows()
                self.assertEqual((row['reason'], row['exit']), ('off', 3))
                self.assertAlmostEqual(row['ts'], time.time(), delta=60)
                self.assertEqual(row['argv'], ['journey', 'a"b\\c', 'tab\there'])
                self.assertEqual(Path(row['cwd']).resolve(), self.repo.resolve())
                self.assertTrue(row['repo'].endswith('/.git'))

    def test_pandora_off_ignores_a_bad_placement(self):
        self.env.update(PANDORA_OFF='1', PANDORA_WHERE='sideways')
        self.assertEqual(self.pnpm('journey').returncode, 3)
        self.assertEqual([row['reason'] for row in self.rows()], ['off'])

    def test_pandora_off_runs_the_command_when_the_logger_is_missing(self):
        # The client's checkout is the shim's own, or PANDORA_HOME: here, gone.
        self.env.update(PANDORA_OFF='1', PANDORA_HOME=str(self.state / 'no-such-checkout'))
        proc = self.pnpm('journey')
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (3, 'real journey\n', ''))

    def test_pandora_off_runs_the_command_when_there_is_no_python(self):
        self.env.update(PANDORA_OFF='1', PANDORA_PYTHON='/nonexistent/python3')
        proc = self.pnpm('journey')
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (3, 'real journey\n', ''))

    def test_pandora_off_runs_the_command_when_the_logger_cannot_import(self):
        broken = self.state / 'broken'
        (broken / 'pandora' / 'client').mkdir(parents=True)
        for name in ('pandora/__init__.py', 'pandora/client/__init__.py'):
            (broken / name).write_text('')
        (broken / 'pandora' / 'client' / 'passthrough.py').write_text(
            (HERE / 'pandora' / 'client' / 'passthrough.py').read_text())
        self.env['PANDORA_HOME'] = str(broken)
        self.env['PANDORA_OFF'] = '1'
        proc = self.pnpm('journey', 'x')
        self.assertEqual((proc.returncode, proc.stdout), (3, 'real journey x\n'), proc.stderr)
        self.assertEqual(self.rows(), [])

    def test_pandora_off_on_anything_else_is_not_logged(self):
        self.env['PANDORA_OFF'] = '1'
        self.assertEqual(self.pnpm('build').returncode, 3)                 # heavy, unclaimed
        self.env['PANDORA_ROUTE_DEPTH'] = '1'
        self.assertEqual(self.pnpm('journey').returncode, 3)               # nested
        self.assertEqual(self.rows(), [])

    def test_pandora_off_outside_an_enrolled_repo_execs_unchanged(self):
        (self.repo / '.git' / 'pandora-enrolled').unlink()
        self.env['PANDORA_OFF'] = '1'
        proc = self.pnpm('journey')
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (3, 'real journey\n', ''))
        self.assertEqual(self.rows(), [])


class ClaimedOnlyAtTheRoot(unittest.TestCase):
    """`subdirectory passthrough`: the shim claims nothing below the worktree root.

    Through the real shim and the real package, so a claimed command that did
    reach the client would say so on stderr: there is no daemon to answer it.
    """

    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        root = Path(home.name)
        self.repo, self.state, fake = root / 'repo', root / 'state', root / 'fake'
        for directory in (self.repo / 'apps' / 'agent', self.state, fake):
            directory.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (fake / 'pnpm').write_text('#!/bin/sh\necho "real $*"\n')
        (fake / 'pnpm').chmod(0o755)
        (self.repo / '.git' / 'pandora-enrolled').write_text(enrollment.render(
            socket_path=str(self.state / 'client.sock'), repo='demo',
            claims=[['test']], heavy=enrollment.heavy_forms([['test']]),
            strip_prefixes=[['run']], subdirectory='passthrough'))
        self.env = dict(os.environ, PATH='%s:%s' % (HERE / 'bin', fake) + ':/usr/bin:/bin')
        for name in ('PANDORA_OFF', 'PANDORA_ROUTE_DEPTH', 'PANDORA_WHERE', 'PANDORA_HOME'):
            self.env.pop(name, None)

    def pnpm(self, cwd, *argv, shell='sh', **extra):
        return subprocess.run([shell, str(HERE / 'bin' / 'pnpm'), *argv], cwd=cwd,
                              env=dict(self.env, **extra), capture_output=True, text=True,
                              timeout=30)

    def rows(self):
        path = self.state / 'passthrough.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] \
            if path.exists() else []

    def test_below_the_root_a_claimed_form_runs_unchanged_and_silently(self):
        for shell in SHELLS:
            for argv in (['test'], ['run', 'test', 'src/x.test.ts']):
                with self.subTest(shell=shell, argv=argv):
                    proc = self.pnpm(self.repo / 'apps' / 'agent', *argv, shell=shell)
                    self.assertEqual((proc.returncode, proc.stdout, proc.stderr),
                                     (0, 'real %s\n' % ' '.join(argv), ''))
        self.assertEqual(self.rows(), [])

    def test_at_the_root_the_same_form_is_still_claimed(self):
        proc = self.pnpm(self.repo, 'test')
        self.assertIn('daemon socket', proc.stderr)     # it reached the client

    def test_an_override_below_the_root_is_ignored_and_counted_as_unclaimed(self):
        proc = self.pnpm(self.repo / 'apps', 'test', PANDORA_WHERE='remote')
        self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, 'real test\n', ''))
        [row] = self.rows()
        self.assertEqual((row['reason'], row['override']), ('override-ignored', 'remote'))


class ClaimShapes(unittest.TestCase):
    """Every claim `enroll` can write reaches the client through the real shim.

    The shim used to match only one- and two-token claims and understood only
    `strip run`. Eichler strips `validate` as well, so `pnpm validate check` ran
    here, unrouted, unqueued. The package the shim hands off to is a fake that
    prints what it was given, so this tests the shell and nothing else.
    """

    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        root = Path(home.name)
        self.repo, fake, package = root / 'repo', root / 'fake', root / 'package'
        for directory in (self.repo, fake, package / 'pandora' / 'client'):
            directory.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (fake / 'pnpm').write_text('#!/bin/sh\necho "real $*"\n')
        (fake / 'pnpm').chmod(0o755)
        for name in ('pandora/__init__.py', 'pandora/client/__init__.py'):
            (package / name).write_text('')
        (package / 'pandora' / 'client' / 'shim.py').write_text(
            'import sys\nprint("routed " + " ".join(sys.argv[sys.argv.index("--") + 1:]))\n')
        claims = [['journey'], ['test:surface', 'desk'], ['surface', 'run', 'all']]
        (self.repo / '.git' / 'pandora-enrolled').write_text(enrollment.render(
            socket_path=str(root / 'client.sock'), repo='demo', claims=claims,
            heavy=enrollment.heavy_forms(claims),
            strip_prefixes=[['run'], ['validate'], ['exec', 'turbo']]))
        self.env = dict(os.environ, PATH='%s:%s' % (HERE / 'bin', fake) + ':/usr/bin:/bin')
        for name in ('PANDORA_OFF', 'PANDORA_ROUTE_DEPTH', 'PANDORA_HOME', 'PANDORA_WHERE'):
            self.env.pop(name, None)
        self.env['PANDORA_HOME'] = str(package)     # the stand-in client, not this checkout

    def pnpm(self, *argv):
        outputs = set()
        # Every POSIX shell here, since the shim may run under either: macOS's
        # `sh` is bash in POSIX mode, and dash is stricter where it exists.
        for shell in SHELLS:
            proc = subprocess.run([shell, str(HERE / 'bin' / 'pnpm'), *argv], cwd=self.repo,
                                  env=self.env, capture_output=True, text=True, timeout=30)
            outputs.add(proc.stdout.strip())
        self.assertEqual(len(outputs), 1, outputs)
        return outputs.pop()

    def test_every_claim_and_strip_shape_is_routed(self):
        for argv in (['journey', 'S0-01'], ['run', 'journey'], ['validate', 'journey', 'x'],
                     ['run', 'validate', 'journey'], ['test:surface', 'desk', '--x'],
                     ['validate', 'test:surface', 'desk'], ['surface', 'run', 'all'],
                     ['run', 'surface', 'run', 'all', 'y'], ['exec', 'turbo', 'journey']):
            with self.subTest(argv=argv):
                self.assertEqual(self.pnpm(*argv), 'routed ' + ' '.join(argv))

    def test_near_misses_run_here_unchanged(self):
        for argv in (['journeys'], ['test:surface'], ['test:surface', 'desks'],
                     ['surface', 'run'], ['turbo', 'journey'], ['why', 'journey']):
            with self.subTest(argv=argv):
                self.assertEqual(self.pnpm(*argv), 'real ' + ' '.join(argv))

    def test_the_marker_lists_every_strip_before_any_claim(self):
        # The shim strips as it reads, in one pass: it relies on this order.
        text = enrollment.render(socket_path='/s', repo='demo', claims=[['a']],
                                strip_prefixes=[['run'], ['validate']])
        keys = [line.split()[0] for line in text.splitlines() if not line.startswith('#')]
        self.assertLess(max(i for i, key in enumerate(keys) if key == 'strip'),
                        min(i for i, key in enumerate(keys) if key == 'claim'))

    def test_the_subdirectory_mode_round_trips_through_the_marker(self):
        text = enrollment.render(socket_path='/s', repo='demo', claims=[['a']],
                                subdirectory='passthrough')
        self.assertEqual(enrollment.parse(text)['subdirectory'], 'passthrough')
        self.assertIsNone(enrollment.parse(enrollment.render(
            socket_path='/s', repo='demo', claims=[['a']]))['subdirectory'])


if __name__ == '__main__':
    unittest.main()
