"""A run's Perfetto trace: phases in order, lengths honest, file beside result."""
import json
import tempfile
import unittest
from pathlib import Path

from pandora.client import trace


class Events(unittest.TestCase):
    def test_phases_in_run_order_with_real_durations(self):
        meta = {'started': 1000.0, 'accepted': 1000.4,
                'pre_accept': {'freeze': 0.1, 'ship': 0.2, 'submit': 0.1}}
        result = {'durations': {'queue': 2.0, 'clone': 0.1, 'execute': 9.5,
                                'destroy': 1.0, 'total': 13.0}}
        events = trace.events(meta, result)
        names = [event['name'] for event in events]
        self.assertEqual(names, ['freeze', 'ship', 'submit', 'request to accepted',
                                 'queue', 'clone', 'execute', 'destroy'])
        by_name = {event['name']: event for event in events}
        self.assertEqual(by_name['execute']['dur'], 9_500_000)
        self.assertGreaterEqual(by_name['queue']['ts'], 1000.4 * 1e6)
        self.assertTrue(all(event['ph'] == 'X' for event in events))

    def test_each_attempt_is_a_row_and_the_last_carries_phases(self):
        result = {'durations': {'execute': 10.0},
                  'attempts': [{'outcome': 'infra_failed', 'wall_seconds': 4.0}]}
        events = trace.events({'started': 5.0}, result)
        self.assertEqual([event['tid'] for event in events], [2, 3])
        self.assertEqual(events[0]['dur'], 4_000_000)
        self.assertIn('infra_failed', events[0]['name'])

    def test_no_clock_no_trace(self):
        self.assertEqual(trace.events({}, {'durations': {'execute': 1}}), [])

    def test_write_lands_beside_the_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = trace.write(tmp, {'started': 1.0},
                               {'durations': {'execute': 1.0}})
            self.assertTrue(Path(path).exists())
            on_disk = json.loads(Path(path).read_text())
            self.assertEqual(on_disk['traceEvents'][0]['ph'], 'X')

    def test_write_without_a_clock_is_nothing_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(trace.write(tmp, {}, {'durations': {'execute': 1}}))


if __name__ == '__main__':
    unittest.main()
