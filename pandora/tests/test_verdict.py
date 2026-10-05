"""Signed run verdicts: the payload, the key, the conditions, the result fields."""
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pandora.engine import runner, verdict
from pandora.engine.ledger import Ledger
from pandora.tests.test_engine import PLAN, FakeDriver, claim

HAVE_KEYGEN = shutil.which('ssh-keygen') is not None
TREE = 'ab' * 20


def fields(**over):
    base = {'argv': ['python3', '-m', 'unittest', 'discover', '-s', 'pandora'],
            'engine': 'e' * 64, 'finished': 1791209006.46, 'golden': '0123456789abcdef',
            'input_id': 'f' * 64, 'job': 'suite', 'outcome': 'passed', 'repo': 'pandora',
            'run_id': 'r1', 'tree': TREE}
    base.update(over)
    return base


class PayloadTest(unittest.TestCase):
    def test_the_payload_is_canonical_json_bytes(self):
        self.assertEqual(
            verdict.payload(**fields()),
            ('{"argv":["python3","-m","unittest","discover","-s","pandora"],'
             '"engine":"%s","finished":1791209006.46,"golden":"0123456789abcdef",'
             '"input_id":"%s","job":"suite","kind":"pandora-verdict","outcome":"passed",'
             '"repo":"pandora","run_id":"r1","tree":"%s","v":1}'
             % ('e' * 64, 'f' * 64, TREE)).encode())

    def test_equal_inputs_give_equal_bytes_whatever_the_argument_order(self):
        one = verdict.payload(**fields())
        two = verdict.payload(**dict(reversed(list(fields().items()))))
        self.assertEqual(one, two)
        self.assertFalse(one.endswith(b'\n'))

    def test_non_ascii_is_escaped_as_the_contract_s_json_dumps_does(self):
        # `json.dumps(obj, sort_keys=True, separators=(',', ':'))`, exactly:
        # ensure_ascii stays on, so the bytes are ASCII and a verifier that
        # re-serializes with the same call gets the same bytes.
        data = verdict.payload(**fields(argv=['echo', 'café']))
        self.assertIn(b'"caf\\u00e9"', data)
        data.decode('ascii')
        self.assertEqual(json.loads(data.decode('utf-8'))['argv'], ['echo', 'café'])


class ConditionsTest(unittest.TestCase):
    MATRIX = [
        # outcome, role, ready, tree -> reason
        ('passed', 'single', 'ready', TREE, None),
        ('command_failed', 'single', 'ready', TREE, 'not_passed'),
        ('infra_failed', 'shard', 'failed', None, 'not_passed'),
        ('passed', 'shard', 'ready', TREE, 'not_whole'),
        ('passed', 'parent', 'ready', TREE, 'not_whole'),
        ('passed', 'shard', 'failed', None, 'not_whole'),
        ('passed', 'single', 'unproven', TREE, 'worker_not_ready'),
        ('passed', 'single', 'drifted', None, 'worker_not_ready'),
        ('passed', 'single', 'ready', None, 'no_synthetic_git'),
        ('passed', None, 'ready', TREE, None),
    ]

    def test_the_first_failing_condition_names_the_skip(self):
        for outcome, role, ready, tree, want in self.MATRIX:
            with self.subTest(outcome=outcome, role=role, ready=ready, tree=tree):
                self.assertEqual(verdict.skip_reason(outcome=outcome, role=role,
                                                     ready=ready, tree=tree), want)

    def test_a_skipped_verdict_never_touches_the_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = {'role': 'single', 'argv': ['x'], 'input_id': 'i', 'job': 'j',
                   'repo': 'r', 'run_id': 'r1'}
            answer = verdict.decide(tmp, row, outcome='passed', tree=None, finished=1.0,
                                    golden='g', ready='ready')
            self.assertEqual(answer, {'tree': None, 'verdict': None,
                                      'verdict_skipped': 'no_synthetic_git'})
            self.assertFalse((Path(tmp) / 'keys').exists())

    def test_a_missing_ssh_keygen_is_a_skip_not_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = {'role': 'single', 'argv': ['x'], 'input_id': 'i', 'job': 'j',
                   'repo': 'r', 'run_id': 'r1'}
            with mock.patch.object(verdict.subprocess, 'run',
                                   side_effect=FileNotFoundError('ssh-keygen')):
                answer = verdict.decide(tmp, row, outcome='passed', tree=TREE,
                                        finished=1.0, golden='g', ready='ready')
        self.assertIsNone(answer['verdict'])
        self.assertEqual(answer['verdict_skipped'], 'sign_failed:ssh-keygen not found')
        self.assertEqual(answer['tree'], TREE)

    def test_an_unknown_golden_is_a_sign_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            answer = verdict.decide(tmp, {'role': 'single'}, outcome='passed', tree=TREE,
                                    finished=1.0, golden=None, ready='ready')
        self.assertEqual(answer['verdict_skipped'], 'sign_failed:golden fingerprint unknown')


class ReadyStateTest(unittest.TestCase):
    def write(self, root, **state):
        (Path(root) / 'worker').mkdir(parents=True, exist_ok=True)
        (Path(root) / 'worker' / 'state.json').write_text(json.dumps(state))

    def test_the_state_file_canary_mark_wrote_is_the_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(verdict.ready_state(tmp), 'unprovisioned')
            self.write(tmp, state='failed')
            self.assertEqual(verdict.ready_state(tmp), 'failed')
            self.write(tmp, state='ready')
            self.assertEqual(verdict.ready_state(tmp), 'ready')

    def test_a_different_kernel_is_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write(tmp, state='ready', kernel='0.0.0-canary')
            real = Path.read_text

            def read(path, *args, **kwargs):
                if str(path) == '/proc/sys/kernel/osrelease':
                    return '7.0.0-now\n'
                return real(path, *args, **kwargs)
            with mock.patch.object(Path, 'read_text', read):
                self.assertEqual(verdict.ready_state(tmp), 'drifted')

    def test_the_worker_root_comes_from_the_environment_or_home(self):
        with mock.patch.dict(os.environ, {'PANDORA_WORKER_ROOT': '/srv/w'}):
            self.assertEqual(verdict.worker_root(), Path('/srv/w'))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PANDORA_WORKER_ROOT', None)
            self.assertEqual(verdict.worker_root(), Path.home() / 'pandora')


class EngineIdTest(unittest.TestCase):
    def test_a_checkout_names_the_digest_its_bundle_would_have(self):
        from pandora.engine import bundle
        self.assertEqual(verdict.engine_id(), bundle.payload()[0])


@unittest.skipUnless(HAVE_KEYGEN, 'ssh-keygen is not installed')
class KeyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'engine'

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_reads_the_signer_and_never_creates_it(self):
        self.assertIsNone(verdict.signer(self.root))
        self.assertFalse((self.root / 'keys').exists())

    def test_the_key_is_generated_once_with_tight_permissions(self):
        key, line = verdict.ensure_key(self.root)
        self.assertEqual(key, self.root / 'keys' / 'verdict')
        self.assertEqual(stat.S_IMODE((self.root / 'keys').stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        self.assertRegex(line, r'^ssh-ed25519 AAAA\S+ pandora-verdict$')
        self.assertEqual(verdict.signer(self.root), line)
        again, same = verdict.ensure_key(self.root)
        self.assertEqual((again, same), (key, line))
        self.assertEqual(sorted(path.name for path in (self.root / 'keys').iterdir()),
                         ['.lock', 'verdict', 'verdict.pub'])

    def test_sign_and_verify_round_trip(self):
        data = verdict.payload(**fields())
        signed = verdict.sign(self.root, data)
        self.assertEqual(signed['payload'], data.decode())
        self.assertTrue(signed['signature'].startswith('-----BEGIN SSH SIGNATURE-----'))
        self.assertEqual(signed['signer'], verdict.signer(self.root))
        self.assertTrue(verdict.verify(signed['payload'], signed['signature'],
                                       signed['signer']))
        tampered = verdict.payload(**fields(tree='cd' * 20))
        self.assertFalse(verdict.verify(tampered, signed['signature'], signed['signer']))
        other = Path(self.tmp.name) / 'other'
        _, stranger = verdict.ensure_key(other)
        self.assertFalse(verdict.verify(data, signed['signature'], stranger))


class ResultFieldsTest(unittest.TestCase):
    """The three fields `write_result` adds, through a faked supervisor."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'engine'
        self.worker = Path(self.tmp.name) / 'worker-root'
        self.paths = runner.Paths(self.root).ensure()
        self.ledger = Ledger(self.paths.ledger)
        claim(self.ledger)
        attempt = self.paths.attempt('r1')
        attempt.mkdir(parents=True, exist_ok=True)
        (attempt / 'toolchain.json').write_text(json.dumps(PLAN['worker']))
        self.ledger.update('r1', state='admitted', reservation_mib=2048, ceiling_mib=4096,
                           cpus_hint=2)
        self.env = mock.patch.dict(os.environ, {'PANDORA_WORKER_ROOT': str(self.worker),
                                                'PANDORA_BUDGET_MIB': '8192'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.ledger.close()
        self.tmp.cleanup()

    def ready(self, state='ready'):
        (self.worker / 'worker').mkdir(parents=True, exist_ok=True)
        (self.worker / 'worker' / 'state.json').write_text(json.dumps({'state': state}))

    def request(self, git):
        (self.paths.attempt('r1') / 'request.json').write_text(json.dumps(
            {'plan': dict(PLAN, git=git), 'git_marks': {'untracked': [], 'ignored': []}}))

    def supervise(self, driver=None):
        return runner.supervise(self.root, 'r1', driver=driver or FakeDriver())

    @unittest.skipUnless(HAVE_KEYGEN, 'ssh-keygen is not installed')
    def test_a_whole_passing_run_on_a_ready_worker_is_signed(self):
        self.ready()
        self.request('synthetic')
        result = self.supervise()
        self.assertEqual(result['tree'], TREE)
        self.assertIsNone(result['verdict_skipped'])
        signed = result['verdict']
        self.assertTrue(verdict.verify(signed['payload'], signed['signature'],
                                       signed['signer']))
        body = json.loads(signed['payload'])
        self.assertEqual(body, {
            'argv': PLAN['argv'], 'engine': verdict.engine_id(),
            'finished': result['finished'], 'golden': runner.toolchain_of(
                PLAN['worker']).fingerprint(),
            'input_id': 'input-a', 'job': 'suite', 'kind': 'pandora-verdict',
            'outcome': 'passed', 'repo': 'demo', 'run_id': 'r1', 'tree': TREE, 'v': 1})
        self.assertEqual(signed['payload'].encode(), verdict.payload(**{
            key: body[key] for key in body if key not in ('kind', 'v')}))
        on_disk = json.loads(self.paths.result('r1').read_text())
        self.assertEqual(on_disk['verdict'], signed)
        self.assertEqual(self.ledger.get('r1')['finished'], result['finished'])

    def test_a_job_without_synthetic_git_has_no_tree(self):
        self.ready()
        self.request('none')
        result = self.supervise()
        self.assertEqual((result['tree'], result['verdict'], result['verdict_skipped']),
                         (None, None, 'no_synthetic_git'))

    def test_a_worker_that_is_not_ready_signs_nothing(self):
        self.ready('unproven')
        self.request('synthetic')
        result = self.supervise()
        self.assertEqual((result['tree'], result['verdict'], result['verdict_skipped']),
                         (TREE, None, 'worker_not_ready'))
        self.assertFalse((self.root / 'keys').exists())

    def test_a_failing_run_signs_nothing(self):
        self.ready()
        self.request('synthetic')
        result = self.supervise(FakeDriver(outcome='failed', exit_code=1))
        self.assertEqual(result['outcome'], 'command_failed')
        self.assertEqual((result['verdict'], result['verdict_skipped']), (None, 'not_passed'))

    def test_a_shard_signs_nothing(self):
        self.ready()
        self.request('synthetic')
        self.ledger.update('r1', role='shard')
        result = self.supervise()
        self.assertEqual((result['verdict'], result['verdict_skipped']), (None, 'not_whole'))

    def test_a_signing_failure_leaves_the_result_otherwise_unchanged(self):
        self.ready()
        self.request('synthetic')
        with mock.patch.object(verdict, 'sign', side_effect=verdict.SignFailed('boom')):
            result = self.supervise()
        self.assertEqual((result['outcome'], result['cli_exit']), ('passed', 0))
        self.assertEqual((result['verdict'], result['verdict_skipped']),
                         (None, 'sign_failed:boom'))


class StatusLineTest(unittest.TestCase):
    def test_status_prints_the_signer_or_says_there_is_none_yet(self):
        from pandora.worker.cli import render_status
        line = 'ssh-ed25519 AAAAC3Nz pandora-verdict'
        self.assertIn('verdict signer: ' + line,
                      render_status({'verdict_signer': line}).splitlines())
        self.assertIn('verdict signer: none yet (created on the first signed run)',
                      render_status({'verdict_signer': None}).splitlines())
        self.assertNotIn('verdict signer', render_status({}))


if __name__ == '__main__':
    unittest.main()
