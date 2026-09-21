import threading
import unittest

from configured_dispatch import dispatch


class DispatchTests(unittest.TestCase):
    def test_failure_stops_new_dispatch_and_collects_already_started_work(self):
        staged, completed, stops = [], [], []
        def stage(index): staged.append(index); return str(index), index
        def run(identity, child, cancelled): return None
        def finish(identity, child, stopped): completed.append(child); return {'exit_code': 1 if child == 1 else 0}, stopped
        def classify(index, child, terminal, stopped): return 'test-failure' if child == 1 else None
        def stop(current, observed): stops.append(observed); return observed
        def persist(kind, value): pass
        reason = dispatch(count=3, parallel=2, stage=stage, run=run, finish=finish,
                          classify=classify, stop=stop, persist=persist, cancelled=threading.Event())
        self.assertEqual((reason, staged, sorted(completed)), ('test-failure', [1, 2], [1, 2]))

    def test_cleanup_failures_do_not_mask_worker_failure_or_skip_reap(self):
        reaped = []
        def stage(index): return '1', 1
        def run(identity, child, cancelled): raise ValueError('worker failed')
        def finish(identity, child, stopped): raise AssertionError('unreachable')
        def stop(current, observed): raise RuntimeError('scheduler unavailable')
        def persist(kind, value):
            if kind == 'stop_reason': raise RuntimeError('disk unavailable')
        with self.assertRaisesRegex(ValueError, 'worker failed'):
            dispatch(count=1, parallel=1, stage=stage, run=run, finish=finish,
                     classify=lambda *args: None, stop=stop, persist=persist,
                     cancelled=threading.Event())


if __name__ == '__main__':
    unittest.main()
