"""Transfer evidence survives client records without changing verdicts or replay."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pandora import cli
from pandora.client import worker as backend
from pandora.client.daemon import Run
from pandora.errors import TransferError, WorkerUnreachable
from pandora.tests.test_fallback import DaemonCase, FakeWorker, Submission
from pandora.tests.test_snapshot import make_repo


class WorkerRecords(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = make_repo(Path(temporary.name) / 'repo', {'file.txt': 'source'})
        self.worker = backend.Worker.__new__(backend.Worker)
        self.worker.link, self.worker._root = None, '/engine'
        self.worker._bundle = {'path': '/engine/bundles/ready'}
        self.worker.engine = lambda *a, **k: {'ok': True, 'run_id': 'r1'}
        self.plan = {'repo': 'demo', 'secrets_exclude_globs': [], 'git': 'none'}

    def submit(self):
        return self.worker.submit(plan=self.plan, worktree=self.repo, request_id='q1')

    def test_completed_report_is_detached_and_size_scan_is_not_repeated(self):
        reports = []
        def send(*a, report=None, on_send=None, **k):
            reports.append(report)
            report['steps']['probe'] = 0.1
            report['cache'] = 'absent'
            on_send()
            return {'path': '/engine/src/demo/input'}
        with mock.patch.object(backend.transfer, 'send', side_effect=send), \
                mock.patch.object(backend, 'source_size', wraps=backend.source_size) as sizes:
            accepted = self.submit()
        sizes.assert_called_once()
        self.assertEqual(accepted.transfer['source_bytes'], 6)
        self.assertEqual(accepted.transfer['cache'], 'absent')
        reports[0]['steps']['probe'] = 99
        self.assertEqual(accepted.transfer['steps']['probe'], 0.1)

    def test_failed_upload_carries_detached_partial_evidence(self):
        reports = []
        def send(*a, report=None, **k):
            reports.append(report)
            report['steps']['rsync'] = 8.0
            report['cache'] = 'absent'
            raise TransferError('upload failed')
        with mock.patch.object(backend.transfer, 'send', side_effect=send):
            with self.assertRaises(TransferError) as caught:
                self.submit()
        self.assertEqual(caught.exception.transfer['steps']['rsync'], 8)
        self.assertIn('ship', caught.exception.pre_accept)
        reports[0]['steps']['rsync'] = 99
        self.assertEqual(caught.exception.transfer['steps']['rsync'], 8)

    def test_failed_root_lookup_has_evidence_without_claiming_a_probe(self):
        self.worker.root = mock.Mock(side_effect=WorkerUnreachable('no worker'))
        with mock.patch.object(backend.transfer, 'send') as send:
            with self.assertRaises(WorkerUnreachable) as caught:
                self.submit()
        send.assert_not_called()
        self.assertEqual(set(caught.exception.transfer['steps']), {'root_lookup'})
        self.assertNotIn('cache', caught.exception.transfer)

    def test_incomplete_source_size_does_not_claim_a_complete_byte_count(self):
        def send(*a, report=None, on_send=None, **k):
            (self.repo / 'file.txt').unlink()
            on_send()
            self.assertNotIn('source_bytes', report)
            raise TransferError('source disappeared')
        with mock.patch.object(backend.transfer, 'send', side_effect=send):
            with self.assertRaises(TransferError):
                self.submit()


class SavedRecords(DaemonCase):
    def test_successful_metadata_and_result_json_keep_client_evidence_out_of_worker_verdict(self):
        evidence = {'cache': 'present', 'steps': {'root_lookup': 0.01, 'probe': 0.2}, 'files': 2}
        def accepted(worker, **kwargs):
            submission = Submission()
            submission.transfer = evidence
            return submission
        with mock.patch.object(FakeWorker, 'submit', accepted):
            answer = self.call(['pnpm', 'unit'])
        [meta_path] = list((self.state / 'runs').glob('*/meta.json'))
        saved = json.loads(meta_path.read_text())
        self.assertEqual(saved['transfer'], evidence)
        result_path = meta_path.parent / 'result.json'
        before = result_path.read_bytes()
        self.assertNotIn('transfer', json.loads(before))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(['--config', str(self.root / 'config.toml'), 'result',
                             saved['id'], '--json'])
        self.assertEqual((answer.exit, code), (0, 0))
        self.assertEqual(json.loads(output.getvalue())['transfer'], evidence)
        self.assertEqual(result_path.read_bytes(), before)
        restored = Run(self.state, 'restored', saved)
        self.assertEqual(restored.transfer, evidence)

    def test_older_saved_run_has_no_invented_transfer_evidence(self):
        output = io.StringIO()
        run_dir = self.state / 'runs' / 'legacy'
        run_dir.mkdir(parents=True)
        (run_dir / 'meta.json').write_text(json.dumps({'id': 'legacy', 'state': 'passed'}))
        (run_dir / 'result.json').write_text(json.dumps({'outcome': 'passed', 'cli_exit': 0}))
        with contextlib.redirect_stdout(output):
            code = cli.main(['--config', str(self.root / 'config.toml'), 'result',
                             'legacy', '--json'])
        self.assertEqual(code, 0)
        self.assertNotIn('transfer', json.loads(output.getvalue()))
