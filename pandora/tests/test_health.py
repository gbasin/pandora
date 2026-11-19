"""The worker monitor: what a poll records, what a transition announces, and
what a stale reading is allowed to claim."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pandora.client import health
from pandora.errors import PandoraError


def answer(**over):
    base = {'ok': True, 'capacity': {'ok': True, 'free_gib': 12.5, 'floor_gib': 4},
            'goldens': ['golden-a', 'golden-b'], 'state': 'ready',
            'canary': {'ok': True, 'failures': 0}, 'kernel_drift': False}
    base.update(over)
    return base


class Worker:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def health(self, **kwargs):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class Clock:
    def __init__(self, at=1000.0):
        self.at = at

    def __call__(self):
        return self.at


def monitor(reply, *, clock=None, store=None, notes=None, interval=60):
    worker = Worker(reply)
    notes = notes if notes is not None else []
    mon = health.Monitor(lambda: worker, interval=interval, notify_enabled=True,
                         store=store, clock=clock or Clock(),
                         notifier=lambda title, msg, **k: notes.append((title, msg)))
    return mon, worker, notes


class Polling(unittest.TestCase):
    def test_an_ok_answer_is_reachable_with_its_facts(self):
        mon, worker, _ = monitor(answer())
        state = mon.poll()
        self.assertEqual(state['worker'], 'reachable')
        self.assertEqual(state['goldens'], 2)
        self.assertEqual(state['disk'], '12.5 GiB free')
        self.assertEqual(state['ready'], 'ready')
        self.assertEqual(worker.calls, 1)

    def test_a_bad_answer_is_degraded_not_down(self):
        mon, _, _ = monitor(answer(ok=False, reason='the last canary failed',
                                   canary={'ok': False, 'failures': 2}))
        state = mon.poll()
        self.assertEqual(state['worker'], 'degraded')
        self.assertEqual(state['reason'], 'the last canary failed')
        self.assertFalse(mon.known_down())

    def test_no_answer_is_down(self):
        mon, _, _ = monitor(PandoraError('ssh: connect timed out'))
        self.assertEqual(mon.poll()['worker'], 'unknown')
        state = mon.poll()
        self.assertEqual(state['worker'], 'down')
        self.assertIn('timed out', state['reason'])
        self.assertTrue(mon.known_down())

    def test_an_unexpected_exception_is_down_never_a_crash(self):
        mon, _, _ = monitor(RuntimeError('boom'))
        self.assertEqual(mon.poll()['worker'], 'unknown')
        self.assertEqual(mon.poll()['worker'], 'down')

    def test_a_pool_below_its_floor_reads_as_such(self):
        mon, _, _ = monitor(answer(ok=False, capacity={'ok': False, 'free_gib': 1.0}))
        self.assertEqual(mon.poll()['disk'], 'below floor')

    def test_one_timeout_keeps_the_previous_reading_without_refreshing_it(self):
        clock = Clock()
        mon, worker, notes = monitor(answer(), clock=clock)
        good = mon.poll()
        clock.at += 60
        worker.reply = subprocess.TimeoutExpired('ssh health', 20)
        state = mon.poll()
        self.assertEqual(state['worker'], 'reachable')
        self.assertEqual(state['at'], good['at'])
        self.assertEqual(state['age_seconds'], 60)
        self.assertFalse(mon.known_down())
        self.assertEqual(notes, [])
        self.assertEqual(mon.poll()['worker'], 'down')
        self.assertEqual([t for t, _ in notes], ['pandora: worker down'])

    def test_an_answer_between_failures_resets_the_count_even_when_degraded(self):
        mon, worker, _ = monitor(answer())
        mon.poll()
        worker.reply = PandoraError('slow')
        mon.poll()
        worker.reply = answer(ok=False, reason='canary failed')
        mon.poll()
        worker.reply = PandoraError('slow again')
        self.assertEqual(mon.poll()['worker'], 'degraded')
        self.assertFalse(mon.known_down())
        self.assertEqual(mon.poll()['worker'], 'down')
        worker.reply = answer()
        self.assertEqual(mon.poll()['worker'], 'reachable')


class EngineProbe(unittest.TestCase):
    def probe(self, *, listing_error=None, capacity_error=None):
        from pandora.engine import service, runner
        with tempfile.TemporaryDirectory() as home:
            driver = mock.Mock()
            driver.instances.return_value = [{'name': 'golden-a'}]
            driver.instances.side_effect = listing_error
            driver.capacity.return_value = {'ok': True, 'free_gib': 100}
            driver.capacity.side_effect = capacity_error
            with mock.patch('pandora.executor.incus.IncusDriver', return_value=driver), \
                    mock.patch.object(runner, 'gc_sources') as collect, \
                    mock.patch.object(service, 'emit', side_effect=lambda value: value):
                result = service.cmd_health(SimpleNamespace(root=home, worker_root=home))
            collect.assert_not_called()
            driver.capacity.assert_called_once_with(floor_gib=4, timeout=5)
            driver.instances.assert_called_once_with(check=True, timeout=5)
            return result

    def test_health_bounds_external_checks_and_does_no_cleanup(self):
        result = self.probe()
        self.assertTrue(result['ok'])
        self.assertEqual(result['goldens'], ['golden-a'])
        self.assertIsNone(result['src_removed'])

    def test_a_slow_listing_returns_degraded_instead_of_hanging(self):
        result = self.probe(listing_error=subprocess.TimeoutExpired('incus list', 5))
        self.assertFalse(result['ok'])
        self.assertIn('goldens unreadable', result['reason'])

    def test_slow_disk_check_reports_unmeasured_and_still_answers(self):
        result = self.probe(capacity_error=subprocess.TimeoutExpired('btrfs', 5))
        self.assertFalse(result['capacity']['measured'])
        self.assertTrue(result['ok'])


class Staleness(unittest.TestCase):
    def test_a_reading_older_than_the_stale_window_is_unknown(self):
        clock = Clock()
        mon, _, _ = monitor(PandoraError('gone'), clock=clock, interval=60)
        mon.poll()
        mon.poll()
        self.assertTrue(mon.known_down())
        clock.at += 60 * health.STALE_FACTOR + 1
        state = mon.state()
        self.assertTrue(state['stale'])
        self.assertEqual(state['worker'], 'unknown')
        self.assertFalse(mon.known_down(), 'stale is not down')

    def test_a_fresh_reading_is_not_stale(self):
        clock = Clock()
        mon, _, _ = monitor(answer(), clock=clock)
        mon.poll()
        clock.at += 30
        self.assertFalse(mon.state()['stale'])


class Transitions(unittest.TestCase):
    def test_down_then_back_announces_both_edges_once(self):
        mon, worker, notes = monitor(answer())
        mon.poll()
        self.assertEqual(notes, [])
        worker.reply = PandoraError('no route')
        mon.poll()
        mon.poll()
        self.assertEqual([t for t, _ in notes], ['pandora: worker down'])
        worker.reply = answer()
        mon.poll()
        self.assertEqual([t for t, _ in notes],
                         ['pandora: worker down', 'pandora: worker back'])

    def test_a_first_reading_of_down_is_not_a_transition(self):
        mon, _, notes = monitor(PandoraError('no route'))
        mon.poll()
        self.assertEqual(notes, [])

    def test_canary_disk_and_kernel_edges(self):
        mon, worker, notes = monitor(answer())
        mon.poll()
        worker.reply = answer(ok=False, canary={'ok': False, 'failures': 3},
                              capacity={'ok': False}, kernel_drift=True)
        mon.poll()
        mon.poll()
        self.assertEqual([t for t, _ in notes],
                         ['pandora: worker canary failed', 'pandora: worker disk floor',
                          'pandora: worker kernel drift'])
        self.assertIn('3 failure', notes[0][1])

    def test_a_degraded_edge_without_a_named_cause_announces_once(self):
        mon, worker, notes = monitor(answer())
        mon.poll()
        worker.reply = answer(ok=False, reason='provisioning is still running')
        mon.poll()
        mon.poll()
        self.assertEqual([t for t, _ in notes], ['pandora: worker degraded'])
        self.assertIn('provisioning is still running', notes[0][1])

    def test_a_degraded_edge_names_the_worker_state_when_there_is_no_reason(self):
        mon, worker, notes = monitor(answer())
        mon.poll()
        worker.reply = answer(ok=False, state='unproven')
        mon.poll()
        self.assertEqual([t for t, _ in notes], ['pandora: worker degraded'])
        self.assertIn('unproven', notes[0][1])

    def test_a_named_degraded_cause_does_not_announce_twice(self):
        # The canary edge says it; the generic edge stands down.
        mon, worker, notes = monitor(answer())
        mon.poll()
        worker.reply = answer(ok=False, canary={'ok': False, 'failures': 1})
        mon.poll()
        self.assertEqual([t for t, _ in notes], ['pandora: worker canary failed'])

    def test_coming_back_degraded_is_a_worker_back_not_a_second_alarm(self):
        mon, worker, notes = monitor(answer())
        mon.poll()
        worker.reply = PandoraError('gone')
        mon.poll()
        mon.poll()
        worker.reply = answer(ok=False, reason='still reimaging')
        mon.poll()
        self.assertEqual([t for t, _ in notes],
                         ['pandora: worker down', 'pandora: worker back'])

    def test_disk_near_the_floor_warns_once_then_the_floor_edge_takes_over(self):
        mon, worker, notes = monitor(answer())
        mon.poll()
        worker.reply = answer(capacity={'ok': True, 'free_gib': 5.0, 'floor_gib': 4})
        mon.poll()
        mon.poll()
        self.assertEqual([t for t, _ in notes], ['pandora: worker disk low'])
        self.assertIn('5.0 GiB', notes[0][1])
        worker.reply = answer(ok=False, capacity={'ok': False, 'free_gib': 1.0,
                                                  'floor_gib': 4})
        mon.poll()
        self.assertEqual([t for t, _ in notes],
                         ['pandora: worker disk low', 'pandora: worker disk floor'])

    def test_a_pool_that_starts_below_the_floor_does_not_warn_twice(self):
        mon, worker, notes = monitor(answer())
        mon.poll()
        worker.reply = answer(ok=False, capacity={'ok': False, 'free_gib': 1.0,
                                                  'floor_gib': 4})
        mon.poll()
        self.assertEqual([t for t, _ in notes], ['pandora: worker disk floor'])

    def test_notifications_can_be_disabled(self):
        calls = []
        mon = health.Monitor(lambda: Worker(answer()), notify_enabled=False, clock=Clock(),
                             notifier=lambda *a, **k: calls.append(k))
        mon.poll()
        mon.open_worker = lambda: Worker(PandoraError('x'))
        mon.poll()
        mon.poll()
        self.assertEqual(calls, [{'enabled': False}])


class Store(unittest.TestCase):
    def test_cached_down_is_unknown_until_a_fresh_probe_answers(self):
        with tempfile.TemporaryDirectory() as home:
            store = Path(home) / 'worker-health.json'
            clock = Clock()
            mon, _, _ = monitor(PandoraError('gone'), clock=clock, store=store)
            mon.poll()
            mon.poll()
            saved = json.loads(store.read_text())
            self.assertEqual(saved['worker'], 'down')
            again, _, notes = monitor(answer(), clock=clock, store=store)
            self.assertFalse(again.known_down())
            self.assertEqual(again.state()['worker'], 'unknown')
            self.assertEqual(again.state()['at'], saved['at'])
            self.assertEqual(again.state()['reason'], saved['reason'])
            again.poll()
            self.assertEqual(again.state()['worker'], 'reachable')
            self.assertEqual([t for t, _ in notes], ['pandora: worker back'])

    def test_cached_down_still_needs_two_new_failed_probes(self):
        with tempfile.TemporaryDirectory() as home:
            store = Path(home) / 'worker-health.json'
            store.write_text(json.dumps({'worker': 'down', 'at': 1000,
                                         'reason': 'old timeout'}))
            mon, _, _ = monitor(PandoraError('new timeout'), store=store)
            self.assertEqual(mon.poll()['worker'], 'unknown')
            self.assertFalse(mon.known_down())
            self.assertEqual(mon.poll()['worker'], 'down')
            self.assertEqual(mon.state()['reason'], 'new timeout')

    def test_cached_reachable_still_announces_down_after_two_new_failures(self):
        with tempfile.TemporaryDirectory() as home:
            store = Path(home) / 'worker-health.json'
            store.write_text(json.dumps({'worker': 'reachable', 'at': 1000}))
            mon, _, notes = monitor(PandoraError('new timeout'), store=store)
            mon.poll()
            self.assertEqual(notes, [])
            mon.poll()
            self.assertEqual([t for t, _ in notes], ['pandora: worker down'])

    def test_cached_degraded_does_not_announce_the_same_problem_again(self):
        with tempfile.TemporaryDirectory() as home:
            store = Path(home) / 'worker-health.json'
            store.write_text(json.dumps({'worker': 'degraded', 'at': 1000,
                                         'reason': 'still initializing'}))
            mon, _, notes = monitor(answer(ok=False, reason='still initializing'),
                                     store=store)
            self.assertEqual(mon.poll()['worker'], 'degraded')
            self.assertEqual(notes, [])

    def test_an_unwritable_store_is_not_fatal(self):
        mon, _, _ = monitor(answer(), store='/nonexistent/dir/health.json')
        self.assertEqual(mon.poll()['worker'], 'reachable')


class Notify(unittest.TestCase):
    def test_it_is_a_no_op_off_darwin_and_when_disabled(self):
        calls = []
        self.assertFalse(health.notify('t', 'm', platform='linux', run=calls.append))
        self.assertFalse(health.notify('t', 'm', enabled=False, platform='darwin',
                                       run=calls.append))
        self.assertEqual(calls, [])

    def test_quotes_and_newlines_are_escaped_before_osascript(self):
        seen = []

        def run(argv, **kwargs):
            seen.append(argv)

        self.assertTrue(health.notify('pandora: "x"', 'a "b"\nc', platform='darwin', run=run))
        script = seen[0][2]
        self.assertIn('display notification "a \\"b\\" c" with title "pandora: \\"x\\""', script)

    def test_a_failing_osascript_is_swallowed(self):
        def run(argv, **kwargs):
            raise OSError('no osascript')

        self.assertFalse(health.notify('t', 'm', platform='darwin', run=run))


class Recheck(unittest.TestCase):
    def test_a_nudge_wakes_the_loop_without_deciding_anything(self):
        mon, _, _ = monitor(answer())
        mon.poll()
        mon.recheck()
        self.assertTrue(mon.nudge.is_set())
        self.assertEqual(mon.state()['worker'], 'reachable', 'a nudge is a question, not a verdict')


def engine_failure(kind, message, code, stderr):
    error = kind(message)
    error.returncode, error.stderr = code, stderr
    return error


class Timer:
    """A monotonic clock each probe advances by `step` seconds."""

    def __init__(self, step):
        self.step, self.at, self.calls = step, 0.0, 0

    def __call__(self):
        self.calls += 1
        if self.calls % 2 == 0:
            self.at += self.step
        return self.at


class FakeGate:
    def __init__(self, reading, previous=None, evidence=None, now=100.0):
        self.reader = lambda: reading
        self.previous = previous
        self.evidence = evidence
        self.clock = lambda: now


class Diagnosis(unittest.TestCase):
    """A failed or slow probe says which stage failed, in one line."""

    def test_ssh_exit_255_is_the_transport_with_its_hint(self):
        from pandora.errors import WorkerUnreachable
        error = engine_failure(
            WorkerUnreachable, 'ssh pandora-worker: ...', 255,
            'debug1: x\nssh: connect to host 10.0.0.1 port 22: Operation timed out\n')
        found = health.classify(error)
        self.assertEqual((found['stage'], found['exit'], found['hint']),
                         ('transport', 255, 'connect timeout'))
        self.assertEqual(health.describe(found),
                         'transport, ssh exit 255 (connect timeout): '
                         'ssh: connect to host 10.0.0.1 port 22: Operation timed out')
        for stderr, hint in (('Permission denied (publickey).', 'auth'),
                             ('mux_client_request_session: read from master failed', 'mux'),
                             ('ssh: Could not resolve hostname x', 'dns')):
            self.assertEqual(health.classify(engine_failure(
                WorkerUnreachable, 'ssh x', 255, stderr))['hint'], hint)

    def test_the_remote_command_failing_is_the_remote_stage(self):
        from pandora.errors import EngineError
        found = health.classify(engine_failure(
            EngineError, 'engine health failed (1): boom', 1,
            'Traceback\nModuleNotFoundError: No module named pandora'))
        self.assertEqual((found['stage'], found['detail']), ('remote', 'exit 1'))
        self.assertEqual(found['stderr'], 'ModuleNotFoundError: No module named pandora')
        found = health.classify(engine_failure(
            EngineError, 'engine health returned non-JSON: hi', 0, ''))
        self.assertEqual((found['stage'], found['detail']), ('remote', 'bad JSON'))
        found = health.classify(engine_failure(
            EngineError, 'engine health said nothing; stderr: ', 0, ''))
        self.assertEqual((found['stage'], found['detail']), ('remote', 'no output'))

    def test_a_timeout_with_no_output_is_the_client_timeout(self):
        error = subprocess.TimeoutExpired(['ssh', 'a', 'very', 'long', 'argv'], 20)
        found = health.classify(error)
        self.assertEqual((found['stage'], found['detail']), ('timeout', 'no output within 20 s'))
        self.assertNotIn('argv', health.describe(found))

    def test_a_timeout_after_output_is_the_remote_command_running_long(self):
        error = subprocess.TimeoutExpired(['ssh'], 20, output=b'{"partial', stderr=b'')
        found = health.classify(error)
        self.assertEqual(found['stage'], 'remote')
        self.assertIn('still running at the 20 s timeout', found['detail'])

    def test_a_failed_probe_logs_one_compact_line_and_keeps_its_latency(self):
        lines = []
        reading = {'swap_used_mib': 3072.0, 'free_percent': 18.0, 'load_per_cpu': 0.9}
        gate = FakeGate(reading, previous=(40.0, 3000.0), evidence=None)
        with tempfile.TemporaryDirectory() as home:
            store = Path(home) / 'worker-health.json'
            worker = Worker(subprocess.TimeoutExpired(['ssh', 'argv'], 20))
            mon = health.Monitor(lambda: worker, notify_enabled=False, clock=Clock(),
                                 store=store, log=lines.append, gate=gate,
                                 timer=Timer(20.0))
            mon.poll()
            self.assertEqual(lines, [
                'worker health probe failed after 20.0 s (consecutive failure 1): '
                'timeout, no output within 20 s; host: swap 3072 MiB '
                '(+72 MiB/min over 60 s), 18.0% free, load 0.90/core'])
            saved = json.loads(store.read_text())
            self.assertEqual(saved['probe'], {'seconds': 20.0, 'at': 1000.0, 'ok': False,
                                              'stage': 'timeout'})
            self.assertEqual(saved['worker'], 'unknown', 'one failure changes no verdict')
            mon.poll()
            self.assertIn('consecutive failure 2', lines[1])
            self.assertEqual(mon.state()['worker'], 'down')
            self.assertEqual(mon.state()['reason'], 'timeout: no output within 20 s')

    def test_a_slow_answer_logs_and_a_quick_one_does_not(self):
        lines = []
        gate = FakeGate({'free_percent': 50.0}, evidence='load average 9.0 per core, limit 8.0')
        mon = health.Monitor(lambda: Worker(answer()), notify_enabled=False, clock=Clock(),
                             log=lines.append, gate=gate, timer=Timer(6.25))
        state = mon.poll()
        self.assertEqual(state['worker'], 'reachable')
        self.assertEqual(state['probe']['seconds'], 6.25)
        self.assertEqual(lines, ['worker health probe slow: answered after 6.2 s; host: '
                                 '50.0% free, local gate closed: load average 9.0 per '
                                 'core, limit 8.0'])
        mon.timer = Timer(0.4)
        mon.poll()
        self.assertEqual(len(lines), 1)

    def test_the_mux_check_tells_a_wedged_master_from_a_missing_one(self):
        worker = SimpleNamespace(link=SimpleNamespace(options=['-o', 'X=y'], host='w'))

        def wedged(argv, **kwargs):
            self.assertEqual(argv[-3:], ['-O', 'check', 'w'])
            raise subprocess.TimeoutExpired(argv, kwargs['timeout'])
        self.assertIn('wedged', health.mux_state(worker, run=wedged))
        self.assertEqual(health.mux_state(worker, run=lambda argv, **k: SimpleNamespace(
            returncode=0, stderr=b'')), 'master running')
        self.assertEqual(health.mux_state(worker, run=lambda argv, **k: SimpleNamespace(
            returncode=255, stderr=b'Control socket connect(/x): No such file')),
            'no master: Control socket connect(/x): No such file')
        self.assertIsNone(health.mux_state(SimpleNamespace()))

    def test_a_host_too_slow_to_read_says_so(self):
        import threading
        release = threading.Event()
        gate = FakeGate({})
        gate.reader = lambda: release.wait(5) and {}
        try:
            self.assertIn('unread after 0.05 s', health.host_pressure(gate, wait=0.05))
        finally:
            release.set()


class Steps:
    """A monotonic clock that returns the next value each call."""

    def __init__(self, values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0)


class DiagnosticsNeverDecide(unittest.TestCase):
    """Nothing that builds the diagnostic line can stop or delay the polling."""

    def test_a_host_that_cannot_start_threads_or_processes_still_goes_down(self):
        lines = []
        worker = Worker(subprocess.TimeoutExpired(['ssh'], 20))
        worker.link = SimpleNamespace(options=['-o', 'X=y'], host='w')
        mon = health.Monitor(lambda: worker, notify_enabled=False, clock=Clock(),
                             log=lines.append, timer=Timer(20.0))
        with mock.patch('threading.Thread.start',
                        side_effect=RuntimeError("can't start new thread")), \
                mock.patch('subprocess.Popen', side_effect=OSError('no processes')):
            mon.poll()
            state = mon.poll()
        self.assertEqual(state['worker'], 'down')
        self.assertEqual(state['probe']['stage'], 'timeout')
        self.assertEqual(len(lines), 2)
        self.assertIn('[diagnostics failed: RuntimeError]', lines[1])
        self.assertIn('consecutive failure 2', lines[1])

    def test_the_thread_outlives_a_poll_that_raises(self):
        lines = []
        mon = health.Monitor(lambda: Worker(answer()), notify_enabled=False, clock=Clock(),
                             log=lines.append)
        mon.interval = 0.01
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("can't start new thread")
            mon.stopping.set()
        mon.poll = flaky
        mon.run()
        self.assertEqual(len(calls), 2)
        self.assertIn('polling continues', lines[0])


class DiagnosticsDuringAnOutage(unittest.TestCase):
    def test_first_failure_down_transition_then_every_ten_minutes(self):
        lines, reads = [], []
        gate = FakeGate({})
        gate.reader = lambda: reads.append(1) or {'free_percent': 40.0}
        worker = Worker(subprocess.TimeoutExpired(['ssh'], 20))
        # Two timer calls per poll: start and end.
        ends = [20, 80, 140, 200, 820]
        mon = health.Monitor(lambda: worker, notify_enabled=False, clock=Clock(),
                             log=lines.append, gate=gate,
                             timer=Steps([v for end in ends for v in (end - 20, end)]
                                         + [900, 900.5]))
        for _ in ends:
            mon.poll()
        self.assertEqual(mon.state()['worker'], 'down')
        self.assertEqual([line.split('(')[1].split(')')[0] for line in lines],
                         ['consecutive failure 1', 'consecutive failure 2',
                          'consecutive failure 5'])
        self.assertEqual(len(reads), 3, 'a poll that logs nothing reads nothing')
        worker.reply = answer()
        mon.poll()
        self.assertEqual(lines[-2:], ['worker health probe answered after 0.5 s, after 5 '
                                      'consecutive failure(s)',
                                      'pandora: worker back: the engine answered; state '
                                      'reachable'])


if __name__ == '__main__':
    unittest.main()
