"""One infra retry, flaky pairs, and the progress lines, 2026-09-22.

Three small promises, each pinned where it is kept:

* an `infra_failed` run is resubmitted once, remote to remote, only when the
  caller has seen none of the command's output and the cause is a named
  retryable one -- and never into the local lane;
* two attempts on one input that disagree are recorded as a flaky pair and
  hinted, and nothing is re-run because of it;
* phase lines are one per transition, carry an estimate only when the ledger
  has enough history for one, and `still queued` is at most once a minute.
"""
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from pandora import cli
from pandora.client import daemon as daemon_module
from pandora.client import progress, stats as statistics
from pandora.client.worker import sync_line
from pandora.engine import fanout, history, retry, runner, service
from pandora.engine.ledger import Ledger
from pandora.engine.result import hint_for
from pandora.errors import WorkerUnreachable
from pandora.executor.interface import CloneFailed
from pandora.tests.test_engine import PLAN, FakeDriver, claim
from pandora.tests.test_fallback import DaemonCase, FakeWorker, Submission
from pandora.tests.test_local import budget
from pandora.tests import test_shards


# -- the cause table -----------------------------------------------------------

class CauseTable(unittest.TestCase):
    def test_every_cause_has_an_answer_and_a_reason(self):
        for cause, (ok, why) in retry.CAUSES.items():
            with self.subTest(cause=cause):
                self.assertIsInstance(ok, bool)
                self.assertTrue(why)

    def test_only_the_transient_causes_are_retryable(self):
        self.assertEqual(sorted(cause for cause, (ok, _) in retry.CAUSES.items() if ok),
                         ['clone-failed', 'execution-failed', 'instance-lost',
                          'supervisor-gone'])

    def test_an_unknown_cause_is_not_retried(self):
        self.assertFalse(retry.retryable('cosmic-ray')[0])

    def test_an_explicit_cause_wins(self):
        self.assertEqual(retry.cause_of({'outcome': 'infra_failed',
                                         'evidence': {'cause': 'instance-lost',
                                                      'error': 'PrepareFailed: x'}}),
                         'instance-lost')

    def test_an_older_result_is_read_from_its_error(self):
        def of(evidence):
            return retry.cause_of({'outcome': 'infra_failed', 'evidence': evidence})
        self.assertEqual(of({'error': 'CloneFailed: copy a -> b: busy'}), 'clone-failed')
        self.assertEqual(of({'error': 'CloneFailed: disk quota 6 GiB on run-x is at or below'}),
                         'disk-quota')
        self.assertEqual(of({'reason': 'supervisor 12 gone at engine restart'}),
                         'supervisor-gone')
        self.assertEqual(of({'error': 'KeyError: toolchain'}), 'engine-error')

    def test_a_resource_verdict_is_never_an_infra_cause(self):
        self.assertIsNone(retry.cause_of({'outcome': 'oom', 'evidence': {}}))
        self.assertFalse(retry.retryable('disk-quota')[0])


class CommandOutput(unittest.TestCase):
    def test_pandoras_own_lines_are_not_output(self):
        self.assertFalse(retry.command_output(
            b'pandora: turbo cache off: no server\npandora: instance ready in 1.2 s\n\n'))

    def test_a_labelled_pandora_line_is_not_output_either(self):
        self.assertFalse(retry.command_output('[1/2] pandora: cpus hint 2\n'))

    def test_one_line_of_the_command_is(self):
        self.assertTrue(retry.command_output(b'pandora: running\n> vitest run\n'))
        self.assertTrue(retry.command_output('[2/2] ok 1 - login\n'))

    def test_an_unfinished_line_counts_as_soon_as_it_cannot_be_ours(self):
        self.assertTrue(retry.could_be_pandora(b'pand'))
        self.assertTrue(retry.could_be_pandora(b'[1/2'))
        self.assertTrue(retry.could_be_pandora(b''))
        self.assertFalse(retry.could_be_pandora(b'RUN  v1'))


# -- the engine: causes, progress, resubmit ------------------------------------

class EngineCausesAndLines(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = runner.Paths(self.root).ensure()
        self.ledger = Ledger(self.paths.ledger)
        self.addCleanup(self.ledger.close)
        os.environ['PANDORA_BUDGET_MIB'] = '8192'
        self.count = 0

    def attempt(self, driver, **over):
        self.count += 1
        run_id = 'r%d' % self.count
        claim(self.ledger, request_id='c%d:suite' % self.count, run_id=run_id, **over)
        self.paths.attempt(run_id).mkdir(parents=True, exist_ok=True)
        (self.paths.attempt(run_id) / 'toolchain.json').write_text(json.dumps(PLAN['worker']))
        self.ledger.update(run_id, state='admitted', reservation_mib=2048, ceiling_mib=4096,
                           cpus_hint=2)
        return run_id, runner.supervise(self.root, run_id, driver=driver)

    def test_a_clone_failure_records_its_cause(self):
        _, result = self.attempt(FakeDriver(explode='clone'))
        self.assertEqual(result['evidence']['cause'], 'clone-failed')
        self.assertEqual(retry.cause_of(result), 'clone-failed')

    def test_a_prepare_failure_records_a_cause_that_is_not_retried(self):
        _, result = self.attempt(FakeDriver(explode='prepare'))
        self.assertEqual(retry.cause_of(result), 'prepare-failed')
        self.assertFalse(retry.retryable(retry.cause_of(result))[0])

    def test_a_machine_left_behind_after_a_pass_is_destroy_incomplete(self):
        _, result = self.attempt(FakeDriver(destroy_clean=False))
        self.assertEqual(retry.cause_of(result), 'destroy-incomplete')

    def test_boot_and_running_are_one_line_each_and_boot_is_recorded(self):
        run_id, result = self.attempt(FakeDriver())
        log = self.paths.log(run_id).read_text()
        self.assertEqual(log.count('pandora: instance ready in '), 1)
        self.assertRegex(log, r'pandora: instance ready in 0\.[78] s\n')
        self.assertIn('pandora: running (cpus hint', log)
        self.assertNotIn('typical', log, 'no history, no estimate')
        self.assertAlmostEqual(result['durations']['boot'], 0.7, delta=0.05)

    def test_the_estimate_appears_once_there_is_enough_history(self):
        for _ in range(history.MIN_SAMPLES):
            run_id, _ = self.attempt(FakeDriver())
        self.assertNotIn('typical', self.paths.log(run_id).read_text())
        run_id, _ = self.attempt(FakeDriver())
        self.assertIn('pandora: running (typical 12 s for suite; cpus hint',
                      self.paths.log(run_id).read_text())


class Resubmit(unittest.TestCase):
    """The engine's half of a retry: same request, new id, admitted like a first."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = runner.Paths(self.root).ensure()
        self.source = self.root / 'src' / 'demo' / 'input-a'
        self.source.mkdir(parents=True)
        os.environ['PANDORA_BUDGET_MIB'] = '8192'
        self.saved = runner.spawn, runner.disk_headroom
        runner.spawn = lambda root, run_id, python=None: 4242
        runner.disk_headroom = lambda paths, driver=None: {'ok': True}
        self.addCleanup(self.restore)
        ledger = Ledger(self.paths.ledger)
        claim(ledger, request_id='c1:suite', run_id='r1', source_path=str(self.source))
        (self.paths.attempt('r1')).mkdir(parents=True)
        (self.paths.attempt('r1') / 'request.json').write_text(json.dumps(
            {'request_id': 'c1:suite', 'input_id': 'input-a', 'source_path': str(self.source),
             'plan': dict(PLAN, size='medium', shards=None)}))
        runner.write_result(self.paths, ledger, 'r1', outcome='infra_failed', layer='executor',
                            exit_code=None, peak_mib=0, durations={},
                            evidence={'cause': 'clone-failed'}, receipt=None)
        ledger.close()

    def restore(self):
        runner.spawn, runner.disk_headroom = self.saved

    def call(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            service.main(['--root', str(self.root), *argv])
        return json.loads(out.getvalue())

    def test_a_retry_is_a_new_attempt_that_names_the_one_it_repeats(self):
        answer = self.call('resubmit', '--run', 'r1', '--request-id', 'c1:suite:retry')
        self.assertTrue(answer['ok'], answer)
        self.assertNotEqual(answer['run_id'], 'r1')
        ledger = Ledger(self.paths.ledger)
        row = ledger.get(answer['run_id'])
        ledger.close()
        self.assertEqual(row['retry_of'], 'r1')
        self.assertEqual(row['input_id'], 'input-a')
        self.assertEqual(row['source_path'], str(self.source))
        request = json.loads((self.paths.attempt(answer['run_id']) / 'request.json').read_text())
        self.assertEqual(request['retry_of'], 'r1')

    def test_the_same_retry_twice_is_one_attempt(self):
        first = self.call('resubmit', '--run', 'r1', '--request-id', 'c1:suite:retry')
        second = self.call('resubmit', '--run', 'r1', '--request-id', 'c1:suite:retry')
        self.assertTrue(second['duplicate'])
        self.assertEqual(first['run_id'], second['run_id'])

    def test_a_source_that_has_gone_is_refused(self):
        self.source.rmdir()
        answer = self.call('resubmit', '--run', 'r1', '--request-id', 'c1:suite:retry')
        self.assertEqual((answer['ok'], answer['code']), (False, 'source-gone'))

    def test_only_an_infra_failure_can_be_resubmitted(self):
        ledger = Ledger(self.paths.ledger)
        ledger.update('r1', outcome='command_failed')
        ledger.close()
        answer = self.call('resubmit', '--run', 'r1', '--request-id', 'c1:suite:retry')
        self.assertEqual(answer['code'], 'not-retryable')


# -- flaky pairs -----------------------------------------------------------------

class FlakyPairs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = runner.Paths(self.tmp.name).ensure()
        self.ledger = Ledger(self.paths.ledger)
        self.addCleanup(self.ledger.close)
        self.count = 0

    def finished(self, outcome, *, exit_code=None, **over):
        self.count += 1
        run_id = 'r%d' % self.count
        claim(self.ledger, request_id='caller%d:suite' % self.count, run_id=run_id, **over)
        time.sleep(0.002)                   # `created` orders the pair
        return runner.write_result(
            self.paths, self.ledger, run_id, outcome=outcome, layer='command',
            exit_code=exit_code if exit_code is not None else (0 if outcome == 'passed' else 1),
            peak_mib=0, durations={}, evidence={}, receipt={'clean': True})

    def test_fail_then_pass_on_one_input_is_a_pair_and_a_hint(self):
        self.finished('command_failed')
        result = self.finished('passed')
        self.assertEqual(result['flaky']['order'], 'failed-then-passed')
        self.assertEqual(result['flaky']['failed'], 'caller1')
        self.assertEqual(result['hint'], 'this input failed then passed with no change; treat '
                                         'as flaky, see pandora result caller1')
        self.assertEqual(self.ledger.get('r2')['flaky_with'], 'r1')

    def test_pass_then_fail_names_the_failure(self):
        self.finished('passed')
        result = self.finished('command_failed')
        self.assertIn('passed then failed', result['hint'])
        self.assertTrue(result['hint'].endswith('pandora result caller2'))

    def test_a_different_selector_is_not_the_same_input(self):
        # `same_input_as` would link these: same repository, job and digest.
        self.finished('command_failed', argv=['node', 'run.mjs', 'S0-01'])
        result = self.finished('passed', argv=['node', 'run.mjs', 'S0-02'])
        self.assertIsNone(result.get('flaky'))
        self.assertEqual(result['same_input_as'], 'r1')

    def test_a_different_input_is_never_a_pair(self):
        self.finished('command_failed')
        self.assertIsNone(self.finished('passed', input_id='input-b').get('flaky'))

    def test_an_infra_failure_between_them_is_not_a_verdict(self):
        self.finished('command_failed')
        self.finished('infra_failed')
        result = self.finished('passed')
        self.assertEqual(result['flaky']['failed_run'], 'r1')

    def test_two_agreeing_verdicts_are_not_a_pair(self):
        self.finished('command_failed')
        self.assertIsNone(self.finished('command_failed').get('flaky'))

    def test_a_drifted_result_is_not_hinted_as_flaky(self):
        self.assertIsNone(hint_for({'outcome': 'passed', 'drifted': True, 'flaky': {
            'order': 'failed-then-passed', 'failed': 'x'}}))

    def test_shards_are_compared_shard_by_shard(self):
        def parent(outcomes, outcome):
            self.count += 1
            pid = 'p%d' % self.count
            claim(self.ledger, request_id='caller%d:surface' % self.count, run_id=pid,
                  role='parent')
            for index, shard_outcome in enumerate(outcomes, 1):
                child = '%s-s%d' % (pid, index)
                claim(self.ledger, request_id='%s:shard:%d' % (pid, index), run_id=child,
                      role='shard', parent=pid, shard_index=index, shard_total=len(outcomes))
                self.ledger.finish(child, outcome=shard_outcome, exit_code=0)
            time.sleep(0.002)
            return runner.write_result(self.paths, self.ledger, pid, outcome=outcome,
                                       layer='command', exit_code=1, peak_mib=0, durations={},
                                       evidence={}, receipt=None)
        parent(['passed', 'command_failed', 'command_failed'], 'command_failed')
        result = parent(['passed', 'passed', 'command_failed'], 'command_failed')
        self.assertIsNone(result['flaky'].get('order'), 'both parents failed')
        self.assertEqual([item['shard'] for item in result['flaky']['shards']], ['2/3'])
        self.assertEqual(result['hint'], 'shard 2/3 of this input failed then passed with no '
                                         'change; treat as flaky, see pandora result caller1')


class History(unittest.TestCase):
    def test_durations_read_as_a_person_would_say_them(self):
        self.assertEqual(history.fmt_seconds(40.4), '40 s')
        self.assertEqual(history.fmt_seconds(250), '4m10s')
        self.assertEqual(history.fmt_seconds(3725), '1h02m')

    def test_the_queue_estimate_is_the_soonest_lane_and_none_without_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / 'ledger.db')
            try:
                now = time.time()
                for number in range(history.MIN_SAMPLES):
                    run_id = 'old%d' % number
                    claim(ledger, request_id=run_id, run_id=run_id)
                    ledger.finish(run_id, outcome='passed', exit_code=0)
                    ledger.db.execute('UPDATE attempts SET created=?, finished=? WHERE run_id=?',
                                      (now - 1000, now - 900, run_id))
                claim(ledger, request_id='live', run_id='live')
                ledger.db.execute('UPDATE attempts SET created=? WHERE run_id=?',
                                  (now - 60, 'live'))
                claim(ledger, request_id='other', run_id='other', job='unknown')
                rows = [ledger.get('live'), ledger.get('other')]
                self.assertAlmostEqual(history.queue_eta(ledger, rows, now=now), 40, delta=1)
                self.assertIsNone(history.queue_eta(ledger, [ledger.get('other')], now=now))
            finally:
                ledger.close()


# -- fan-out: one shard retries alone -----------------------------------------

class ShardRetry(test_shards.FanoutTest):
    """Inherits the fan-out harness; only the tests below run under this name."""

    def clone_fails_once_for(self, index):
        original, failed = self.driver.clone, []

        def clone(golden, run_id, limits=None):
            if self.role_of(run_id).get('shard_index') == index and not failed:
                failed.append(run_id)
                raise CloneFailed('pool busy')
            return original(golden, run_id, limits=limits)

        self.driver.clone = clone
        return failed

    def test_a_shard_that_failed_before_output_is_retried_alone(self):
        self.arrange()
        failed = self.clone_fails_once_for(2)
        result = self.parent(want=2)
        self.assertEqual(result['outcome'], 'passed', result['evidence'])
        retried = result['evidence']['shard_retries']
        self.assertEqual([(item['shard'], item['failed_run'], item['cause']) for item in retried],
                         [(2, failed[0], 'clone-failed')])
        self.assertEqual(result['shards'][1]['retried_from'], failed[0])
        roles = [self.role_of(run_id)['role'] for run_id, _ in self.spawned]
        self.assertEqual(roles.count('shard'), 3, 'two shards plus one retry')
        log = self.paths.log('p1').read_text()
        self.assertEqual(log.count('shard 2/2: infrastructure failure before output '
                                   '(clone-failed); retrying once'), 1)

    def test_a_shard_that_already_printed_is_not_retried(self):
        self.arrange()
        original = self.driver.clone

        def clone(golden, run_id, limits=None):
            instance = original(golden, run_id, limits=limits)
            if self.role_of(run_id).get('shard_index') == 1:
                self.outcomes[run_id] = 'lost'
            return instance

        self.driver.clone = clone
        result = self.parent(want=2)
        self.assertEqual(result['outcome'], 'infra_failed')
        self.assertEqual(result['evidence']['cause'], 'shard-failed')
        self.assertNotIn('shard_retries', result['evidence'])

    def test_only_once(self):
        self.arrange()
        original = self.driver.clone

        def clone(golden, run_id, limits=None):
            if self.role_of(run_id).get('shard_index') == 1:
                raise CloneFailed('pool busy')
            return original(golden, run_id, limits=limits)

        self.driver.clone = clone
        result = self.parent(want=2)
        self.assertEqual(result['outcome'], 'infra_failed')
        self.assertEqual(len(result['evidence']['shard_retries']), 1)


# Only the tests defined above run under ShardRetry.
for _name in [name for name in dir(test_shards.FanoutTest) if name.startswith('test_')]:
    if _name not in ShardRetry.__dict__:
        setattr(ShardRetry, _name, None)


class FanoutQueueLine(unittest.TestCase):
    def test_the_first_line_carries_the_estimate_and_the_repeat_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / 'ledger.db')
            try:
                claim(ledger, request_id='live', run_id='live')
                rows = [ledger.get('live')]
                self.assertEqual(fanout.queue_line(ledger, 'shard 2/4', rows, first=True),
                                 'shard 2/4 queued behind 1 run')
                self.assertEqual(fanout.queue_line(ledger, 'shard 2/4', rows, first=False),
                                 'shard 2/4 still queued behind 1 run')
            finally:
                ledger.close()

    def test_waiting_is_said_once_and_not_every_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = runner.Paths(tmp).ensure()
            ledger = Ledger(paths.ledger)
            os.environ['PANDORA_BUDGET_MIB'] = '4096'
            saved = runner.spawn, fanout.POLL
            runner.spawn, fanout.POLL = (lambda root, run_id, python=None: 1), 0.01
            try:
                claim(ledger, request_id='big', run_id='big', size_class='large')
                ledger.update('big', state='running', reservation_mib=4000)
                claim(ledger, request_id='wait', run_id='wait')
                said = []
                with self.assertRaises(fanout.AdmissionTimeout):
                    fanout.admit_and_spawn(paths, ledger, 'wait', dict(PLAN, size_class='medium'),
                                           note=said.append, label='shard 1/2',
                                           deadline=time.monotonic() + 0.3)
                self.assertEqual(said, ['shard 1/2 queued behind 1 run'])
            finally:
                runner.spawn, fanout.POLL = saved
                ledger.close()


# -- the daemon: the retry itself ------------------------------------------------

INFRA = {'outcome': 'infra_failed', 'cli_exit': 70, 'job': 'unit', 'hint': None,
         'run_id': 'r1', 'evidence': {'cause': 'clone-failed'}}
PASS = {'outcome': 'passed', 'cli_exit': 0, 'job': 'unit', 'hint': None, 'run_id': 'r2',
        'evidence': {}}


class RetryWorker(FakeWorker):
    """Follows a script of results, one per attempt, and records resubmissions."""

    script = []
    output = {}
    resubmitted = []
    resubmit_raises = None

    def follow(self, run_id, *, on_log=None, on_status=None, **kwargs):
        if on_status is not None:
            on_status({'ok': True, 'state': 'running'})
        for chunk in RetryWorker.output.get(run_id, []):
            on_log(chunk)
        return dict(RetryWorker.script.pop(0)), 0

    def resubmit(self, run_id, *, request_id):
        if RetryWorker.resubmit_raises is not None:
            raise RetryWorker.resubmit_raises
        RetryWorker.resubmitted.append((run_id, request_id))
        return Submission('r2')


class DaemonRetry(DaemonCase):
    def setUp(self):
        super().setUp()
        RetryWorker.script, RetryWorker.output = [], {}
        RetryWorker.resubmitted, RetryWorker.resubmit_raises = [], None
        self.daemon.worker_factory = RetryWorker
        self.daemon.workers.clear()

    def run_unit(self):
        answer = self.call(['pnpm', 'unit'])
        return answer, self.result_of(answer.accepted['run'])

    def test_an_infra_failure_before_output_is_resubmitted_once(self):
        RetryWorker.script = [INFRA, PASS]
        RetryWorker.output = {'r1': [b'pandora: turbo cache off: no server\n'],
                              'r2': [b'all green\n']}
        answer, result = self.run_unit()
        self.assertEqual(answer.exit, 0)
        run_id = answer.accepted['run']
        self.assertEqual(RetryWorker.resubmitted, [('r1', '%s:unit:retry' % run_id)])
        self.assertIn(b'pandora: infrastructure failure before output (clone-failed); '
                      b'retrying once\n', answer.err)
        self.assertEqual(answer.out, b'all green\n', 'pandora lines are never stdout')
        self.assertIn(b'pandora: turbo cache off: no server\n', answer.err)
        self.assertEqual([item['remote'] for item in result['attempts']], ['r1', 'r2'])
        self.assertEqual(result['retry'], {'retried': True, 'of': 'r1', 'cause': 'clone-failed'})
        meta = json.loads((self.state / 'runs' / run_id / 'meta.json').read_text())
        self.assertEqual(meta['remote'], 'r2')
        self.assertEqual(len(meta['attempts']), 1)
        self.assertFalse(self.marker.exists(), 'a retry never runs locally')

    def test_the_second_failure_is_70_and_the_hint_names_both(self):
        RetryWorker.script = [INFRA, dict(INFRA, run_id='r2',
                                          evidence={'cause': 'instance-lost'})]
        answer, result = self.run_unit()
        self.assertEqual(answer.exit, 70)
        self.assertEqual(len(RetryWorker.resubmitted), 1)
        last = answer.err.rstrip().splitlines()[-1].decode()
        self.assertTrue(last.startswith('pandora: hint: infrastructure failed on both '
                                        'attempts: r1 (clone-failed), then r2 (instance-lost)'),
                        last)
        self.assertEqual(len(result['attempts']), 2)
        self.assertFalse(self.marker.exists())

    def test_output_that_reached_the_caller_forbids_the_retry(self):
        RetryWorker.script = [INFRA]
        RetryWorker.output = {'r1': [b'pandora: instance ready in 0.4 s\n', b'RUN  v1.6\n']}
        answer, result = self.run_unit()
        self.assertEqual(answer.exit, 70)
        self.assertEqual(RetryWorker.resubmitted, [])
        self.assertNotIn(b'retrying once', answer.err)
        self.assertIn("output had already reached you", result['hint'])
        self.assertEqual(result['retry']['retried'], False)

    def test_half_a_line_of_output_is_output(self):
        RetryWorker.script = [INFRA]
        RetryWorker.output = {'r1': [b'compiling']}
        answer, _ = self.run_unit()
        self.assertEqual(answer.exit, 70)
        self.assertEqual(RetryWorker.resubmitted, [])

    def test_a_cause_the_table_refuses_is_not_retried_and_says_why(self):
        RetryWorker.script = [dict(INFRA, evidence={'cause': 'prepare-failed'})]
        answer, result = self.run_unit()
        self.assertEqual(answer.exit, 70)
        self.assertEqual(RetryWorker.resubmitted, [])
        self.assertIn('infrastructure failure (prepare-failed) was not retried: a golden that '
                      'failed to build', result['hint'])

    def test_a_retry_the_worker_will_not_take_is_70_and_never_local(self):
        RetryWorker.script = [INFRA]
        RetryWorker.resubmit_raises = WorkerUnreachable('gone')
        answer, result = self.run_unit()
        self.assertEqual(answer.exit, 70)
        self.assertIn(b'the retry could not be submitted', answer.err)
        self.assertFalse(self.marker.exists(), 'a failed retry fell back')

    def test_a_command_failure_is_a_verdict_and_is_never_retried(self):
        RetryWorker.script = [dict(PASS, outcome='command_failed', cli_exit=1)]
        answer, result = self.run_unit()
        self.assertEqual(answer.exit, 1)
        self.assertEqual(RetryWorker.resubmitted, [])
        self.assertNotIn('retry', result)

    def test_stats_counts_the_retry(self):
        RetryWorker.script = [INFRA, PASS]
        self.run_unit()
        report = self.daemon.stats('24h')
        self.assertEqual(report['retries'], {'runs': 1, 'recovered': 1,
                                             'causes': {'clone-failed': 1}})


class RetryVerdict(unittest.TestCase):
    """The rules the socket tests cannot reach cheaply: cancel, write-back, doubt."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.daemon = daemon_module.Daemon.__new__(daemon_module.Daemon)

    def fresh(self, argv=('pnpm', 'unit')):
        return daemon_module.Run(self.tmp.name, 'x%d' % time.monotonic_ns(),
                                 {'argv': list(argv), 'job': 'unit'})

    def test_a_cancelled_run_is_never_retried(self):
        run = self.fresh()
        run.cancelled.set()
        go, _, why = self.daemon.retry_verdict(run, {'options': {}}, dict(INFRA))
        self.assertEqual((go, why), (False, 'the run was cancelled'))

    def test_write_back_before_output_may_retry(self):
        go, _, _ = self.daemon.retry_verdict(self.fresh(), {'options': {'update': True}},
                                             dict(INFRA))
        self.assertTrue(go)

    def test_write_back_this_daemon_cannot_read_is_not_retried(self):
        run = self.fresh(argv=('pnpm', 'writer', '--update'))
        go, _, why = self.daemon.retry_verdict(run, None, dict(INFRA))
        self.assertFalse(go)
        self.assertIn('write-back is armed', why)

    def test_the_retry_is_never_retried(self):
        run = self.fresh()
        run.attempts.append({'remote': 'r1'})
        self.assertFalse(self.daemon.retry_verdict(run, {'options': {}}, dict(INFRA))[0])


# -- progress lines on the client ----------------------------------------------

class ProgressLines(DaemonCase):
    def test_a_transfer_is_announced_before_accepted(self):
        original = FakeWorker.submit

        def submit(self, *, progress=None, **kwargs):
            progress('syncing 3 files, 1 KiB')
            return Submission()

        FakeWorker.submit = submit
        self.addCleanup(setattr, FakeWorker, 'submit', original)
        answer = self.call(['pnpm', 'unit'])
        self.assertIn('syncing 3 files, 1 KiB', answer.notices)

    def test_attach_says_where_the_run_is_once(self):
        answer = self.call(['pnpm', 'unit'])
        run_id = answer.accepted['run']
        import socket
        from pandora.client.protocol import Reader, VERSION, dump
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(self.daemon.socket_path))
        sock.sendall(dump({'v': VERSION, 'op': 'attach', 'run': run_id, 'from': 0}))
        frame = Reader(sock).line()
        sock.close()
        self.assertEqual(frame['phase'], 'run %s finished: passed, exit 0' % run_id)

    def test_a_running_remote_run_names_its_phase_and_elapsed_time(self):
        run = daemon_module.Run(self.state, 'abc', {'job': 'unit'})
        run.state, run.remote, run.phase, run.accepted = 'running', 'r9', 'running', 1000.0
        self.assertEqual(progress.attach_line(self.state, run, now=1080.0),
                         'run abc is running on the worker as r9 (1m20s since accepted)')

    def test_the_typical_time_comes_from_this_macs_history(self):
        for number in range(3):
            directory = self.state / 'runs' / ('h%d' % number)
            directory.mkdir(parents=True)
            (directory / 'meta.json').write_text(json.dumps(
                {'id': 'h%d' % number, 'job': 'unit', 'lane': 'remote', 'state': 'passed'}))
            (directory / 'result.json').write_text(json.dumps(
                {'durations': {'execute': 200.0 + number * 50}}))
        run = daemon_module.Run(self.state, 'abc', {'job': 'unit'})
        run.state, run.remote, run.phase, run.accepted = 'running', 'r9', 'admitted', 1000.0
        self.assertEqual(progress.attach_line(self.state, run, now=1010.0),
                         'run abc is starting an instance on the worker as r9 (10 s since '
                         'accepted; typical 4m10s for unit)')


class LocalQueueLine(unittest.TestCase):
    def test_a_wait_is_said_once_with_its_estimate(self):
        lane = budget(max_running=1)
        lane.estimate = lambda: 40.0
        lane.reserve('a', repo='demo', job='unit', worktree='/tmp/a', singleton=False)
        self.assertTrue(lane.admit('a', repo='demo', job='unit')['admitted'])
        lane.reserve('b', repo='demo', job='unit', worktree='/tmp/b', singleton=False)
        said = []
        self.assertIsNone(lane.admit('b', repo='demo', job='unit', timeout=0.4, poll=0.05,
                                     note=said.append))
        self.assertEqual(said, ['queued behind 1 run, ~40 s'])

    def test_the_repeat_is_once_a_minute_and_carries_no_estimate(self):
        lane = budget()
        said = []
        at = lane.say_queued(said.append, None)
        self.assertEqual(lane.say_queued(said.append, at), at)
        self.assertEqual(len(said), 1)
        lane.say_queued(said.append, at - progress.STILL_EVERY)
        self.assertEqual(said[-1], 'still queued behind 0 runs')


class SyncLine(unittest.TestCase):
    def test_it_names_the_tree_it_is_about_to_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'a').write_bytes(b'x' * 3 * 1048576)
            (Path(tmp) / 'b').write_bytes(b'y' * 1024)
            manifest = [{'path': 'a'}, {'path': 'b'}, {'path': 'l', 'link': 'a'}]
            self.assertEqual(sync_line(tmp, manifest), 'syncing 3 files, 3 MiB')


# -- the readers: result and stats ---------------------------------------------

class Readers(unittest.TestCase):
    def test_result_shows_both_attempts_and_the_flaky_pair(self):
        text = cli.render_result('abc', {
            'outcome': 'passed', 'cli_exit': 0, 'wall_seconds': 60,
            'attempts': [{'remote': 'r1', 'outcome': 'infra_failed', 'cause': 'clone-failed'},
                         {'remote': 'r2', 'outcome': 'passed', 'cause': None}],
            'flaky': {'order': 'failed-then-passed', 'failed': 'old', 'passed': 'abc'}})
        self.assertIn('  attempt 1 r1: infra_failed (clone-failed)', text)
        self.assertIn('  attempt 2 r2: passed', text)
        self.assertIn('flaky: whole run failed-then-passed (failed old, passed abc)', text)

    def test_stats_counts_flaky_pairs_in_its_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            for run_id, flaky in (('a', None),
                                  ('b', {'order': 'failed-then-passed', 'failed': 'a'}),
                                  ('c', {'with': 'x', 'shards': [{'shard': '1/2'},
                                                                 {'shard': '2/2'}]})):
                directory = Path(tmp) / 'runs' / run_id
                directory.mkdir(parents=True)
                (directory / 'meta.json').write_text(json.dumps(
                    {'id': run_id, 'job': 'unit', 'state': 'passed', 'started': time.time()}))
                (directory / 'result.json').write_text(json.dumps(
                    {'outcome': 'passed', 'flaky': flaky}))
            report = statistics.build(tmp, since=time.time() - 3600)
            self.assertEqual(report['flaky'], {'pairs': 1, 'shard_pairs': 2})
            self.assertIn('flaky: 1 run pair(s), 2 shard pair(s)', statistics.render(report))


if __name__ == '__main__':
    unittest.main()
