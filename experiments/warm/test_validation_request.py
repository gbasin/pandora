import unittest

from validation_request import validate_request


class ValidationRequestTests(unittest.TestCase):
    def test_accepts_only_the_versioned_bounded_shape(self):
        value = {'version': 1, 'suite': 'postgres', 'args': ['api', '--foundation-only']}
        self.assertEqual(validate_request(value), value)
        self.assertIsNot(validate_request(value)['args'], value['args'])

    def test_rejects_unknown_or_unstructured_requests(self):
        for value in (
            {}, {'version': 2, 'suite': 'unit', 'args': []},
            {'version': True, 'suite': 'unit', 'args': []},
            {'version': 1, 'suite': 'native', 'args': []},
            {'version': 1, 'suite': ['unit'], 'args': []},
            {'version': 1, 'suite': 'unit', 'args': '--grep x'},
            {'version': 1, 'suite': 'unit', 'args': [1]},
            {'version': 1, 'suite': 'unit', 'args': ['apps/api/x.test.ts']},
            {'version': 1, 'suite': 'browser-integration', 'args': ['flow.spec.ts']},
            {'version': 1, 'suite': 'postgres', 'args': []},
            {'version': 1, 'suite': 'postgres', 'args': ['scenarios', '--foundation-only']},
            {'version': 1, 'suite': 'postgres', 'args': ['api', '--foundation-only', 'extra']},
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_request(value)


if __name__ == '__main__':
    unittest.main()
