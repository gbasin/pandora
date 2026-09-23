"""The agent-facing verbs: `--help`, `run --detach`, `wait <ids>`, `result`."""
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from pandora import cli
from pandora.client import shim
from pandora.tests.test_fallback import DaemonCase, FakeWorker


def capture(function, *args):
    """(code, stdout, stderr), with real `.buffer`s, since streams write bytes."""
    out, err = (io.TextIOWrapper(io.BytesIO(), encoding='utf-8') for _ in range(2))
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = function(*args)
    out.flush()
    err.flush()
    return code, out.buffer.getvalue().decode(), err.buffer.getvalue().decode()


class Help(unittest.TestCase):
    def test_one_screen_with_the_invariants_and_the_fanout_verbs(self):
        code, out, _ = capture(lambda: _exit_code(cli.main, ['--help']))
        self.assertEqual(code, 0)
        self.assertLessEqual(len(out.splitlines()), 50)
        for needle in ('70', '75', '124', '130', 'PANDORA_OFF=1', 'repository root',
                       'pandora ps', 'pandora wait', 'pandora logs', 'pandora cancel',
                       'pandora result', 'pandora stats', 'run --detach',
                       'wait <id> <id>', 'PANDORA_SHARDS', 'result <id> --json',
                       'pandora: hint:', '--update', 'pandora resolve'):
            self.assertIn(needle, out)


def _exit_code(function, argv):
    try:
        return function(argv)
    except SystemExit as stop:
        return stop.code


class Verbs(DaemonCase):
    def pandora(self, *argv):
        return capture(cli.main, ['--state', str(self.state),
                                  '--config', str(self.root / 'config.toml'), *argv])

    def detach(self, *command):
        here = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, here)
        return capture(shim.main, ['--sock', str(self.daemon.socket_path), '--real', 'pnpm',
                                   '--state', str(self.state), '--detach', '--', *command])

    def test_detach_prints_only_the_id_and_the_run_completes(self):
        code, out, _ = self.detach('unit')
        self.assertEqual(code, 0)
        run_id = out.strip()
        self.assertRegex(run_id, r'^[0-9a-f]{12}$')
        code, out, _ = self.pandora('wait', run_id)
        self.assertEqual(code, 0)
        self.assertEqual(self.result_of(run_id)['outcome'], 'passed')

    def test_detach_refuses_a_passthrough_rather_than_running_it_in_the_foreground(self):
        code, out, err = self.detach('not-claimed-anything')
        self.assertEqual(code, 70)
        self.assertEqual(out, '')
        self.assertIn('nothing was started', err)

    def test_wait_on_several_prints_a_line_each_and_the_first_failure(self):
        first = self.detach('unit')[1].strip()
        second = self.detach('unit')[1].strip()
        code, out, _ = self.pandora('wait', first, second)
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual([line.split()[0] for line in lines], [first, second])
        self.assertTrue(all(line.split()[1] == 'passed' for line in lines), lines)
        code, out, _ = self.pandora('wait', first, 'nosuchrun000')
        self.assertEqual(code, 70)
        self.assertEqual(len(out.splitlines()), 2)

    def test_a_run_whose_stream_was_lost_is_not_counted_as_passed(self):
        # `wait a b` exits non-zero unless every run was seen to pass. A stream
        # cut off before the exit frame is a run nobody saw finish: 70, not 0.
        codes = {'good000000000': 0, 'lost000000000': None}
        with mock.patch.object(cli, 'attach', lambda sock, run_id, **k: codes[run_id]):
            code, out, _ = self.pandora('wait', 'good000000000', 'lost000000000')
            self.assertEqual(code, 70)
            self.assertEqual(out.splitlines()[1].split()[:2], ['lost000000000', 'lost'])
            # A deadline that has not passed does not turn a lost stream into 124.
            code, out, _ = self.pandora('wait', 'good000000000', 'lost000000000',
                                        '--max-wait', '600')
            self.assertEqual(code, 70)
            self.assertIn('lost', out)

    def test_result_is_a_summary_and_json_is_everything(self):
        run_id = self.detach('unit')[1].strip()
        self.pandora('wait', run_id)
        code, out, _ = self.pandora('result', run_id)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith(run_id + ': passed, exit 0'), out)
        code, out, _ = self.pandora('result', run_id, '--json')
        self.assertEqual(json.loads(out)['outcome'], 'passed')

    def test_an_earlier_run_over_the_same_tree_is_named_as_such(self):
        # A tree digest, not the command: the word must not claim more.
        submit = FakeWorker.submit

        def same_tree(worker, **kwargs):
            submission = submit(worker, **kwargs)
            submission.same_tree_as = 'r0'
            return submission
        with mock.patch.object(FakeWorker, 'submit', same_tree):
            _code, _out, err = self.detach('unit')
        self.assertIn('same tree as r0', err)
        self.assertNotIn('same input', err)

    def test_result_json_carries_same_tree_as_and_the_old_key(self):
        directory = self.state / 'runs' / 'old1'
        directory.mkdir(parents=True)
        # Written by an engine from before the rename: the old key only.
        (directory / 'result.json').write_text(json.dumps(
            {'outcome': 'passed', 'cli_exit': 0, 'input_id': 'abc', 'same_input_as': 'r0'}))
        code, out, _ = self.pandora('result', 'old1', '--json')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['same_tree_as'], 'r0')
        self.assertEqual(json.loads(out)['same_input_as'], 'r0')
        _code, out, _ = self.pandora('result', 'old1')
        self.assertIn('input abc (same tree as r0)', out)


if __name__ == '__main__':
    unittest.main()
