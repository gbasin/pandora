"""Signed run verdicts: the payload, the key, the conditions, the result fields."""
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pandora.engine import runner, verdict
from pandora.engine.ledger import Ledger
from pandora.executor.interface import Golden
from pandora.worker import facts, versions
from pandora.tests.test_engine import PLAN, FakeDriver, claim

HAVE_KEYGEN = shutil.which('ssh-keygen') is not None
TREE = 'ab' * 20
KERNEL = '6.8.0-test'
MANIFEST = versions.normalize({'packages': {'incus': '6.0.5-8'}})


def versions_file(root):
    """Store MANIFEST where provisioning would, under worker root `root`."""
    path = facts.manifest_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(versions.render(MANIFEST))
    return path


def matching(kernel=KERNEL, unattended=False, **packages):
    """A quick survey of a host that matches MANIFEST, but for the overrides."""
    have = dict.fromkeys(MANIFEST['packages'], '1.0')
    have['incus'] = '6.0.5-8'
    have.update(packages)
    return {'packages': have, 'package_notes': {},
            'worker': {'unattended_upgrades': unattended}, 'host': {'kernel': kernel}}


def fields(**over):
    base = {'argv': ['python3', '-m', 'unittest', 'discover', '-s', 'pandora'],
            'cwd': '.', 'engine': 'e' * 64,
            'env_digest': verdict.env_digest({'NODE_ENV': 'test', 'CI': '1'}),
            'finished': 1791209006.46, 'golden': '0123456789abcdef',
            'golden_pins': {'image': 'a' * 64, 'lockfiles': {'uv.lock': 'b' * 64}},
            'input_id': 'f' * 64, 'job': 'suite', 'outcome': 'passed', 'repo': 'pandora',
            'run_id': 'r1', 'tree': TREE}
    base.update(over)
    return base


class PayloadTest(unittest.TestCase):
    def test_the_payload_is_canonical_json_bytes(self):
        self.assertEqual(
            verdict.payload(**fields()),
            ('{"argv":["python3","-m","unittest","discover","-s","pandora"],"cwd":".",'
             '"engine":"%s",'
             '"env_digest":"72fb0a5fd4a0f8aa51dbdb4267b749c588fb5d0047230d5dd5316201c1d0432c",'
             '"finished":1791209006.46,"golden":"0123456789abcdef",'
             '"golden_pins":{"image":"%s","lockfiles":{"uv.lock":"%s"}},'
             '"input_id":"%s","job":"suite","kind":"pandora-verdict","outcome":"passed",'
             '"repo":"pandora","run_id":"r1","tree":"%s","v":1}'
             % ('e' * 64, 'a' * 64, 'b' * 64, 'f' * 64, TREE)).encode())

    def test_the_env_digest_is_sha256_of_the_canonical_mapping(self):
        self.assertEqual(verdict.env_digest({}),
                         '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a')
        self.assertEqual(verdict.env_digest(None), verdict.env_digest({}))
        self.assertEqual(verdict.env_digest({'B': '2', 'A': '1'}),
                         verdict.env_digest({'A': '1', 'B': '2'}))
        self.assertNotEqual(verdict.env_digest({'A': '1'}), verdict.env_digest({'A': '2'}))

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
        ('passed', 'single', 'unprovisioned', None, 'worker_not_ready'),
        ('passed', 'single', 'ready', None, 'no_synthetic_git'),
        ('passed', None, 'ready', TREE, None),
    ]
    # outcome, role, ready, tree, drift -> reason
    DRIFTED = [
        ('passed', 'single', 'ready', TREE, 'package incus', 'worker_drifted'),
        ('passed', 'single', 'ready', None, 'package incus', 'no_synthetic_git'),
        ('passed', 'single', 'unproven', TREE, 'package incus', 'worker_not_ready'),
        ('passed', 'shard', 'ready', TREE, 'package incus', 'not_whole'),
        ('command_failed', 'single', 'ready', TREE, 'package incus', 'not_passed'),
        ('passed', 'single', 'ready', TREE, '', None),
    ]

    def test_the_first_failing_condition_names_the_skip(self):
        for outcome, role, ready, tree, want in self.MATRIX:
            with self.subTest(outcome=outcome, role=role, ready=ready, tree=tree):
                self.assertEqual(verdict.skip_reason(outcome=outcome, role=role,
                                                     ready=ready, tree=tree), want)

    def test_a_tree_that_failed_names_its_reason(self):
        self.assertEqual(verdict.skip_reason(outcome='passed', role='single', ready='ready',
                                             tree=None, tree_failed='lfs_pointers'),
                         'tree_failed:lfs_pointers')
        # The earlier conditions still come first.
        self.assertEqual(verdict.skip_reason(outcome='command_failed', role='single',
                                             ready='ready', tree=None,
                                             tree_failed='lfs_pointers'), 'not_passed')
        self.assertEqual(verdict.skip_reason(outcome='passed', role='single',
                                             ready='unproven', tree=None,
                                             tree_failed='lfs_pointers'), 'worker_not_ready')

    def test_drift_comes_after_ready_and_the_tree(self):
        for outcome, role, ready, tree, drift, want in self.DRIFTED:
            with self.subTest(outcome=outcome, role=role, ready=ready, tree=tree, drift=drift):
                self.assertEqual(verdict.skip_reason(outcome=outcome, role=role, ready=ready,
                                                     tree=tree, drift=drift), want)

    def test_a_drift_skip_logs_the_detail_and_never_touches_the_key(self):
        lines = []
        with tempfile.TemporaryDirectory() as tmp:
            answer = verdict.decide(tmp, {'role': 'single'}, outcome='passed', tree=TREE,
                                    finished=1.0, golden='g', ready='ready',
                                    drift='package incus: want 1, have 2 (version differs)',
                                    note=lines.append)
            self.assertFalse((Path(tmp) / 'keys').exists())
        self.assertEqual(answer, {'tree': TREE, 'verdict': None,
                                  'verdict_skipped': 'worker_drifted'})
        self.assertEqual(lines, ['verdict not signed: worker_drifted: '
                                 'package incus: want 1, have 2 (version differs)'])

    def test_a_job_without_synthetic_git_never_pays_for_the_survey(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(verdict, 'worker_drift',
                                  side_effect=AssertionError('surveyed')) as drift:
            answer = verdict.decide(tmp, {'role': 'single'}, outcome='passed', tree=None,
                                    finished=1.0, golden='g', ready='ready')
        self.assertEqual(answer['verdict_skipped'], 'no_synthetic_git')
        drift.assert_not_called()

    def test_a_note_that_raises_does_not_reach_the_run(self):
        def note(text):
            raise OSError('disk full')
        with tempfile.TemporaryDirectory() as tmp:
            answer = verdict.decide(tmp, {'role': 'single'}, outcome='passed', tree=TREE,
                                    finished=1.0, golden='g', ready='ready', drift='x',
                                    note=note)
        self.assertEqual(answer['verdict_skipped'], 'worker_drifted')

    def test_a_skipped_verdict_never_touches_the_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = {'role': 'single', 'argv': ['x'], 'input_id': 'i', 'job': 'j',
                   'repo': 'r', 'run_id': 'r1'}
            answer = verdict.decide(tmp, row, outcome='passed', tree=None, finished=1.0,
                                    golden='g', ready='ready', drift='')
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
                                        finished=1.0, golden='g', ready='ready', drift='')
        self.assertIsNone(answer['verdict'])
        self.assertEqual(answer['verdict_skipped'], 'sign_failed:ssh-keygen not found')
        self.assertEqual(answer['tree'], TREE)

    def test_an_unknown_golden_is_a_sign_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            answer = verdict.decide(tmp, {'role': 'single'}, outcome='passed', tree=TREE,
                                    finished=1.0, golden=None, ready='ready', drift='')
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

    def test_a_different_kernel_is_drift_not_unreadiness(self):
        # The kernel moved to the drift check with the rest of what `status`
        # compares; the ready state is the marker alone.
        with tempfile.TemporaryDirectory() as tmp:
            self.write(tmp, state='ready', kernel='0.0.0-canary')
            self.assertEqual(verdict.ready_state(tmp), 'ready')
            versions_file(tmp)
            with mock.patch.object(facts, 'quick_survey',
                                   return_value=matching(kernel='7.0.0-now')):
                detail = verdict.survey_drift(tmp)
        self.assertIn('host kernel: want 0.0.0-canary, have 7.0.0-now', detail)

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

    def test_concurrent_generation_yields_one_key(self):
        # Two supervisors finishing at once on a fresh engine root: one key,
        # and both callers read the same public line. Threads and processes.
        for round_ in range(3):
            root = Path(self.tmp.name) / ('threads-%d' % round_)
            barrier = threading.Barrier(2)
            lines = []

            def make():
                barrier.wait()
                lines.append(verdict.ensure_key(root)[1])
            workers = [threading.Thread(target=make) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0], lines[1])
            self.assertEqual(verdict.signer(root), lines[0])
            self.assertEqual(sorted(path.name for path in (root / 'keys').iterdir()),
                             ['.lock', 'verdict', 'verdict.pub'])
        root = Path(self.tmp.name) / 'processes'
        script = ('import sys, time; from pandora.engine import verdict; '
                  'time.sleep(max(0, float(sys.argv[2]) - time.time())); '
                  'print(verdict.ensure_key(sys.argv[1])[1])')
        start = str(time.time() + 1.0)
        top = str(Path(__file__).resolve().parents[2])
        procs = [subprocess.Popen([sys.executable, '-c', script, str(root), start],
                                  cwd=top, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for _ in range(2)]
        outs = [proc.communicate(timeout=60) for proc in procs]
        self.assertEqual([proc.returncode for proc in procs], [0, 0], outs)
        lines = [out.decode().strip() for out, _ in outs]
        self.assertEqual(lines[0], lines[1])
        self.assertEqual(verdict.signer(root), lines[0])
        self.assertEqual(sorted(path.name for path in (root / 'keys').iterdir()),
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


class PinningDriver(FakeDriver):
    """A driver that resolves the base image and records the golden it launched."""

    def __init__(self, image='f00d' * 16, built=(), **kw):
        super().__init__(**kw)
        self.image, self.built, self.lookups, self.launched = image, set(built), [], []

    def image_fingerprint(self, alias):
        self.lookups.append(alias)
        return self.image

    def exists(self, name):
        return name in self.built

    def prepare(self, toolchain, source=None, log=print):
        name = 'golden-' + toolchain.fingerprint()
        self.launched.append((name, dict(toolchain.pins)))
        self.built.add(name)
        return Golden(name=name, fingerprint=toolchain.fingerprint(), snapshot='warm')


class PinnedVerdictTest(unittest.TestCase):
    """The golden `prepare` launched is the golden the verdict names."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'engine'
        self.worker = Path(self.tmp.name) / 'worker-root'
        self.paths = runner.Paths(self.root).ensure()
        self.source = self.root / 'src' / 'demo' / 'input-a'
        self.source.mkdir(parents=True)
        (self.source / 'uv.lock').write_text('locked\n')
        (self.source / 'requirements-dev.txt').write_text('pytest\n')
        self.ledger = Ledger(self.paths.ledger)
        claim(self.ledger, source_path=str(self.source))
        attempt = self.paths.attempt('r1')
        attempt.mkdir(parents=True, exist_ok=True)
        (attempt / 'toolchain.json').write_text(json.dumps(PLAN['worker']))
        (attempt / 'request.json').write_text(json.dumps(
            {'plan': dict(PLAN, git='synthetic'), 'git_marks': {'untracked': [], 'ignored': []}}))
        self.ledger.update('r1', state='admitted', reservation_mib=2048, ceiling_mib=4096,
                           cpus_hint=2)
        # A ready, undrifted worker, so the run is signed.
        (self.worker / 'worker').mkdir(parents=True)
        (self.worker / 'worker' / 'state.json').write_text(json.dumps(
            {'state': 'ready', 'kernel': KERNEL}))
        versions_file(self.worker)
        survey = mock.patch.object(facts, 'quick_survey', return_value=matching())
        survey.start()
        self.addCleanup(survey.stop)
        self.env = mock.patch.dict(os.environ, {'PANDORA_WORKER_ROOT': str(self.worker),
                                                'PANDORA_BUDGET_MIB': '8192'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.ledger.close()
        self.tmp.cleanup()

    @unittest.skipUnless(HAVE_KEYGEN, 'ssh-keygen is not installed')
    def test_the_launched_golden_is_the_signed_golden(self):
        import hashlib
        driver = PinningDriver()
        result = runner.supervise(self.root, 'r1', driver=driver)
        body = json.loads(result['verdict']['payload'])
        [(launched, pins)] = driver.launched
        self.assertEqual('golden-' + body['golden'], launched)
        self.assertNotEqual(body['golden'], runner.toolchain_of(PLAN['worker']).fingerprint())
        self.assertEqual(len(body['golden']), 16)
        self.assertEqual(body['golden_pins'], {
            'image': 'f00d' * 16,
            'lockfiles': {'requirements-dev.txt': hashlib.sha256(b'pytest\n').hexdigest(),
                          'uv.lock': hashlib.sha256(b'locked\n').hexdigest()}})
        self.assertEqual(pins['base_image'], 'f00d' * 16)
        # The resolved table is on disk, so every later reader names one golden.
        on_disk = json.loads((self.paths.attempt('r1') / 'toolchain.json').read_text())
        self.assertEqual(runner.golden_of(self.paths, 'r1'), body['golden'])
        self.assertEqual(on_disk['pins']['base_image'], 'f00d' * 16)

    def test_a_resolved_toolchain_is_not_resolved_again(self):
        driver = PinningDriver()
        runner.settle_toolchain(self.paths, 'r1', str(self.source), driver)
        driver.image = 'beef' * 16
        again = runner.settle_toolchain(self.paths, 'r1', str(self.source), driver)
        self.assertEqual(again['pins']['base_image'], 'f00d' * 16)
        self.assertEqual(driver.lookups, [PLAN['worker']['base_image']])


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

    def ready(self, state='ready', observed=None, manifest=True):
        (self.worker / 'worker').mkdir(parents=True, exist_ok=True)
        (self.worker / 'worker' / 'state.json').write_text(json.dumps(
            {'state': state, 'kernel': KERNEL}))
        if manifest:
            versions_file(self.worker)
        survey = mock.patch.object(facts, 'quick_survey',
                                   return_value=observed or matching())
        survey.start()
        self.addCleanup(survey.stop)

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
            # No image lookup on this driver and no source tree: resolved to
            # nothing, which names the recipe's own golden.
            'golden_pins': {'image': None, 'lockfiles': {}},
            'input_id': 'input-a', 'job': 'suite', 'kind': 'pandora-verdict',
            'outcome': 'passed', 'repo': 'demo', 'run_id': 'r1', 'tree': TREE, 'v': 1,
            'cwd': '.', 'env_digest': verdict.env_digest({})})
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

    def test_a_drifted_worker_signs_nothing_and_logs_why(self):
        self.ready(observed=matching(incus='6.0.6-1'))
        self.request('synthetic')
        result = self.supervise()
        self.assertEqual((result['outcome'], result['cli_exit']), ('passed', 0))
        self.assertEqual((result['tree'], result['verdict'], result['verdict_skipped']),
                         (TREE, None, 'worker_drifted'))
        self.assertIn('pandora: verdict not signed: worker_drifted: package incus: '
                      'want 6.0.5-8, have 6.0.6-1 (version differs)\n',
                      self.paths.log('r1').read_text())
        self.assertFalse((self.root / 'keys' / 'verdict').exists())

    def test_an_unreadable_manifest_fails_closed(self):
        self.ready(manifest=False)
        self.request('synthetic')
        result = self.supervise()
        self.assertEqual(result['verdict_skipped'], 'worker_drifted')
        self.assertIn('worker_drifted: manifest unreadable: no versions manifest at ',
                      self.paths.log('r1').read_text())
        (self.worker / 'worker' / 'versions.toml').write_text('[packages\n')
        self.assertTrue(verdict.survey_drift(self.worker).startswith(
            'manifest unreadable: '))

    def test_an_unreadable_dpkg_fails_closed(self):
        self.ready()
        self.request('synthetic')
        with mock.patch.object(facts, 'quick_survey',
                               side_effect=facts.Unreadable('dpkg-query not found')):
            result = self.supervise()
        self.assertEqual(result['verdict_skipped'], 'worker_drifted')
        self.assertIn('worker_drifted: dpkg unreadable: dpkg-query not found',
                      self.paths.log('r1').read_text())

    def test_a_drift_check_that_throws_fails_closed_without_reaching_the_run(self):
        self.ready()
        self.request('synthetic')
        with mock.patch.object(facts, 'quick_survey', side_effect=RuntimeError('boom')):
            result = self.supervise()
        self.assertEqual((result['outcome'], result['verdict_skipped']),
                         ('passed', 'worker_drifted'))
        self.assertIn('worker_drifted: drift check failed: RuntimeError',
                      self.paths.log('r1').read_text())

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


class DriftCacheTest(unittest.TestCase):
    """One survey per minute per engine root, whichever supervisor asks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = Path(self.tmp.name) / 'engine'
        self.worker = Path(self.tmp.name) / 'worker'
        (self.worker / 'worker').mkdir(parents=True)
        (self.worker / 'worker' / 'state.json').write_text(json.dumps(
            {'state': 'ready', 'kernel': KERNEL}))
        versions_file(self.worker)
        self.now = 1000.0
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def survey(self, root):
        self.calls.append(root)
        return 'package incus: drifted %d' % len(self.calls)

    def ask(self):
        return verdict.worker_drift(self.engine, self.worker, clock=lambda: self.now,
                                    survey=self.survey)

    def test_the_cache_dedupes_within_the_ttl_and_expires_after(self):
        self.assertEqual(self.ask(), 'package incus: drifted 1')
        self.now += 59.0
        self.assertEqual(self.ask(), 'package incus: drifted 1')
        self.assertEqual(len(self.calls), 1)
        self.now += 2.0
        self.assertEqual(self.ask(), 'package incus: drifted 2')
        self.assertEqual(len(self.calls), 2)
        cache = verdict.drift_cache_path(self.engine)
        self.assertEqual(cache.parent, self.engine / 'keys')
        self.assertEqual(stat.S_IMODE(cache.parent.stat().st_mode), 0o700)
        self.assertEqual(sorted(path.name for path in cache.parent.iterdir()),
                         ['drift.json'])

    def test_a_clean_answer_is_cached_too(self):
        with mock.patch.object(facts, 'quick_survey', return_value=matching()) as survey:
            for _ in range(3):
                self.assertEqual(verdict.worker_drift(self.engine, self.worker,
                                                      clock=lambda: self.now), '')
        self.assertEqual(survey.call_count, 1)

    def test_the_cache_is_shared_across_processes(self):
        # Every supervisor is its own process; the file is what they share.
        self.ask()
        script = ('import sys, json; from pandora.engine import verdict; '
                  'print(json.dumps(verdict.worker_drift(sys.argv[1], sys.argv[2], '
                  'clock=lambda: %r, survey=lambda root: "fresh")))' % (self.now + 30))
        top = str(Path(__file__).resolve().parents[2])
        out = subprocess.run([sys.executable, '-c', script, str(self.engine),
                              str(self.worker)], cwd=top, stdout=subprocess.PIPE,
                             timeout=60, check=True).stdout
        self.assertEqual(json.loads(out), 'package incus: drifted 1')

    def test_a_new_canary_or_manifest_starts_afresh(self):
        self.ask()
        state = self.worker / 'worker' / 'state.json'
        state.write_text(json.dumps({'state': 'ready', 'kernel': KERNEL, 'at': 2}))
        os.utime(state, ns=(1, 1))
        self.assertEqual(self.ask(), 'package incus: drifted 2')
        manifest = facts.manifest_path(self.worker)
        manifest.write_text(manifest.read_text() + '\n')
        self.assertEqual(self.ask(), 'package incus: drifted 3')

    def test_a_failed_survey_is_cached_too(self):
        def boom(root):
            self.calls.append(root)
            raise RuntimeError('hung')
        for _ in range(3):
            self.assertEqual(verdict.worker_drift(self.engine, self.worker,
                                                  clock=lambda: self.now, survey=boom),
                             'drift check failed: RuntimeError')
        self.assertEqual(len(self.calls), 1)

    def test_a_hung_dpkg_costs_one_run_not_every_run(self):
        folder = Path(self.tmp.name) / 'bin'
        folder.mkdir()
        calls = Path(self.tmp.name) / 'calls'
        fake = folder / 'dpkg-query'
        fake.write_text('#!/bin/sh\necho call >> %s\nexec sleep 30\n' % calls)
        fake.chmod(0o755)
        (folder / 'sleep').symlink_to(shutil.which('sleep'))
        started = time.monotonic()
        with mock.patch.dict(os.environ, {'PATH': str(folder)}), \
                mock.patch.object(facts, 'QUICK_TIMEOUT', 0.5):
            for _ in range(3):
                detail = verdict.worker_drift(self.engine, self.worker,
                                              clock=lambda: self.now)
                self.assertEqual(detail, 'dpkg unreadable: dpkg-query timed out after 0.5s')
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(calls.read_text().splitlines(), ['call'])

    def test_each_worker_root_has_its_own_answer(self):
        other = Path(self.tmp.name) / 'other'
        (other / 'worker').mkdir(parents=True)
        shutil.copy(self.worker / 'worker' / 'state.json', other / 'worker' / 'state.json')
        shutil.copy(facts.manifest_path(self.worker), facts.manifest_path(other))
        os.utime(other / 'worker' / 'state.json',
                 ns=((self.worker / 'worker' / 'state.json').stat().st_mtime_ns,) * 2)
        os.utime(facts.manifest_path(other),
                 ns=(facts.manifest_path(self.worker).stat().st_mtime_ns,) * 2)
        self.assertEqual(self.ask(), 'package incus: drifted 1')
        self.assertEqual(verdict.worker_drift(self.engine, other, clock=lambda: self.now,
                                              survey=self.survey),
                         'package incus: drifted 2')
        self.assertEqual(self.calls, [self.worker, other])

    def test_writing_the_cache_sweeps_stale_temp_files(self):
        keys = verdict.drift_cache_path(self.engine).parent
        keys.mkdir(parents=True)
        stale, young = keys / '.drift-stale', keys / '.drift-young'
        stale.write_text('{')
        young.write_text('{')
        old = time.time() - 2 * verdict.DRIFT_TTL
        os.utime(stale, (old, old))
        (keys / 'verdict').write_text('not a drift file')
        os.utime(keys / 'verdict', (old, old))
        self.ask()
        self.assertEqual(sorted(path.name for path in keys.iterdir()),
                         ['.drift-young', 'drift.json', 'verdict'])

    def test_a_clock_that_went_backward_does_not_trust_the_cache(self):
        self.ask()
        self.now -= 5.0
        self.ask()
        self.assertEqual(len(self.calls), 2)

    def test_a_corrupt_or_unwritable_cache_only_costs_a_survey(self):
        cache = verdict.drift_cache_path(self.engine)
        cache.parent.mkdir(parents=True)
        cache.write_text('{not json')
        self.assertEqual(self.ask(), 'package incus: drifted 1')
        with mock.patch.object(verdict.tempfile, 'mkstemp', side_effect=OSError('ro')):
            self.now += 120
            self.assertEqual(self.ask(), 'package incus: drifted 2')


class StatusAgreementTest(unittest.TestCase):
    """`pandora worker status` and the signing check read the same fake facts
    the same way: `drifted` exactly when signing refuses."""

    CASES = [
        ('clean', {}, False),
        ('pinned version differs', {'incus': '6.0.6-1'}, True),
        ('star package missing', {'git': None}, True),
        ('kernel differs', {'kernel': '7.0.0-other'}, True),
        ('setting differs', {'unattended': True}, True),
    ]

    def status(self, root, engine, observed):
        from pandora.worker import service

        class Driver:
            def pool_usage(self):
                return {}

            def capacity(self, floor_gib):
                return {'ok': True}

            def instances(self):
                return []
        full = dict(observed, missing={},
                    host=dict(observed['host'], hostname='w', cores=1))
        out = io.StringIO()
        with mock.patch.object(facts, 'survey', return_value=full), \
                mock.patch.object(service, 'driver_for', return_value=Driver()), \
                mock.patch.object(service.goldens, 'index', return_value=[]), \
                mock.patch.object(sys, 'stdout', out):
            service.main(['--root', str(root), '--engine-root', str(engine), 'status'])
        return json.loads(out.getvalue())

    def test_status_and_signing_agree(self):
        for name, over, drifted in self.CASES:
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                root, engine = Path(tmp) / 'worker', Path(tmp) / 'engine'
                (root / 'worker').mkdir(parents=True)
                (root / 'worker' / 'state.json').write_text(json.dumps(
                    {'state': 'ready', 'kernel': KERNEL}))
                versions_file(root)
                kwargs = {key: over[key] for key in ('kernel', 'unattended') if key in over}
                observed = matching(**kwargs, **{key: value for key, value in over.items()
                                                 if key not in kwargs})
                status = self.status(root, engine, observed)
                with mock.patch.object(facts, 'quick_survey', return_value=observed):
                    detail = verdict.worker_drift(engine, root)
                self.assertEqual(status['state'], 'drifted' if drifted else 'ready')
                self.assertEqual(bool(detail), drifted, detail)
                self.assertEqual(detail, verdict.describe_drift(status['drift']))


class NoStoredManifestTest(StatusAgreementTest):
    """No manifest at `<root>/worker/versions.toml`: signing refuses, so status
    reads `drifted` and says why."""

    CASES = []

    def test_status_is_drifted_and_signing_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, engine = Path(tmp) / 'worker', Path(tmp) / 'engine'
            (root / 'worker').mkdir(parents=True)
            (root / 'worker' / 'state.json').write_text(json.dumps(
                {'state': 'ready', 'kernel': KERNEL}))
            defaults = versions.load(None)
            observed = {'packages': dict(defaults['packages']), 'package_notes': {},
                        'worker': dict(defaults['worker']), 'host': {'kernel': KERNEL}}
            status = self.status(root, engine, observed)
            detail = verdict.worker_drift(engine, root)
        self.assertEqual(status['state'], 'drifted')
        self.assertFalse(status['ok'])
        self.assertEqual(status['drift'], [{'kind': 'object', 'name': 'manifest',
                                            'want': 'present', 'have': None,
                                            'detail': 'not stored'}])
        self.assertTrue(detail.startswith('manifest unreadable: no versions manifest'),
                        detail)


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
