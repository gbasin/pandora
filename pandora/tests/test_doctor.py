"""`pandora doctor`, and the launcher it caught.

Each check is fed a PATH built in a temporary directory, so "the shim wins" and
"a version manager sits behind it" are real files on a real PATH rather than
mocks. The daemon checks run against the real daemon from `test_fallback`, and
once against no daemon at all, which must not create the state directory it
looked for.
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from pandora.client import doctor, enrollment
from pandora.tests.test_fallback import DaemonCase

HERE = Path(__file__).resolve().parents[2]


def setUpModule():
    # `doctor.run` asks launchd about supervision; no test here may reach the
    # real `launchctl`, so it points at a path that does not exist, which reads
    # as "not loaded". `test_launchd` covers the answers launchd can give.
    from unittest import mock
    from pandora.client import launchd
    patch = mock.patch.object(launchd, 'LAUNCHCTL', '/nonexistent/launchctl')
    patch.start()
    unittest.addModuleCleanup(patch.stop)


def write_exe(path, text='#!/bin/sh\nexit 0\n'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


class Scratch(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.root = Path(os.path.realpath(home.name))

    def shim_dir(self, name='shim'):
        directory = self.root / name
        directory.mkdir()
        (directory / 'pnpm').symlink_to(HERE / 'bin' / 'pnpm')
        (directory / '.pandora-shim').write_text('')
        return directory


class PnpmOnPath(Scratch):
    def test_the_shim_first_and_a_real_pnpm_behind_it(self):
        shim, real = self.shim_dir(), write_exe(self.root / 'real' / 'pnpm').parent
        item = doctor.check_pnpm({'PATH': '%s:%s' % (shim, real)})
        self.assertEqual(item['status'], 'ok', item)
        self.assertEqual(item['facts']['real'], str(real / 'pnpm'))
        self.assertEqual(item['facts']['shim_resolved'], str(HERE / 'bin' / 'pnpm'))

    def test_a_pnpm_ahead_of_the_shim_fails(self):
        shim, real = self.shim_dir(), write_exe(self.root / 'real' / 'pnpm').parent
        item = doctor.check_pnpm({'PATH': '%s:%s' % (real, shim)})
        self.assertEqual(item['status'], 'fail')
        self.assertIn('later on PATH', item['detail'])

    def test_no_real_pnpm_fails(self):
        item = doctor.check_pnpm({'PATH': str(self.shim_dir())})
        self.assertEqual(item['status'], 'fail')
        self.assertIn('127', item['detail'])

    def test_a_second_shim_under_another_spelling_fails(self):
        first, second = self.shim_dir('one'), self.shim_dir('two')
        item = doctor.check_pnpm({'PATH': '%s:%s' % (first, second)})
        self.assertEqual(item['status'], 'fail')
        self.assertIn('another Pandora shim', item['detail'])

    def test_shim_dir_names_the_other_spelling(self):
        first, second = self.shim_dir('one'), self.shim_dir('two')
        real = write_exe(self.root / 'real' / 'pnpm').parent
        item = doctor.check_pnpm({'PATH': '%s:%s:%s' % (first, second, real),
                                  'PANDORA_SHIM_DIR': str(second)})
        self.assertEqual(item['status'], 'ok', item)

    def test_a_version_manager_behind_the_shim_warns(self):
        shim = self.shim_dir()
        for fragment, label in (('.volta/bin', 'Volta'), ('mise/shims', 'mise'),
                                ('corepack/bin', 'corepack')):
            with self.subTest(label=label):
                real = write_exe(self.root / fragment / 'pnpm').parent
                item = doctor.check_pnpm({'PATH': '%s:%s' % (shim, real)})
                self.assertEqual((item['status'], item['facts']['manager']), ('warn', label))


class EnvironmentChecks(unittest.TestCase):
    def test_a_leaked_recursion_guard_fails(self):
        item = doctor.check_recursion({'PANDORA_ROUTE_DEPTH': '1', 'PANDORA_REAL_PNPM': '/x'})
        self.assertEqual(item['status'], 'fail')
        self.assertIn('inside a Pandora run', item['detail'])
        self.assertEqual(doctor.check_recursion({})['status'], 'ok')

    def test_switches_are_information_and_a_bad_placement_fails(self):
        self.assertEqual(doctor.check_variables({})['status'], 'ok')
        item = doctor.check_variables({'PANDORA_OFF': '1', 'PANDORA_WHERE': 'local'})
        self.assertEqual(item['status'], 'info')
        self.assertIn('PANDORA_OFF=1', item['detail'])
        self.assertIn('PANDORA_WHERE=local', item['detail'])
        self.assertEqual(doctor.check_variables({'PANDORA_WHERE': 'mars'})['status'], 'fail')


class ShimMarkers(Scratch):
    def test_a_marker_away_from_the_shim_is_stale(self):
        shim = self.shim_dir()
        other = self.root / 'tools'
        other.mkdir()
        (other / '.pandora-shim').write_text('')
        item = doctor.check_shim_markers({'PATH': '%s:%s' % (shim, other)}, str(shim / 'pnpm'))
        self.assertEqual(item['status'], 'warn')
        self.assertIn(str(other), item['detail'])

    def test_a_shim_without_its_marker_warns(self):
        shim = self.shim_dir()
        (shim / '.pandora-shim').unlink()
        item = doctor.check_shim_markers({'PATH': str(shim)}, str(shim / 'pnpm'))
        self.assertEqual(item['status'], 'warn')
        self.assertEqual(doctor.check_shim_markers({'PATH': str(self.shim_dir('ok'))},
                                                   str(self.root / 'ok' / 'pnpm'))['status'],
                         'ok')


class Launcher(Scratch):
    """`bin/pandora` reached through symlinks, from a cwd that is not a checkout."""

    def link_chain(self):
        # absolute link -> relative link -> the real script
        (self.root / 'lib').mkdir()
        (self.root / 'lib' / 'pandora').symlink_to(HERE / 'bin' / 'pandora')
        (self.root / 'bin').mkdir()
        (self.root / 'bin' / 'pandora').symlink_to(Path('..') / 'lib' / 'pandora')
        return self.root / 'bin'

    def env(self, path):
        env = {key: value for key, value in os.environ.items()
               if key not in ('PYTHONPATH', 'PANDORA_HOME')}
        env['PATH'] = '%s:/usr/bin:/bin' % path
        # /usr/bin/python3 on macOS is 3.9, which has no tomllib; the launcher
        # honors PANDORA_PYTHON, so the test names the interpreter it runs under.
        env['PANDORA_PYTHON'] = sys.executable
        return env

    def test_the_launcher_follows_its_links_from_any_directory(self):
        bindir = self.link_chain()
        proc = subprocess.run([str(bindir / 'pandora'), 'doctor', '--package-home'],
                              cwd=str(self.root), env=self.env(bindir),
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), str(HERE))

    def test_doctor_reports_the_launcher_and_its_package(self):
        bindir = self.link_chain()
        item, home = doctor.check_launcher(self.env(bindir))
        self.assertEqual(item['status'], 'ok', item)
        self.assertEqual(os.path.realpath(home), str(HERE))

    def test_a_launcher_that_cannot_import_fails(self):
        write_exe(self.root / 'broken' / 'pandora',
                  "#!/bin/sh\necho \"No module named 'pandora'\" >&2\nexit 1\n")
        item, home = doctor.check_launcher(self.env(self.root / 'broken'))
        self.assertEqual(item['status'], 'fail')
        self.assertIn("No module named 'pandora'", item['detail'])
        self.assertIsNone(home)

    def test_the_pnpm_shim_finds_its_package_through_a_link_when_the_marker_has_no_home(self):
        # The heavy path hands off to Python; with no `home` in the marker, the
        # package must be found beside the file the link resolves to.
        shim = self.shim_dir()
        real = write_exe(self.root / 'real' / 'pnpm', '#!/bin/sh\necho "real $*"\n').parent
        repo, state = self.root / 'repo', self.root / 'state'
        state.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        (repo / '.git' / 'pandora-enrolled').write_text(enrollment.render(
            socket_path=str(state / 'client.sock'), repo='demo', claims=[['journey']],
            heavy=enrollment.heavy_forms([['journey']])))
        env = self.env('%s:%s' % (shim, real))
        for name in ('PANDORA_OFF', 'PANDORA_ROUTE_DEPTH', 'PANDORA_WHERE'):
            env.pop(name, None)
        proc = subprocess.run([str(shim / 'pnpm'), 'build'], cwd=str(repo), env=env,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual((proc.returncode, proc.stdout), (0, 'real build\n'), proc.stderr)
        self.assertTrue((state / 'passthrough.jsonl').is_file(), proc.stderr)


def legacy(text, home):
    """A file as written before the shim stopped reading `home`: the line after `repo`."""
    if not home:
        return text
    lines = text.splitlines(keepends=True)
    at = next(index for index, line in enumerate(lines) if line.startswith('repo ')) + 1
    return ''.join(lines[:at] + ['home %s\n' % home] + lines[at:])


class RepositoryAndCwd(Scratch):
    def setUp(self):
        super().setUp()
        self.repo = self.root / 'repo'
        (self.repo / '.git').mkdir(parents=True)
        (self.repo / 'apps').mkdir()
        self.config = {'repos': [{'name': 'demo', 'root': str(self.repo), 'config': ''}],
                       'source': '/cfg.toml'}

    def enroll(self, home=None, sock='/s/client.sock', claims=(('journey',),)):
        (self.repo / '.git' / 'pandora-enrolled').write_text(legacy(enrollment.render(
            socket_path=sock, repo='demo', claims=[list(claim) for claim in claims]), home))

    def statuses(self, items):
        return {item['name']: item['status'] for item in items}

    def test_not_enrolled_with_a_config_says_how_to_enroll(self):
        (self.repo / 'pandora.toml').write_text('')
        [item] = doctor.check_repository(str(self.repo), self.config, Path('/s/client.sock'))
        self.assertEqual(item['status'], 'fail')
        self.assertIn('pandora enroll %s' % self.repo, item['detail'])

    def configure(self, *forms):
        (self.repo / 'pandora.toml').write_text(
            'version = 1\n[repo]\nname = "demo"\nentrypoints = ["pnpm"]\n'
            '[worker]\nbase_image = "images:ubuntu/26.04"\n' + ''.join(
                '[[jobs]]\nid = "j%d"\nargs = "none"\nforms = [{ prefix = ["%s"] }]\n'
                'run = { argv = ["true"] }\n' % (number, form)
                for number, form in enumerate(forms)))

    def register(self, home=None, sock='/s/client.sock'):
        (self.repo / '.git' / 'pandora-repo').write_text(legacy(enrollment.registration_text(
            socket_path=sock, repo='demo'), home))

    def cache(self, worktree=None, home=None, sock='/s/client.sock'):
        """The cache the daemon would write for this worktree, dated as it would date it."""
        from pandora.config import loader
        worktree = worktree or self.repo
        path = worktree / 'pandora.toml'
        text = legacy(enrollment.cache_text(loader.load(path), socket_path=sock, repo='demo',
                                            derived='own', digest_path=path), home)
        stamp = time.time_ns() - 60 * 10**9
        os.utime(path, ns=(stamp, stamp))
        enrollment.write_cache(enrollment.cache_path(worktree), text, [path])
        return enrollment.cache_path(worktree)

    def row(self, name, cwd=None, config=None):
        return next(item for item in doctor.check_repository(
            str(cwd or self.repo), config or self.config, Path('/s/client.sock'))
            if item['name'] == name)

    def test_registered_with_a_fresh_cache_on_both_sides(self):
        self.configure('journey')
        self.register()
        self.cache()
        items = doctor.check_repository(str(self.repo), self.config, Path('/s/client.sock'))
        self.assertEqual(self.statuses(items), {'repository': 'ok', 'claim cache': 'ok',
                                                'claim caches': 'info',
                                                'daemon enrollment': 'ok'})
        self.assertIn('1 claimed form(s)', self.row('claim cache')['detail'])

    def test_a_stale_cache_is_a_warning_that_the_next_claimed_command_refreshes_it(self):
        self.configure('journey')
        self.register()
        self.cache()
        self.configure('journey', 'check')          # edited now, after the cache
        item = self.row('claim cache')
        self.assertEqual(item['status'], 'warn')
        self.assertIn('cache stale for this worktree', item['detail'])
        self.assertIn('the next command here refreshes it', item['detail'])
        items = doctor.check_repository(str(self.repo), self.config, Path('/s/client.sock'))
        self.assertNotIn('fail', self.statuses(items).values())

    def test_a_config_replaced_behind_an_older_mtime_is_stale_by_its_digest(self):
        self.configure('journey')
        self.register()
        cache = self.cache()
        self.configure('journey', 'check')
        stamp = cache.stat().st_mtime_ns - 10**9
        os.utime(self.repo / 'pandora.toml', ns=(stamp, stamp))
        item = self.row('claim cache')
        self.assertEqual(item['status'], 'warn')
        self.assertIn('other content', item['detail'])
        self.assertIn('the next claimed command refreshes it', item['detail'])

    def test_a_worktree_without_a_cache_warns_that_its_next_command_writes_one(self):
        self.configure('journey')
        self.register()
        item = self.row('claim cache')
        self.assertEqual(item['status'], 'warn')
        self.assertIn('the next command here asks the daemon', item['detail'])

    def test_the_v02_marker_alone_warns_to_enroll_once(self):
        self.configure('journey')
        self.enroll()
        self.assertEqual(self.row('repository')['status'], 'warn')
        self.assertIn('pandora enroll %s' % self.repo, self.row('repository')['detail'])
        self.assertEqual(self.row('claim cache')['status'], 'info')

    def branch(self, *forms):
        """A sibling worktree of the enrolled repository with its own pandora.toml."""
        branch = self.root / 'branch'
        branch.mkdir()
        (self.repo / '.git' / 'worktrees' / 'branch').mkdir(parents=True)
        (self.repo / '.git' / 'worktrees' / 'branch' / 'gitdir').write_text(
            str(branch / '.git') + '\n')
        (branch / '.git').write_text('gitdir: %s\n' % (self.repo / '.git' / 'worktrees'
                                                         / 'branch'))
        saved = (self.repo / 'pandora.toml').read_text()
        self.configure(*forms)
        (branch / 'pandora.toml').write_text((self.repo / 'pandora.toml').read_text())
        (self.repo / 'pandora.toml').write_text(saved)
        return branch

    def test_a_branch_with_its_own_pandora_toml_is_fresh_by_its_own_file(self):
        self.configure('journey')
        self.register()
        branch = self.branch('journey', 'check')
        self.cache()
        self.cache(branch)
        item = self.row('claim cache', cwd=branch)
        self.assertEqual(item['status'], 'ok', item)
        self.assertCountEqual(item['facts']['claims'], ['journey', 'check'])
        self.assertEqual(self.row('claim cache')['facts']['claims'], ['journey'])
        self.assertEqual(
            (self.repo / '.git' / 'worktrees' / 'branch' / 'pandora-claims').is_file(), True)

    def test_every_worktree_is_counted_by_freshness(self):
        self.configure('journey')
        self.register()
        self.branch('journey', 'check')             # no cache yet
        self.cache()
        item = self.row('claim caches')
        self.assertEqual(item['status'], 'info')
        self.assertEqual((item['facts']['fresh'], item['facts']['stale'],
                          item['facts']['missing']), (1, 0, 1))

    def test_a_cache_naming_a_client_home_says_it_is_ignored(self):
        other = self.root / 'other'
        self.configure('journey')
        self.register()
        self.cache(home=str(other))
        item = self.row('client home')
        self.assertEqual(item['status'], 'info')
        self.assertIn('no longer read', item['detail'])
        self.assertIn('next claimed command', item['detail'])

    def test_no_client_home_row_without_the_legacy_line(self):
        self.configure('journey')
        self.register()
        self.cache()
        items = doctor.check_repository(str(self.repo), self.config, Path('/s/client.sock'))
        self.assertNotIn('client home', self.statuses(items))
        self.assertNotIn('home ', enrollment.cache_path(self.repo).read_text())

    def test_an_enrollment_the_daemon_does_not_know_fails(self):
        self.register()
        items = doctor.check_repository(str(self.repo), {'repos': [], 'source': '/cfg.toml'},
                                        Path('/s/client.sock'))
        self.assertEqual(self.statuses(items)['daemon enrollment'], 'fail')

    def test_a_registration_naming_a_client_home_says_enroll_rewrites_it(self):
        self.configure('journey')
        self.register(home=str(self.root / 'gone'))
        item = self.row('client home')
        self.assertEqual(item['status'], 'info')
        self.assertIn('pandora enroll %s' % self.repo, item['detail'])
        self.assertNotIn('delete', item['detail'])

    def test_a_cache_routing_elsewhere_warns(self):
        self.configure('journey')
        self.register()
        self.cache(sock='/elsewhere/client.sock')
        items = doctor.check_repository(str(self.repo), self.config, Path('/s/client.sock'))
        self.assertEqual(self.statuses(items)['client socket'], 'warn')

    def test_a_subdirectory_warns_and_cites_64(self):
        self.assertEqual(doctor.check_cwd(str(self.repo))['status'], 'ok')
        item = doctor.check_cwd(str(self.repo / 'apps'))
        self.assertEqual(item['status'], 'warn')
        self.assertIn('exit 64', item['detail'])

    def test_a_root_only_repository_says_a_subdirectory_is_unclaimed(self):
        (self.repo / '.git' / 'pandora-enrolled').write_text(enrollment.render(
            socket_path='/s/client.sock', repo='demo', claims=[['journey']],
            subdirectory='passthrough'))
        item = doctor.check_cwd(str(self.repo / 'apps'))
        self.assertEqual(item['status'], 'warn')
        self.assertIn('only at the root', item['detail'])
        self.assertNotIn('exit 64', item['detail'])


class Cli(Scratch):
    def test_help_names_it_and_json_is_the_report(self):
        import contextlib
        import io
        import json
        from pandora import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(['--help'])
        self.assertIn('pandora doctor [--json]', out.getvalue())
        out = io.StringIO()
        here = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, here)
        with contextlib.redirect_stdout(out):
            code = cli.main(['--state', str(self.root / 'state'),
                             '--config', str(self.root / 'none.toml'), 'doctor', '--json'])
        report = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(report['ok'])
        self.assertEqual({'name', 'status', 'detail', 'facts'}, set(report['checks'][0]))


class NoDaemon(Scratch):
    def test_every_check_still_runs_and_nothing_is_created(self):
        state = self.root / 'state'
        report = doctor.run(state=str(state), config=str(self.root / 'none.toml'),
                            env={'PATH': '/usr/bin:/bin'}, cwd=str(self.root))
        names = {item['name']: item for item in report['checks']}
        self.assertFalse(report['ok'])
        self.assertEqual(names['daemon']['status'], 'fail')
        self.assertIn('pandora daemon', names['daemon']['detail'])
        # Since #91 a claimed command with no daemon passes through where the
        # daemon was never installed, and exits 70 where it was; nothing falls back.
        self.assertIn('runs here unmanaged', names['daemon']['detail'])
        self.assertIn('exits 70', names['daemon']['detail'])
        self.assertNotIn('fall back', names['daemon']['detail'])
        self.assertEqual(names['worker']['status'], 'fail')
        self.assertIn('recursion guard', names)
        self.assertIn('variables', names)
        self.assertFalse(state.exists(), 'doctor created the state directory')


class AgainstARealDaemon(DaemonCase):
    def test_the_daemon_answers_with_its_package_and_the_worker_reading(self):
        self.daemon.health.poll()
        (self.repo / '.git').mkdir()
        (self.repo / '.git' / 'pandora-repo').write_text(enrollment.registration_text(
            socket_path=str(self.daemon.socket_path), repo='demo'))
        report = doctor.run(state=str(self.state), config=str(self.root / 'config.toml'),
                            env={'PATH': '/usr/bin:/bin'}, cwd=str(self.repo))
        names = {item['name']: item for item in report['checks']}
        self.assertEqual(names['daemon']['status'], 'ok', names['daemon'])
        self.assertIn('same package', names['daemon']['detail'])
        self.assertEqual(names['worker']['status'], 'ok', names['worker'])
        self.assertIn('reachable', names['worker']['detail'])
        self.assertEqual(names['repository']['status'], 'ok')
        self.assertEqual(names['daemon enrollment']['status'], 'ok')
        self.assertEqual(names['working directory']['status'], 'ok')
        # No pnpm on this PATH, so the report as a whole fails, and says so.
        self.assertEqual(names['pnpm on PATH']['status'], 'fail')
        self.assertFalse(report['ok'])
        self.assertIn('check(s) failed', doctor.render(report))

    def test_the_daemon_says_which_code_it_imported(self):
        from unittest import mock
        from pandora.engine import bundle
        import json as json_module
        written = json_module.loads((self.state / 'daemon.json').read_text())
        self.assertIn('client/daemon.py', written['code_modules'])
        self.assertEqual(written['code'], bundle.code_digest(names=written['code_modules']))
        item, pong = doctor.check_daemon(self.daemon.socket_path, None)
        self.assertEqual((pong['code'], item['status']), (written['code'], 'ok'))
        # The checkout moved on after the daemon started.
        with mock.patch.object(doctor.bundle, 'code_digest', return_value='0' * 64):
            item, _pong = doctor.check_daemon(self.daemon.socket_path, None)
        self.assertEqual(item['status'], 'warn')
        self.assertIn('daemon code differs from the checkout; restart it: `pandora daemon '
                      '--restart`', item['detail'])

    def test_only_modules_the_daemon_loads_count(self):
        # A change to the worker half or the canary is no reason to restart the
        # daemon, and a restart ends local runs. A fresh interpreter, because
        # this test process has imported everything.
        import json as json_module
        import sys as system
        out = subprocess.run(
            [system.executable, '-c', 'import json, pandora.client.daemon; '
             'from pandora.engine import bundle; print(json.dumps(bundle.loaded_modules()))'],
            cwd=str(HERE), capture_output=True, text=True, check=True).stdout
        names = json_module.loads(out)
        self.assertIn('client/daemon.py', names)
        self.assertNotIn('worker/canary.py', names)
        self.assertNotIn('client/doctor.py', names)

    def test_a_worker_the_daemon_knows_is_down_fails(self):
        from pandora.errors import WorkerUnreachable
        from pandora.tests.test_fallback import FakeWorker
        FakeWorker.health_raises = WorkerUnreachable('no route')
        self.daemon.health.poll()
        _item, pong = doctor.check_daemon(self.daemon.socket_path, None)
        item = doctor.check_worker(pong, self.state)
        self.assertEqual(item['status'], 'fail')
        self.assertIn('DOWN', item['detail'])


CONFIG = '''
version = 1
[repo]
name = "demo"
entrypoints = ["pnpm"]
[worker]
base_image = "demo"
[[jobs]]
id = "check"
forms = [{ prefix = ["check"] }]
run = { argv = ["true"] }
outputs = [{ kind = "artifacts", paths = ["reports/**"] }]
'''


class DeclaredOutputs(Scratch):
    """Outputs landed in the worktree must not become next run's source."""

    def make_repo(self):
        from pandora.tests.test_snapshot import make_repo
        repo = make_repo(self.root / 'repo', {'a.txt': 'a\n', 'pandora.toml': CONFIG})
        return repo

    def test_an_unignored_output_dir_is_warned(self):
        repo = self.make_repo()
        (repo / 'reports').mkdir()
        (repo / 'reports/junit.xml').write_text('<r/>')
        item = doctor.check_outputs(repo)[0]
        self.assertEqual(item['status'], 'warn', item)
        self.assertIn('source cache', item['detail'])

    def test_an_ignored_output_dir_is_fine(self):
        repo = self.make_repo()
        (repo / '.gitignore').write_text('reports/\n')
        (repo / 'reports').mkdir()
        (repo / 'reports/junit.xml').write_text('<r/>')
        item = doctor.check_outputs(repo)[0]
        self.assertEqual(item['status'], 'ok', item)

    def test_nothing_landed_yet_is_fine(self):
        repo = self.make_repo()
        item = doctor.check_outputs(repo)[0]
        self.assertEqual(item['status'], 'ok', item)

    def test_no_configuration_is_not_a_check(self):
        repo = self.make_repo()
        (repo / 'pandora.toml').unlink()
        self.assertEqual(doctor.check_outputs(repo), [])


if __name__ == '__main__':
    unittest.main()
