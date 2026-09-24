"""The agent-facing verbs: `--help`, `run --detach`, `wait <ids>`, `result`."""
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from pandora import cli
from pandora.client import shim
from pandora.errors import TransferError
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


class OldSpellings(unittest.TestCase):
    """`enrol` and `unenrol` still work for one release, and say what replaces them."""

    def unenroll(self, verb):
        with tempfile.TemporaryDirectory() as tmp:
            common = Path(tmp) / '.git'
            common.mkdir()
            (common / 'pandora-enrolled').write_text('sock /s\n')
            code, _, err = capture(cli.main, [verb, tmp])
            return code, err, (common / 'pandora-enrolled').exists()

    def test_the_old_spelling_runs_the_same_command_with_a_notice(self):
        code, err, left = self.unenroll('unenrol')
        self.assertEqual((code, left), (0, False))
        self.assertIn('`pandora unenrol` is deprecated; use `pandora unenroll`', err)

    def test_the_new_spelling_says_nothing_about_it(self):
        code, err, left = self.unenroll('unenroll')
        self.assertEqual((code, left), (0, False))
        self.assertNotIn('deprecated', err)

    def test_enrol_is_an_alias_of_enroll(self):
        with mock.patch.object(cli, 'cmd_enroll', return_value=0) as command:
            code, _, err = capture(cli.main, ['enrol', '/nowhere'])
        self.assertEqual(code, 0)
        command.assert_called_once()
        self.assertIn('use `pandora enroll`', err)


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

    def test_a_refused_run_says_why_it_never_reached_the_worker(self):
        FakeWorker.raises = TransferError('rsync to h failed (255): unexpected end of file')
        code, out, err = self.detach('surface')           # large: refused, not local
        self.assertEqual(code, 70, err)
        [meta] = [json.loads(path.read_text())
                  for path in (self.state / 'runs').glob('*/meta.json')]
        self.assertEqual(meta['state'], 'refused')
        self.assertEqual(meta['reason'], 'transfer-failed')
        self.assertEqual(meta['refusal']['cause'], 'transfer-failed')
        self.assertIn('unexpected end of file', meta['refusal']['detail'])
        code, out, _ = self.pandora('result', meta['id'])
        self.assertEqual(code, 70)
        self.assertEqual(out.splitlines()[0], '%s: refused before reaching the worker: '
                         'transfer-failed: rsync to h failed (255): unexpected end of file'
                         % meta['id'])
        code, out, _ = self.pandora('result', meta['id'], '--json')
        self.assertEqual((code, json.loads(out)['refusal']['cause']), (70, 'transfer-failed'))

    def test_a_queued_or_unknown_run_keeps_the_old_message(self):
        directory = self.state / 'runs' / 'q1'
        directory.mkdir(parents=True)
        (directory / 'meta.json').write_text(json.dumps({'id': 'q1', 'state': 'queued'}))
        for run_id in ('q1', 'nosuchrun000'):
            code, _out, err = self.pandora('result', run_id)
            self.assertEqual(code, 1)
            self.assertIn('still running, or it never reached the worker', err)

    def test_a_row_that_fell_back_points_at_the_local_run(self):
        directory = self.state / 'runs' / 'f1'
        directory.mkdir(parents=True)
        (directory / 'meta.json').write_text(json.dumps(
            {'id': 'f1', 'state': 'fell_back', 'exit_code': 70, 'fell_back_to': 'l1'}))
        code, out, _ = self.pandora('result', 'f1')
        self.assertEqual(out.strip(), 'f1: fell_back; see pandora result l1')
        self.assertEqual(code, 1)                     # the successor has no verdict yet
        successor = self.state / 'runs' / 'l1'
        successor.mkdir()
        (successor / 'meta.json').write_text(json.dumps({'id': 'l1', 'state': 'command_failed',
                                                         'exit_code': 3}))
        self.assertEqual(self.pandora('result', 'f1')[0], 3)

    def test_ps_names_the_pre_accept_step_of_a_queued_remote_row(self):
        from pandora.client.daemon import Run
        for run_id, phase in (('p1', 'ship'), ('p2', 'submit'), ('p3', None)):
            run = Run(self.state, run_id, {'argv': ['pnpm', 'check']},
                      on_save=self.daemon.status.update)
            run.phase = phase
            run.save()
        code, out, _ = self.pandora('ps')
        self.assertEqual(code, 0)
        words = {line.split()[0]: line.split()[1:3] for line in out.splitlines()
                 if line.split()[:1] and line.split()[0] in ('p1', 'p2', 'p3')}
        self.assertEqual(words, {'p1': ['remote', 'shipping'], 'p2': ['remote', 'submitting'],
                                 'p3': ['remote', 'queued']})

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


class ASlowDaemon(unittest.TestCase):
    """Operator verbs wait 30 s for a starved daemon, not 2 or 5 (2026-09-24: 66 s at load 90)."""

    DELAY = 5.5

    def serve(self, frames):
        import socket
        import threading
        from pandora.client.protocol import Reader, dump
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / 's.sock')
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(4)
        self.addCleanup(server.close)

        def answer():
            conn, _ = server.accept()
            with conn:
                Reader(conn).line()
                time.sleep(self.DELAY)
                for frame in frames:
                    conn.sendall(dump(frame))
        threading.Thread(target=answer, daemon=True).start()
        return path

    def test_the_constant_is_thirty_seconds(self):
        from pandora.client import doctor
        self.assertEqual(cli.OPERATOR_SECONDS, 30.0)
        self.assertEqual(cli.ask.__defaults__[0], 30.0)
        self.assertEqual(doctor.ping.__defaults__[0], 30.0)

    def test_wait_hears_a_daemon_that_answers_after_five_seconds(self):
        path = self.serve([{'t': 'accepted', 'run': 'x1', 'owned': True},
                           {'t': 'exit', 'code': 3, 'run': 'x1'}])
        code, _, err = capture(cli.attach, path, 'x1')
        self.assertEqual(code, 3, err)

    def test_doctor_hears_a_daemon_that_answers_after_five_seconds(self):
        from pandora.client import doctor
        path = self.serve([{'t': 'pong', 'pid': 1}])
        self.assertEqual(doctor.ping(path)['t'], 'pong')
