"""A full worker, seen from the Mac: the run waits before `accepted`, and says so.

The daemon is real (socket, classifier, rows); the worker is fake and answers
`submit` with `state: queued`, then whatever `status` rows a test scripts. The
subject is the pre-accept wait: notice frames with the queue place, the
heartbeat under them, and every way the wait can end -- admitted, timed out,
canceled, withdrawn for a restart -- none of which is a fallback.
"""
import json
import socket
import threading
import time
import unittest

from pandora.client import stats
from pandora.client.protocol import Reader, VERSION, dump
from pandora.errors import WorkerUnreachable
from pandora.tests.test_fallback import DaemonCase, FakeWorker, Submission

PLACE = {'position': 2, 'ahead': 1, 'running': 3, 'eta_seconds': 250,
         'bound_seconds': 600, 'waited_seconds': 0}


class QueueWorker(FakeWorker):
    """Answers `submit` with a queued run; `status` walks a scripted list."""

    rows = []
    final = None
    calls = []
    cancel_answer = {'ok': True, 'withdrawn': True}
    withdraw_answer = {'ok': True, 'withdrawn': True}

    def submit(self, **kwargs):
        submission = Submission('rq1')
        submission.state = 'queued'
        submission.queued = dict(PLACE)
        return submission

    def status(self, run_id):
        QueueWorker.calls.append('status')
        row = QueueWorker.rows.pop(0) if len(QueueWorker.rows) > 1 else QueueWorker.rows[0]
        if isinstance(row, Exception):
            raise row
        return dict(row, ok=True)

    def result(self, run_id):
        return {'ok': True, 'result': QueueWorker.final}

    def cancel(self, run_id):
        QueueWorker.calls.append('cancel')
        if isinstance(QueueWorker.cancel_answer, Exception):
            raise QueueWorker.cancel_answer
        return QueueWorker.cancel_answer

    def withdraw(self, run_id):
        QueueWorker.calls.append('withdraw')
        if isinstance(QueueWorker.withdraw_answer, Exception):
            raise QueueWorker.withdraw_answer
        return QueueWorker.withdraw_answer


def queued(position=2, running=3):
    return {'state': 'queued', 'queue': dict(PLACE, position=position, running=running,
                                             ahead=position - 1)}


ADMITTED = {'state': 'running', 'reservation_mib': 4096, 'cpus_hint': 2, 'size_class': 'large'}


class QueueCase(DaemonCase):
    def setUp(self):
        super().setUp()
        QueueWorker.rows, QueueWorker.final, QueueWorker.calls = [queued()], None, []
        QueueWorker.cancel_answer = {'ok': True, 'withdrawn': True}
        QueueWorker.withdraw_answer = {'ok': True, 'withdrawn': True}
        # Pinned rather than swapped through `worker_factory`: the health poll
        # may already have built and cached a plain FakeWorker by now.
        worker = QueueWorker('fake@nowhere')
        self.daemon.worker_for = lambda repo: worker
        self.daemon.QUEUE_POLL = 0.02

    def frames(self, argv, *, until=('exit', 'error', 'draining'), during=None, timeout=30):
        """Every frame one client saw, in order. `during(frames)` runs once per frame."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(self.daemon.socket_path))
        sock.sendall(dump({'v': VERSION, 'op': 'run', 'cwd': str(self.repo), 'argv': argv,
                           'env': {}, 'tty': False}))
        reader, seen = Reader(sock), []
        try:
            while True:
                frame = reader.line()
                if frame is None:
                    return seen
                seen.append(frame)
                if during is not None:
                    during(seen)
                if frame.get('t') in until:
                    return seen
        finally:
            sock.close()

    def meta(self):
        return [json.loads(path.read_text())
                for path in (self.state / 'runs').glob('*/meta.json')]


class AQueuedRunWaitsBeforeAccepted(QueueCase):
    def test_it_is_told_where_it_stands_then_accepted_after_admission(self):
        QueueWorker.rows = [queued(2), queued(1, running=3), dict(ADMITTED)]
        seen = self.frames(['pnpm', 'surface'])
        kinds = [frame['t'] for frame in seen]
        first = next(frame for frame in seen if frame['t'] == 'notice'
                     and 'queued' in frame.get('msg', ''))
        self.assertEqual(first['msg'], 'queued on the worker behind 4 runs (position 2), '
                                       '~4m10s; gives up after 10m00s')
        self.assertEqual(first['queued']['position'], 2)
        self.assertEqual(first['queued']['eta_seconds'], 250)
        self.assertLess(kinds.index('notice'), kinds.index('accepted'))
        accepted = next(frame for frame in seen if frame['t'] == 'accepted')
        self.assertEqual(accepted['remote'], 'rq1')
        self.assertEqual(accepted['reservation_mib'], 4096)
        self.assertEqual(seen[-1], {'t': 'exit', 'code': 0, 'run': accepted['run']})
        self.assertNotIn('cancel', QueueWorker.calls)

    def test_the_line_repeats_at_most_once_a_minute_as_still_queued(self):
        self.daemon.QUEUE_SAY_EVERY = 0.05
        QueueWorker.rows = [queued(2)] * 12 + [dict(ADMITTED)]
        seen = self.frames(['pnpm', 'surface'])
        lines = [frame['msg'] for frame in seen if frame['t'] == 'notice']
        self.assertTrue(lines[0].startswith('queued on the worker behind'))
        self.assertTrue(any(line.startswith('still queued behind') for line in lines), lines)

    def test_the_heartbeat_keeps_beating_under_the_wait(self):
        from pandora.client import daemon as daemon_module
        original = daemon_module.Heartbeat.EVERY
        daemon_module.Heartbeat.EVERY = 0.02
        self.addCleanup(setattr, daemon_module.Heartbeat, 'EVERY', original)
        QueueWorker.rows = [queued(2)] * 40 + [dict(ADMITTED)]
        seen = self.frames(['pnpm', 'surface'])
        before = [frame['t'] for frame in seen[:[f['t'] for f in seen].index('accepted')]]
        self.assertIn('working', before)

    def test_ps_shows_the_row_queued_with_its_place(self):
        QueueWorker.rows = [queued(2)]
        rows = []

        def look(seen):
            if not rows and any(frame['t'] == 'notice' for frame in seen):
                rows.extend(self.daemon.ps())
                self.daemon.live(rows[0]['id']).canceled.set()
        self.frames(['pnpm', 'surface'], during=look)
        self.assertEqual(rows[0]['state'], 'queued')
        self.assertEqual(rows[0]['phase'], 'queued')
        self.assertEqual(rows[0]['queue']['position'], 2)
        from pandora import cli
        self.assertEqual(cli.state_word(rows[0]), 'queued #2')

    def test_a_queued_run_admitted_and_finished_between_polls_still_ran(self):
        QueueWorker.rows = [{'state': 'finished'}]
        QueueWorker.final = {'outcome': 'passed', 'evidence': {}}
        seen = self.frames(['pnpm', 'surface'])
        self.assertIn('accepted', [frame['t'] for frame in seen])


class TheWaitEndsWithoutRunning(QueueCase):
    def test_a_queue_timeout_is_exit_70_never_a_fallback_and_is_recorded(self):
        QueueWorker.rows = [queued(2), {'state': 'finished'}]
        QueueWorker.final = {'outcome': 'infra_failed', 'cli_exit': 70,
                             'durations': {'queue': 600.0},
                             'evidence': {'cause': 'queue-timeout',
                                          'queue': dict(PLACE, waited_seconds=600)}}
        seen = self.frames(['pnpm', 'unit'])          # small: would once have gone local
        error = seen[-1]
        self.assertEqual((error['t'], error['code'], error['exit']),
                         ('error', 'queue-timeout', 70))
        self.assertIn('waited 10m00s in the worker queue', error['msg'])
        self.assertNotIn('accepted', [frame['t'] for frame in seen])
        self.assertFalse(self.marker.exists())
        meta = self.meta()
        self.assertEqual(len(meta), 1)
        self.assertEqual((meta[0]['state'], meta[0]['exit_code']), ('infra_failed', 70))
        self.assertEqual(meta[0]['refusal']['cause'], 'queue-timeout')
        report = stats.build(self.state)
        self.assertEqual(report['worker_queue']['timeouts'], 1)
        self.assertEqual(report['worker_queue']['wait_seconds']['n'], 1)
        self.assertEqual(report['fallbacks'], [])

    def test_a_cancel_while_queued_withdraws_it_on_the_worker(self):
        QueueWorker.rows = [queued(2)]

        def cancel(seen):
            if seen[-1]['t'] == 'notice' and 'queued' in seen[-1]['msg']:
                self.daemon.live(self.meta()[0]['id']).canceled.set()
        seen = self.frames(['pnpm', 'surface'], during=cancel)
        self.assertEqual((seen[-1]['code'], seen[-1]['exit']), ('canceled', 130))
        self.assertIn('cancel', QueueWorker.calls)
        self.assertEqual(self.meta()[0]['state'], 'cancelled')

    def test_a_caller_that_leaves_while_queued_is_withdrawn(self):
        QueueWorker.rows = [queued(2)]
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(str(self.daemon.socket_path))
        sock.sendall(dump({'v': VERSION, 'op': 'run', 'cwd': str(self.repo),
                           'argv': ['pnpm', 'surface'], 'env': {}, 'tty': False}))
        reader = Reader(sock)
        while 'queued' not in (reader.line() or {}).get('msg', 'queued'):
            pass
        sock.close()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (
                self.meta() and self.meta()[0]['state'] == 'withdrawn'):
            time.sleep(0.02)
        self.assertEqual(self.meta()[0]['state'], 'withdrawn')
        self.assertIn('cancel', QueueWorker.calls)

    def test_a_restart_withdraws_a_queued_run_and_the_caller_asks_again(self):
        QueueWorker.rows = [queued(2)]
        drained = {}

        def drain(seen):
            if seen[-1]['t'] == 'notice' and not drained:
                drained.update(self.daemon.drain(pid=1))
        seen = self.frames(['pnpm', 'surface'], during=drain)
        self.assertEqual(seen[-1]['t'], 'draining')
        # Not the blocker count: the submit thread may withdraw the row before
        # `drain` counts, which is the point of withdrawing it.
        self.assertIn('withdraw', QueueWorker.calls)
        self.assertEqual(self.meta()[0]['state'], 'withdrawn')
        self.assertEqual(self.daemon.drain(pid=1)['blockers'], [])
        self.daemon.undrain()

    def test_a_status_outage_withdraws_the_row_rather_than_orphan_it(self):
        QueueWorker.rows = [queued(2)] + [WorkerUnreachable('ssh: connect timed out')] * 7
        seen = self.frames(['pnpm', 'unit'])
        self.assertEqual((seen[-1]['code'], seen[-1]['exit']), ('worker-unreachable', 70))
        self.assertIn('withdrawn and nothing ran', seen[-1]['msg'])
        self.assertIn('withdraw', QueueWorker.calls)
        self.assertFalse(self.marker.exists())

    def test_a_withdrawal_nobody_can_confirm_is_uncertain_not_a_fallback(self):
        QueueWorker.rows = [WorkerUnreachable('gone')]
        QueueWorker.withdraw_answer = WorkerUnreachable('gone')
        seen = self.frames(['pnpm', 'unit'])
        self.assertEqual((seen[-1]['code'], seen[-1]['exit']), ('execution-uncertain', 70))
        self.assertFalse(self.marker.exists())

    def test_admission_that_wins_the_withdrawal_race_is_followed(self):
        QueueWorker.rows = [WorkerUnreachable('gone')] * 6 + [dict(ADMITTED)]
        QueueWorker.withdraw_answer = {'ok': True, 'withdrawn': False, 'state': 'running'}
        seen = self.frames(['pnpm', 'surface'])
        self.assertIn('accepted', [frame['t'] for frame in seen])

    def test_a_cancel_the_worker_does_not_confirm_is_uncertain(self):
        QueueWorker.rows = [queued(2)]
        QueueWorker.cancel_answer = WorkerUnreachable('gone')

        def cancel(seen):
            if seen[-1]['t'] == 'notice' and 'queued' in seen[-1]['msg']:
                self.daemon.live(self.meta()[0]['id']).canceled.set()
        seen = self.frames(['pnpm', 'surface'], during=cancel)
        self.assertEqual((seen[-1]['code'], seen[-1]['exit']), ('execution-uncertain', 70))
        self.assertEqual(self.meta()[0]['state'], 'infra_failed')


class Detached(QueueCase):
    """`pandora run --detach` takes the id at `queued` and leaves (owner's call)."""

    def submit_detached(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(str(self.daemon.socket_path))
        sock.sendall(dump({'v': VERSION, 'op': 'run', 'cwd': str(self.repo),
                           'argv': ['pnpm', 'surface'], 'env': {}, 'tty': False,
                           'detach': True}))
        reader = Reader(sock)
        while True:
            frame = reader.line()
            if frame.get('t') == 'notice' and frame.get('queued'):
                sock.close()
                return frame

    def settled(self, state):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            rows = [row for row in self.meta() if row['state'] == state]
            if rows:
                return rows[0]
            time.sleep(0.02)
        self.fail('no row reached %s: %s' % (state, self.meta()))

    def test_the_queued_notice_carries_the_run_id_and_leaving_does_not_withdraw(self):
        QueueWorker.rows = [queued(2)] * 25 + [dict(ADMITTED)]
        frame = self.submit_detached()
        self.assertTrue(frame['run'])
        row = self.settled('passed')
        self.assertEqual(row['id'], frame['run'])
        self.assertNotIn('cancel', QueueWorker.calls)
        self.assertNotIn('withdraw', QueueWorker.calls)

    def test_a_restart_hands_a_detached_queued_run_over_instead_of_withdrawing(self):
        QueueWorker.rows = [queued(2)]
        frame = self.submit_detached()
        self.daemon.drain(pid=1)
        # Followed from here (the fake worker's `follow` passes at once).
        row = self.settled('passed')
        self.assertEqual((row['id'], row['remote']), (frame['run'], 'rq1'))
        self.assertNotIn('withdraw', QueueWorker.calls)
        self.daemon.undrain()

    def test_the_shim_returns_at_the_queued_notice_only_when_detached(self):
        from pandora.client import shim

        class Sock:
            def __init__(self, frames):
                self.data = b''.join(dump(frame) for frame in frames)

            def sendall(self, data):
                pass

            def recv(self, size):
                chunk, self.data = self.data[:size], self.data[size:]
                return chunk

            def settimeout(self, value):
                pass
        frames = [{'t': 'notice', 'msg': 'queued on the worker behind 1 run', 'run': 'abc',
                   'queued': {'position': 1}},
                  {'t': 'accepted', 'run': 'abc', 'remote': 'rq1'}]
        _, frame = shim.handshake(Sock(frames), {'detach': True})
        self.assertEqual((frame['t'], frame['run']), ('notice', 'abc'))
        _, frame = shim.handshake(Sock(frames), {})
        self.assertEqual(frame['t'], 'accepted')


class Recovery(unittest.TestCase):
    def test_a_lost_submit_reply_for_a_queued_row_attaches_as_queued(self):
        from pandora.client.worker import Worker
        worker = Worker.__new__(Worker)
        worker.lookup = lambda request_id, plan=None: {
            'ok': True, 'found': True, 'spawned': True, 'queued': True, 'run_id': 'rq',
            'state': 'queued'}
        answer = worker.recover('x:suite', {}, WorkerUnreachable('lost'))
        self.assertTrue(answer['duplicate'])
        self.assertEqual(answer['state'], 'queued')
        self.assertEqual(answer['queued'], {})


if __name__ == '__main__':
    unittest.main()
