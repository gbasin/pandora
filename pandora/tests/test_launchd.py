"""`pandora daemon --install | --uninstall | --restart | --stop`, and doctor's supervision line.

The real `launchctl` is never called: every function takes the `run` callable
the module uses, and `FakeLaunchd` answers `print`, `bootstrap`, `bootout` and
`kickstart` from a dictionary, the way launchd would for one user agent. The
lock is real, though -- a child process takes `daemon.lock` exactly the way
`Daemon.acquire_lock` does -- because "a daemon is already running" is a fact
about a lock and not about a file that says so.
"""
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pandora.client import doctor, launchd

PRINT = '''gui/501/%(label)s = {
\tactive count = 1
\tpath = /Users/me/Library/LaunchAgents/%(label)s.plist
\tstate = %(state)s

\tprogram = /x/bin/pandora
\targuments = {
\t\t/x/bin/pandora
\t\tdaemon
\t}

\tenvironment = {
\t\tPATH => /opt/homebrew/bin:/usr/bin
\t}

\tdomain = gui/501 [100022]
\truns = 2
%(pid)s\tlast exit code = 0
}
'''


class FakeLaunchd:
    """One user domain holding at most one agent per label."""

    def __init__(self, loaded=None, *, bootstrap_fails=False):
        self.loaded = dict(loaded or {})          # label -> pid or None
        self.calls = []
        self.bootstrap_fails = bootstrap_fails
        self.next_pid = 9000

    def __call__(self, argv, **_kwargs):
        self.calls.append(argv[1:])
        verb, rest = argv[1], argv[2:]
        if verb == 'print':
            label = rest[0].split('/', 2)[2]
            if label not in self.loaded:
                return self.done(113, err='Bad request.\nCould not find service "%s" in '
                                          'domain for user gui: 501' % label)
            pid = self.loaded[label]
            return self.done(0, out=PRINT % {
                'label': label, 'state': 'running' if pid else 'not running',
                'pid': '\tpid = %d\n' % pid if pid else ''})
        if verb == 'bootstrap':
            if self.bootstrap_fails:
                return self.done(5, err='Bootstrap failed: 5: Input/output error')
            self.loaded[plistlib.loads(Path(rest[1]).read_bytes())['Label']] = self.spawn()
            return self.done(0)
        if verb == 'load':
            self.loaded[plistlib.loads(Path(rest[-1]).read_bytes())['Label']] = self.spawn()
            return self.done(0)
        if verb == 'bootout':
            label = rest[0].split('/', 2)[2]
            return self.done(0 if self.loaded.pop(label, 'gone') != 'gone' else 3)
        if verb == 'kickstart':
            label = rest[-1].split('/', 2)[2]
            if label not in self.loaded:
                return self.done(113)
            if '-k' in rest or not self.loaded[label]:
                self.loaded[label] = self.spawn()
            return self.done(0)
        raise AssertionError('unexpected launchctl call %r' % argv)

    def spawn(self):
        self.next_pid += 1
        return self.next_pid

    @staticmethod
    def done(code, out='', err=''):
        return subprocess.CompletedProcess([], code, out, err)

    def verbs(self):
        return [call[0] for call in self.calls]


HOLD = '''
import fcntl, os, sys, time
handle = open(sys.argv[1], 'a+')
fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
handle.seek(0); handle.truncate(); handle.write(str(os.getpid()) + '\\n'); handle.flush()
print('held', flush=True)
time.sleep(60)
'''


class Case(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(os.path.realpath(scratch.name))
        self.home = self.root / 'home'
        self.state = self.root / 'state'
        self.state.mkdir()
        self.said = []
        # Belt and braces: a path that reaches the real launchctl by mistake
        # fails to start rather than touching this Mac's launchd.
        patch = mock.patch.object(launchd, 'LAUNCHCTL', str(self.root / 'no-launchctl'))
        patch.start()
        self.addCleanup(patch.stop)

    def hold_lock(self):
        """A process that holds `daemon.lock` the way a hand-started daemon does."""
        proc = subprocess.Popen([sys.executable, '-c', HOLD, str(self.state / 'daemon.lock')],
                                stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (proc.poll() is None and proc.kill(), proc.wait()))
        self.assertEqual(proc.stdout.readline().strip(), 'held')
        return proc

    def install(self, fake, label='com.pandora.daemon', env=None):
        return launchd.install(label, config_path=self.root / 'config.toml', state=self.state,
                               env=env if env is not None else {'PATH': '/usr/bin:/bin'},
                               uid=501, home=self.home, python='/opt/homebrew/bin/python3',
                               run=fake, say=self.said.append)


class Plist(Case):
    def test_install_writes_the_agent_and_loads_it(self):
        fake = FakeLaunchd()
        self.install(fake)
        path = self.home / 'Library' / 'LaunchAgents' / 'com.pandora.daemon.plist'
        body = plistlib.loads(path.read_bytes())
        self.assertEqual(body['Label'], 'com.pandora.daemon')
        program = Path(body['ProgramArguments'][0])
        self.assertTrue(program.is_absolute())
        self.assertEqual(program, program.resolve(), 'the plist must name the checkout')
        self.assertEqual(program, Path(__file__).resolve().parents[2] / 'bin' / 'pandora')
        self.assertEqual(body['ProgramArguments'][1:],
                         ['--config', str(self.root / 'config.toml'), 'daemon'])
        self.assertIs(body['RunAtLoad'], True)
        self.assertIs(body['KeepAlive'], True)
        log = str(self.state / 'logs' / 'daemon.log')
        self.assertEqual(body['StandardOutPath'], log)
        self.assertEqual(body['StandardErrorPath'], log)
        self.assertTrue((self.state / 'logs').is_dir())
        path_entries = body['EnvironmentVariables']['PATH'].split(':')
        for entry in ('/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin'):
            self.assertIn(entry, path_entries)
        self.assertEqual(fake.verbs(), ['print', 'bootstrap', 'kickstart', 'print'])
        self.assertEqual(fake.calls[1], ['bootstrap', 'gui/501', str(path)])
        self.assertEqual(fake.calls[2], ['kickstart', 'gui/501/com.pandora.daemon'])
        text = '\n'.join(self.said)
        self.assertIn('wrote ' + str(path), text)
        self.assertIn('state = running, pid = 9001', text)
        self.assertIn('pandora daemon --restart', text)
        self.assertEqual(launchd.label_for(self.state), 'com.pandora.daemon')

    def test_an_explicit_state_is_passed_on(self):
        body = launchd.render('x', program='/p/bin/pandora', config_path='/c.toml',
                              state='/s', path='/usr/bin', state_arg=True)
        self.assertEqual(body['ProgramArguments'],
                         ['/p/bin/pandora', '--config', '/c.toml', '--state', '/s', 'daemon'])

    def test_path_carries_the_tools_found_here_and_not_the_shim(self):
        tools = self.root / 'tools'
        tools.mkdir()
        for name in ('pnpm', 'node'):
            (tools / name).write_text('#!/bin/sh\n')
            (tools / name).chmod(0o755)
        shim = self.root / 'shim'
        shim.mkdir()
        (shim / 'pnpm').symlink_to(Path(__file__).resolve().parents[2] / 'bin' / 'pnpm')
        (shim / '.pandora-shim').write_text('')
        path = launchd.service_path({'PATH': '%s:%s:/usr/bin' % (shim, tools)},
                                    python='/somewhere/python3').split(':')
        self.assertEqual(path[:2], ['/somewhere', str(tools)])
        self.assertNotIn(str(shim), path)
        self.assertEqual(path[2:], list(launchd.BASE_PATH))

    def test_a_label_and_a_bootstrap_fallback(self):
        fake = FakeLaunchd(bootstrap_fails=True)
        self.install(fake, label='me.example.pandora')
        self.assertTrue((self.home / 'Library' / 'LaunchAgents' / 'me.example.pandora.plist')
                        .is_file())
        self.assertIn('load', fake.verbs())
        self.assertEqual(launchd.label_for(self.state), 'me.example.pandora')

    def test_reinstall_boots_the_old_agent_out_first(self):
        fake = FakeLaunchd({'com.pandora.daemon': 4242})
        self.install(fake)
        self.assertEqual(fake.verbs(), ['print', 'bootout', 'bootstrap', 'kickstart', 'print'])

    def test_the_plist_lints(self):
        if sys.platform != 'darwin':
            self.skipTest('plutil is macOS only')
        self.install(FakeLaunchd())
        path = self.home / 'Library' / 'LaunchAgents' / 'com.pandora.daemon.plist'
        proc = subprocess.run(['plutil', '-lint', str(path)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class TheLock(Case):
    def test_install_refuses_while_a_hand_started_daemon_holds_the_lock(self):
        proc = self.hold_lock()
        fake = FakeLaunchd()
        with self.assertRaises(launchd.Refused) as caught:
            self.install(fake)
        self.assertIn('pid %d' % proc.pid, str(caught.exception))
        self.assertIn('pandora daemon --stop', str(caught.exception))
        self.assertFalse((self.home / 'Library').exists(), 'nothing may be written')
        self.assertNotIn('bootstrap', fake.verbs())

    def test_install_over_its_own_launchd_daemon_is_a_reinstall(self):
        proc = self.hold_lock()
        fake = FakeLaunchd({'com.pandora.daemon': proc.pid})
        self.install(fake)
        self.assertIn('bootstrap', fake.verbs())

    def test_lock_holder_reads_the_pid_and_creates_nothing(self):
        self.assertIsNone(launchd.lock_holder(self.state))
        self.assertFalse((self.state / 'daemon.lock').exists())
        proc = self.hold_lock()
        self.assertEqual(launchd.lock_holder(self.state), proc.pid)

    def test_stop_sends_sigterm_and_waits_for_the_lock(self):
        proc = self.hold_lock()
        launchd.stop('com.pandora.daemon', state=self.state, uid=501, run=FakeLaunchd(),
                     say=self.said.append)
        self.assertIn('stopped the daemon (pid %d)' % proc.pid, self.said)
        self.assertIsNotNone(proc.wait(timeout=5))
        self.assertIsNone(launchd.lock_holder(self.state))

    def test_stop_refuses_a_daemon_launchd_would_restart(self):
        proc = self.hold_lock()
        with self.assertRaises(launchd.Refused) as caught:
            launchd.stop('com.pandora.daemon', state=self.state, uid=501,
                         run=FakeLaunchd({'com.pandora.daemon': proc.pid}), say=self.said.append)
        self.assertIn('--uninstall', str(caught.exception))
        self.assertIsNone(proc.poll())

    def test_stop_with_no_daemon_says_so(self):
        launchd.stop('com.pandora.daemon', state=self.state, uid=501, run=FakeLaunchd(),
                     say=self.said.append)
        self.assertIn('no daemon holds', self.said[0])


class Verbs(Case):
    def test_uninstall_boots_out_and_deletes(self):
        fake = FakeLaunchd()
        self.install(fake)
        launchd.uninstall('com.pandora.daemon', state=self.state, uid=501, home=self.home,
                          run=fake, say=self.said.append)
        self.assertFalse((self.home / 'Library' / 'LaunchAgents' / 'com.pandora.daemon.plist')
                         .exists())
        self.assertNotIn('com.pandora.daemon', fake.loaded)
        self.assertFalse((self.state / launchd.RECORD).exists())
        self.assertTrue(any(line.startswith('removed ') for line in self.said))

    def test_restart_kickstarts_and_refuses_when_not_installed(self):
        fake = FakeLaunchd({'com.pandora.daemon': 100})
        launchd.restart('com.pandora.daemon', uid=501, run=fake, say=self.said.append)
        self.assertIn(['kickstart', '-k', 'gui/501/com.pandora.daemon'], fake.calls)
        self.assertIn('pid = 9001', self.said[-1])
        with self.assertRaises(launchd.Refused):
            launchd.restart('com.pandora.daemon', uid=501, run=FakeLaunchd())

    def test_the_cli_parses_the_verbs(self):
        from pandora import cli
        with mock.patch.object(cli, 'cmd_daemon_supervision', return_value=0) as called:
            self.assertEqual(cli.main(['daemon', '--restart']), 0)
        self.assertTrue(called.call_args.args[0].restart)
        with self.assertRaises(SystemExit):
            cli.main(['daemon', '--install', '--uninstall'])


class Supervision(Case):
    def supervision(self, pong, fake):
        return doctor.check_supervision(pong, self.state, platform='darwin', launchctl=fake)

    def test_ok_when_launchd_runs_the_daemon_that_answered(self):
        item = self.supervision({'pid': 4242}, FakeLaunchd({'com.pandora.daemon': 4242}))
        self.assertEqual(item['status'], 'ok', item)
        self.assertIn('--restart', item['detail'])

    def test_warn_when_the_daemon_was_started_by_hand(self):
        item = self.supervision({'pid': 4242}, FakeLaunchd())
        self.assertEqual(item['status'], 'warn', item)
        self.assertIn('started by hand', item['detail'])
        self.assertIn('pandora daemon --install', item['detail'])

    def test_fail_when_loaded_with_another_pid(self):
        item = self.supervision({'pid': 4242}, FakeLaunchd({'com.pandora.daemon': 77}))
        self.assertEqual(item['status'], 'fail', item)
        self.assertEqual(item['facts']['launchd_pid'], 77)
        item = self.supervision({'pid': 4242}, FakeLaunchd({'com.pandora.daemon': None}))
        self.assertEqual(item['status'], 'fail', item)
        self.assertIn('not running', item['detail'])

    def test_loaded_with_no_daemon_fails_and_neither_is_information(self):
        self.assertEqual(self.supervision(None, FakeLaunchd({'com.pandora.daemon': None}))
                         ['status'], 'fail')
        self.assertEqual(self.supervision(None, FakeLaunchd())['status'], 'info')

    def test_a_recorded_label_is_the_one_asked_about(self):
        (self.state / launchd.RECORD).write_text('{"label": "me.example.pandora"}')
        fake = FakeLaunchd({'me.example.pandora': 4242})
        self.assertEqual(self.supervision({'pid': 4242}, fake)['status'], 'ok')
        self.assertEqual(fake.calls, [['print', 'gui/%d/me.example.pandora' % os.getuid()]])

    def test_not_darwin_is_not_checked(self):
        item = doctor.check_supervision({'pid': 1}, self.state, platform='linux',
                                        launchctl=FakeLaunchd())
        self.assertEqual(item['status'], 'info')


if __name__ == '__main__':
    unittest.main()
