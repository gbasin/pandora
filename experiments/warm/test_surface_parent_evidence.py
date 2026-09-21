import unittest

from surface_parent_evidence import summarize, validate_summary
from test_surface_suite import plan, report


class SurfaceParentEvidenceTests(unittest.TestCase):
    def test_out_of_order_reports_are_canonical_and_missing_shards_are_explicit(self):
        frozen = plan()
        value = summarize(frozen, [report(frozen, 2), report(frozen, 1, code=1)], keep_going=False, stop_reason='test-failure')
        self.assertEqual((value['completed_shards'], value['unrun_shards'], value['exit_code']), ([1, 2], [3], 1))
        self.assertEqual(validate_summary(frozen, value), value)


if __name__ == '__main__':
    unittest.main()
