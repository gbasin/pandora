"""`pandora selftest`: the pieces that need no worker, and the one that does.

Everything except `LiveRun` is a unit test: the scratch `config.toml` and
`pandora.toml` are built here and read back through the real loaders, the
isolation guard refuses the live state directory, and the toolchain choice is
decided against a stub SSH link. `LiveRun` is the real submission against the
production worker; it runs only when PANDORA_SELFTEST_LIVE=1 is set, so CI and
`python3 -m unittest discover -s pandora` never pay an incus run by accident.
"""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from pandora import cli
from pandora.client import selftest, settings, verdicts
from pandora.config import loader
from pandora.engine import pinning
from pandora.engine.runner import toolchain_of
from pandora.exits import INFRA
from pandora.tests.test_cli import capture, _exit_code
from pandora.tests.test_fallback import DaemonCase, FakeWorker
from pandora.tests.test_verdicts import make_repo, verdict_threads

HAS_SSH_KEYGEN = shutil.which('ssh-keygen') is not None

WORKER_SPEC = {'base_image': 'images:ubuntu/26.04',
               'packages': ['docker.io', 'ca-certificates'],
               'node_version': '24.9.0', 'pnpm_version': '',
               'service_images': [], 'install_command': 'true',
               'prepare_command': 'echo build', 'source_id': 'acme',
               'env': {'CI': 'true'}, 'workdir': '/work'}


class Scratch(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.root = Path(home.name)


class ClientConfig(Scratch):
    def test_the_scratch_config_loads(self):
        path = self.root / 'config.toml'
        selftest.write_client_config(path, host='ubuntu@1.2.3.4',
                                     engine_root='pandora-engine',
                                     state=self.root / 'state', name='e2e-box')
        config = settings.load(path)
        self.assertEqual(config['worker']['host'], 'ubuntu@1.2.3.4')
        self.assertEqual(config['client']['state'], str(self.root / 'state'))
        self.assertEqual(config['client']['name'], 'e2e-box')
        self.assertFalse(config['notify']['enabled'])
        self.assertEqual(config['repos'], [])


class RepoToml(Scratch):
    def load(self, spec=None):
        (self.root / 'pandora.toml').write_text(
            selftest.repo_toml(spec or selftest.MINIMAL_WORKER))
        return loader.load(self.root / 'pandora.toml')

    def test_the_file_loads_and_claims_selftest(self):
        config = self.load()
        self.assertEqual(config['repo']['name'], 'pandora-selftest')
        self.assertIn('selftest', config['jobs'])
        self.assertEqual(config['jobs']['selftest']['forms'][0]['prefix'], ['selftest'])

    def test_the_job_asks_for_a_tree_and_publishes_to_origin(self):
        config = self.load()
        self.assertEqual(config['jobs']['selftest']['git'], 'synthetic')
        self.assertEqual(config['verdicts'], {'publish': True, 'remote': 'origin'})
        queued = loader.validate(tomllib.loads(selftest.repo_toml(selftest.MINIMAL_WORKER,
                                                                  queue=True)))
        self.assertEqual(queued['verdicts']['publish'], True)

    def test_the_claimed_forms_and_policies(self):
        from pandora.config import classify
        config = self.load()
        self.assertIn(['selftest'], classify.claim_index(config))
        self.assertEqual(classify.policy_index(config)[0]['size'], 'small')

    def test_plain_and_update_invocations_classify_remote(self):
        from pandora.config import classify
        config = self.load()
        plain = classify.classify(config, ['pnpm', 'selftest'])
        self.assertEqual(plain['decision'], 'remote')
        self.assertEqual(plain['plan']['argv'], ['sh', 'selftest.sh'])
        self.assertEqual(plain['plan']['outputs'], [])
        update = classify.classify(config, ['pnpm', 'selftest', '--update'])
        self.assertEqual(update['decision'], 'remote')
        self.assertEqual(update['plan']['argv'], ['sh', 'selftest.sh', '--update'])
        kinds = [output['kind'] for output in update['plan']['outputs']]
        self.assertEqual(kinds, ['writeback'])
        self.assertTrue(update['plan']['writeback'])

    def test_a_borrowed_toolchain_keeps_its_fingerprint(self):
        """Dropping `prepare_command` must not change the golden's name."""
        borrowed = self.load(dict(WORKER_SPEC, prepare_command=''))
        self.assertEqual(toolchain_of(borrowed['worker']).fingerprint(),
                         toolchain_of(WORKER_SPEC).fingerprint())

    def test_env_table_round_trips(self):
        config = self.load(dict(WORKER_SPEC))
        self.assertEqual(config['worker']['env'], {'CI': 'true'})
        self.assertEqual(config['worker']['prepare_command'], 'echo build')


class RepoFiles(Scratch):
    def test_write_repo_makes_a_git_repository(self):
        repo = self.root / 'repo'
        selftest.write_repo(repo, selftest.MINIMAL_WORKER)
        self.assertTrue((repo / '.git').is_dir())
        self.assertTrue((repo / 'selftest.sh').is_file())
        self.assertTrue((repo / 'pandora.toml').is_file())
        self.assertIn('pandora selftest ran on the worker',
                      (repo / 'selftest.sh').read_text())

    def test_the_origin_is_a_bare_repository_in_the_scratch(self):
        repo = self.root / 'repo'
        selftest.write_repo(repo, selftest.MINIMAL_WORKER)
        bare = selftest.write_origin(self.root, repo)
        self.assertEqual(bare, self.root / 'origin.git')
        self.assertEqual(subprocess.run(['git', '--git-dir', str(bare), 'rev-parse',
                                         '--is-bare-repository'], capture_output=True,
                                        text=True).stdout.strip(), 'true')
        self.assertEqual(subprocess.run(['git', '-C', str(repo), 'remote', 'get-url',
                                         'origin'], capture_output=True,
                                        text=True).stdout.strip(), str(bare))


class Isolation(Scratch):
    def test_the_default_state_directory_is_refused(self):
        with self.assertRaises(selftest.SelftestError) as caught:
            selftest.check_state_dir(str(settings.DEFAULT_STATE), {})
        self.assertEqual(caught.exception.exit, INFRA)

    def test_the_live_configured_state_is_refused(self):
        real = {'client': {'state': str(self.root / 'live')}}
        with self.assertRaises(selftest.SelftestError):
            selftest.check_state_dir(str(self.root / 'live'), real)

    def test_another_directory_is_honored_and_none_means_mkdtemp(self):
        real = {'client': {'state': str(self.root / 'live')}}
        self.assertEqual(selftest.check_state_dir(str(self.root / 'other'), real),
                         (self.root / 'other').resolve())
        self.assertIsNone(selftest.check_state_dir(None, real))

    def test_no_worker_host_exits_70(self):
        real = self.root / 'config.toml'
        real.write_text('[worker]\nhost = ""\n')
        with self.assertRaises(selftest.SelftestError) as caught:
            selftest.run(config_path=str(real))
        self.assertEqual(caught.exception.exit, INFRA)
        self.assertIn('no worker host', str(caught.exception))


class Names(Scratch):
    def test_the_client_name_is_honest_and_valid(self):
        self.assertEqual(selftest.e2e_name('garys-studio.example.com'),
                         'e2e-garys-studio')
        self.assertTrue(settings.CLIENT_NAME.fullmatch(selftest.e2e_name('x')))
        self.assertEqual(selftest.e2e_name('a host!'), 'e2e-a-host-')


class ToolchainChoice(Scratch):
    """The worker names the golden; the selftest asks it with its lockfiles."""

    @staticmethod
    def asker(warm=()):
        asked = []

        def ask(spec, lockfiles):
            asked.append((spec, lockfiles))
            name = 'golden-' + toolchain_of(dict(spec, pins={
                'lockfile:' + key: value for key, value in lockfiles.items()})).fingerprint()
            return {'golden': name, 'warm': name in warm}
        return ask, asked

    def test_a_warm_borrowed_golden_wins(self):
        spec = dict(WORKER_SPEC, prepare_command='')
        acme = self.root / 'acme'
        acme.mkdir()
        (acme / 'pnpm-lock.yaml').write_text('lock\n')
        digests = {'pnpm-lock.yaml': hashlib.sha256(b'lock\n').hexdigest()}
        warm = 'golden-' + toolchain_of(dict(spec, pins={
            'lockfile:pnpm-lock.yaml': digests['pnpm-lock.yaml']})).fingerprint()
        ask, asked = self.asker(warm=[warm])
        chosen, label, reused, lockroot = selftest.choose_toolchain(
            [('acme', spec, str(acme))], ask, say=lambda text: None)
        self.assertTrue(reused)
        self.assertIn('borrowed from acme', label)
        self.assertEqual(chosen['prepare_command'], '')
        self.assertEqual(asked[0][1], digests)
        self.assertEqual(lockroot, str(acme))
        repo = self.root / 'repo'
        selftest.write_repo(repo, chosen)
        selftest.copy_lockfiles(lockroot, repo)
        self.assertEqual(pinning.lockfiles(repo), digests)

    def test_no_warm_golden_falls_to_the_minimal_toolchain(self):
        ask, _ = self.asker()
        spec, label, reused, lockroot = selftest.choose_toolchain(
            [('acme', dict(WORKER_SPEC), str(self.root))], ask, say=lambda text: None)
        self.assertFalse(reused)
        self.assertIn('minimal', label)
        self.assertEqual(spec['source_id'], 'pandora-selftest')
        self.assertIsNone(lockroot)

    def test_a_refused_golden_verb_falls_to_the_minimal_toolchain(self):
        from pandora.errors import EngineError

        def refused(spec, lockfiles):
            raise EngineError('engine golden failed (2): invalid choice')
        notes = []
        spec, label, reused, lockroot = selftest.choose_toolchain(
            [('acme', dict(WORKER_SPEC), str(self.root))], refused, say=notes.append)
        self.assertFalse(reused)
        self.assertEqual(spec['source_id'], 'pandora-selftest')
        self.assertIsNone(lockroot)
        self.assertTrue(any('not warm' in line for line in notes), notes)

    def test_an_ok_false_golden_answer_is_not_warm(self):
        def refuses(spec, lockfiles):
            return {'ok': False, 'code': 'bad-request', 'detail': 'no', 'warm': True}
        _, label, reused, _ = selftest.choose_toolchain(
            [('acme', dict(WORKER_SPEC), str(self.root))], refuses, say=lambda text: None)
        self.assertFalse(reused)
        self.assertIn('minimal', label)

    def test_an_unreachable_worker_still_raises(self):
        from pandora.errors import WorkerUnreachable

        def gone(spec, lockfiles):
            raise WorkerUnreachable('ssh: no route')
        with self.assertRaises(WorkerUnreachable):
            selftest.choose_toolchain([('acme', dict(WORKER_SPEC), str(self.root))], gone,
                                      say=lambda text: None)

    def test_borrowed_toolchains_skip_an_unreadable_repo(self):
        repo = self.root / 'repo'
        repo.mkdir()
        (repo / 'pandora.toml').write_text('version = 1\n[repo]\nname = "x"\n'
                                          'entrypoints = ["pnpm"]\n[worker]\n'
                                          'base_image = "images:x"\n[[jobs]]\n'
                                          'id = "j"\nforms = [{prefix = ["j"]}]\n'
                                          'run = {argv = ["true"]}\n')
        notes = []
        found = selftest.borrowed_toolchains(
            [{'name': 'gone', 'root': str(self.root / 'gone'), 'config': ''},
             {'name': 'x', 'root': str(repo), 'config': ''}], say=notes.append)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0], 'x')
        self.assertTrue(any('gone' in line for line in notes))


class CleanEnv(Scratch):
    def test_pandora_variables_are_dropped(self):
        env = selftest.clean_env({'PATH': '/bin', 'PANDORA_OFF': '1',
                                  'PANDORA_CONFIG': '/x', 'HOME': '/h'})
        self.assertEqual(env, {'PATH': '/bin', 'HOME': '/h'})


class Receipts(Scratch):
    def rows(self, argv, code=0):
        run = self.root / 'state' / 'runs' / 'r1'
        run.mkdir(parents=True)
        (run / 'meta.json').write_text(json.dumps(
            {'id': 'r1', 'argv': ['pnpm'] + argv, 'state': 'finished',
             'pre_accept': {'freeze': 0.1, 'ship': 0.2, 'submit': 0.3},
             'queue_ms': 900, 'started': 1}))
        if code is not None:
            (run / 'result.json').write_text(json.dumps(
                {'outcome': 'passed', 'cli_exit': code, 'lane': 'remote',
                 'durations': {'clone': 0.1, 'execute': 0.2, 'destroy': 0.3}}))

    def test_receipt_finds_the_run_and_its_result(self):
        self.rows(['selftest'])
        meta, result = selftest.receipt(self.root / 'state', ['selftest'])
        self.assertEqual(meta['id'], 'r1')
        self.assertEqual(result['outcome'], 'passed')

    def test_receipt_raises_without_the_run(self):
        with self.assertRaises(selftest.SelftestError) as caught:
            selftest.receipt(self.root / 'state', ['selftest'], wait=0)
        self.assertEqual(caught.exception.exit, 1)

    def test_the_report_renders_phases(self):
        self.rows(['selftest'])
        meta, result = selftest.receipt(self.root / 'state', ['selftest'])
        record = selftest.run_report(meta, result, 3.5)
        text = selftest.phases_line(record)
        for needle in ('freeze 0.1s', 'ship 0.2s', 'submit 0.3s', '900 ms',
                       'clone 0.1s', 'execute 0.2s', 'destroy 0.3s'):
            self.assertIn(needle, text)


def new_key(path):
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C',
                    'pandora-verdict', '-f', str(path)], check=True, capture_output=True)
    return Path(str(path) + '.pub').read_text().strip()


def sign(key, body):
    payload = json.dumps(body, sort_keys=True, separators=(',', ':'))
    proc = subprocess.run(['ssh-keygen', '-Y', 'sign', '-f', str(key), '-n', 'pandora-verdict'],
                          input=payload.encode(), check=True, capture_output=True)
    return payload, proc.stdout.decode()


@unittest.skipUnless(HAS_SSH_KEYGEN, 'ssh-keygen is not installed')
class VerdictKey(Scratch):
    """A real key and receipts it signed, for the verdict checks below."""

    TREE = 'c' * 40

    def setUp(self):
        super().setUp()
        key = self.root / 'verdict'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C',
                        'pandora-verdict', '-f', str(key)], check=True, capture_output=True)
        self.key = key
        self.signer = (self.root / 'verdict.pub').read_text().strip()
        self.said = []

    def sign(self, body, key=None):
        payload = json.dumps(body, sort_keys=True, separators=(',', ':'))
        proc = subprocess.run(['ssh-keygen', '-Y', 'sign', '-f', str(key or self.key),
                               '-n', 'pandora-verdict'],
                              input=payload.encode(), check=True, capture_output=True)
        return payload, proc.stdout.decode()

    def body(self, **changes):
        body = {'argv': ['sh', 'selftest.sh'], 'engine': 'e', 'finished': 1.0,
                'golden': '0' * 16, 'input_id': 'f' * 64, 'job': 'selftest',
                'kind': 'pandora-verdict', 'outcome': 'passed', 'repo': 'pandora-selftest',
                'run_id': 'r1', 'tree': self.TREE, 'v': 1}
        body.update(changes)
        return body

    def result(self, payload=None, signature=None, signer=None, **changes):
        if payload is None:
            payload, signature = self.sign(self.body(**changes))
        return {'outcome': 'passed', 'tree': self.TREE, 'verdict_skipped': None,
                'verdict': {'payload': payload, 'signature': signature,
                            'signer': signer or self.signer}}


class Verdicts(VerdictKey):
    """The receipt's signed verdict, checked with a real key and `ssh-keygen -Y`."""

    def check(self, result):
        return selftest.check_verdict(result, 'selftest', say=self.said.append)

    def refused(self, result, needle):
        with self.assertRaises(selftest.SelftestError) as caught:
            self.check(result)
        self.assertEqual(caught.exception.exit, 1)
        self.assertIn(needle, str(caught.exception))

    def test_a_good_signature_verifies(self):
        self.assertEqual(self.check(self.result()), 'signed, tree cccccccccccc')
        self.assertEqual(self.said, [])

    def test_a_tampered_payload_or_another_signer_fails(self):
        good = self.result()
        tampered = dict(good, verdict=dict(good['verdict'],
                                           payload=good['verdict']['payload'].replace(
                                               '"passed"', '"failed"')))
        self.refused(tampered, 'does not verify')
        other = self.root / 'other'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(other)],
                       check=True, capture_output=True)
        stranger = (self.root / 'other.pub').read_text().strip()
        self.refused(dict(good, verdict=dict(good['verdict'], signer=stranger)),
                     'does not verify')

    def test_a_signed_payload_that_disagrees_with_the_run_fails(self):
        self.refused(self.result(job='other'), 'disagrees with the run on job')
        self.refused(self.result(tree='d' * 40), 'disagrees with the run on tree')

    def test_an_unready_or_drifted_worker_are_the_allowed_skips(self):
        result = {'outcome': 'passed', 'tree': self.TREE, 'verdict': None,
                  'verdict_skipped': 'worker_not_ready'}
        self.assertEqual(self.check(result), 'none (worker_not_ready)')
        self.assertTrue(self.said)
        self.assertEqual(self.check(dict(result, verdict_skipped='worker_drifted')),
                         'none (worker_drifted)')
        for reason in ('not_passed', 'not_whole', 'no_synthetic_git', None):
            with self.subTest(reason=reason):
                self.refused(dict(result, verdict_skipped=reason), repr(reason))

    def test_a_tree_that_is_not_40_hex_fails(self):
        self.refused({'outcome': 'passed', 'tree': None, 'verdict': None,
                      'verdict_skipped': 'no_synthetic_git'}, 'not a 40-hex git tree id')

    def test_an_engine_before_verdicts_is_noted_not_failed(self):
        self.assertEqual(self.check({'outcome': 'passed'}),
                         'not checked (engine predates verdicts)')
        self.assertIn('predates signed verdicts', self.said[0])


class ExpectSigned(VerdictKey):
    """`--expect-signed`: the worker must sign, and every way it did not is exit 1."""

    LINE = ('pandora: verdict not signed: worker_drifted: package incus: '
            'want 6.0.5-8, have 6.0.6-1 (version differs)')

    def run_dir(self, *lines):
        folder = self.root / 'run'
        folder.mkdir(exist_ok=True)
        text = ''.join(line + '\n' for line in lines)
        frames = [{'t': 'log', 's': 'err', 'b64': base64.b64encode(text.encode()).decode()}]
        (folder / 'log').write_text(''.join(json.dumps(f) + '\n' for f in frames))
        return folder

    def strict(self, result, run_dir=None):
        return selftest.check_verdict(result, 'selftest', expect_signed=True,
                                      run_dir=run_dir, say=self.said.append)

    def refused_strictly(self, needle, result, run_dir=None):
        with self.assertRaises(selftest.SelftestError) as caught:
            self.strict(result, run_dir)
        self.assertEqual(caught.exception.exit, 1)
        self.assertIn(needle, str(caught.exception))
        return str(caught.exception)

    def test_a_good_signature_still_passes(self):
        self.assertEqual(self.strict(self.result()), 'signed, tree cccccccccccc')

    def test_every_skip_reason_is_exit_1_and_names_it(self):
        unsigned = {'outcome': 'passed', 'tree': self.TREE, 'verdict': None}
        for reason in ('worker_not_ready', 'worker_drifted', 'not_passed', 'not_whole',
                       'no_synthetic_git', 'sign_failed:ssh-keygen not found', None):
            with self.subTest(reason=reason):
                said = self.refused_strictly(repr(reason),
                                             dict(unsigned, verdict_skipped=reason))
                self.assertIn('--expect-signed', said)
        self.assertEqual(self.said, [])

    def test_the_run_logs_not_signed_line_is_quoted(self):
        run_dir = self.run_dir('selftest ok', self.LINE)
        said = self.refused_strictly('worker_drifted', {
            'outcome': 'passed', 'tree': self.TREE, 'verdict': None,
            'verdict_skipped': 'worker_drifted'}, run_dir)
        self.assertIn('the run log says: ' + self.LINE, said)
        said = self.refused_strictly('worker_not_ready', {
            'outcome': 'passed', 'tree': self.TREE, 'verdict': None,
            'verdict_skipped': 'worker_not_ready'}, self.run_dir('selftest ok'))
        self.assertNotIn('run log says', said)

    def test_an_engine_before_verdicts_is_exit_1(self):
        self.refused_strictly('predates signed verdicts', {'outcome': 'passed'})
        self.assertEqual(self.said, [])

    def test_publication_not_exercised_is_exit_1(self):
        origin = self.root / 'origin.git'
        for result, needle in (
                ({'outcome': 'passed', 'tree': self.TREE, 'verdict': None,
                  'verdict_skipped': 'worker_not_ready'},
                 'not signed (worker_not_ready), so publication was not exercised'),
                ({'outcome': 'passed', 'tree': self.TREE, 'verdict': None,
                  'verdict_skipped': 'worker_drifted'},
                 'not signed (worker_drifted), so publication was not exercised'),
                ({'outcome': 'passed'}, 'publication was not exercised')):
            with self.subTest(needle=needle):
                with self.assertRaises(selftest.SelftestError) as caught:
                    selftest.check_publication(result, self.root, origin, seen=set(),
                                               expect_signed=True, say=self.said.append)
                self.assertEqual(caught.exception.exit, 1)
                self.assertIn(needle, str(caught.exception))
        self.assertEqual(self.said, [])


@unittest.skipUnless(HAS_SSH_KEYGEN, 'ssh-keygen is not installed')
class Publication(DaemonCase):
    """The daemon's real publication hook pushing to a bare origin, read back.

    A real daemon with a fake worker whose result carries a verdict signed by a
    throwaway key; the repository has a bare `origin` beside it and opts in to
    `[verdicts] publish = true`. `check_publication` then asserts what the
    selftest asserts after each live submission.
    """

    TREE = 'c' * 40

    def setUp(self):
        super().setUp()
        self.key = self.root / 'verdict'
        self.signer = new_key(self.key)
        self.result = self.signed('r1')
        follow = lambda worker, run_id, **kw: (json.loads(json.dumps(self.result)), 0)  # noqa: E731
        patcher = mock.patch.object(FakeWorker, 'follow', follow)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(verdict_threads)
        make_repo(self.root)
        self.origin = self.root / 'origin.git'
        toml = self.repo / 'pandora.toml'
        toml.write_text(toml.read_text() + '[verdicts]\npublish = true\n')
        self.seen = set()
        self.said = []
        self.ref = 'refs/pandora/verdicts/%s/unit' % self.TREE

    def signed(self, run_id):
        payload, signature = sign(self.key, {
            'argv': ['sh'], 'engine': 'e', 'finished': 1.0, 'golden': '0' * 16,
            'input_id': 'f' * 64, 'job': 'unit', 'kind': 'pandora-verdict',
            'outcome': 'passed', 'repo': 'demo', 'run_id': run_id, 'tree': self.TREE,
            'v': 1})
        return {'outcome': 'passed', 'cli_exit': 0, 'tree': self.TREE,
                'verdict_skipped': None,
                'verdict': {'payload': payload, 'signature': signature,
                            'signer': self.signer}}

    def submit(self):
        """One run through the daemon; (result.json, run directory)."""
        before = set((self.state / 'runs').iterdir()) if (self.state / 'runs').is_dir() else set()
        answer = self.call(['unit'])
        self.assertEqual(answer.exit, 0)
        [run_dir] = set((self.state / 'runs').iterdir()) - before
        return json.loads((run_dir / 'result.json').read_text()), run_dir

    def check(self, result, run_dir, wait=selftest.PUBLISH_WAIT):
        return selftest.check_publication(result, run_dir, self.origin, seen=self.seen,
                                          wait=wait, say=self.said.append)

    def refused(self, needle, *args, **kw):
        with self.assertRaises(selftest.SelftestError) as caught:
            self.check(*args, **kw)
        self.assertEqual(caught.exception.exit, 1)
        self.assertIn(needle, str(caught.exception))

    def test_signed_published_then_already_on_the_remote(self):
        result, run_dir = self.submit()
        self.assertEqual(self.check(result, run_dir), 'published ' + self.ref)
        self.assertIn('pandora: verdict published %s\n' % self.ref,
                      selftest.run_log_text(run_dir))
        self.assertEqual(json.loads((run_dir / verdicts.RECORD).read_text())['state'],
                         'published')
        self.assertEqual(self.seen, {self.ref})
        # The --update submission: another argv and run, the same tree and job.
        self.result = self.signed('r2')
        result, run_dir = self.submit()
        self.assertEqual(self.check(result, run_dir),
                         'already on the remote ' + self.ref)
        self.assertIn('pandora: verdict published %s (already on the remote)\n' % self.ref,
                      selftest.run_log_text(run_dir))
        self.assertEqual(self.said, [])

    def test_signed_but_the_ref_is_missing_fails(self):
        result, run_dir = self.submit()
        verdict_threads()
        subprocess.run(['git', '--git-dir', str(self.origin), 'update-ref', '-d', self.ref],
                       check=True, capture_output=True)
        self.refused('the scratch origin has no ' + self.ref, result, run_dir)

    def test_a_second_publish_that_pushes_again_fails(self):
        result, run_dir = self.submit()
        self.check(result, run_dir)
        verdict_threads()
        subprocess.run(['git', '--git-dir', str(self.origin), 'update-ref', '-d', self.ref],
                       check=True, capture_output=True)
        result, run_dir = self.submit()
        self.refused('expected present ' + self.ref, result, run_dir, wait=1.0)

    def test_a_tampered_published_signature_fails(self):
        result, run_dir = self.submit()
        verdict_threads()
        other = self.root / 'other'
        new_key(other)
        payload, signature = sign(other, {'tree': self.TREE})
        blobs = {}
        for name, text in (('payload.json', result['verdict']['payload']),
                           ('signer', self.signer + '\n'), ('verdict.sig', signature)):
            blobs[name] = subprocess.run(
                ['git', '--git-dir', str(self.origin), 'hash-object', '-w', '--stdin'],
                input=text.encode(), check=True, capture_output=True).stdout.decode().strip()
        listing = ''.join('100644 blob %s\t%s\n' % (blobs[n], n) for n in sorted(blobs))
        tree = subprocess.run(['git', '--git-dir', str(self.origin), 'mktree'],
                              input=listing.encode(), check=True,
                              capture_output=True).stdout.decode().strip()
        commit = subprocess.run(['git', '--git-dir', str(self.origin), 'commit-tree',
                                 '--no-gpg-sign', '-m', 'x', tree],
                                env=dict(os.environ, **verdicts.IDENTITY), check=True,
                                capture_output=True).stdout.decode().strip()
        subprocess.run(['git', '--git-dir', str(self.origin), 'update-ref', self.ref, commit],
                       check=True, capture_output=True)
        self.refused('not the pushed commit', result, run_dir)
        self.seen.add(self.ref)
        self.result = self.signed('r2')
        result, run_dir = self.submit()
        self.refused('does not verify against its signer', result, run_dir)

    def test_no_record_from_the_daemon_fails(self):
        toml = self.repo / 'pandora.toml'
        toml.write_text(toml.read_text().replace('publish = true', 'publish = false'))
        result, run_dir = self.submit()
        self.refused('no verdict-publish.json beside result.json', result, run_dir, wait=0.5)

    def test_an_unready_worker_publishes_nothing_and_says_so(self):
        self.result = {'outcome': 'passed', 'cli_exit': 0, 'tree': self.TREE,
                       'verdict': None, 'verdict_skipped': 'worker_not_ready'}
        result, run_dir = self.submit()
        verdict_threads()
        self.assertEqual(self.check(result, run_dir), 'not exercised (worker_not_ready)')
        self.assertEqual(self.said, ['verdict not signed (worker_not_ready); '
                                     'publication not exercised'])
        self.assertFalse((run_dir / verdicts.RECORD).exists())
        self.assertEqual(selftest.published_refs(self.origin), [])

    def test_a_drifted_worker_publishes_nothing_and_says_so(self):
        self.result = {'outcome': 'passed', 'cli_exit': 0, 'tree': self.TREE,
                       'verdict': None, 'verdict_skipped': 'worker_drifted'}
        result, run_dir = self.submit()
        verdict_threads()
        self.assertEqual(self.check(result, run_dir), 'not exercised (worker_drifted)')
        self.assertEqual(self.said, ['verdict not signed (worker_drifted); '
                                     'publication not exercised'])

    def test_an_unready_worker_with_a_ref_on_the_origin_fails(self):
        result, run_dir = self.submit()
        self.check(result, run_dir)
        self.result = {'outcome': 'passed', 'cli_exit': 0, 'tree': self.TREE,
                       'verdict': None, 'verdict_skipped': 'worker_not_ready'}
        result, run_dir = self.submit()
        self.refused('the scratch origin holds ' + self.ref, result, run_dir)

    def test_an_engine_before_verdicts_checks_nothing(self):
        self.assertIsNone(self.check({'outcome': 'passed'}, self.root))
        self.assertEqual(self.said, [])


class Help(Scratch):
    def test_help_names_selftest_honestly(self):
        code, out, _ = capture(lambda: _exit_code(cli.main, ['--help']))
        self.assertEqual(code, 0)
        self.assertIn('selftest', out)
        code, out, _ = capture(lambda: _exit_code(cli.main, ['selftest', '--help']))
        self.assertEqual(code, 0)
        for needle in ('worker', 'incus', 'e2e-', 'scratch', 'origin', '--expect-signed'):
            self.assertIn(needle, out)


@unittest.skipUnless(os.environ.get('PANDORA_SELFTEST_LIVE'),
                     'a real run on the production worker; '
                     'set PANDORA_SELFTEST_LIVE=1 to include it')
class LiveRun(unittest.TestCase):
    """The real path, once: shim, test daemon, SSH, engine, incus, receipt.

    This is the run `pandora selftest` exists to drive. It submits `pnpm
    selftest` in a scratch repository to the worker the caller's client
    configuration names -- a real incus run, recorded as `e2e-<host>` -- and
    asserts the run passed and the receipt came home. Never run by CI.
    """

    def test_one_run_through_the_whole_path(self):
        report, code = selftest.run(environ=dict(os.environ))
        self.assertEqual(code, 0, report)
        self.assertTrue(report['ok'])
        self.assertEqual(len(report['runs']), 1)
        record = report['runs'][0]
        self.assertEqual(record['outcome'], 'passed')
        self.assertEqual(record['exit'], 0)
        self.assertEqual(record['lane'], 'remote')
        for phase in ('freeze', 'ship', 'submit'):
            self.assertIn(phase, record['pre_accept'])
        for phase in ('clone', 'execute', 'destroy'):
            self.assertIn(phase, record['engine'])
        # Signed, skipped for an unready worker, or not checked on an old engine.
        self.assertIn('verdict', record)


if __name__ == '__main__':
    unittest.main()
