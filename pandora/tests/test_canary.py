"""The canary's plan, derived from the enrolled configurations, with no worker.

On 2026-09-23 the canary proved two toolchain files that were not the toolchain
the enrolled `pandora.toml` ran on. These pin down the derivation: the golden
it proves is the one a routed run would build, and the journey it runs is the
journey job's own argv.
"""
import copy
import tempfile
import unittest
from pathlib import Path

from pandora.config import loader
from pandora.engine.runner import toolchain_of
from pandora.errors import ConfigError
from pandora.executor.interface import Golden, Instance, Receipt, Result, Usage
from pandora.worker import canary, enrolled

EXAMPLES = Path(__file__).resolve().parents[1] / 'config/examples'
JOURNEYS = EXAMPLES / 'acme.pandora.toml'
SURFACES = EXAMPLES / 'acme-surfaces.pandora.toml'


def load(path):
    return loader.load(path)


class CanaryTable(unittest.TestCase):
    def raw(self):
        import tomllib
        return tomllib.loads(JOURNEYS.read_text())

    def test_the_table_is_parsed_and_kept_out_of_the_golden_identity(self):
        config = load(JOURNEYS)
        self.assertEqual(config['canary']['journey'], 'S0-01')
        self.assertEqual(config['canary']['compose'], 'tools/stack/compose.yml')
        self.assertNotIn('canary', config['worker'])
        raw = self.raw()
        del raw['worker']['canary']
        self.assertEqual(toolchain_of(loader.validate(raw)['worker']).fingerprint(),
                         enrolled.fingerprint_of(config),
                         'choosing a canary journey must not mint a new golden')

    def test_an_unknown_key_is_refused(self):
        raw = self.raw()
        raw['worker']['canary']['journeys'] = 'S0-02'
        with self.assertRaisesRegex(ConfigError, 'worker.canary has unknown key journeys'):
            loader.validate(raw)

    def test_an_id_needs_a_job_that_can_take_it(self):
        raw = self.raw()
        raw['worker']['canary']['surface'] = 'desk'
        with self.assertRaisesRegex(ConfigError, 'needs a job with id surface'):
            loader.validate(raw)
        raw = self.raw()
        raw['worker']['canary'] = {'journey': 'x', 'journey_job': 'check'}
        with self.assertRaisesRegex(ConfigError, 'takes no arguments'):
            loader.validate(raw)

    def test_absent_means_no_checks(self):
        raw = self.raw()
        del raw['worker']['canary']
        config = loader.validate(raw)
        self.assertIsNone(config['canary']['journey'])
        self.assertEqual(enrolled.canary_targets([('acme', config)],
                                                 engine_root='/e')[0]['notes'][0],
                         'no [worker.canary] journey, so no journey check')


class Derivation(unittest.TestCase):
    def enrollments(self, *pairs):
        return {'repos': [{'name': name, 'root': '/code/' + name, 'config': str(path)}
                          for name, path in pairs]}

    def fake_loader(self, calls):
        def load_for(root, fallback):
            calls.append((root, fallback))
            return load(fallback)
        return load_for

    def test_configs_go_through_the_daemon_loader_per_enrollment(self):
        calls = []
        entries = enrolled.configs(self.enrollments(('acme', JOURNEYS)),
                                   load=self.fake_loader(calls))
        self.assertEqual(calls, [('/code/acme', str(JOURNEYS))])
        self.assertEqual(entries[0][0], 'acme')

    def test_an_unreadable_config_fails_the_call_and_names_the_repo(self):
        def broken(root, fallback):
            raise ConfigError('no pandora.toml at ' + root)
        with self.assertRaisesRegex(ConfigError, 'enrolled repository acme: no pandora'):
            enrolled.configs(self.enrollments(('acme', JOURNEYS)), load=broken)

    def test_the_journey_check_is_the_journey_jobs_own_argv(self):
        config = load(JOURNEYS)
        [target] = enrolled.canary_targets([('acme', config)],
                                           engine_root='/home/ubuntu/pandora-engine')
        self.assertEqual(target['fingerprint'], enrolled.fingerprint_of(config))
        self.assertEqual(target['toolchain']['source_id'], 'acme-journey-runner-proxy')
        self.assertEqual(target['source'], '/home/ubuntu/pandora-engine/src/acme/latest')
        journey = target['journey']
        self.assertEqual(journey['argv'],
                         ['node', 'tools/validation/journey-runner.mjs', 'run', 'S0-01'])
        # The environment a routed journey gets: the repo's `set`, the job's
        # own `env`, and the job's `unset` applied last.
        self.assertEqual(journey['env']['JOURNEY_REPLAY'], 'cover')
        self.assertNotIn('CI', journey['env'])
        self.assertEqual(journey['cwd'], '/work')
        self.assertEqual(journey['compose'], 'tools/stack/compose.yml')
        self.assertIsNone(target['surface'])

    def test_the_surface_check_is_the_surface_jobs_validate(self):
        [target] = enrolled.canary_targets([('acme', load(SURFACES))], engine_root='/e')
        surface = target['surface']
        self.assertEqual(surface['step'], 'validate')
        self.assertEqual(surface['argv'], ['node', 'tools/validation/surface-runner.mjs',
                                           'validate', 'web'])
        self.assertEqual(surface['env']['SURFACE_WORKERS'], '1')

    def test_without_validate_the_surface_check_is_a_one_shard_plan(self):
        config = copy.deepcopy(load(SURFACES))
        config['jobs']['surface']['validate'] = None
        surface = enrolled.surface_check(config)
        self.assertEqual(surface['step'], 'plan')
        self.assertEqual(surface['argv'][3], 'web')
        self.assertIn('1', surface['argv'])
        self.assertIn(enrolled.PLAN_PATH, surface['argv'])
        self.assertFalse(any('{' in item for item in surface['argv']))

    def test_one_target_per_distinct_fingerprint(self):
        journeys, surfaces = load(JOURNEYS), load(SURFACES)
        targets = enrolled.canary_targets(
            [('acme', journeys), ('acme-copy', journeys), ('surfaces', surfaces)],
            engine_root='/e', source='/tmp/tree')
        self.assertEqual([item['repos'] for item in targets],
                         [['acme', 'acme-copy'], ['surfaces']])
        self.assertEqual({item['source'] for item in targets}, {'/tmp/tree'})

    def test_named_fingerprints_say_which_repositories_name_them(self):
        journeys, surfaces = load(JOURNEYS), load(SURFACES)
        named = enrolled.named_fingerprints([('b', journeys), ('a', journeys),
                                             ('s', surfaces)])
        self.assertEqual(named, {enrolled.fingerprint_of(journeys): 'a,b',
                                 enrolled.fingerprint_of(surfaces): 's'})

    def test_named_families_follow_the_repo_and_the_source_id(self):
        families = enrolled.named_families([('acme', load(JOURNEYS)),
                                            ('surfaces', load(SURFACES))])
        self.assertEqual(families, {('acme', 'acme-journey-runner-proxy'),
                                    ('acme', 'acme-surfaces')})


class FakeDriver:
    """The IncusDriver surface `canary.run` touches, recording what it was asked."""

    project = 'pandora'

    def __init__(self, built=()):
        self.built = set(built)
        self.prepared, self.executed = [], []

    def incus(self, *args, **kwargs):
        return 0, 'pandora,\n', ''

    def capacity(self, floor_gib):
        return {'ok': True, 'free_gib': 20.0, 'total_bytes': 40 << 30}

    def golden_name(self, toolchain):
        return 'golden-' + toolchain.fingerprint()

    def exists(self, name):
        return name in self.built

    def prepare(self, toolchain, source=None, log=None):
        self.prepared.append(source)
        name = self.golden_name(toolchain)
        self.built.add(name)
        return Golden(name=name, fingerprint=toolchain.fingerprint(), snapshot='warm',
                      reused=True)

    def clone(self, golden, tag, limits=None):
        return Instance(name=tag, run_id=tag, golden=golden.name, clone_seconds=0.1)

    def harden(self, instance, limits):
        pass

    def sh(self, name, script, check=False, timeout=None):
        if 'docker info' in script:
            return 0, 'overlay2 2\n', ''
        if 'up -d' in script:
            return 0, 'ok\n4\n', ''
        if 'dd if=' in script:
            return 0, 'dd: error writing: Disk quota exceeded\n', ''
        return 0, '0\n', ''

    def execute(self, instance, argv, env=None, cwd=None, limits=None):
        self.executed.append((instance.name, list(argv), dict(env or {})))
        if instance.name == 'canary-oom':
            return Result(exit_code=137, outcome='oom', seconds=1.0, usage=Usage(),
                          evidence={'reason': 'ceiling', 'events': {'oom_kill': 1}})
        return Result(exit_code=0, outcome='ok', seconds=1.0,
                      usage=Usage(memory_peak=3000 * 1048576))

    def volume_bytes(self, name):
        return 4 << 30

    def destroy(self, instance):
        return Receipt(run_id=instance.run_id, instance=instance.name, seconds=0.1,
                       instance_gone=True, volume_gone=True, veth_gone=True, cgroup_gone=True)


class CanaryRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = load(JOURNEYS)
        self.missing = str(self.root / 'src/acme/latest')

    def tearDown(self):
        self.tmp.cleanup()

    def targets(self, source):
        return enrolled.canary_targets([('acme', self.config)], engine_root='/e',
                                       source=source)

    def test_an_absent_source_and_no_golden_says_the_first_run_has_not_happened(self):
        driver = FakeDriver()
        verdict = canary.run(self.root, targets=self.targets(self.missing), driver=driver)
        self.assertFalse(verdict['ok'])
        [row] = [row for row in verdict['checks'] if not row['ok']]
        self.assertIn('acme source for golden-', row['check'])
        self.assertIn(self.missing + ' is absent', row['detail'])
        self.assertIn('first routed run', row['detail'])
        self.assertEqual(driver.prepared, [], 'nothing was built from a tree that is not there')

    def test_an_absent_source_is_fine_when_the_golden_is_built(self):
        name = 'golden-' + enrolled.fingerprint_of(self.config)
        driver = FakeDriver(built={name})
        verdict = canary.run(self.root, targets=self.targets(self.missing), driver=driver)
        self.assertTrue(verdict['ok'], verdict['reason'])
        self.assertEqual(set(driver.prepared), {None})
        journey = [item for item in driver.executed if item[0] == 'canary-journey'][0]
        self.assertEqual(journey[1][-1], 'S0-01')
        self.assertEqual(journey[2]['JOURNEY_REPLAY'], 'cover')
        self.assertIn('acme journey S0-01 passes', [row['check'] for row in verdict['checks']])

    def test_a_present_source_builds_the_golden(self):
        tree = self.root / 'tree'
        tree.mkdir()
        driver = FakeDriver()
        verdict = canary.run(self.root, targets=self.targets(str(tree)), driver=driver)
        self.assertTrue(verdict['ok'], verdict['reason'])
        self.assertEqual(driver.prepared[0], str(tree))

    def prepared_run(self, exit_code):
        """A `[worker]` table with a prepare_command, against a built golden."""
        self.config = copy.deepcopy(self.config)
        self.config['worker']['prepare_command'] = 'pnpm -r build'

        class Preparing(FakeDriver):
            def execute(self, instance, argv, env=None, cwd=None, limits=None):
                if instance.name == 'canary-journey' and argv[:2] == ['bash', '-c']:
                    self.executed.append((instance.name, list(argv), dict(env or {}), cwd))
                    return Result(exit_code=exit_code, outcome='ok' if exit_code == 0
                                  else 'failed', seconds=3.2, usage=Usage())
                return super().execute(instance, argv, env=env, cwd=cwd, limits=limits)
        driver = Preparing(built={'golden-' + enrolled.fingerprint_of(self.config)})
        return driver, canary.run(self.root, targets=self.targets(self.missing), driver=driver)

    def test_the_journey_clone_runs_prepare_command_first(self):
        """#88: the canary proved a journey in a clone no routed run ever gets."""
        driver, verdict = self.prepared_run(0)
        self.assertTrue(verdict['ok'], verdict['reason'])
        names = [item[0] for item in driver.executed]
        self.assertEqual(names[:2], ['canary-journey', 'canary-journey'])
        prep, journey = driver.executed[0], driver.executed[1]
        self.assertEqual(prep[1], ['bash', '-c', 'pnpm -r build'])
        self.assertEqual(prep[3], '/work')
        self.assertEqual(prep[2]['JOURNEY_REPLAY'], 'cover', 'the job env, as the runner')
        self.assertEqual(journey[1][-1], 'S0-01')
        self.assertIn('acme prepare_command in 3s',
                      [row['check'] for row in verdict['checks']])

    def test_a_failing_prepare_command_fails_the_canary(self):
        driver, verdict = self.prepared_run(2)
        self.assertFalse(verdict['ok'])
        [row] = [row for row in verdict['checks'] if not row['ok']]
        self.assertEqual(row['check'], 'acme prepare_command in 3s')
        self.assertIn('exit=2', row['detail'])
        self.assertNotIn(['node', 'tools/validation/journey-runner.mjs', 'run', 'S0-01'],
                         [item[1] for item in driver.executed])

    def test_no_prepare_command_runs_no_extra_step(self):
        name = 'golden-' + enrolled.fingerprint_of(self.config)
        driver = FakeDriver(built={name})
        verdict = canary.run(self.root, targets=self.targets(self.missing), driver=driver)
        self.assertFalse(any(item[1][:2] == ['bash', '-c'] for item in driver.executed
                             if item[0] == 'canary-journey'))
        self.assertFalse(any('prepare_command' in row['check'] for row in verdict['checks']))

    def test_no_targets_is_a_failure_not_a_pass(self):
        verdict = canary.run(self.root, targets=[], driver=FakeDriver())
        self.assertFalse(verdict['ok'])
        self.assertIn('a toolchain to prove', verdict['reason'])


class GcCommand(unittest.TestCase):
    """`pandora worker gc` passes the enrolled fingerprints as `--protect`."""

    def test_the_client_protects_what_the_enrolled_configs_name(self):
        import argparse
        import contextlib
        import io
        from unittest import mock
        from pandora.worker import cli
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / 'config.toml'
            settings_path.write_text('[[repos]]\nname = "acme"\nroot = "%s"\nconfig = "%s"\n'
                                     % (tmp, JOURNEYS))
            args = argparse.Namespace(config=str(settings_path), dry_run=True, keep=1,
                                      protect=['golden-abc'], json=False)
            fingerprint = enrolled.fingerprint_of(load(JOURNEYS))
            answer = {'ok': True, 'dry_run': True, 'removed': [], 'failed': [],
                      'kept': [{'kind': 'golden', 'name': 'golden-' + fingerprint,
                                'why': 'named by acme pandora.toml'}], 'pool': {}}
            out = io.StringIO()
            with mock.patch.object(cli, 'remote_call', return_value=answer) as call, \
                    contextlib.redirect_stdout(out):
                self.assertEqual(cli.cmd_gc(args), 0)
        argv = call.call_args[0][1]
        self.assertEqual(argv[:3], ['gc', '--dry-run', '--keep'])
        self.assertIn('%s=acme' % fingerprint, argv)
        self.assertIn('abc=the command line', argv)
        self.assertIn('acme=acme-journey-runner-proxy', argv)
        # The marker that tells the worker the family list is an answer --
        # empty means "nothing is enrolled", not "nobody could say" -- and
        # the repos that scope the orphan rule to this client's enrollment.
        self.assertIn('--families-known', argv)
        self.assertEqual(argv[argv.index('--repos') + 1], 'acme')
        self.assertIn('named by acme pandora.toml', out.getvalue())

    def test_no_config_file_ships_no_enrollment_claims(self):
        """`load` of an absent config yields zero repos; that is "nobody could
        say", never the authoritative "nothing is enrolled"."""
        import argparse
        import contextlib
        import io
        from unittest import mock
        from pandora.worker import cli
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(config=str(Path(tmp) / 'absent.toml'),
                                      dry_run=True, keep=None, protect=[],
                                      json=False)
            answer = {'ok': True, 'dry_run': True, 'removed': [], 'failed': [],
                      'kept': [], 'pool': {}}
            out = io.StringIO()
            with mock.patch.object(cli, 'remote_call', return_value=answer) as call, \
                    contextlib.redirect_stdout(out):
                self.assertEqual(cli.cmd_gc(args), 0)
        argv = call.call_args[0][1]
        self.assertNotIn('--families-known', argv)
        self.assertNotIn('--family', argv)
        self.assertNotIn('--repos', argv)


if __name__ == '__main__':
    unittest.main()
