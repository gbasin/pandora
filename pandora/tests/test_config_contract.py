"""pandora.toml is the contract: caches refresh themselves, and a file this code cannot read is refused.

Four rulings (2026-09-24):

* A claim cache is a cache of the worktree's config. When a claimed command
  finds it derived from other content, it is rewritten from the current file and
  the caller is told once, on stderr, with no enroll.
* No file names a client home any more. The shim runs the client from its own
  checkout. A file that still has the line is read, the line is ignored, and the
  client says so.
* A daemon that does not understand a key or value in pandora.toml refuses the
  claimed command with the key, the value and the fix, with no version numbers.
* A claimed command with the daemon installed on this Mac but not answering
  exits 70 with the doctor hint. With no client configuration at all it still
  passes through (#91). PANDORA_OFF still bypasses everything.
"""
import contextlib
import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pandora import cli
from pandora.client import daemon as daemon_module
from pandora.client import enrollment, install, shim
from pandora.client.protocol import Reader, dump
from pandora.config import loader
from pandora.errors import ConfigError, UnknownSchema
from pandora.exits import INFRA
from pandora.tests.test_fallback import CONFIG, DaemonCase

HERE = Path(__file__).resolve().parents[2]

MINIMAL = '''version = 1
[repo]
name = "demo"
entrypoints = ["pnpm"]
[matching]
strip_prefixes = [["run"]]
subdirectory = "%(subdirectory)s"
%(extra)s
[worker]
base_image = "images:ubuntu/26.04"
[[jobs]]
id = "unit"
args = "optional"
forms = [{ prefix = ["unit"] }, { prefix = ["test", "unit"] }]
run = { argv = ["true", "{args}"] }
'''


def minimal(subdirectory='passthrough', extra=''):
    return MINIMAL % {'subdirectory': subdirectory, 'extra': extra}


class LoaderNamesWhatItDoesNotKnow(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.path = Path(home.name) / 'pandora.toml'

    def load(self, text):
        self.path.write_text(text)
        return loader.load(self.path)

    def test_an_unknown_key_carries_the_key_and_its_value(self):
        with self.assertRaises(UnknownSchema) as caught:
            self.load(minimal(extra='flavor = "mint"'))
        self.assertEqual((caught.exception.key, caught.exception.value),
                         ('matching.flavor', 'mint'))
        self.assertEqual(caught.exception.path, str(self.path))

    def test_an_unknown_value_carries_the_key_and_the_value(self):
        with self.assertRaises(UnknownSchema) as caught:
            self.load(minimal(subdirectory='sideways'))
        self.assertEqual((caught.exception.key, caught.exception.value),
                         ('matching.subdirectory', 'sideways'))

    def test_an_unknown_top_level_table_is_named_without_a_prefix(self):
        with self.assertRaises(UnknownSchema) as caught:
            self.load(minimal() + '[future]\nx = 1\n')
        self.assertEqual((caught.exception.key, caught.exception.value), ('future', {'x': 1}))

    def test_an_unknown_template_value_is_unknown_schema_too(self):
        with self.assertRaises(UnknownSchema) as caught:
            self.load(minimal().replace('["true", "{args}"]', '["true", "{future}", "{args}"]'))
        self.assertEqual(caught.exception.value, '{future}')

    def test_an_unknown_version_names_no_version_number_it_expects(self):
        with self.assertRaises(UnknownSchema) as caught:
            self.load(minimal().replace('version = 1', 'version = 2'))
        self.assertEqual((caught.exception.key, caught.exception.value), ('version', 2))
        self.assertNotIn('1', str(caught.exception).split(': ', 1)[-1])

    def test_a_malformed_file_is_a_plain_config_error(self):
        with self.assertRaises(ConfigError) as caught:
            self.load('version = \n')
        self.assertNotIsInstance(caught.exception, UnknownSchema)

    def test_claims_are_read_from_a_file_that_does_not_validate(self):
        self.path.write_text(minimal(extra='flavor = "mint"'))
        found = loader.claimed_forms(self.path)
        self.assertEqual(found, {'strip': [['run']], 'claims': [['unit'], ['test', 'unit']],
                                 'subdirectory': 'passthrough'})

    def test_claims_of_an_unreadable_file_are_empty(self):
        self.path.write_text('not [toml')
        self.assertEqual(loader.claimed_forms(self.path),
                         {'strip': [], 'claims': [], 'subdirectory': None})


class CacheOfAFileThisCodeCannotRead(unittest.TestCase):
    """The claims stay, so a claimed command reaches the daemon and is refused there."""

    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.root = Path(home.name)
        (self.root / '.git').mkdir()

    def test_an_unknown_key_keeps_the_claims_and_no_policy(self):
        (self.root / 'pandora.toml').write_text(minimal(extra='flavor = "mint"'))
        text, _sources = enrollment.derive(self.root, {'name': 'demo'}, socket_path='/s',
                                           client='/nonexistent/config.toml')
        parsed = enrollment.parse(text)
        self.assertEqual(parsed['claim'], [['unit'], ['test', 'unit']])
        self.assertEqual(parsed['policy'], [])
        self.assertEqual(parsed['subdirectory'], 'passthrough')
        self.assertIn('# refuses what it claims: ', text)

    def test_a_malformed_file_still_claims_nothing(self):
        (self.root / 'pandora.toml').write_text('version = \n')
        text, _sources = enrollment.derive(self.root, {'name': 'demo'}, socket_path='/s',
                                           client='/nonexistent/config.toml')
        self.assertEqual(enrollment.parse(text)['claim'], [])

    def test_no_file_written_now_names_a_home(self):
        (self.root / 'pandora.toml').write_text(minimal())
        text, _sources = enrollment.derive(self.root, {'name': 'demo'}, socket_path='/s',
                                           client='/nonexistent/config.toml')
        registration = enrollment.registration_text(socket_path='/s', repo='demo')
        for written in (text, registration):
            self.assertIsNone(enrollment.parse(written)['home'])

    def test_write_cache_says_when_it_replaced_other_claims(self):
        cache = self.root / '.git' / 'pandora-claims'
        source = self.root / 'pandora.toml'
        source.write_text('x')
        self.assertIs(enrollment.write_cache(cache, 'claim a\n', [source]), True)   # first
        self.assertFalse(enrollment.write_cache(cache, 'claim a\n', [source]))      # nothing
        self.assertEqual(enrollment.write_cache(cache, 'claim b\n', [source]), enrollment.CHANGED)


class DaemonRefusesWhatItCannotRead(DaemonCase):
    def enroll(self):
        git = self.repo / '.git'
        git.mkdir(exist_ok=True)
        (git / 'pandora-repo').write_text(enrollment.registration_text(
            socket_path=str(self.daemon.socket_path), repo='demo'))
        return git / 'pandora-claims'

    def test_an_unknown_value_is_refused_with_the_key_the_value_and_the_fix(self):
        text = CONFIG % {'marker': self.marker}
        (self.repo / 'pandora.toml').write_text(
            text.replace('subdirectory = "reroot"', 'subdirectory = "elsewhere"'))
        answer = self.call(['pnpm', 'unit'])
        self.assertEqual(answer.error['code'], 'config-unknown')
        self.assertEqual(answer.exit, INFRA)
        message = answer.error['msg']
        self.assertIn('matching.subdirectory = "elsewhere"', message)
        self.assertIn('git -C %s pull && pandora daemon --restart' % daemon_module.PACKAGE_HOME,
                      message)
        self.assertIn('pandora ps', message)
        self.assertIn('Nothing ran', message)
        self.assertIsNone(re.search(r'\bv\d', message), message)       # no version numbers
        self.assertFalse(self.marker.exists())

    def test_an_unknown_key_is_refused_and_named(self):
        text = CONFIG % {'marker': self.marker}
        (self.repo / 'pandora.toml').write_text(text.replace('[matching]\n',
                                                             '[matching]\ncolor = 3\n'))
        answer = self.call(['pnpm', 'unit'])
        self.assertEqual(answer.error['code'], 'config-unknown')
        self.assertIn('matching.color = 3', answer.error['msg'])

    def test_a_same_mtime_replacement_is_read_again(self):
        path = self.repo / 'pandora.toml'
        self.assertEqual(self.call(['pnpm', 'unit']).exit, 0)        # warms the parse cache
        stamp = path.stat().st_mtime_ns
        path.write_text(path.read_text().replace('[matching]\n', '[matching]\ncolor = 3\n'))
        os.utime(path, ns=(stamp, stamp))
        answer = self.call(['pnpm', 'unit'])
        self.assertEqual(answer.error['code'], 'config-unknown')

    def test_a_malformed_file_still_passes_through(self):
        (self.repo / 'pandora.toml').write_text('version = \n')
        answer = self.call(['pnpm', 'unit'])
        self.assertEqual(answer.error['code'], 'passthrough')

    def test_drift_on_a_remote_job_loads_and_warns_once(self):
        # Live configs set `drift` on remote jobs; the key does nothing there,
        # so the daemon says so once rather than refusing the file.
        text = CONFIG % {'marker': self.marker}
        (self.repo / 'pandora.toml').write_text(
            text.replace('size = "small"', 'size = "small"\ndrift = "warn"', 1))
        answer = self.call(['pnpm', 'unit'])
        self.assertEqual(answer.exit, 0, answer.error)
        drift = [line for line in answer.notices if 'drift' in line]
        self.assertEqual(len(drift), 1, answer.notices)
        self.assertIn('frozen snapshot', drift[0])
        again = self.call(['pnpm', 'unit'])
        self.assertFalse(any('drift' in line for line in again.notices), again.notices)

    def test_the_slow_path_claims_what_the_unreadable_file_claims(self):
        self.enroll()
        text = CONFIG % {'marker': self.marker}
        (self.repo / 'pandora.toml').write_text(text.replace('[matching]\n',
                                                             '[matching]\ncolor = 3\n'))
        answer = self.daemon.claims({'cwd': str(self.repo), 'argv': ['unit']})
        self.assertTrue(answer['claimed'])

    def test_a_changed_config_refreshes_the_cache_and_says_so_once(self):
        cache = self.enroll()
        first = self.daemon.claims({'cwd': str(self.repo), 'argv': ['unit']})
        self.assertNotIn('refreshed', first)                   # a first write is not news
        self.assertNotIn('home ', cache.read_text())
        text = CONFIG % {'marker': self.marker}
        (self.repo / 'pandora.toml').write_text(text.replace('prefix = ["surface"]',
                                                             'prefix = ["browser"]'))
        answer = self.call(['pnpm', 'unit'])
        self.assertEqual(answer.exit, 0, answer.error)
        self.assertIn('claim cache refreshed from pandora.toml', answer.notices)
        self.assertIn(['browser'], enrollment.parse(cache.read_text())['claim'])
        again = self.call(['pnpm', 'unit'])
        self.assertNotIn('claim cache refreshed from pandora.toml', again.notices)

    def test_the_slow_path_answer_carries_the_refresh(self):
        self.enroll()
        self.daemon.claims({'cwd': str(self.repo), 'argv': ['unit']})
        text = CONFIG % {'marker': self.marker}
        (self.repo / 'pandora.toml').write_text(text.replace('prefix = ["surface"]',
                                                             'prefix = ["browser"]'))
        answer = self.daemon.claims({'cwd': str(self.repo), 'argv': ['browser']})
        self.assertEqual((answer['claimed'], answer['refreshed']), (True, 'pandora.toml'))

    def test_a_protocol_mismatch_names_the_fix_and_no_version(self):
        import socket
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(str(self.daemon.socket_path))
        try:
            sock.sendall(dump({'v': 999, 'op': 'ping'}))
            frame = Reader(sock).line()
        finally:
            sock.close()
        self.assertEqual(frame['code'], 'version')
        self.assertIn('pandora daemon --restart', frame['msg'])
        self.assertIsNone(re.search(r'\bv\d|999', frame['msg']), frame['msg'])


class TheFixFollowsHowPandoraIsInstalled(unittest.TestCase):
    def test_a_checkout_is_pulled_and_the_daemon_restarted_when_idle(self):
        fix = install.update_fix('/src/pandora', data='/nonexistent-data')
        self.assertIn('pandora ps', fix)
        self.assertIn('git -C /src/pandora pull && pandora daemon --restart', fix)

    def test_an_installed_version_is_upgraded_from_its_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            version = data / 'versions' / 'abc123'
            version.mkdir(parents=True)
            (version / install.META).write_text(json.dumps({'source': '/src/pandora'}))
            self.assertEqual(install.update_fix(str(version), data=data),
                             '`git -C /src/pandora pull && pandora upgrade --from /src/pandora`')
            self.assertNotRegex(install.update_fix(str(version), data=data), r'\bv\d')
            # A version built from a release has no source checkout: bare
            # upgrade fetches the latest release.
            (version / install.META).write_text(json.dumps({'release': 'v0.3.0'}))
            self.assertEqual(install.update_fix(str(version), data=data),
                             '`pandora upgrade`')


class InstalledButNotAnswering(unittest.TestCase):
    """The one decision the client makes alone, now split on whether Pandora is installed."""

    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.root = Path(home.name).resolve()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.ran = self.root / 'ran'
        self.real = self.root / 'fake-pnpm'
        self.real.write_text('#!/bin/sh\necho "real $*"\necho ran > %s\n' % self.ran)
        self.real.chmod(0o755)
        self.repo = self.root / 'repo'
        (self.repo / '.git').mkdir(parents=True)
        self.marker = self.repo / '.git' / 'pandora-enrolled'
        self.marker.write_text(enrollment.render(
            socket_path=str(self.state / 'client.sock'), repo='demo', claims=[['unit']],
            heavy=enrollment.heavy_forms([['unit']])))
        self.config = self.root / 'config.toml'
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.repo)
        grace = mock.patch.object(shim, 'DAEMON_GRACE_SECONDS', 0.3)
        grace.start()
        self.addCleanup(grace.stop)

    def run_shim(self, *command, installed):
        if installed:
            self.config.write_text('[client]\nstate = "%s"\n' % self.state)
        env = mock.patch.dict(os.environ, {'PANDORA_CONFIG': str(self.config)})
        err = io.StringIO()
        with env, contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = shim.main(['--sock', str(self.state / 'client.sock'), '--real',
                              str(self.real), '--state', str(self.state), '--', *command])
        return code, err.getvalue()

    def test_installed_and_silent_exits_70_with_the_doctor_hint(self):
        code, err = self.run_shim('unit', installed=True)
        self.assertEqual(code, INFRA)
        self.assertIn('installed on this Mac (%s)' % self.config, err)
        self.assertIn('pandora: hint: run `pandora doctor`', err)
        self.assertFalse(self.ran.exists())
        self.assertFalse((self.state / 'passthrough.jsonl').exists())

    def test_never_installed_still_passes_through(self):
        code, err = self.run_shim('unit', installed=False)
        self.assertEqual(code, 0)
        self.assertTrue(self.ran.exists())
        self.assertIn('as if Pandora were not installed', err)

    def test_a_daemon_that_comes_back_within_the_grace_is_used(self):
        sock = mock.Mock()
        calls = []

        def connect(path, timeout):
            calls.append(path)
            if len(calls) < 3:
                raise ConnectionRefusedError('restarting')
            return sock
        clock = iter([0.0, 0.1, 0.2, 0.3])
        with mock.patch.object(shim, 'connect', side_effect=connect), \
                mock.patch.object(shim.time, 'sleep'):
            self.assertIs(shim.connect_within('/s', 5.0, clock=lambda: next(clock)), sock)
        self.assertEqual(len(calls), 3)

    def test_no_grace_means_one_try(self):
        with mock.patch.object(shim, 'connect', side_effect=FileNotFoundError('gone')) as one:
            with self.assertRaises(FileNotFoundError):
                shim.connect_within('/s', 0.0)
        self.assertEqual(one.call_count, 1)

    def test_detach_and_remote_keep_their_own_refusals(self):
        with mock.patch.dict(os.environ, {'PANDORA_WHERE': 'remote'}):
            code, err = self.run_shim('unit', installed=True)
        self.assertEqual(code, INFRA)
        self.assertIn('pandora: hint: run `pandora doctor`', err)
        self.assertFalse(self.ran.exists())

    def test_a_legacy_home_line_is_read_ignored_and_named(self):
        text = self.marker.read_text().replace('repo demo\n', 'repo demo\nhome /old/checkout\n')
        self.marker.write_text(text)
        code, err = self.run_shim('unit', installed=False)
        self.assertEqual(code, 0)                       # routed as before
        self.assertIn('names /old/checkout as the client home', err)
        self.assertIn('no longer read', err)

    def test_a_legacy_home_is_named_before_a_refresh_rewrites_the_file(self):
        cache = self.repo / '.git' / 'pandora-claims'
        cache.write_text(enrollment.render(socket_path=str(self.state / 'client.sock'),
                                           repo='demo', claims=[['unit']], derived='own')
                         .replace('repo demo\n', 'repo demo\nhome /old/checkout\n'))
        (self.repo / 'pandora.toml').write_text(minimal(subdirectory='reroot'))
        self.config.write_text('[client]\nstate = "%s"\n[[repos]]\nname = "demo"\nroot = "%s"\n'
                               % (self.state, self.repo))
        env = mock.patch.dict(os.environ, {'PANDORA_CONFIG': str(self.config)})
        err = io.StringIO()
        with env, contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            shim.main(['--sock', str(self.state / 'client.sock'), '--real', str(self.real),
                       '--state', str(self.state), '--refresh', '--', 'unit'])
        self.assertIn('names /old/checkout as the client home', err.getvalue())

    def test_the_client_says_when_it_refreshed_claims_without_a_daemon(self):
        (self.repo / 'pandora.toml').write_text(minimal(subdirectory='reroot'))
        self.config.write_text('[client]\nstate = "%s"\n[[repos]]\nname = "demo"\nroot = "%s"\n'
                               % (self.state, self.repo))
        (self.repo / '.git' / 'pandora-repo').write_text(enrollment.registration_text(
            socket_path=str(self.state / 'client.sock'), repo='demo'))
        cache = self.repo / '.git' / 'pandora-claims'
        cache.write_text(enrollment.render(socket_path=str(self.state / 'client.sock'),
                                           repo='demo', claims=[['old']], derived='own'))
        env = mock.patch.dict(os.environ, {'PANDORA_CONFIG': str(self.config)})
        err = io.StringIO()
        with env, contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = shim.main(['--sock', str(self.state / 'client.sock'), '--real',
                              str(self.real), '--state', str(self.state), '--refresh',
                              '--', 'unit'])
        self.assertIn('pandora: claim cache refreshed from pandora.toml', err.getvalue())
        self.assertEqual(code, INFRA)                   # then: installed, and no daemon
        self.assertIn(['unit'], enrollment.parse(cache.read_text())['claim'])


class ThroughTheShellShim(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.root = Path(home.name).resolve()
        self.state = self.root / 'state'
        self.state.mkdir()
        fake = self.root / 'fake'
        fake.mkdir()
        (fake / 'pnpm').write_text('#!/bin/sh\necho "real $*"\n')
        (fake / 'pnpm').chmod(0o755)
        self.repo = self.root / 'repo'
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.config = self.root / 'config.toml'
        self.config.write_text('[client]\nstate = "%s"\n' % self.state)
        self.env = dict(os.environ, PATH='%s:%s' % (HERE / 'bin', fake) + ':/usr/bin:/bin',
                        PANDORA_CONFIG=str(self.config))
        for name in ('PANDORA_OFF', 'PANDORA_ROUTE_DEPTH', 'PANDORA_HOME', 'PANDORA_WHERE',
                     'PANDORA_PYTHON'):
            self.env.pop(name, None)

    def mark(self, home=None):
        text = enrollment.render(socket_path=str(self.state / 'client.sock'), repo='demo',
                                 claims=[['unit']], heavy=enrollment.heavy_forms([['unit']]))
        if home:
            text = text.replace('repo demo\n', 'repo demo\nhome %s\n' % home)
        (self.repo / '.git' / 'pandora-enrolled').write_text(text)

    def pnpm(self, *argv, **extra):
        return subprocess.run(['sh', str(HERE / 'bin' / 'pnpm'), *argv], cwd=self.repo,
                              env=dict(self.env, **extra), capture_output=True, text=True,
                              timeout=60)

    def test_pandora_off_still_bypasses_an_installed_daemon_that_is_down(self):
        self.mark()
        proc = self.pnpm('unit', PANDORA_OFF='1')
        self.assertEqual((proc.returncode, proc.stdout), (0, 'real unit\n'), proc.stderr)

    def test_the_shim_runs_its_own_checkout_whatever_the_file_names(self):
        # A `home` naming a checkout with no package used to make every claimed
        # command fail to start the client. Now it is ignored.
        self.mark(home=str(self.root / 'gone'))
        self.config.unlink()                            # never installed: passes through
        proc = self.pnpm('unit')
        self.assertEqual((proc.returncode, proc.stdout), (0, 'real unit\n'), proc.stderr)
        self.assertIn('no longer read', proc.stderr)

    def test_installed_and_down_is_exit_70_through_the_shell_too(self):
        self.mark()
        proc = self.pnpm('unit', PANDORA_PYTHON=os.environ.get('PANDORA_PYTHON') or 'python3')
        self.assertEqual(proc.returncode, INFRA, proc.stderr)
        self.assertEqual(proc.stdout, '')
        self.assertIn('pandora doctor', proc.stderr)


class EnrollIsConsent(unittest.TestCase):
    def test_help_says_consent_and_no_reenroll(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(['enroll', '--help'])
        text = ' '.join(out.getvalue().split())
        self.assertIn('Consent', text)
        self.assertIn('no enroll', text)


if __name__ == '__main__':
    unittest.main()
