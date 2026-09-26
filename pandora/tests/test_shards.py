"""Sharding: the schema, the arithmetic, and a whole fan-out without a worker.

The fan-out is tested against a driver that writes the files a real run would
write, because the properties worth holding are all about what happens *after*
the shards finish: that a missing report is not zero failures, that a partition
with a hole is not a pass, and that two shards disagreeing about a file is
reported rather than resolved by whichever finished last.
"""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path, PurePosixPath

from pandora.config import loader
from pandora.engine import batches, fanout, runner
from pandora.engine import shards as sharding
from pandora.engine.ledger import Ledger
from pandora.errors import ConfigError
from pandora.executor.interface import Golden, Instance, Receipt, Result, Usage

ARGV_SHARDS = {
    'strategy': 'argv',
    'template': '--shard={i}/{n}',
    'default': 4,
    'max': 8,
    'plan': ['node', 'runner.mjs', 'plan', '{args}', '--build', '--shards', '{n}',
             '--out', '{plan}'],
    'expect_flag': '--expect',
    'report': 'apps/*/test-results/run-{i}-of-{n}.json',
    'plan_outputs': ['apps/desk/dist'],
}

BASE = {
    'version': 1,
    'repo': {'name': 'demo', 'entrypoints': ['pnpm']},
    'worker': {'base_image': 'images:ubuntu/26.04'},
    'jobs': [{'id': 'surface', 'args': 'required', 'forms': [{'prefix': ['test:surface']}],
              'run': {'argv': ['node', 'runner.mjs', 'run', '{args}', '--no-build']},
              'outputs': [{'kind': 'artifacts', 'paths': ['apps/desk/test-results']}],
              'shards': dict(ARGV_SHARDS)}],
}


def config(**over):
    document = json.loads(json.dumps(BASE))
    document['jobs'][0]['shards'].update(over)
    return document


def without(*keys):
    document = json.loads(json.dumps(BASE))
    for key in keys:
        document['jobs'][0]['shards'].pop(key, None)
    return document


class SchemaTest(unittest.TestCase):
    def loaded(self, document):
        return loader.validate(document)['jobs']['surface']['shards']

    def test_an_argv_strategy_keeps_its_template(self):
        self.assertEqual(self.loaded(config())['template'], '--shard={i}/{n}')

    def test_an_env_strategy_keeps_its_variables(self):
        document = without('template')
        document['jobs'][0]['shards'].update(
            {'strategy': 'env', 'env': {'JOURNEY_SHARD': '{i}/{n}'}})
        self.assertEqual(self.loaded(document)['env'], {'JOURNEY_SHARD': '{i}/{n}'})

    def test_an_unknown_key_is_refused_with_the_allowed_set(self):
        document = config(workers=4)
        with self.assertRaises(ConfigError) as caught:
            loader.validate(document)
        self.assertIn('workers', str(caught.exception))
        self.assertIn('allowed:', str(caught.exception))

    def test_an_argv_strategy_may_not_also_set_variables(self):
        with self.assertRaises(ConfigError):
            loader.validate(config(env={'X': '{i}'}))

    def test_a_template_without_both_tokens_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            loader.validate(config(template='--shard={i}'))
        self.assertIn('{n}', str(caught.exception))

    def test_a_max_below_the_default_is_refused(self):
        with self.assertRaises(ConfigError):
            loader.validate(config(max=2, default=4))

    def test_a_plan_with_no_report_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            loader.validate(without('report'))
        self.assertIn('report', str(caught.exception))

    def test_a_report_with_no_plan_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            loader.validate(without('plan'))
        self.assertIn('needs a plan', str(caught.exception))

    def test_a_plan_that_writes_nowhere_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            loader.validate(config(plan=['node', 'runner.mjs', 'plan', '--shards', '{n}']))
        self.assertIn('{plan}', str(caught.exception))

    def test_a_report_may_not_escape_the_worktree(self):
        with self.assertRaises(ConfigError):
            loader.validate(config(report='../elsewhere/{i}.json'))

    def test_tier_one_needs_no_plan_at_all(self):
        document = without('plan', 'expect_flag', 'report', 'plan_outputs')
        self.assertIsNone(self.loaded(document)['plan'])

    def test_a_sharded_job_must_declare_where_its_report_comes_home(self):
        document = json.loads(json.dumps(BASE))
        document['jobs'][0]['outputs'] = []
        with self.assertRaises(ConfigError) as caught:
            loader.validate(document)
        self.assertIn('artifacts', str(caught.exception))


class ArithmeticTest(unittest.TestCase):
    def setUp(self):
        self.config = loader.validate(BASE)['jobs']['surface']['shards']

    def test_the_default_is_used_when_nothing_asks(self):
        self.assertEqual(sharding.count(self.config)[0], 4)

    def test_the_repository_ceiling_wins_over_the_caller(self):
        n, why = sharding.count(self.config, want=99)
        self.assertEqual(n, 8)
        self.assertIn('max=8', why)

    def test_free_lanes_lower_it_and_say_so(self):
        n, why = sharding.count(self.config, want=4, free_slots=2)
        self.assertEqual(n, 2)
        self.assertIn('free-slots=2', why)

    def test_it_never_reaches_zero(self):
        self.assertEqual(sharding.count(self.config, want=4, free_slots=0)[0], 1)

    def test_the_environment_override_is_read_and_a_typo_is_ignored(self):
        self.assertEqual(sharding.requested({'PANDORA_SHARDS': '3'}, self.config), 3)
        self.assertEqual(sharding.requested({'PANDORA_SHARDS': 'two'}, self.config), 4)
        self.assertEqual(sharding.requested({}, self.config), 4)

    def test_a_shard_command_carries_its_index_and_its_plan(self):
        argv = sharding.child_argv(['node', 'runner.mjs', 'run', 'desk'], self.config,
                                   index=2, total=4, plan_path='.pandora/plan.json')
        self.assertEqual(argv[-3:], ['--shard=2/4', '--expect', '.pandora/plan.json'])

    def test_every_shard_gets_the_index_pair_whatever_the_strategy(self):
        env = sharding.child_env({'CI': 'true'}, self.config, index=3, total=4)
        self.assertEqual(env[sharding.INDEX_VAR], '3')
        self.assertEqual(env[sharding.TOTAL_VAR], '4')
        self.assertEqual(env['CI'], 'true')

    def test_an_env_strategy_renders_its_variables(self):
        document = without('template')
        document['jobs'][0]['shards'].update(
            {'strategy': 'env', 'env': {'JOURNEY_SHARD': '{i}/{n}'}})
        config = loader.validate(document)['jobs']['surface']['shards']
        env = sharding.child_env({}, config, index=1, total=4)
        self.assertEqual(env['JOURNEY_SHARD'], '1/4')
        # An env-sharded job's command line is untouched.
        self.assertEqual(sharding.child_argv(['x'], config, index=1, total=4), ['x'])

    def test_the_plan_command_splices_the_arguments_and_the_count(self):
        argv = sharding.plan_argv(self.config, ['desk', '--grep', 'S3'], total=2)
        self.assertEqual(argv, ['node', 'runner.mjs', 'plan', 'desk', '--grep', 'S3',
                                '--build', '--shards', '2', '--out', sharding.PLAN_PATH])

    def test_the_report_path_is_rendered_per_shard(self):
        self.assertEqual(sharding.report_path(self.config, index=2, total=4),
                         'apps/*/test-results/run-2-of-4.json')


class VerifyTest(unittest.TestCase):
    PLANNED = [['a', 'b'], ['c', 'd']]

    def test_an_exact_partition_verifies(self):
        result = sharding.verify(self.PLANNED, {1: ['a', 'b'], 2: ['c', 'd']})
        self.assertTrue(result['verified'])
        self.assertEqual(result['observed_tests'], 4)

    def test_order_within_a_shard_does_not_matter(self):
        self.assertTrue(sharding.verify(self.PLANNED, {1: ['b', 'a'], 2: ['c', 'd']})['verified'])

    def test_a_missing_report_is_never_an_empty_shard(self):
        result = sharding.verify(self.PLANNED, {1: ['a', 'b']})
        self.assertFalse(result['verified'])
        self.assertEqual(result['missing_reports'], [2])
        self.assertIn('no report from shard 2', result['reason'])

    def test_a_test_that_ran_twice_is_named(self):
        result = sharding.verify(self.PLANNED, {1: ['a', 'b'], 2: ['b', 'c', 'd']})
        self.assertFalse(result['verified'])
        self.assertEqual(result['duplicated'], ['b'])

    def test_a_test_that_ran_nowhere_is_named(self):
        result = sharding.verify(self.PLANNED, {1: ['a'], 2: ['c', 'd']})
        self.assertFalse(result['verified'])
        self.assertEqual(result['missing'], ['b'])

    def test_a_test_nobody_planned_is_named(self):
        result = sharding.verify(self.PLANNED, {1: ['a', 'b'], 2: ['c', 'd', 'z']})
        self.assertFalse(result['verified'])
        self.assertEqual(result['unexpected'], ['z'])

    def test_an_empty_planned_shard_needs_no_report(self):
        result = sharding.verify([['a'], []], {1: ['a'], 2: []})
        self.assertTrue(result['verified'])

    def test_ids_are_read_from_objects_or_bare_strings(self):
        self.assertEqual(sharding.ids([{'id': 'a'}, 'b', {'testId': 'c'}]), ['a', 'b', 'c'])

    def test_an_inventory_that_renumbers_itself_is_refused(self):
        with self.assertRaises(ValueError):
            sharding.inventory({'inventory': [{'shard': 2, 'testIds': ['a']}]})

    def test_a_report_with_no_observed_list_is_refused(self):
        with self.assertRaises(ValueError):
            sharding.observed({'status': 'passed'})


class CollisionTest(unittest.TestCase):
    def test_identical_bytes_at_one_path_are_not_a_collision(self):
        self.assertEqual(sharding.collisions({1: {'a': 'x'}, 2: {'a': 'x'}}), [])

    def test_different_bytes_at_one_path_are(self):
        found = sharding.collisions({1: {'a': 'x'}, 2: {'a': 'y'}})
        self.assertEqual(found, [{'path': 'a', 'shards': [1, 2]}])

    def test_paths_only_one_shard_wrote_are_left_alone(self):
        self.assertEqual(sharding.collisions({1: {'a': 'x'}, 2: {'b': 'y'}}), [])


# --- a whole fan-out, with the executor faked -------------------------------

class WritingDriver:
    """A driver that also writes the files a real run would have produced.

    `trees` maps a run id to {worktree-relative path: text}. `collect` pulls
    those out exactly as the Incus driver's `incus file pull` would, which is
    what lets the aggregator be tested on real files rather than on a mock of
    its own reasoning.
    """

    def __init__(self, trees, outcomes=None):
        self.trees = trees
        self.outcomes = {} if outcomes is None else outcomes
        self.current = None
        self.destroyed = []
        self.limits = {}
        self.cwds = {}

    def prepare(self, toolchain, source=None, log=print):
        return Golden(name='golden-x', fingerprint='x', snapshot='warm', reused=True)

    def clone(self, golden, run_id, limits=None):
        self.current = run_id
        return Instance(name='run-' + run_id, run_id=run_id, golden=golden.name,
                        clone_seconds=0.1, start_seconds=0.2)

    def inject(self, name, source, dest, method='device-rsync'):
        return 0.1

    def harden(self, instance, limits):
        return {}

    def execute(self, instance, argv, env=None, cwd='/work', limits=None, on_log=None,
                on_tick=None, reattach=False):
        self.limits[instance.run_id] = limits
        self.cwds[instance.run_id] = cwd
        if on_log:
            on_log('ran %s\n' % ' '.join(argv))
        outcome = self.outcomes.get(instance.run_id, 'ok')
        return Result(exit_code=0 if outcome == 'ok' else 1, outcome=outcome, seconds=1.0,
                      usage=Usage(memory_peak=900 * 1048576), evidence={'samples': []})

    def incus(self, *args, check=True, timeout=None):
        if args[:3] == ('file', 'pull', '-r'):
            target, dest = args[3], Path(args[4])
            name, _, guest = target.partition('/')
            run_id = name[len('run-'):]
            relative = guest[len('work/'):] if guest.startswith('work/') else guest
            parent = str(PurePosixPath(relative).parent)
            wrote = False
            for path, text in self.trees.get(run_id, {}).items():
                if path != relative and not path.startswith(relative.rstrip('/') + '/'):
                    continue
                suffix = path if parent == '.' else path[len(parent) + 1:]
                out = dest / suffix
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(text)
                wrote = True
            return (0 if wrote else 1), '', ''
        return 0, '', ''

    def destroy(self, instance):
        self.destroyed.append(instance.name)
        return Receipt(run_id=instance.run_id, instance=instance.name, seconds=0.1,
                       instance_gone=True, volume_gone=True, veth_gone=True, cgroup_gone=True)


PLAN_DOC = {'inventory': [{'shard': 1, 'testIds': ['t1', 't2']},
                          {'shard': 2, 'testIds': ['t3']}]}


class FanoutHarness(unittest.TestCase):
    """One parent, one plan step, two shards, with the driver writing real files.

    No tests of its own, so that another module can build on it without running
    this one's twice.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = runner.Paths(self.root).ensure()
        self.ledger = Ledger(self.paths.ledger)
        self.config = loader.validate(BASE)['jobs']['surface']['shards']
        self.trees, self.outcomes = {}, {}
        self.driver = WritingDriver(self.trees, self.outcomes)
        self.spawned = []
        self.original = runner.spawn
        runner.spawn = self.spawn
        # This machine's own memory decides how many lanes are free, and a Mac
        # running the unit tests is not the worker. Name the budget so the
        # arithmetic under test is the shard arithmetic and not the host's.
        self.budget = os.environ.get('PANDORA_BUDGET_MIB')
        os.environ['PANDORA_BUDGET_MIB'] = '65536'
        # The shard admit loop checks the pool floor; a Mac running the unit
        # tests has no btrfs pool, so headroom is faked like the budget.
        self.headroom, runner.disk_headroom = (runner.disk_headroom,
                                               lambda paths, driver=None: {'ok': True})
        # The poll interval is a courtesy to a human watching a four-minute
        # shard, not a property of the fan-out. Tests do not need to wait it out.
        self.poll, fanout.POLL = fanout.POLL, 0.02

    def tearDown(self):
        runner.spawn = self.original
        runner.disk_headroom = self.headroom
        fanout.POLL = self.poll
        if self.budget is None:
            os.environ.pop('PANDORA_BUDGET_MIB', None)
        else:
            os.environ['PANDORA_BUDGET_MIB'] = self.budget
        self.ledger.close()
        self.tmp.cleanup()

    def role_of(self, run_id):
        """A fresh connection: SQLite objects belong to the thread that made them."""
        ledger = Ledger(self.paths.ledger)
        try:
            row = ledger.get(run_id)
            return dict(row) if row is not None else {}
        finally:
            ledger.close()

    def spawn(self, root, run_id, *, python=None):
        """Run the child supervisor in a thread instead of a detached process."""
        thread = threading.Thread(
            target=lambda: runner.supervise(root, run_id, driver=self.driver), daemon=True)
        self.spawned.append((run_id, thread))
        thread.start()
        return 4242

    OUTPUTS = [{'kind': 'artifacts', 'paths': ['apps/desk/test-results']}]

    def parent(self, *, want=None, keep_going=False, args=('desk',), outputs=None,
               source_path='/src'):
        self.ledger.claim('req', 'p1', repo='demo', job='surface', input_id='i',
                          source_path=source_path, argv=['node', 'runner.mjs', 'run', 'desk',
                                                         '--no-build'],
                          env={}, cwd='.', outputs=outputs or self.OUTPUTS,
                          size_class='medium', role='parent')
        attempt = self.paths.attempt('p1')
        attempt.mkdir(parents=True, exist_ok=True)
        (attempt / 'toolchain.json').write_text(json.dumps(
            {'base_image': 'i', 'packages': [], 'node_version': '', 'pnpm_version': '',
             'service_images': [], 'install_command': '', 'source_id': 'x', 'env': {}}))
        (attempt / 'shards.json').write_text(json.dumps(
            {'shards': self.config, 'args': list(args), 'want': want,
             'keep_going': keep_going}))
        (attempt / 'request.json').write_text(json.dumps(
            {'request_id': 'req', 'input_id': 'i', 'source_path': source_path,
             'plan': {'repo': 'demo', 'job': 'surface', 'timeout_minutes': 45}}))
        self.ledger.update('p1', state='admitted', reservation_mib=0, ceiling_mib=0)
        return fanout.supervise_parent(self.root, 'p1', driver=self.driver)

    def arrange(self, *, planned=PLAN_DOC, reports=None, extra=None):
        """Teach the driver what each attempt writes, as the run ids appear."""
        original = self.driver.clone

        def clone(golden, run_id, limits=None):
            row = self.role_of(run_id)
            if row['role'] == 'plan':
                self.trees[run_id] = {sharding.PLAN_PATH: json.dumps(planned),
                                      'apps/desk/dist/app.js': 'built once'}
            else:
                index = row['shard_index']
                observed = (reports or {}).get(index, planned['inventory'][index - 1]['testIds'])
                tree = {}
                if observed is not None:
                    tree['apps/desk/test-results/run-%d-of-%d.json'
                         % (index, row['shard_total'])] = json.dumps(
                             {'observed': [{'id': test} for test in observed]})
                tree.update((extra or {}).get(index, {}))
                self.trees[run_id] = tree
            return original(golden, run_id, limits=limits)

        self.driver.clone = clone


class FanoutTest(FanoutHarness):
    def test_a_clean_fan_out_passes_and_says_it_is_verified(self):
        self.arrange()
        result = self.parent(want=2)
        self.assertEqual(result['outcome'], 'passed')
        self.assertEqual(result['cli_exit'], 0)
        self.assertTrue(result['verification']['verified'])
        self.assertEqual([row['shard'] for row in result['shards']], [1, 2])
        self.assertEqual(result['evidence']['shard_count']['total'], 2)

    def test_the_plan_step_runs_once_and_its_build_reaches_every_shard(self):
        self.arrange()
        self.parent(want=2)
        roles = [self.role_of(rid)['role'] for rid, _ in self.spawned]
        self.assertEqual(roles.count('plan'), 1)
        self.assertEqual(roles.count('shard'), 2)
        for run_id, _ in self.spawned:
            if self.role_of(run_id)['role'] == 'shard':
                graft = self.paths.attempt(run_id) / 'planout'
                self.assertTrue((graft / 'apps/desk/dist/app.js').is_file())
                self.assertTrue((graft / sharding.PLAN_PATH).is_file())

    def test_a_shard_that_files_no_report_cannot_pass_the_parent(self):
        self.arrange(reports={2: None})
        result = self.parent(want=2)
        self.assertNotEqual(result['outcome'], 'passed')
        self.assertEqual(result['verification']['missing_reports'], [2])
        self.assertNotEqual(result['cli_exit'], 0)

    def test_a_shard_that_ran_the_wrong_tests_cannot_pass_the_parent(self):
        self.arrange(reports={2: ['t9']})
        result = self.parent(want=2)
        self.assertEqual(result['outcome'], 'infra_failed')
        self.assertEqual(result['verification']['missing'], ['t3'])
        self.assertEqual(result['verification']['unexpected'], ['t9'])

    def test_every_child_runs_with_the_requests_timeout(self):
        # The plan's `timeout_minutes` lives in request.json, which a child
        # inherits from its parent: the wall clock is 45 minutes everywhere.
        self.arrange()
        self.parent(want=2)
        for run_id, _ in self.spawned:
            self.assertEqual(self.driver.limits[run_id].wall_seconds, 2700,
                             self.role_of(run_id)['role'])

    def test_a_failing_shard_makes_the_parent_fail_with_its_code(self):
        self.arrange()
        self.outcomes.clear()
        original = self.driver.clone

        def clone(golden, run_id, limits=None):
            instance = original(golden, run_id, limits=limits)
            if self.role_of(run_id)['shard_index'] == 1:
                self.outcomes[run_id] = 'failed'
            return instance

        self.driver.clone = clone
        result = self.parent(want=2)
        self.assertEqual(result['outcome'], 'command_failed')
        self.assertEqual(result['cli_exit'], 1)
        self.assertEqual([row['outcome'] for row in result['shards']],
                         ['command_failed', 'passed'])

    def test_two_shards_writing_one_path_differently_exit_75_and_keep_both(self):
        self.arrange(extra={1: {'apps/desk/test-results/trace.txt': 'from one'},
                            2: {'apps/desk/test-results/trace.txt': 'from two'}})
        result = self.parent(want=2)
        self.assertEqual(result['cli_exit'], fanout.COLLISION_EXIT)
        self.assertEqual([item['path'] for item in result['collisions']],
                         ['apps/desk/test-results/trace.txt'])
        kept = self.paths.outputs('p1') / '.pandora-shards'
        self.assertEqual((kept / 'shard-1/apps/desk/test-results/trace.txt').read_text(),
                         'from one')
        self.assertEqual((kept / 'shard-2/apps/desk/test-results/trace.txt').read_text(),
                         'from two')

    def test_an_empty_planned_shard_is_never_dispatched(self):
        self.arrange(planned={'inventory': [{'shard': 1, 'testIds': ['t1', 't2']},
                                            {'shard': 2, 'testIds': []}]})
        result = self.parent(want=2)
        self.assertEqual(result['outcome'], 'passed')
        self.assertTrue(result['verification']['verified'])
        self.assertEqual(len(result['shards']), 1)

    def test_a_tier_one_fan_out_runs_but_is_marked_unverified(self):
        document = without('plan', 'expect_flag', 'report', 'plan_outputs')
        self.config = loader.validate(document)['jobs']['surface']['shards']
        self.arrange()
        result = self.parent(want=2)
        self.assertEqual(result['outcome'], 'passed')
        self.assertFalse(result['verification']['verified'])
        self.assertTrue(result['verification']['unverified'])
        self.assertEqual(len(result['shards']), 2)
        self.assertEqual([self.role_of(rid)['role'] for rid, _ in self.spawned],
                         ['shard', 'shard'])

    def test_the_parent_holds_no_reservation_and_no_cpu_lane(self):
        self.arrange()
        self.parent(want=2)
        row = self.ledger.get('p1')
        self.assertEqual(row['reservation_mib'], 0)
        self.assertIsNone(row['instance'])


QUEUE_SHARDS = {
    'strategy': 'queue',
    'default': 4,
    'max': 8,
    'plan': ['node', 'runner.mjs', 'plan', '{args}', '--build', '--shards', '{n}',
             '--out', '{plan}'],
    'plan_outputs': ['apps/desk/dist'],
    'batch_size': 2,
    'batch_attempts': 2,
}

QUEUE_BASE = json.loads(json.dumps(BASE))
QUEUE_BASE['jobs'][0]['shards'] = dict(QUEUE_SHARDS)


class QueueSchemaTest(unittest.TestCase):
    def loaded(self, document):
        return loader.validate(document)['jobs']['surface']['shards']

    def test_a_queue_strategy_needs_a_plan(self):
        document = json.loads(json.dumps(QUEUE_BASE))
        document['jobs'][0]['shards'].pop('plan')
        with self.assertRaises(ConfigError):
            self.loaded(document)

    def test_static_partition_keys_are_refused_under_queue(self):
        for key in ('template', 'report', 'expect_flag'):
            document = json.loads(json.dumps(QUEUE_BASE))
            document['jobs'][0]['shards'][key] = ('--shard={i}/{n}' if key == 'template'
                                                  else 'x')
            with self.assertRaises(ConfigError, msg=key):
                self.loaded(document)

    def test_batch_knobs_are_refused_under_static_strategies(self):
        for key in ('batch_size', 'batch_attempts'):
            document = config(**{key: 3})
            with self.assertRaises(ConfigError, msg=key):
                self.loaded(document)

    def test_the_defaults(self):
        shards = self.loaded(json.loads(json.dumps(QUEUE_BASE)))
        self.assertEqual(shards['batch_size'], 2)
        self.assertEqual(shards['batch_attempts'], 2)

    def test_batch_size_zero_means_the_engine_chooses(self):
        document = json.loads(json.dumps(QUEUE_BASE))
        document['jobs'][0]['shards'].pop('batch_size')
        self.assertEqual(self.loaded(document)['batch_size'], 0)


class QueueMathTest(unittest.TestCase):
    def test_flatten_keeps_the_plan_order_across_shards(self):
        inventory = [['t1', 't2'], ['t3']]
        self.assertEqual(sharding.flatten(inventory), ['t1', 't2', 't3'])

    def test_batch_up_splits_in_order(self):
        self.assertEqual(sharding.batch_up(['a', 'b', 'c', 'd', 'e'], 2),
                         [(1, ['a', 'b']), (2, ['c', 'd']), (3, ['e'])])

    def test_the_default_size_aims_four_pulls_per_shard(self):
        self.assertEqual(sharding.batch_size(list(range(40)), 2, 0), 5)
        self.assertEqual(sharding.batch_size(list(range(3)), 8, 0), 1)

    def test_exact_coverage_verifies(self):
        out = sharding.verify_queue(['t1', 't2', 't3'], {1: ['t1'], 2: ['t2', 't3']}, {})
        self.assertTrue(out['verified'])

    def test_a_dead_batch_names_its_unrun_tests(self):
        out = sharding.verify_queue(['t1', 't2'], {1: ['t1']}, {2: ['t2']})
        self.assertFalse(out['verified'])
        self.assertEqual(out['dead_batches'], [2])
        self.assertEqual(out['missing'], ['t2'])

    def test_an_id_seen_twice_is_named(self):
        out = sharding.verify_queue(['t1', 't2'], {1: ['t1', 't2'], 2: ['t2']}, {})
        self.assertFalse(out['verified'])
        self.assertEqual(out['duplicated'], ['t2'])

    def test_a_stranger_is_named(self):
        out = sharding.verify_queue(['t1'], {1: ['t1', 't9']}, {})
        self.assertFalse(out['verified'])
        self.assertEqual(out['unexpected'], ['t9'])


class BatchQueueTest(unittest.TestCase):
    """The directory protocol, without a fan-out around it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / 'queue'
        self.queue = batches.Queue(self.dir).seed(
            sharding.batch_up(['t1', 't2', 't3', 't4'], 2))

    def tearDown(self):
        self.tmp.cleanup()

    def test_claims_are_first_free_first_served_and_each_runs_once(self):
        one = self.queue.claim('rA')
        two = self.queue.claim('rB')
        self.assertEqual((one['batch'], two['batch']), (1, 2))
        self.assertEqual(one['testIds'], ['t1', 't2'])

    def test_a_completed_batch_leaves_the_queue(self):
        spec = self.queue.claim('rA')
        self.queue.complete(spec['batch'], 'rA', outcome='ok', report=True)
        self.assertEqual(self.queue.snapshot()['done'], [1])

    def test_a_dead_holders_lease_requeues_within_the_cap_then_dies(self):
        gone = lambda rid: True
        self.queue.claim('rA')
        self.assertEqual(self.queue.release_dead(gone, cap=2), ([1], []))
        again = self.queue.claim('rB')
        self.assertEqual(again['attempt'], 2)
        self.assertEqual(self.queue.release_dead(gone, cap=2), ([], [1]))

    def test_a_live_holders_lease_is_left_alone(self):
        self.queue.claim('rA')
        requeued, dead = self.queue.release_dead(lambda rid: False, cap=2)
        self.assertEqual((requeued, dead), ([], []))
        self.assertEqual(self.queue.snapshot()['leased'], [1])

    def test_halt_stops_claims_but_not_completions(self):
        self.queue.claim('rA')
        self.queue.halt()
        self.assertIsNone(self.queue.claim('rB'))

    def test_drained_needs_nothing_pending_and_nothing_leased(self):
        self.queue.claim('rA')
        self.assertFalse(self.queue.drained())


class QueueDriver(WritingDriver):
    """A WritingDriver that plays the queue-fed runner: each `execute` reads
    the batch spec the supervisor pushed and writes that batch's report.

    `scripts` maps a batch number to a list of behaviors, one consumed per
    attempt -- 'lost' simulates the instance dying mid-batch (no report), any
    other outcome writes the report and returns it. `reports` overrides what a
    batch reports having observed.
    """

    def __init__(self, trees, outcomes=None, scripts=None, reports=None):
        super().__init__(trees, outcomes)
        self.scripts = scripts or {}
        self.reports = reports or {}
        self.executions = {}   # run_id -> [batch seq]

    def incus(self, *args, check=True, timeout=None):
        if args[:2] == ('file', 'push'):
            # ('file', 'push', '-p', <src>, <instance>/<guest path>)
            dest, source = args[-1], args[-2]
            name, _, guest = dest.partition('/')
            run_id = name[len('run-'):]
            relative = guest[len('work/'):]
            self.trees.setdefault(run_id, {})[relative] = Path(source).read_text()
            return 0, '', ''
        return super().incus(*args, check=check, timeout=timeout)

    def execute(self, instance, argv, env=None, cwd='/work', limits=None, on_log=None,
                on_tick=None, reattach=False):
        if env is None or 'PANDORA_BATCH_INDEX' not in env:
            return super().execute(instance, argv, env=env, cwd=cwd, limits=limits,
                                   on_log=on_log, on_tick=on_tick, reattach=reattach)
        run_id, seq = instance.run_id, int(env['PANDORA_BATCH_INDEX'])
        self.executions.setdefault(run_id, []).append(seq)
        spec = json.loads(self.trees[run_id][sharding.BATCH_PATH])
        behaviors = self.scripts.get(seq, ['ok'])
        behavior = behaviors.pop(0) if behaviors else 'ok'
        if behavior == 'lost':
            return Result(exit_code=-1, outcome='lost', seconds=0.1,
                          usage=Usage(), evidence={})
        observed = self.reports.get(seq, spec['testIds'])
        if observed is not None:
            report = env.get('PANDORA_BATCH_REPORT')
            self.trees[run_id][report] = json.dumps(
                {'observed': [{'id': test} for test in observed]})
        self.trees[run_id]['apps/desk/test-results/batch-%d.txt' % seq] = 'ran'
        return Result(exit_code=0 if behavior == 'ok' else 1, outcome=behavior,
                      seconds=0.1, usage=Usage(memory_peak=900 * 1048576),
                      evidence={'samples': []})


class QueueFanoutTest(FanoutHarness):
    """The same parent/plan/shards world, fed by a queue instead of a partition."""

    BIG_PLAN = {'inventory': [{'shard': 1, 'testIds': ['t1', 't2', 't3', 't4']},
                              {'shard': 2, 'testIds': ['t5', 't6', 't7', 't8']}]}

    def setUp(self):
        super().setUp()
        self.config = loader.validate(QUEUE_BASE)['jobs']['surface']['shards']
        self.driver = QueueDriver(self.trees, self.outcomes)

    def arrange_queue(self, *, planned=None, scripts=None, reports=None):
        planned = planned or self.BIG_PLAN
        self.driver.scripts = scripts or {}
        self.driver.reports = reports or {}
        original = self.driver.clone

        def clone(golden, run_id, limits=None):
            if self.role_of(run_id)['role'] == 'plan':
                self.trees[run_id] = {sharding.PLAN_PATH: json.dumps(planned),
                                      'apps/desk/dist/app.js': 'built once'}
            return original(golden, run_id, limits=limits)

        self.driver.clone = clone

    def test_every_batch_runs_once_and_the_fan_out_verifies(self):
        self.arrange_queue()
        result = self.parent(want=2)
        self.assertEqual(result['outcome'], 'passed')
        self.assertTrue(result['verification']['verified'])
        self.assertEqual(result['verification']['planned_tests'], 8)
        self.assertEqual(result['verification']['observed_tests'], 8)
        seen = sorted(seq for seqs in self.driver.executions.values() for seq in seqs)
        self.assertEqual(seen, [1, 2, 3, 4])

    def test_a_shard_that_dies_mid_batch_loses_only_that_batch(self):
        # Shard's first attempt at batch 3 dies with the instance; a successor
        # claims the requeued lease and the suite still verifies.
        self.arrange_queue(scripts={3: ['lost', 'ok']})
        result = self.parent(want=2)
        self.assertEqual(result['outcome'], 'passed')
        self.assertTrue(result['verification']['verified'])
        seen = sorted(seq for seqs in self.driver.executions.values() for seq in seqs)
        self.assertEqual(seen.count(3), 2)

    def test_a_poison_batch_exhausts_the_cap_and_is_named(self):
        self.arrange_queue(scripts={2: ['lost', 'lost']})
        result = self.parent(want=2)
        self.assertNotEqual(result['outcome'], 'passed')
        self.assertEqual(result['verification']['dead_batches'], [2])
        self.assertEqual(sorted(result['verification']['missing']), ['t3', 't4'])

    def test_a_failed_batch_stops_the_shard_and_halts_the_queue(self):
        # One shard: a batch that fails stops it, so the rest of the queue
        # stands unclaimed -- the static rule (a failure stops new dispatch)
        # in its queue shape. Between shards the parent's halt does the same.
        self.arrange_queue(scripts={1: ['failed']})
        result = self.parent(want=1)
        self.assertEqual(result['outcome'], 'command_failed')
        self.assertFalse(result['verification']['verified'])
        self.assertEqual(sorted(result['verification']['missing']),
                         ['t3', 't4', 't5', 't6', 't7', 't8'])

    def test_keep_going_drains_the_queue_past_a_failure(self):
        self.arrange_queue(scripts={1: ['failed']})
        result = self.parent(want=2, keep_going=True)
        self.assertEqual(result['outcome'], 'command_failed')
        self.assertTrue(result['verification']['verified'])
        self.assertEqual(result['verification']['observed_tests'], 8)


if __name__ == '__main__':
    unittest.main()
