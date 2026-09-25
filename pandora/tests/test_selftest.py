"""`pandora selftest`: the pieces that need no worker, and the one that does.

Everything except `LiveRun` is a unit test: the scratch `config.toml` and
`pandora.toml` are built here and read back through the real loaders, the
isolation guard refuses the live state directory, and the toolchain choice is
decided against a stub SSH link. `LiveRun` is the real submission against the
production worker; it runs only when PANDORA_SELFTEST_LIVE=1 is set, so CI and
`python3 -m unittest discover -s pandora` never pay an incus run by accident.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from pandora import cli
from pandora.client import selftest, settings
from pandora.config import loader
from pandora.engine.runner import toolchain_of
from pandora.exits import INFRA
from pandora.tests.test_cli import capture, _exit_code

WORKER_SPEC = {'base_image': 'images:ubuntu/26.04',
               'packages': ['docker.io', 'ca-certificates'],
               'node_version': '24.9.0', 'pnpm_version': '',
               'service_images': [], 'install_command': 'true',
               'prepare_command': 'echo build', 'source_id': 'eichler',
               'env': {'CI': 'true'}, 'workdir': '/work'}


class FakeLink:
    """The `snapshot list` answers `IncusDriver.prepare` would get, without SSH."""

    def __init__(self, warm=()):
        self.warm = set(warm)
        self.calls = []

    def run(self, argv, **kw):
        self.calls.append(argv)
        name = argv[list(argv).index('list') + 1]
        if name in self.warm:
            return 0, 'warm,2026-01-01\n', ''
        return 1, '', 'no such instance'


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
    def test_a_warm_borrowed_golden_wins(self):
        spec = dict(WORKER_SPEC, prepare_command='')
        warm = 'golden-' + toolchain_of(spec).fingerprint()
        chosen, label, reused = selftest.choose_toolchain(
            [('eichler', spec)], FakeLink(warm=[warm]), say=lambda text: None)
        self.assertTrue(reused)
        self.assertIn('borrowed from eichler', label)
        self.assertEqual(chosen['prepare_command'], '')

    def test_no_warm_golden_falls_to_the_minimal_toolchain(self):
        spec, label, reused = selftest.choose_toolchain(
            [('eichler', dict(WORKER_SPEC))], FakeLink(), say=lambda text: None)
        self.assertFalse(reused)
        self.assertIn('minimal', label)
        self.assertEqual(spec['source_id'], 'pandora-selftest')

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


class Help(Scratch):
    def test_help_names_selftest_honestly(self):
        code, out, _ = capture(lambda: _exit_code(cli.main, ['--help']))
        self.assertEqual(code, 0)
        self.assertIn('selftest', out)
        code, out, _ = capture(lambda: _exit_code(cli.main, ['selftest', '--help']))
        self.assertEqual(code, 0)
        for needle in ('worker', 'incus', 'e2e-', 'scratch'):
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


if __name__ == '__main__':
    unittest.main()
