"""`pandora upgrade`: snapshots, the `current` flip, pruning, the launchers and a safe restart.

The checkout is a real git repository in a temporary directory, so "committed
tree", "dirty" and "untracked" are git's answers rather than mocks. launchd is
`FakeLaunchd` from `test_launchd`, the daemon's `ping` and its side of the
drain are callables, and the clock is fake, so the drain's polls cost nothing.
"""
import io
import json
import os
import plistlib
import subprocess
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from pandora.client import drain, install, launchd
from pandora.tests.test_launchd import FakeLaunchd

HERE = Path(__file__).resolve().parents[2]


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@t',
                           *args], check=True, capture_output=True, text=True).stdout


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class Case(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(os.path.realpath(scratch.name))
        self.data = self.root / 'data' / 'pandora'
        self.home = self.root / 'home'
        self.state = self.root / 'state'
        self.state.mkdir()
        self.said = []
        patch = mock.patch.object(launchd, 'LAUNCHCTL', str(self.root / 'no-launchctl'))
        patch.start()
        self.addCleanup(patch.stop)

    def checkout(self, name='checkout'):
        repo = self.root / name
        (repo / 'bin').mkdir(parents=True)
        (repo / 'pandora' / 'client').mkdir(parents=True)
        (repo / 'bin' / 'pandora').write_text('#!/bin/sh\n# launcher\n')
        (repo / 'bin' / 'pandora').chmod(0o755)
        (repo / 'bin' / 'pnpm').write_text('#!/bin/sh\n# Pandora pnpm shim\n')
        (repo / 'bin' / 'pnpm').chmod(0o755)
        (repo / 'pandora' / '__init__.py').write_text('')
        (repo / 'pandora' / 'cli.py').write_text('VERSION = 1\n')
        (repo / 'pandora' / 'client' / 'shim.py').write_text('')
        (repo / 'pandora' / 'client' / '__init__.py').write_text('')
        (repo / 'pandora' / 'client' / 'daemon.py').write_text('')
        (repo / 'README.md').write_text('readme\n')
        git(repo, 'init', '-q')
        git(repo, 'add', '-A')
        git(repo, 'commit', '-q', '-m', 'one')
        return repo

    def commit(self, repo, text):
        (repo / 'pandora' / 'cli.py').write_text(text)
        git(repo, 'commit', '-q', '-am', text)
        return git(repo, 'rev-parse', 'HEAD').strip()

    def build(self, repo, **kwargs):
        return install.snapshot(install.describe(repo, **kwargs), self.data)


class Snapshots(Case):
    def test_the_committed_tree_lands_under_its_short_commit(self):
        repo = self.checkout()
        (repo / 'scratch.txt').write_text('untracked\n')
        head = git(repo, 'rev-parse', 'HEAD').strip()
        version = self.build(repo)
        path = self.data / 'versions' / head[:12]
        self.assertEqual((version['name'], version['path'], version['reused']),
                         (head[:12], str(path), False))
        self.assertEqual((path / 'pandora' / 'cli.py').read_text(), 'VERSION = 1\n')
        self.assertTrue(os.access(path / 'bin' / 'pandora', os.X_OK))
        self.assertFalse((path / 'scratch.txt').exists(), 'untracked files are not the tree')
        meta = json.loads((path / '.pandora-version').read_text())
        self.assertEqual((meta['commit'], meta['source'], meta['dirty']), (head, str(repo), False))
        self.assertEqual(meta['code'], install.code_digest(path))
        self.assertEqual([p.name for p in (self.data / 'versions').iterdir()], [head[:12]],
                         'no stage is left behind')

    def test_the_same_commit_is_reused(self):
        repo = self.checkout()
        first = self.build(repo)
        second = self.build(repo)
        self.assertTrue(second['reused'])
        self.assertEqual(first['meta'], second['meta'])

    def test_an_edited_version_is_never_reused(self):
        repo = self.checkout()
        first = self.build(repo)
        path = Path(first['path'])
        (path / 'pandora' / 'cli.py').write_text('EDITED = 1\n')
        again = self.build(repo)
        self.assertEqual(again['name'], '%s-%s' % (first['name'], first['meta']['code'][:8]))
        self.assertEqual(again['edited'], first['name'])
        self.assertFalse(again['reused'])
        self.assertEqual((Path(again['path']) / 'pandora' / 'cli.py').read_text(), 'VERSION = 1\n')
        self.assertEqual((path / 'pandora' / 'cli.py').read_text(), 'EDITED = 1\n',
                         'the edited one may be running; it is left as it is')
        self.assertTrue(self.build(repo)['reused'], 'the rebuilt one is reused next time')

    def test_uncommitted_changes_are_refused(self):
        repo = self.checkout()
        (repo / 'untracked.txt').write_text('x\n')
        install.describe(repo)                              # untracked is not dirty
        (repo / 'pandora' / 'cli.py').write_text('VERSION = 2\n')
        with self.assertRaises(install.Refused) as caught:
            install.describe(repo)
        self.assertIn('uncommitted changes (1 file)', str(caught.exception))
        self.assertIn('--dirty', str(caught.exception))
        self.assertFalse(self.data.exists(), 'a refusal writes nothing')

    def test_a_dirty_snapshot_is_the_working_tree_under_its_own_name(self):
        repo = self.checkout()
        head = git(repo, 'rev-parse', 'HEAD').strip()
        (repo / 'pandora' / 'cli.py').write_text('VERSION = 2\n')
        (repo / 'README.md').unlink()
        version = self.build(repo, dirty_ok=True)
        self.assertRegex(version['name'], r'^%s-dirty-[0-9a-f]{8}$' % head[:12])
        path = Path(version['path'])
        self.assertEqual((path / 'pandora' / 'cli.py').read_text(), 'VERSION = 2\n')
        self.assertFalse((path / 'README.md').exists())
        self.assertTrue(version['meta']['dirty'])
        # Other edits on the same commit are another directory, never an overwrite.
        (repo / 'pandora' / 'cli.py').write_text('VERSION = 3\n')
        again = self.build(repo, dirty_ok=True)
        self.assertNotEqual(again['name'], version['name'])
        self.assertEqual((path / 'pandora' / 'cli.py').read_text(), 'VERSION = 2\n')

    def test_not_a_pandora_checkout_is_refused(self):
        other = self.root / 'other'
        other.mkdir()
        git(other, 'init', '-q')
        with self.assertRaises(install.Refused) as caught:
            install.describe(other)
        self.assertIn('not a Pandora checkout', str(caught.exception))

    def test_the_source_defaults_to_where_current_came_from(self):
        repo = self.checkout()
        install.flip(self.data, self.build(repo)['name'])
        self.assertEqual(install.source_for(None, self.data), str(repo))
        self.assertEqual(install.source_for('/x', self.data), '/x')


class Flip(Case):
    def test_current_is_a_relative_link_replaced_in_one_rename(self):
        repo = self.checkout()
        one = self.build(repo)['name']
        self.commit(repo, 'VERSION = 2\n')
        two = self.build(repo)['name']
        install.flip(self.data, one)
        link = self.data / 'current'
        self.assertEqual(os.readlink(link), os.path.join('versions', one))
        # A reader resolving `current` in a loop while it flips back and forth
        # must always land on a whole version, never on nothing.
        seen, stop = set(), threading.Event()

        def reader():
            while not stop.is_set():
                seen.add((Path(os.path.realpath(link)) / 'pandora' / 'cli.py').is_file())
        thread = threading.Thread(target=reader)
        thread.start()
        for index in range(300):
            install.flip(self.data, two if index % 2 == 0 else one)
        stop.set()
        thread.join()
        self.assertEqual(seen, {True})
        self.assertEqual(install.installed(self.data)['name'], one)
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), ['current', 'versions'])

    def test_a_current_that_is_a_directory_is_refused(self):
        repo = self.checkout()
        name = self.build(repo)['name']
        (self.data / 'current').mkdir()
        with self.assertRaises(install.Refused):
            install.flip(self.data, name)

    def test_package_home_names_current_once_it_exists(self):
        env = {'XDG_DATA_HOME': str(self.data.parent)}
        self.assertEqual(install.package_home(env), str(HERE))
        repo = self.checkout()
        install.flip(self.data, self.build(repo)['name'])
        self.assertEqual(install.package_home(env), str(self.data / 'current'))

    def test_code_in_a_version_directory_names_its_own_current(self):
        # A launchd daemon with no XDG_DATA_HOME in its plist cannot find the
        # data root, and must still never write its version directory down.
        running = self.data / 'versions' / 'v9'
        (running / 'pandora').mkdir(parents=True)
        self.assertEqual(install.package_home({'HOME': str(self.root / 'elsewhere')},
                                              running=running),
                         str(self.data / 'current'))

    def test_the_plist_carries_xdg_data_home_when_it_is_set(self):
        body = launchd.render('x', program='/p/bin/pandora', config_path='/c.toml',
                              state='/s', path='/usr/bin', data_home='/d')
        self.assertEqual(body['EnvironmentVariables']['XDG_DATA_HOME'], '/d')
        body = launchd.render('x', program='/p/bin/pandora', config_path='/c.toml',
                              state='/s', path='/usr/bin')
        self.assertNotIn('XDG_DATA_HOME', body['EnvironmentVariables'])

    def test_data_root_follows_xdg_and_ignores_a_relative_one(self):
        self.assertEqual(install.data_root({'XDG_DATA_HOME': '/x'}), Path('/x/pandora'))
        self.assertEqual(install.data_root({'XDG_DATA_HOME': 'rel', 'HOME': '/h'}),
                         Path('/h/.local/share/pandora'))
        self.assertEqual(install.data_root({}, home='/u'), Path('/u/.local/share/pandora'))


class Prune(Case):
    def versions(self, count):
        repo = self.checkout()
        names = []
        for index in range(count):
            if index:
                self.commit(repo, 'VERSION = %d\n' % (index + 1))
            names.append(self.build(repo)['name'])
            os.utime(self.data / 'versions' / names[-1], (index * 100, index * 100))
        return names

    def test_keeps_the_newest_current_and_the_daemons_home(self):
        names = self.versions(6)                  # oldest first
        install.flip(self.data, names[0])
        os.utime(self.data / 'versions' / names[0], (0, 0))   # current, but installed long ago
        daemon_home = self.data / 'versions' / names[1]
        removed = install.prune(self.data, keep=3, protect=[str(daemon_home)])
        self.assertEqual(sorted(removed), sorted(names[2:3]))
        left = sorted(p.name for p in (self.data / 'versions').iterdir())
        self.assertEqual(left, sorted([names[0], names[1]] + names[3:]), 'no .trash-* left')

    def test_a_dead_upgrades_stage_goes_and_a_live_one_stays(self):
        self.versions(1)
        dead = self.data / 'versions' / '.incoming-999999-abc'
        live = self.data / 'versions' / ('.incoming-%d-abc' % os.getpid())
        dead.mkdir()
        live.mkdir()
        install.prune(self.data, keep=3, is_alive=lambda pid: pid == os.getpid())
        self.assertFalse(dead.exists())
        self.assertTrue(live.exists())


class Launchers(Case):
    def test_links_into_a_checkout_are_re_pointed_through_current(self):
        repo = self.checkout()
        install.flip(self.data, self.build(repo)['name'])
        bindir = self.root / 'bin'
        bindir.mkdir()
        (bindir / 'pandora').symlink_to(repo / 'bin' / 'pandora')
        (bindir / 'pnpm').symlink_to(repo / 'bin' / 'pnpm')
        env = {'PATH': '%s:/usr/bin:/bin' % bindir}
        self.assertFalse(install.through_current(bindir / 'pandora', self.data))
        links = install.launcher_links(env, self.data, source=str(repo))
        self.assertEqual([item['status'] for item in links], ['fixed', 'fixed'])
        for name in ('pandora', 'pnpm'):
            self.assertEqual(os.readlink(bindir / name), str(self.data / 'current' / 'bin' / name))
            self.assertTrue(install.through_current(bindir / name, self.data))
        self.assertEqual([item['status'] for item in install.launcher_links(env, self.data)],
                         ['ok', 'ok'])
        self.assertEqual(sorted(os.listdir(bindir)), ['pandora', 'pnpm'], 'no scratch link left')

    def test_a_link_into_another_checkout_is_left_alone(self):
        # Someone else's install -- the live one, when this runs with another
        # data directory -- is reported, never re-pointed.
        repo = self.checkout()
        other = self.checkout('other')
        install.flip(self.data, self.build(repo)['name'])
        bindir = self.root / 'bin'
        bindir.mkdir()
        (bindir / 'pandora').symlink_to(other / 'bin' / 'pandora')
        links = install.launcher_links({'PATH': str(bindir)}, self.data, source=str(repo))
        self.assertEqual(links[0]['status'], 'other')
        self.assertEqual(os.readlink(bindir / 'pandora'), str(other / 'bin' / 'pandora'))
        self.assertIn('not the checkout upgraded here; left alone', install.link_lines(links)[0])
        # A link into an older version of this data root is ours to move.
        old = self.data / 'versions' / install.installed(self.data)['name']
        (bindir / 'pandora').unlink()
        (bindir / 'pandora').symlink_to(old / 'bin' / 'pandora')
        links = install.launcher_links({'PATH': str(bindir)}, self.data, source=str(repo))
        self.assertEqual(links[0]['status'], 'fixed')

    def test_a_link_to_the_version_directory_is_not_through_current(self):
        repo = self.checkout()
        version = self.build(repo)
        install.flip(self.data, version['name'])
        pinned = self.root / 'pinned'
        pinned.symlink_to(Path(version['path']) / 'bin' / 'pandora')
        self.assertFalse(install.through_current(pinned, self.data))
        self.assertTrue(install.through_current(self.data / 'current', self.data))
        self.assertFalse(install.through_current(version['path'], self.data))

    def test_a_copy_is_reported_and_a_missing_one_is_named(self):
        repo = self.checkout()
        install.flip(self.data, self.build(repo)['name'])
        bindir = self.root / 'bin'
        bindir.mkdir()
        (bindir / 'pandora').write_bytes((repo / 'bin' / 'pandora').read_bytes())
        (bindir / 'pandora').chmod(0o755)
        links = install.launcher_links({'PATH': str(bindir)}, self.data)
        self.assertEqual([item['status'] for item in links], ['foreign', 'missing'])
        lines = install.link_lines(links)
        self.assertIn('is not a symlink to a Pandora launcher', lines[0])
        self.assertIn('ln -s %s' % (self.data / 'current' / 'bin' / 'pnpm'), lines[1])


class PhysicalHome(Case):
    def test_the_launcher_imports_from_the_version_not_through_current(self):
        # A process that starts through `current` must keep importing from the
        # version it started with after the next flip, so its PYTHONPATH is the
        # version directory itself. The "interpreter" prints what it was given.
        import shutil
        version = self.data / 'versions' / 'v1'
        (version / 'pandora').mkdir(parents=True)
        (version / 'pandora' / 'cli.py').write_text('')
        shutil.copytree(HERE / 'bin', version / 'bin')
        install.flip(self.data, 'v1')
        bindir = self.root / 'bin'
        bindir.mkdir()
        (bindir / 'pandora').symlink_to(self.data / 'current' / 'bin' / 'pandora')
        python = self.root / 'python'
        python.write_text('#!/bin/sh\nprintf "%s\\n" "$PYTHONPATH"\n')
        python.chmod(0o755)
        env = {key: value for key, value in os.environ.items()
               if key not in ('PYTHONPATH', 'PANDORA_HOME')}
        env.update(PATH='%s:/usr/bin:/bin' % bindir, PANDORA_PYTHON=str(python))
        proc = subprocess.run([str(bindir / 'pandora'), 'ps'], cwd='/', env=env,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), str(version))

    def test_a_pandora_typed_inside_a_checkout_runs_its_own_package(self):
        # `python -m` puts the cwd first on sys.path; `-P` keeps it off, so a
        # checkout's package in the cwd does not shadow the launcher's own.
        import sys
        shadow = self.root / 'shadow'
        (shadow / 'pandora').mkdir(parents=True)
        (shadow / 'pandora' / '__init__.py').write_text('')
        (shadow / 'pandora' / 'cli.py').write_text('print("SHADOW")\n')
        env = {key: value for key, value in os.environ.items()
               if key not in ('PYTHONPATH', 'PANDORA_HOME')}
        env.update(PATH='/usr/bin:/bin', PANDORA_PYTHON=sys.executable)
        proc = subprocess.run([str(HERE / 'bin' / 'pandora'), 'doctor', '--package-home'],
                              cwd=str(shadow), env=env, capture_output=True, text=True,
                              timeout=30)
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, str(HERE)), proc.stderr)

    def test_the_shim_hands_python_the_version_directory(self):
        # The claimed path of the shim, with PANDORA_OFF so no daemon is needed:
        # the marker's `home` is `current`, and Python must get the version.
        import shutil
        version = self.data / 'versions' / 'v1'
        (version / 'pandora' / 'client').mkdir(parents=True)
        (version / 'pandora' / 'cli.py').write_text('')
        (version / 'pandora' / 'client' / 'passthrough.py').write_text('')
        (version / 'pandora' / 'client' / 'shim.py').write_text('')
        shutil.copytree(HERE / 'bin', version / 'bin')
        install.flip(self.data, 'v1')
        shim = self.root / 'shim'
        shim.mkdir()
        (shim / 'pnpm').symlink_to(self.data / 'current' / 'bin' / 'pnpm')
        real = self.root / 'real'
        real.mkdir()
        (real / 'pnpm').write_text('#!/bin/sh\necho real\n')
        (real / 'pnpm').chmod(0o755)
        python = self.root / 'python'
        python.write_text('#!/bin/sh\nprintf "%s|%s\\n" "$PYTHONPATH" "$*"\n')
        python.chmod(0o755)
        repo = self.root / 'repo'
        repo.mkdir()
        git(repo, 'init', '-q')
        (repo / '.git' / 'pandora-enrolled').write_text(
            'sock %s\nhome %s\nclaim build\n' % (self.state / 'client.sock', self.data / 'current'))
        env = {'PATH': '%s:%s:/usr/bin:/bin' % (shim, real), 'HOME': str(self.root),
               'PANDORA_PYTHON': str(python), 'PANDORA_OFF': '1'}
        proc = subprocess.run([str(shim / 'pnpm'), 'build'], cwd=str(repo), env=env,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        pythonpath, argv = proc.stdout.strip().split('|', 1)
        self.assertEqual(pythonpath, str(version))
        self.assertTrue(argv.startswith('-B -c '), argv)
        self.assertIn(' pandora.client.passthrough --real ', argv)
        # No cache, and PANDORA_OFF unset: the `--refresh` start is pinned too.
        (repo / '.git' / 'pandora-enrolled').unlink()
        (repo / '.git' / 'pandora-repo').write_text(
            'sock %s\nhome %s\n' % (self.state / 'client.sock', self.data / 'current'))
        del env['PANDORA_OFF']
        proc = subprocess.run([str(shim / 'pnpm'), 'build'], cwd=str(repo), env=env,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        pythonpath, argv = proc.stdout.strip().split('|', 1)
        self.assertEqual(pythonpath, str(version))
        self.assertIn(' pandora.client.shim ', argv)
        self.assertIn(' --refresh ', argv)


class WrittenHomes(Case):
    """What `daemon --install` and `enroll` write down once a snapshot is installed."""

    def test_the_plist_runs_current_once_it_exists(self):
        # Before: the checkout, as before `upgrade` existed (test_launchd).
        data = self.home / '.local' / 'share' / 'pandora'
        repo = self.checkout()
        version = install.snapshot(install.describe(repo), data)
        install.flip(data, version['name'])
        fake = FakeLaunchd()
        body = launchd.install('com.pandora.daemon', config_path=self.root / 'c.toml',
                               state=self.state, env={'PATH': '/usr/bin:/bin'}, uid=501,
                               home=self.home, python='/usr/bin/python3', run=fake,
                               say=self.said.append)
        program = str(data / 'current' / 'bin' / 'pandora')
        self.assertEqual(body['ProgramArguments'][0], program, 'through current, as spelled')
        self.assertEqual(launchd.agent_program('com.pandora.daemon', self.home), program)
        self.assertIn('`pandora upgrade`', self.said[-1])

    def test_enroll_writes_no_client_home(self):
        from pandora import cli
        from pandora.client import enrollment
        from pandora.tests.test_config import MINIMAL
        repo = self.root / 'target'
        repo.mkdir()
        git(repo, 'init', '-q')
        (repo / 'pandora.toml').write_text(MINIMAL)
        data = install.data_root()                  # the test package's scratch directory
        install.flip(data, install.snapshot(install.describe(self.checkout()), data)['name'])
        self.addCleanup(lambda: os.unlink(data / 'current'))
        config = self.root / 'client.toml'
        config.write_text('[worker]\nhost = "w"\n\n[client]\nstate = "%s"\n' % self.state)
        with mock.patch('sys.stderr'), mock.patch('pandora.cli.check_daemon_knows_claims',
                                                  return_value=None):
            code = cli.main(['--state', str(self.state), '--config', str(config),
                             'enroll', str(repo)])
        self.assertEqual(code, 0)
        # The shim runs the client from where it is linked, which `pandora
        # upgrade` points through `current`; no file names a home to go stale.
        for path in (repo / '.git' / enrollment.REGISTRATION, enrollment.cache_path(repo)):
            self.assertIsNone(enrollment.parse(path.read_text())['home'], path)


class Doctor(Case):
    """Each way the snapshot layout can disagree with itself, as `doctor` reports it."""

    def setUp(self):
        super().setUp()
        self.repo = self.checkout()
        self.version = self.build(self.repo)
        install.flip(self.data, self.version['name'])
        self.bindir = self.root / 'bin'
        self.bindir.mkdir()

    def link(self, name, target):
        (self.bindir / name).symlink_to(target)
        return str(self.bindir / name)

    def test_no_snapshot_is_information_and_a_dangling_current_fails(self):
        from pandora.client import doctor
        empty = self.root / 'empty'
        self.assertEqual(doctor.check_install(empty, None, None)['status'], 'info')
        empty.mkdir()
        (empty / 'current').symlink_to('versions/gone')
        item = doctor.check_install(empty, None, None)
        self.assertEqual(item['status'], 'fail')
        self.assertIn('holds no pandora package', item['detail'])

    def test_launchers_through_current_are_ok_and_a_moved_checkout_is_noted(self):
        from pandora.client import doctor
        launcher = self.link('pandora', self.data / 'current' / 'bin' / 'pandora')
        shim = self.link('pnpm', self.data / 'current' / 'bin' / 'pnpm')
        item = doctor.check_install(self.data, launcher, shim)
        self.assertEqual(item['status'], 'ok', item)
        self.assertIn('current is %s, from %s' % (self.version['name'], self.repo), item['detail'])
        head = self.commit(self.repo, 'VERSION = 2\n')
        item = doctor.check_install(self.data, launcher, shim)
        self.assertEqual(item['status'], 'ok', 'pulling changes nothing live')
        self.assertIn('the checkout is at %s since' % head[:12], item['detail'])

    def test_a_launcher_or_shim_into_the_checkout_warns(self):
        from pandora.client import doctor
        launcher = self.link('pandora', self.repo / 'bin' / 'pandora')
        shim = self.link('pnpm', self.data / 'current' / 'bin' / 'pnpm')
        item = doctor.check_install(self.data, launcher, shim)
        self.assertEqual(item['status'], 'warn')
        self.assertIn('`pandora` on PATH (%s) runs %s, not current' % (launcher, self.repo),
                      item['detail'])
        item = doctor.check_install(self.data, self.link('x', self.data / 'current' / 'bin' /
                                                         'pandora'),
                                    self.link('pnpm2', self.repo / 'bin' / 'pnpm'))
        self.assertIn('the pnpm shim', item['detail'])
        self.assertEqual(item['status'], 'warn')

    def daemon(self, home, supervised=None):
        from pandora.client import doctor
        pong = {'t': 'pong', 'pid': 7, 'v': 2, 'home': str(home), 'code': None}
        with mock.patch.object(doctor, 'ping', return_value=pong):
            item, _ = doctor.check_daemon(self.state / 'client.sock', None, self.data,
                                          supervised=supervised)
        return item

    def test_restart_advice_follows_who_runs_the_daemon(self):
        old = self.version
        self.commit(self.repo, 'VERSION = 2\n')
        install.flip(self.data, self.build(self.repo)['name'])
        item = self.daemon(old['path'], supervised=lambda pid: pid == 7)
        self.assertTrue(item['detail'].endswith(': `pandora daemon --restart`'), item)
        item = self.daemon(old['path'], supervised=lambda pid: False)
        self.assertIn('`pandora daemon --stop`, then start it again', item['detail'])
        self.assertNotIn('daemon --restart`', item['detail'].replace('`--restart` cannot', ''))

    def test_a_daemon_on_current_is_ok(self):
        item = self.daemon(self.version['path'])
        self.assertEqual(item['status'], 'ok', item)
        self.assertIn('runs current (%s)' % self.version['name'], item['detail'])

    def test_a_daemon_behind_current_is_told_to_restart_or_upgrade(self):
        old = self.version
        self.commit(self.repo, 'VERSION = 2\n')
        new = self.build(self.repo)
        install.flip(self.data, new['name'])
        item = self.daemon(old['path'])
        self.assertEqual(item['status'], 'warn')
        self.assertIn('daemon runs %s, current is %s; restart it' % (old['name'], new['name']),
                      item['detail'])
        # The checkout moved on too: an upgrade, not just a restart.
        head = self.commit(self.repo, 'VERSION = 3\n')
        item = self.daemon(old['path'])
        self.assertIn('and %s is at %s since; run `pandora upgrade --from %s`'
                      % (self.repo, head[:12], self.repo), item['detail'])

    def test_an_edited_version_directory_is_named_with_the_way_out(self):
        from pandora.client import doctor
        pong = {'t': 'pong', 'pid': 7, 'v': 2, 'home': self.version['path'], 'code': '0' * 64,
                'code_modules': ['cli.py']}
        with mock.patch.object(doctor, 'ping', return_value=pong):
            item, _ = doctor.check_daemon(self.state / 'client.sock', None, self.data)
        self.assertEqual(item['status'], 'warn')
        self.assertIn('something edited the version directory. `pandora upgrade --from %s` '
                      'builds it again under a new name' % self.repo, item['detail'])

    def test_a_daemon_on_a_checkout_is_told_to_install_from_current(self):
        item = self.daemon(self.repo)
        self.assertEqual(item['status'], 'warn')
        self.assertIn('daemon runs the checkout %s, current is %s. `pandora daemon --install`'
                      % (self.repo, self.version['name']), item['detail'])

    def test_a_client_home_line_is_information_whatever_it_names(self):
        from pandora.client import doctor, enrollment
        target = self.root / 'target'
        target.mkdir()
        git(target, 'init', '-q')
        registration = target / '.git' / enrollment.REGISTRATION
        text = enrollment.registration_text(socket_path=str(self.state / 'client.sock'),
                                            repo='demo')
        for home in (self.repo, self.version['path'], self.data / 'current',
                     self.data / 'versions' / 'gone'):
            registration.write_text(text.replace('repo demo\n', 'repo demo\nhome %s\n' % home))
            items = {item['name']: item for item in
                     doctor.check_repository(str(target), None, self.state / 'client.sock',
                                             self.data)}
            self.assertEqual(items['client home']['status'], 'info', home)
            self.assertIn('no longer read', items['client home']['detail'])

ROWS = [
    {'id': 'r-local-run', 'lane': 'local', 'state': 'running', 'argv': ['check']},
    {'id': 'r-local-q', 'lane': 'local', 'state': 'queued', 'argv': ['test']},
    {'id': 'r-ship', 'lane': 'remote', 'state': 'queued', 'phase': 'ship', 'argv': ['journey']},
    {'id': 'r-accepted', 'lane': 'remote', 'state': 'running', 'argv': ['check']},
    {'id': 'r-done', 'lane': 'local', 'state': 'passed', 'argv': ['check']},
]


class Kickstarts(FakeLaunchd):
    """launchd, plus the one thing the successor does that upgrade waits for: the marker goes."""

    def __init__(self, loaded, state):
        super().__init__(loaded)
        self.state = state
        self.refuse_kickstart = False
        self.successor_settles = True
        self.marker_at_kickstart = None

    def __call__(self, argv, **kwargs):
        if argv[1] == 'kickstart' and '-k' in argv[2:]:
            self.marker_at_kickstart = drain.marker_path(self.state).exists()
            if self.refuse_kickstart:
                self.calls.append(argv[1:])
                return self.done(5, err='Kickstart failed')
            if self.successor_settles:
                drain.clear_marker(self.state)
        return super().__call__(argv, **kwargs)


class TheDaemon(Case):
    def test_only_a_pong_is_a_daemon(self):
        def raises(error):
            def ping():
                raise error
            return ping
        self.assertEqual(install.probe(raises(FileNotFoundError('x')))[0], 'absent')
        self.assertEqual(install.probe(raises(ConnectionRefusedError('x')))[0], 'absent')
        self.assertEqual(install.probe(raises(TimeoutError('timed out')))[0], 'silent')
        self.assertEqual(install.probe(lambda: {'t': 'error', 'msg': 'v1 vs v2'}),
                         ('silent', 'it answered v1 vs v2'))
        self.assertEqual(install.probe(lambda: {'t': 'pong', 'pid': 1})[0], 'pong')


class Upgrade(Case):
    """The whole verb, against a fake launchd, a fake daemon and a fake clock."""

    LABEL = 'com.pandora.daemon'

    def setUp(self):
        super().setUp()
        self.repo = self.checkout()
        # The daemon runs an older version, from a plist written through current.
        old = self.build(self.repo)['name']
        install.flip(self.data, old)
        self.old = str(self.data / 'versions' / old)
        self.commit(self.repo, 'VERSION = 2\n')
        self.fake = Kickstarts({self.LABEL: 4242}, self.state)
        self.pong = {'t': 'pong', 'pid': 4242, 'home': self.old}
        self.write_plist(self.data / 'current' / 'bin' / 'pandora')
        (self.state / 'launchd.json').write_text(json.dumps({'label': self.LABEL}))
        self.clock = FakeClock()
        self.rows = []
        self.drains = 0
        self.flips = []

    def write_plist(self, program):
        path = launchd.plist_path(self.LABEL, self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps(launchd.render(
            self.LABEL, program=program, config_path='/c.toml', state=self.state,
            path='/usr/bin')))

    def ping(self):
        pid = self.fake.loaded.get(self.LABEL)
        if pid not in (None, 4242):
            # A daemon launchd started after the restart runs what current names.
            return {'t': 'pong', 'pid': pid, 'home': os.path.realpath(self.data / 'current')}
        if isinstance(self.pong, Exception):
            raise self.pong
        return self.pong

    def ask(self, _sock, request, timeout=30.0):
        """The daemon's side of a drain: the marker, and the rows that still block."""
        if request['op'] == 'ping':
            return {'t': 'pong', 'pid': 4242}
        self.assertEqual(request['op'], 'drain')
        if request.get('cancel'):
            drain.clear_marker(self.state)
            return {'t': 'drain', 'draining': False}
        self.drains += 1
        if not drain.marker_path(self.state).exists():
            drain.write_marker(self.state, {'since': 0, 'pid': request.get('pid')})
        rows = self.rows.pop(0) if self.rows else []
        return {'t': 'drain', 'draining': True, 'blockers': drain.blockers(rows)}

    def current(self):
        return install.installed(self.data)['path']

    def upgrade(self, **kwargs):
        import sys
        return install.upgrade(state=self.state, data=self.data,
                               env={'PATH': '/usr/bin:/bin', 'PANDORA_PYTHON': sys.executable},
                               home=self.home, platform='darwin', launchctl=self.fake,
                               ping=self.ping, ask=self.ask, clock=self.clock,
                               sleep=self.clock.sleep, say=self.said.append,
                               **dict({'source': str(self.repo)}, **kwargs))

    def kicked(self):
        return any(call[:2] == ['kickstart', '-k'] for call in self.fake.calls)

    def test_waits_then_flips_and_restarts(self):
        self.rows = [ROWS[:1], [], []]
        code = self.upgrade()
        text = '\n'.join(self.said)
        self.assertEqual(code, 0, text)
        new = install.installed(self.data)['name']
        self.assertNotEqual(self.current(), self.old)
        self.assertIn('daemon   pid 4242 runs %s' % Path(self.old).name, text)
        self.assertIn('r-local-run local running', text)
        # The flip comes after the wait, not before it.
        self.assertLess(text.index('waiting up to'), text.index('current  %s -> %s'
                                                               % (Path(self.old).name, new)))
        self.assertTrue(self.kicked())
        self.assertIn('runs %s' % new, self.said[-1] if 'pruned' not in self.said[-1]
                      else self.said[-2])

    def test_a_wait_that_runs_out_changes_nothing_and_undrains(self):
        self.rows = [ROWS] * 1000
        code = self.upgrade(wait=30)
        self.assertEqual(code, 75)
        self.assertFalse(self.kicked())
        self.assertEqual(self.current(), self.old, 'current did not move')
        self.assertFalse(drain.marker_path(self.state).exists(), 'left draining')
        self.assertIn('Nothing changed: current is still %s' % Path(self.old).name,
                      '\n'.join(self.said))
        self.assertIn('--now', self.said[-1])
        self.assertIn('retry later, or `pandora upgrade --now` to end them', self.said)

    def test_current_moves_while_the_daemon_holds_new_runs_just_before_the_kickstart(self):
        # No window for a run to start between the last poll and the restart:
        # the daemon is draining when current moves and when launchd restarts it.
        self.rows = [ROWS[2:3], []]
        real = install.flip
        with mock.patch.object(install, 'flip', side_effect=lambda data, name: (
                self.flips.append(drain.marker_path(self.state).exists()),
                real(data, name))):
            code = self.upgrade()
        self.assertEqual(code, 0, self.said)
        self.assertEqual(self.flips, [True])
        self.assertTrue(self.fake.marker_at_kickstart)
        self.assertEqual(self.drains, 2)

    def test_a_refused_kickstart_puts_current_back_and_undrains(self):
        self.fake.refuse_kickstart = True
        self.assertEqual(self.upgrade(), 1)
        self.assertEqual(self.current(), self.old)
        self.assertFalse(drain.marker_path(self.state).exists())
        self.assertIn('Nothing changed', self.said[-1])

    def test_a_successor_that_never_settles_is_an_error_with_the_way_back(self):
        self.fake.successor_settles = False
        self.assertEqual(self.upgrade(), 1)
        self.assertIn('`pandora upgrade --version %s`' % Path(self.old).name, self.said[-1])

    def test_a_drain_that_cannot_be_ended_does_not_claim_nothing_changed(self):
        self.rows = [ROWS] * 1000
        ask = self.ask

        def silent_on_cancel(sock, request, timeout=30.0):
            if request.get('cancel'):
                raise TimeoutError('timed out')
            return ask(sock, request, timeout)
        self.ask = silent_on_cancel
        self.assertEqual(self.upgrade(wait=30), 75)
        self.assertEqual(self.current(), self.old)
        text = '\n'.join(self.said)
        self.assertIn('could not end the drain', text)
        self.assertNotIn('Nothing changed', text)
        self.assertIn('may hold new commands for up to 30s more', text)

    def test_the_check_after_the_restart_is_ten_seconds_not_twenty_timeouts(self):
        asked = []

        def slow_after_restart():
            if self.fake.loaded.get(self.LABEL) != 4242:
                asked.append(1)
                self.clock.now += 3            # a ping that times out costs its timeout
                raise TimeoutError('timed out')
            return self.pong
        self.ping = slow_after_restart
        self.assertEqual(self.upgrade(), 1)
        self.assertLessEqual(len(asked), 4)

    def test_now_does_not_wait_and_says_what_ends(self):
        self.rows = [ROWS, ROWS]
        self.assertEqual(self.upgrade(now=True), 0, self.said)
        self.assertEqual(self.drains, 1)
        self.assertEqual(self.clock.slept, [])
        self.assertTrue(self.kicked())
        text = '\n'.join(self.said)
        self.assertIn('a remote run still freezing or shipping ends with exit 70', text)
        self.assertIn('one submitting is looked up on the worker', text)

    def test_a_plist_that_runs_a_checkout_changes_nothing_unless_asked(self):
        self.write_plist(self.repo / 'bin' / 'pandora')
        self.assertEqual(self.upgrade(), 1)
        self.assertFalse(self.kicked())
        self.assertEqual(self.current(), self.old)
        self.assertIn('`pandora upgrade --no-restart` moves current', self.said[-1])
        self.said.clear()
        self.assertEqual(self.upgrade(no_restart=True), 0)
        self.assertNotEqual(self.current(), self.old)
        self.assertFalse(self.kicked())

    def test_a_hand_started_daemon_changes_nothing(self):
        self.fake.loaded.clear()
        self.assertEqual(self.upgrade(), 1)
        self.assertIn('launchd does not run this daemon', '\n'.join(self.said))
        self.assertEqual(self.current(), self.old)

    def test_a_daemon_that_does_not_answer_ping_is_not_restarted(self):
        # A daemon busy on a swapping Mac times out; it may be driving runs.
        self.pong = TimeoutError('timed out')
        self.assertEqual(self.upgrade(), 75)
        self.assertFalse(self.kicked())
        self.assertEqual(self.current(), self.old)
        self.assertIn('did not answer (timed out)', '\n'.join(self.said))
        # --now is the explicit override, and restarts through the recorded label.
        self.said.clear()
        self.assertEqual(self.upgrade(now=True), 0, self.said)
        self.assertTrue(self.kicked())

    def test_an_error_frame_is_not_a_pong(self):
        self.pong = {'t': 'error', 'code': 'version', 'msg': 'protocol v1 vs v2'}
        self.assertEqual(self.upgrade(), 75)
        self.assertEqual(self.current(), self.old)

    def test_no_socket_and_another_state_never_touches_the_default_agent(self):
        # `--state /tmp/x` with nothing listening: no launchd.json there, so
        # the machine's own agent under the default label is left alone.
        self.pong = FileNotFoundError('no socket')
        (self.state / 'launchd.json').unlink()
        self.assertEqual(self.upgrade(), 0)
        self.assertFalse(self.kicked())
        self.assertNotIn('print', self.fake.verbs())
        self.assertIn('no daemon answers', '\n'.join(self.said))

    def test_no_socket_while_a_daemon_holds_the_lock_changes_nothing(self):
        from pandora.tests.test_launchd import HOLD
        import sys
        self.pong = FileNotFoundError('no socket')
        proc = subprocess.Popen([sys.executable, '-c', HOLD, str(self.state / 'daemon.lock')],
                                stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait()))
        self.assertEqual(proc.stdout.readline().strip(), 'held')
        self.assertEqual(self.upgrade(), 75)
        self.assertFalse(self.kicked())
        self.assertIn('holds', '\n'.join(self.said))
        self.assertEqual(self.current(), self.old)

    def test_no_socket_and_a_loaded_agent_that_is_down_is_started(self):
        self.pong = FileNotFoundError('no socket')
        self.fake.loaded[self.LABEL] = None
        self.assertEqual(self.upgrade(), 0, self.said)
        self.assertTrue(any(call[0] == 'kickstart' for call in self.fake.calls))

    def test_a_new_daemon_that_never_answers_is_an_error_with_the_way_back(self):
        self.rows = []
        self.ping_after = None

        def silent_after_restart():
            if self.fake.loaded.get(self.LABEL) != 4242:
                raise ConnectionRefusedError('nobody')
            return self.pong
        self.ping = silent_after_restart
        code = self.upgrade()
        self.assertEqual(code, 1)
        self.assertIn('no daemon has answered 10 s after the restart', self.said[-1]
                      if 'pruned' not in self.said[-1] else self.said[-2])
        self.assertIn('`pandora upgrade --version %s`' % Path(self.old).name,
                      '\n'.join(self.said))

    def test_a_version_that_cannot_import_is_refused_before_anything_moves(self):
        (self.repo / 'pandora' / 'client' / 'daemon.py').write_text('import nonexistent_mod\n')
        git(self.repo, 'commit', '-q', '-am', 'broken')
        with self.assertRaises(install.Refused) as caught:
            self.upgrade()
        self.assertIn("No module named 'nonexistent_mod'", str(caught.exception))
        self.assertIn('Nothing changed', str(caught.exception))
        self.assertEqual(self.current(), self.old)
        self.assertFalse(self.kicked())

    def test_version_goes_back_to_an_installed_one(self):
        self.upgrade(now=True)
        self.fake.loaded[self.LABEL] = 4242
        self.pong = {'t': 'pong', 'pid': 4242, 'home': self.current()}
        self.said.clear()
        self.assertEqual(self.upgrade(version=Path(self.old).name, source=None, now=True), 0,
                         self.said)
        self.assertEqual(self.current(), self.old)
        with self.assertRaises(install.Refused):
            self.upgrade(version='nope', source=None)

    def test_a_daemon_already_on_the_new_version_is_left_alone(self):
        self.upgrade(now=True)
        self.fake.calls.clear()
        self.said.clear()
        self.fake.loaded[self.LABEL] = 4242
        self.pong = {'t': 'pong', 'pid': 4242, 'home': self.current()}
        self.assertEqual(self.upgrade(), 0)
        self.assertIn('the daemon already runs %s' % Path(self.current()).name, self.said)
        self.assertFalse(self.kicked())

    def test_no_daemon_and_no_agent(self):
        self.pong = FileNotFoundError('no socket')
        self.fake.loaded.clear()
        self.assertEqual(self.upgrade(), 0)
        self.assertIn('no daemon answers', '\n'.join(self.said))

    def test_old_versions_are_pruned_after_the_restart(self):
        for index in range(4):
            self.commit(self.repo, 'VERSION = %d\n' % (index + 10))
            self.assertEqual(self.upgrade(now=True), 0, self.said)
            self.fake.loaded[self.LABEL] = 4242
            self.pong = {'t': 'pong', 'pid': 4242, 'home': self.current()}
        names = sorted(p.name for p in (self.data / 'versions').iterdir())
        self.assertEqual(len(names), 3, names)
        self.assertIn(install.installed(self.data)['name'], names)

    def test_a_scratch_data_root_never_re_points_the_launchers(self):
        bindir = self.root / 'pathbin'
        bindir.mkdir()
        (bindir / 'pandora').symlink_to(self.repo / 'bin' / 'pandora')
        import sys
        code = install.upgrade(state=self.state, source=str(self.repo), data=self.data,
                               env={'PATH': str(bindir), 'PANDORA_PYTHON': sys.executable},
                               home=self.home, platform='darwin', launchctl=self.fake,
                               ping=self.ping, ask=self.ask, clock=self.clock,
                               sleep=self.clock.sleep, say=self.said.append, now=True)
        self.assertEqual(code, 0, self.said)
        self.assertEqual(os.readlink(bindir / 'pandora'), str(self.repo / 'bin' / 'pandora'))
        self.assertIn('ln -sf %s %s' % (self.data / 'current' / 'bin' / 'pandora',
                                        bindir / 'pandora'), '\n'.join(self.said))
        # The default data root, or --relink, does move it.
        self.said.clear()
        code = install.upgrade(state=self.state, source=str(self.repo), data=self.data,
                               env={'PATH': str(bindir), 'PANDORA_PYTHON': sys.executable},
                               home=self.home, platform='darwin', launchctl=self.fake,
                               ping=self.ping, ask=self.ask, clock=self.clock,
                               sleep=self.clock.sleep, say=self.said.append, now=True,
                               relink=True, no_restart=True)
        self.assertEqual(os.readlink(bindir / 'pandora'),
                         str(self.data / 'current' / 'bin' / 'pandora'))

    def test_the_cli_refuses_a_dirty_checkout_and_parses_the_flags(self):
        from pandora import cli
        (self.repo / 'pandora' / 'cli.py').write_text('dirty\n')
        # HOME and XDG_DATA_HOME are the test package's scratch; PATH holds no
        # launcher; and nothing past the refusal may run.
        with mock.patch.object(install, 'upgrade', wraps=install.upgrade) as called, \
                mock.patch.object(install, 'snapshot', side_effect=AssertionError('went on')), \
                mock.patch.dict(os.environ, PATH='/usr/bin:/bin'), \
                mock.patch('sys.stderr') as err:
            code = cli.main(['--state', str(self.state), '--config', str(self.root / 'none.toml'),
                             'upgrade', '--from', str(self.repo), '--wait', '5', '--keep', '2'])
        self.assertEqual(code, 1)
        self.assertEqual(called.call_args.kwargs['wait'], 5)
        self.assertEqual(called.call_args.kwargs['keep'], 2)
        written = ''.join(call.args[0] for call in err.write.call_args_list)
        self.assertIn('uncommitted changes', written)

    def test_the_cli_keeps_at_least_two(self):
        from pandora import cli
        with mock.patch('sys.stderr'):
            self.assertEqual(cli.main(['--state', str(self.state), '--config',
                                       str(self.root / 'none.toml'), 'upgrade', '--keep', '1']),
                             64)

    def test_bare_upgrade_means_the_latest_release(self):
        from pandora import cli
        with mock.patch.object(install, 'upgrade', return_value=0) as called:
            code = cli.main(['--state', str(self.state), '--config',
                             str(self.root / 'none.toml'), 'upgrade'])
        self.assertEqual(code, 0)
        self.assertEqual(called.call_args.kwargs['release'], 'latest')

    def test_the_cli_refuses_a_release_mixed_with_a_checkout_source(self):
        from pandora import cli
        for argv in (['upgrade', '--release', 'v1.0.0', '--from', str(self.repo)],
                     ['upgrade', '--release', '--dirty'],
                     ['upgrade', '--release', 'v1.0.0', '--version', 'abc123']):
            with mock.patch('sys.stderr') as err, \
                    mock.patch.object(install, 'upgrade') as never:
                code = cli.main(['--state', str(self.state), '--config',
                                 str(self.root / 'none.toml'), *argv])
            self.assertEqual(code, 64, argv)
            written = ''.join(call.args[0] for call in err.write.call_args_list)
            self.assertIn('--release installs a published tarball', written)
            never.assert_not_called()

    def test_a_release_is_fetched_and_installed_as_its_tag(self):
        blob = Releases.tarball_of(self, 'pandora-9.9.9')
        api = install.RELEASE_API + '/releases/tags/v9.9.9'
        body = json.dumps({'tag_name': 'v9.9.9',
                           'tarball_url': 'https://codeload/auto.tgz',
                           'assets': [{'name': 'pandora-9.9.9.tar.gz',
                                       'browser_download_url': 'https://x/asset.tgz'}]
                           }).encode()
        fetch = Releases.fetcher({api: body, 'https://x/asset.tgz': blob})
        self.rows = [[]]
        code = self.upgrade(release='v9.9.9', fetch=fetch)
        text = '\n'.join(self.said)
        self.assertEqual(code, 0, text)
        self.assertEqual(install.installed(self.data)['name'], 'v9.9.9')
        self.assertIn('fetched https://x/asset.tgz', text)
        meta = install.read_meta(self.data / 'versions' / 'v9.9.9')
        self.assertEqual(meta['release'], 'v9.9.9')
        self.assertTrue((self.data / 'versions' / 'v9.9.9' / 'bin' / 'pandora').is_file())


class Releases(Case):
    """`release_tree`: a release's tarball resolved over a fake HTTP fetch."""

    def tarball_of(self, root):
        tree = self.root / ('tree-%s' % root)
        (tree / 'bin').mkdir(parents=True)
        (tree / 'pandora' / 'client').mkdir(parents=True)
        (tree / 'bin' / 'pandora').write_text('#!/bin/sh\n')
        (tree / 'bin' / 'pandora').chmod(0o755)
        (tree / 'pandora' / 'cli.py').write_text('VERSION = 9\n')
        (tree / 'pandora' / '__init__.py').write_text('')
        (tree / 'pandora' / 'client' / '__init__.py').write_text('')
        (tree / 'pandora' / 'client' / 'daemon.py').write_text('')
        (tree / 'pandora' / 'client' / 'shim.py').write_text('')
        blob = io.BytesIO()
        with tarfile.open(fileobj=blob, mode='w:gz') as archive:
            archive.add(tree, arcname=root)
        return blob.getvalue()

    @staticmethod
    def fetcher(mapping):
        def fetch(url):
            if url not in mapping:
                raise AssertionError('unexpected fetch of %s' % url)
            return mapping[url]
        return fetch

    def test_latest_resolves_and_the_version_is_named_by_the_tag(self):
        blob = self.tarball_of('pandora-9.9.9')
        api = install.RELEASE_API + '/releases/latest'
        body = json.dumps({'tag_name': 'v9.9.9', 'assets': [],
                           'tarball_url': 'https://codeload/auto.tgz'}).encode()
        fetch = self.fetcher({api: body, 'https://codeload/auto.tgz': blob})
        with tempfile.TemporaryDirectory() as tmp:
            info = install.release_tree('latest', tmp, fetch=fetch)
            self.assertEqual((info['name'], info['release'], info['commit'], info['dirty']),
                             ('v9.9.9', 'v9.9.9', '', False))
            version = install.snapshot(info, self.data)
        self.assertEqual(version['name'], 'v9.9.9')
        path = self.data / 'versions' / 'v9.9.9'
        self.assertEqual(version['path'], str(path))
        self.assertEqual((path / 'pandora' / 'cli.py').read_text(), 'VERSION = 9\n')
        meta = install.read_meta(path)
        self.assertEqual(meta['release'], 'v9.9.9')
        self.assertIsNone(meta['source'])
        # The same release fetched again is reused, not rebuilt.
        with tempfile.TemporaryDirectory() as tmp:
            again = install.snapshot(install.release_tree('latest', tmp, fetch=fetch),
                                     self.data)
        self.assertTrue(again['reused'])

    def test_the_release_asset_is_preferred_over_the_source_archive(self):
        blob = self.tarball_of('pandora-9.9.9')
        api = install.RELEASE_API + '/releases/tags/v9.9.9'
        body = json.dumps({'tag_name': 'v9.9.9',
                           'tarball_url': 'https://codeload/auto.tgz',
                           'assets': [{'name': 'pandora-9.9.9.tar.gz',
                                       'browser_download_url': 'https://x/asset.tgz'}]
                           }).encode()
        fetch = self.fetcher({api: body, 'https://x/asset.tgz': blob})
        with tempfile.TemporaryDirectory() as tmp:
            info = install.release_tree('v9.9.9', tmp, fetch=fetch)
        self.assertEqual(info['url'], 'https://x/asset.tgz')

    def test_a_release_json_without_a_tag_is_refused(self):
        fetch = self.fetcher({install.RELEASE_API + '/releases/latest': b'{}'})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(install.Refused) as caught:
                install.release_tree('latest', tmp, fetch=fetch)
        self.assertIn('no release tag', str(caught.exception))

    def test_a_tarball_with_no_pandora_tree_is_refused(self):
        tree = self.root / 'tree-empty'
        tree.mkdir()
        blob = io.BytesIO()
        with tarfile.open(fileobj=blob, mode='w:gz') as archive:
            archive.add(tree, arcname='pandora-0.0.0')
        api = install.RELEASE_API + '/releases/latest'
        body = json.dumps({'tag_name': 'v0.0.0',
                           'tarball_url': 'https://x/t.tgz'}).encode()
        fetch = self.fetcher({api: body, 'https://x/t.tgz': blob.getvalue()})
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(install.Refused) as caught:
                install.release_tree('latest', tmp, fetch=fetch)
        self.assertIn('no Pandora tree', str(caught.exception))
        self.assertFalse(self.data.exists(), 'a refusal writes nothing')


if __name__ == '__main__':
    unittest.main()
