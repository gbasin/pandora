import unittest

from artifact_limits import (ArtifactDeliveryLimitExceeded,
                             DEFAULT_ARTIFACT_DELIVERY_LIMIT_BYTES,
                             artifact_delivery_limit, artifact_paths,
                             enforce_declared_artifact_limit)


class ArtifactLimitTests(unittest.TestCase):
    def state(self, sizes):
        return {'artifact_sizes': sizes, 'artifact_total_bytes': sum(sizes.values())}

    def test_default_is_two_gib(self):
        self.assertEqual(artifact_delivery_limit(), 2 * 1024 ** 3)
        self.assertEqual(DEFAULT_ARTIFACT_DELIVERY_LIMIT_BYTES, 2 * 1024 ** 3)

    def test_limit_requires_positive_integer_bytes(self):
        for value in (0, -1, True, 1.5, '2'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'positive integer'):
                artifact_delivery_limit(value)

    def test_exactly_at_limit_is_allowed(self):
        manifest = {'results/junit.xml': 'a', 'stdout.log': 'b'}
        self.assertEqual(enforce_declared_artifact_limit(
            manifest, self.state({'results/junit.xml': 7, 'stdout.log': 3}), 10), 10)

    def test_excess_blocks_delivery_and_explains_recovery(self):
        manifest = {'results/video.webm': 'a'}
        with self.assertRaisesRegex(ArtifactDeliveryLimitExceeded, 'remote test result remains retained') as raised:
            enforce_declared_artifact_limit(manifest, self.state({'results/video.webm': 11}), 10)
        self.assertEqual(raised.exception.total_bytes, 11)
        self.assertEqual(raised.exception.limit_bytes, 10)
        self.assertIn('retry the same invocation', str(raised.exception))

    def test_rejects_unsafe_manifest_paths_before_using_remote_stats(self):
        for path in ('../outside', '/absolute', '', 'results/../escape', 'results\nlog', './results/log', 'results//log'):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'Unsafe'):
                artifact_paths({path: 'a'})

    def test_remote_stats_must_cover_exactly_the_manifest(self):
        manifest = {'results/junit.xml': 'a'}
        with self.assertRaisesRegex(ValueError, 'do not match'):
            enforce_declared_artifact_limit(manifest, self.state({'other': 1}), 1)

    def test_remote_total_and_sizes_must_agree(self):
        manifest = {'results/junit.xml': 'a'}
        with self.assertRaisesRegex(ValueError, 'total does not match'):
            enforce_declared_artifact_limit(
                manifest, {'artifact_sizes': {'results/junit.xml': 1}, 'artifact_total_bytes': 2}, 2)

    def test_remote_total_must_be_an_integer_not_an_equivalent_boolean(self):
        manifest = {'results/junit.xml': 'a'}
        with self.assertRaisesRegex(ValueError, 'total does not match'):
            enforce_declared_artifact_limit(
                manifest, {'artifact_sizes': {'results/junit.xml': 1}, 'artifact_total_bytes': True}, 2)

    def test_rejects_non_integer_or_negative_remote_sizes(self):
        manifest = {'results/junit.xml': 'a'}
        for size in (-1, True, 1.5):
            with self.subTest(size=size), self.assertRaisesRegex(ValueError, 'invalid size'):
                enforce_declared_artifact_limit(manifest, self.state({'results/junit.xml': size}), 10)


if __name__ == '__main__':
    unittest.main()
