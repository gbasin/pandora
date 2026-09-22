"""The worker monitor: what a poll records, what a transition announces, and
what a stale reading is allowed to claim."""
import json
import tempfile
import unittest
from pathlib import Path

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
        state = mon.poll()
        self.assertEqual(state['worker'], 'down')
        self.assertIn('timed out', state['reason'])
        self.assertTrue(mon.known_down())

    def test_an_unexpected_exception_is_down_never_a_crash(self):
        mon, _, _ = monitor(RuntimeError('boom'))
        self.assertEqual(mon.poll()['worker'], 'down')

    def test_a_pool_below_its_floor_reads_as_such(self):
        mon, _, _ = monitor(answer(ok=False, capacity={'ok': False, 'free_gib': 1.0}))
        self.assertEqual(mon.poll()['disk'], 'below floor')


class Staleness(unittest.TestCase):
    def test_a_reading_older_than_the_stale_window_is_unknown(self):
        clock = Clock()
        mon, _, _ = monitor(PandoraError('gone'), clock=clock, interval=60)
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

    def test_notifications_can_be_disabled(self):
        calls = []
        mon = health.Monitor(lambda: Worker(answer()), notify_enabled=False, clock=Clock(),
                             notifier=lambda *a, **k: calls.append(k))
        mon.poll()
        mon.open_worker = lambda: Worker(PandoraError('x'))
        mon.poll()
        self.assertEqual(calls, [{'enabled': False}])


class Store(unittest.TestCase):
    def test_a_reading_survives_a_daemon_restart(self):
        with tempfile.TemporaryDirectory() as home:
            store = Path(home) / 'worker-health.json'
            clock = Clock()
            mon, _, _ = monitor(PandoraError('gone'), clock=clock, store=store)
            mon.poll()
            saved = json.loads(store.read_text())
            self.assertEqual(saved['worker'], 'down')
            again, _, notes = monitor(answer(), clock=clock, store=store)
            self.assertTrue(again.known_down(), 'loaded from disk before any poll')
            again.poll()
            self.assertEqual([t for t, _ in notes], ['pandora: worker back'])

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


if __name__ == '__main__':
    unittest.main()
