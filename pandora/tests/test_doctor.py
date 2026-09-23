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
import unittest
from pathlib import Path

from pandora.client import doctor, enrolment
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
        # honours PANDORA_PYTHON, so the test names the interpreter it runs under.
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
        (repo / '.git' / 'pandora-enrolled').write_text(enrolment.render(
            socket_path=str(state / 'client.sock'), repo='demo', claims=[['journey']],
            heavy=enrolment.heavy_forms([['journey']])))
        env = self.env('%s:%s' % (shim, real))
        for name in ('PANDORA_OFF', 'PANDORA_ROUTE_DEPTH', 'PANDORA_WHERE'):
            env.pop(name, None)
        proc = subprocess.run([str(shim / 'pnpm'), 'build'], cwd=str(repo), env=env,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual((proc.returncode, proc.stdout), (0, 'real build\n'), proc.stderr)
        self.assertTrue((state / 'passthrough.jsonl').is_file(), proc.stderr)


class RepositoryAndCwd(Scratch):
    def setUp(self):
        super().setUp()
        self.repo = self.root / 'repo'
        (self.repo / '.git').mkdir(parents=True)
        (self.repo / 'apps').mkdir()
        self.config = {'repos': [{'name': 'demo', 'root': str(self.repo), 'config': ''}],
                       'source': '/cfg.toml'}

    def enrol(self, home=str(HERE), sock='/s/client.sock'):
        (self.repo / '.git' / 'pandora-enrolled').write_text(enrolment.render(
            socket_path=sock, repo='demo', claims=[['journey']], home=home))

    def statuses(self, items):
        return {item['name']: item['status'] for item in items}

    def test_not_enrolled_with_a_config_says_how_to_enrol(self):
        (self.repo / 'pandora.toml').write_text('')
        [item] = doctor.check_repository(str(self.repo), self.config, Path('/s/client.sock'))
        self.assertEqual(item['status'], 'fail')
        self.assertIn('pandora enrol %s' % self.repo, item['detail'])

    def test_enrolled_on_both_sides(self):
        self.enrol()
        items = doctor.check_repository(str(self.repo), self.config, Path('/s/client.sock'))
        self.assertEqual(self.statuses(items), {'repository': 'ok', 'daemon enrolment': 'ok'})

    def test_a_marker_the_daemon_does_not_know_fails(self):
        self.enrol()
        items = doctor.check_repository(str(self.repo), {'repos': [], 'source': '/cfg.toml'},
                                        Path('/s/client.sock'))
        self.assertEqual(self.statuses(items)['daemon enrolment'], 'fail')

    def test_a_marker_naming_a_removed_checkout_fails(self):
        self.enrol(home=str(self.root / 'gone'))
        items = doctor.check_repository(str(self.repo), self.config, Path('/s/client.sock'))
        self.assertEqual(self.statuses(items)['marker home'], 'fail')

    def test_a_marker_routing_elsewhere_warns(self):
        self.enrol(sock='/elsewhere/client.sock')
        items = doctor.check_repository(str(self.repo), self.config, Path('/s/client.sock'))
        self.assertEqual(self.statuses(items)['marker socket'], 'warn')

    def test_a_subdirectory_warns_and_cites_64(self):
        self.assertEqual(doctor.check_cwd(str(self.repo))['status'], 'ok')
        item = doctor.check_cwd(str(self.repo / 'apps'))
        self.assertEqual(item['status'], 'warn')
        self.assertIn('exit 64', item['detail'])


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
        self.assertEqual(names['worker']['status'], 'fail')
        self.assertIn('recursion guard', names)
        self.assertIn('variables', names)
        self.assertFalse(state.exists(), 'doctor created the state directory')


class AgainstARealDaemon(DaemonCase):
    def test_the_daemon_answers_with_its_package_and_the_worker_reading(self):
        self.daemon.health.poll()
        (self.repo / '.git').mkdir()
        (self.repo / '.git' / 'pandora-enrolled').write_text(enrolment.render(
            socket_path=str(self.daemon.socket_path), repo='demo', claims=[['unit']],
            home=str(HERE)))
        report = doctor.run(state=str(self.state), config=str(self.root / 'config.toml'),
                            env={'PATH': '/usr/bin:/bin'}, cwd=str(self.repo))
        names = {item['name']: item for item in report['checks']}
        self.assertEqual(names['daemon']['status'], 'ok', names['daemon'])
        self.assertIn('same package', names['daemon']['detail'])
        self.assertEqual(names['worker']['status'], 'ok', names['worker'])
        self.assertIn('reachable', names['worker']['detail'])
        self.assertEqual(names['repository']['status'], 'ok')
        self.assertEqual(names['daemon enrolment']['status'], 'ok')
        self.assertEqual(names['working directory']['status'], 'ok')
        # No pnpm on this PATH, so the report as a whole fails, and says so.
        self.assertEqual(names['pnpm on PATH']['status'], 'fail')
        self.assertFalse(report['ok'])
        self.assertIn('check(s) failed', doctor.render(report))

    def test_a_worker_the_daemon_knows_is_down_fails(self):
        from pandora.errors import WorkerUnreachable
        from pandora.tests.test_fallback import FakeWorker
        FakeWorker.health_raises = WorkerUnreachable('no route')
        self.daemon.health.poll()
        _item, pong = doctor.check_daemon(self.daemon.socket_path, None)
        item = doctor.check_worker(pong, self.state)
        self.assertEqual(item['status'], 'fail')
        self.assertIn('DOWN', item['detail'])


if __name__ == '__main__':
    unittest.main()
