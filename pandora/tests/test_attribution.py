"""Every run records which session submitted it, and `ps --json`, `logs` and `result` say so."""
import json
import os
import unittest
from unittest import mock

from pandora.client import attribution, shim
from pandora import cli
from pandora.tests.test_cli import capture
from pandora.tests.test_fallback import DaemonCase


class WhoSubmitted(unittest.TestCase):
    TABLE = {500: (400, 'ttys001', 'Python'), 400: (300, 'ttys001', 'sh'),
             300: (200, 'ttys001', 'claude'), 200: (100, 'ttys001', 'zsh'),
             100: (1, 'ttys001', 'login'), 1: (0, '??', 'launchd')}

    def test_a_session_variable_wins_in_order(self):
        env = {'CLAUDE_CODE_SESSION_ID': 'c1', 'CODEX_COMPANION_SESSION_ID': 'c1'}
        self.assertEqual(shim.submitter(env), {'via': 'CLAUDE_CODE_SESSION_ID', 'id': 'c1'})
        env['PANDORA_SESSION'] = 'p1'
        self.assertEqual(shim.submitter(env), {'via': 'PANDORA_SESSION', 'id': 'p1'})
        self.assertEqual(shim.submitter({'CODEX_COMPANION_SESSION_ID': 'x1'}),
                         {'via': 'CODEX_COMPANION_SESSION_ID', 'id': 'x1'})
        self.assertIsNone(shim.submitter({'CLAUDE_SESSION_ID': 'not a real name'}))

    def test_the_client_never_runs_ps(self):
        with mock.patch('subprocess.run', side_effect=AssertionError('ps in the client')):
            self.assertIsNone(shim.submitter({}))
            request = shim.build_request(['unit'], cwd='/tmp')
        self.assertNotIn('submitter', request)

    def test_the_top_interactive_process_of_the_chain(self):
        self.assertEqual(attribution.top_interactive(self.TABLE, 500), 'zsh:200')

    def test_a_parent_without_a_terminal_ends_the_chain(self):
        table = {400: (300, 'ttys001', 'sh'), 300: (200, '??', 'tmux'),
                 200: (1, '??', 'launchd')}
        self.assertEqual(attribution.top_interactive(table, 400), 'sh:400')

    def test_the_peer_pid_of_a_unix_socket_is_the_other_process(self):
        import socket
        one, two = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(one.close)
        self.addCleanup(two.close)
        self.assertEqual(attribution.peer_pid(one), os.getpid())

    def test_the_request_carries_it_and_the_variable_itself_does_not_travel(self):
        with mock.patch.dict(os.environ, {'PANDORA_SESSION': 'orchestrator-7'}):
            request = shim.build_request(['unit'], cwd='/tmp')
        self.assertEqual(request['submitter'], {'via': 'PANDORA_SESSION', 'id': 'orchestrator-7'})
        self.assertNotIn('PANDORA_SESSION', request['env'])


class Recorded(DaemonCase):
    def pandora(self, *argv):
        return capture(cli.main, ['--state', str(self.state),
                                  '--config', str(self.root / 'config.toml'), *argv])

    def detach(self, *command):
        here = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, here)
        return capture(shim.main, ['--sock', str(self.daemon.socket_path), '--real', 'pnpm',
                                   '--state', str(self.state), '--detach', '--', *command])

    def test_ps_json_logs_and_result_show_the_submitter(self):
        with mock.patch.dict(os.environ, {'PANDORA_SESSION': 'agent-3'}):
            code, out, err = self.detach('unit')
        self.assertEqual(code, 0, err)
        run_id = out.strip()
        self.pandora('wait', run_id)
        meta = json.loads((self.state / 'runs' / run_id / 'meta.json').read_text())
        self.assertEqual(meta['submitter'], {'via': 'PANDORA_SESSION', 'id': 'agent-3'})
        code, out, _ = self.pandora('ps', '--json')
        [row] = [row for row in json.loads(out)['runs'] if row['id'] == run_id]
        self.assertEqual(row['submitter']['id'], 'agent-3')
        code, out, _ = self.pandora('result', run_id)
        self.assertIn('  submitted by agent-3 (PANDORA_SESSION)', out)
        code, out, _ = self.pandora('result', run_id, '--json')
        self.assertEqual(json.loads(out)['submitter']['id'], 'agent-3')
        code, out, err = self.pandora('logs', run_id)
        self.assertIn('run %s was submitted by agent-3 (PANDORA_SESSION)' % run_id, err)
        self.assertNotIn('agent-3', out)

    def test_without_a_session_variable_the_daemon_names_the_callers_session(self):
        table = {os.getpid(): (4000, 'ttys009', 'python3'), 4000: (3000, 'ttys009', 'zsh'),
                 3000: (1, 'ttys009', 'login')}
        with mock.patch.object(attribution, 'process_table', return_value=table):
            answer = self.call(['pnpm', 'unit'])
        self.assertEqual(answer.exit, 0, answer.error)
        meta = json.loads((self.state / 'runs' / answer.accepted['run'] / 'meta.json')
                          .read_text())
        self.assertEqual(meta['submitter'], {'via': 'process', 'id': 'zsh:4000'})

    def test_a_session_variable_means_no_ps_in_the_daemon_either(self):
        with mock.patch.dict(os.environ, {'PANDORA_SESSION': 'agent-4'}), \
                mock.patch.object(attribution, 'process_table',
                                  side_effect=AssertionError('ps')):
            code, out, err = self.detach('unit')
        self.assertEqual(code, 0, err)

    def test_a_malformed_submitter_is_not_stored(self):
        from pandora.client.daemon import submitted_by
        self.assertIsNone(submitted_by({'submitter': 'agent'}))
        self.assertIsNone(submitted_by({'submitter': {'via': 'x', 'id': 3}}))
        self.assertEqual(submitted_by({'submitter': {'via': 'v', 'id': 'i' * 500}})['id'],
                         'i' * 200)
