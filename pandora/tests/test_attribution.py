"""Every run records which session submitted it, and `ps --json`, `logs` and `result` say so."""
import json
import os
import unittest
from unittest import mock

from pandora.client import shim
from pandora import cli
from pandora.tests.test_cli import capture
from pandora.tests.test_fallback import DaemonCase


class WhoSubmitted(unittest.TestCase):
    TABLE = {500: (400, 'ttys001', 'Python'), 400: (300, 'ttys001', 'sh'),
             300: (200, 'ttys001', 'claude'), 200: (100, 'ttys001', 'zsh'),
             100: (1, 'ttys001', 'login'), 1: (0, '??', 'launchd')}

    def test_a_session_variable_wins_in_order(self):
        env = {'CLAUDE_SESSION_ID': 'c1', 'CODEX_COMPANION_SESSION_ID': 'x1'}
        self.assertEqual(shim.submitter(env, table=dict),
                         {'via': 'CLAUDE_SESSION_ID', 'id': 'c1'})
        env['PANDORA_SESSION'] = 'p1'
        self.assertEqual(shim.submitter(env, table=dict), {'via': 'PANDORA_SESSION', 'id': 'p1'})
        self.assertEqual(shim.submitter({'CODEX_COMPANION_SESSION_ID': 'x1'}, table=dict),
                         {'via': 'CODEX_COMPANION_SESSION_ID', 'id': 'x1'})

    def test_otherwise_the_top_interactive_process_of_the_chain(self):
        self.assertEqual(shim.top_interactive(self.TABLE, 400), 'zsh:200')
        with mock.patch.object(os, 'getppid', return_value=400):
            self.assertEqual(shim.submitter({}, table=lambda: self.TABLE),
                             {'via': 'process', 'id': 'zsh:200'})

    def test_a_parent_without_a_terminal_ends_the_chain(self):
        table = {400: (300, 'ttys001', 'sh'), 300: (200, '??', 'tmux'),
                 200: (1, '??', 'launchd')}
        self.assertEqual(shim.top_interactive(table, 400), 'sh:400')

    def test_no_process_table_means_no_submitter(self):
        self.assertIsNone(shim.submitter({}, table=dict))

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

    def test_a_malformed_submitter_is_not_stored(self):
        from pandora.client.daemon import submitted_by
        self.assertIsNone(submitted_by({'submitter': 'agent'}))
        self.assertIsNone(submitted_by({'submitter': {'via': 'x', 'id': 3}}))
        self.assertEqual(submitted_by({'submitter': {'via': 'v', 'id': 'i' * 500}})['id'],
                         'i' * 200)
