"""A `submit` whose reply is lost is asked about, never assumed to have failed.

The engine can claim a request, spawn its supervisor, and then have its answer
lost on the way back: ssh exits 255, or what arrives is not JSON. Treating that
as "the worker did not take it" admits the same command into the local lane, so
it runs twice -- on the worker and on this Mac. These tests put a real engine
(`service.main` against a scratch root, with the supervisor spawn stubbed) behind
a transport that admits and then raises, and check each answer the re-ask can
give.
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from pandora.client import worker as worker_module
from pandora.client.daemon import UNCERTAIN
from pandora.engine import runner, service
from pandora.engine.ledger import Ledger
from pandora.errors import EngineError, ExecutionUncertain, WorkerUnreachable
from pandora.tests.test_engine import PLAN
from pandora.tests.test_fallback import DaemonCase, FakeWorker

SUBMIT_PLAN = dict(PLAN, shards=None, secrets_exclude_globs=[], git='none', writeback=False)


class LossyWorker(worker_module.Worker):
    """A `Worker` whose engine is real and local, and whose transport can fail.

    `lose` names what happens to `submit`: 'after' runs it and loses the reply,
    'before' fails without the engine seeing it. `unreachable` makes every call
    after that fail too, as a worker that went away entirely.
    """

    def __init__(self, root, *, lose, unreachable=False):
        self.root_dir = root
        self.link = self.state = None
        self.lose = lose
        self.unreachable = unreachable
        self.calls = []

    def bundle_path(self):
        return '/bundle'

    def root(self):
        return str(self.root_dir)

    def engine(self, argv, stdin=None, **kwargs):
        self.calls.append(argv[0])
        if argv[0] != 'submit' and self.unreachable:
            raise WorkerUnreachable('ssh worker: Connection closed by remote host')
        if argv[0] == 'submit' and self.lose == 'before':
            raise WorkerUnreachable('ssh worker: Connection timed out')
        out = io.StringIO()
        with redirect_stdout(out), mock.patch('sys.stdin', io.StringIO(stdin or '')):
            service.main(['--root', str(self.root_dir), *argv])
        if argv[0] == 'submit' and self.lose == 'after':
            raise EngineError('engine submit returned non-JSON: Connection reset')
        return json.loads(out.getvalue())


class LostReply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'engine'
        self.worktree = Path(self.tmp.name) / 'tree'
        self.worktree.mkdir()
        self.spawned, self.waiting = [], []
        for patch in (
                mock.patch.dict(os.environ, {'PANDORA_BUDGET_MIB': '8192'}),
                mock.patch.object(runner, 'spawn',
                                  lambda root, run_id, python=None: self.spawned.append(run_id)
                                  or 4242),
                mock.patch.object(runner, 'spawn_waiter',
                                  lambda root, run_id, python=None: self.waiting.append(run_id)
                                  or 4343),
                mock.patch.object(runner, 'disk_headroom', lambda paths, driver=None: {'ok': True}),
                mock.patch.object(worker_module.snapshot, 'freeze',
                                  lambda *a, **k: ([{'path': 'a'}], [], 'input-a')),
                mock.patch.object(worker_module.transfer, 'send',
                                  lambda *a, **k: {'path': str(self.worktree), 'reused': True})):
            patch.start()
            self.addCleanup(patch.stop)

    def submit(self, worker, request_id='c1:suite'):
        return worker.submit(plan=SUBMIT_PLAN, worktree=str(self.worktree),
                             request_id=request_id)

    def ledger_rows(self):
        ledger = Ledger(runner.Paths(self.root).ensure().ledger)
        try:
            return [dict(row) for row in ledger.recent()]
        finally:
            ledger.close()

    def test_a_run_the_engine_started_is_attached_to_not_run_again(self):
        worker = LossyWorker(self.root, lose='after')
        submission = self.submit(worker)
        self.assertEqual(worker.calls, ['submit', 'lookup'])
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(submission.run_id, self.spawned[0])
        self.assertTrue(submission.duplicate)
        self.assertEqual(submission.input_id, 'input-a')

    def test_a_request_the_engine_never_saw_may_still_fall_back(self):
        worker = LossyWorker(self.root, lose='before')
        with self.assertRaises(WorkerUnreachable):
            self.submit(worker)
        self.assertEqual(self.spawned, [])
        # The lookup fenced the id: the same submit arriving late is a duplicate
        # of a finished row and starts nothing.
        late = LossyWorker(self.root, lose=None)
        answer = late.engine(['submit'], stdin=json.dumps({
            'request_id': 'c1:suite', 'input_id': 'input-a',
            'source_path': str(self.worktree), 'plan': SUBMIT_PLAN}))
        self.assertTrue(answer['duplicate'])
        self.assertEqual(answer['state'], 'finished')
        self.assertEqual(self.spawned, [])

    def test_a_worker_that_cannot_be_asked_is_uncertain_never_a_fallback(self):
        worker = LossyWorker(self.root, lose='after', unreachable=True)
        with self.assertRaises(ExecutionUncertain):
            self.submit(worker)
        # Not a subclass of either fallback error: no handler written for them
        # can swallow it.
        self.assertFalse(issubclass(ExecutionUncertain, (WorkerUnreachable, EngineError)))

    def test_a_refusal_whose_reply_was_lost_is_still_that_refusal(self):
        runner.disk_headroom = lambda paths, driver=None: {'ok': False, 'reason': 'full'}
        worker = LossyWorker(self.root, lose='after')
        with self.assertRaises(EngineError) as caught:
            self.submit(worker)
        self.assertEqual(json.loads(str(caught.exception))['code'], 'disk-floor')
        self.assertEqual(self.spawned, [])

    def test_lookup_reports_what_the_ledger_holds(self):
        LossyWorker(self.root, lose=None).engine(['submit'], stdin=json.dumps({
            'request_id': 'c2:suite', 'input_id': 'input-a',
            'source_path': str(self.worktree), 'plan': SUBMIT_PLAN}))
        answer = LossyWorker(self.root, lose=None).engine(
            ['lookup', '--request-id', 'c2:suite'])
        self.assertEqual((answer['found'], answer['spawned'], answer['state']),
                         (True, True, 'admitted'))
        answer = LossyWorker(self.root, lose=None).engine(
            ['lookup', '--request-id', 'nobody'])
        self.assertEqual((answer['found'], answer['fenced']), (False, False))
        self.assertEqual(len(self.ledger_rows()), 1, 'a lookup without --fence wrote a row')


class UncertainOverTheSocket(DaemonCase):
    def test_an_uncertain_submission_ends_the_run_with_70_and_runs_nothing_here(self):
        # `unit` is small: every provably non-executing cause falls back for it.
        FakeWorker.raises = ExecutionUncertain('submit failed (ssh 255)')
        answer = self.call(['pnpm', 'unit'])
        self.assertEqual(answer.exit, 70)
        self.assertEqual(answer.error['code'], 'execution-uncertain')
        self.assertIn(UNCERTAIN, answer.error['msg'])
        self.assertIsNone(answer.accepted)
        self.assertFalse(self.marker.exists(), 'the command ran on this Mac as well')
        [meta] = [json.loads(path.read_text())
                  for path in (self.state / 'runs').glob('*/meta.json')]
        self.assertEqual((meta['state'], meta['exit_code']), ('infra_failed', 70))


if __name__ == '__main__':
    unittest.main()
