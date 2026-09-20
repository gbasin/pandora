import fcntl
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dependency_images import pin, release, remember


def image(n):
    return 'pandora-deps:' + f'{n:064x}'


class DependencyImages(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.attempt = self.root / 'runs' / ('a' * 32)
        self.attempt.mkdir(parents=True)
        self.handle = (self.attempt / 'attempt.lock').open('a')
        fcntl.flock(self.handle, fcntl.LOCK_EX)
        self.addCleanup(self.handle.close)
        self.run = patch('dependency_images.subprocess.run', return_value=subprocess.CompletedProcess([], 0))
        self.docker = self.run.start()
        self.addCleanup(self.run.stop)

    def fill(self):
        for n in range(5):
            remember(self.root, image(n))

    def receipt(self, *, identity=None, verified=True, name='terminal.json'):
        (self.attempt / name).write_text(json.dumps({
            'attempt': identity or self.attempt.name, 'cleanup_verified': verified}))

    def removed(self):
        return [call.args[0][-1] for call in self.docker.call_args_list]

    def test_pin_before_container_creation_protects_old_image_from_retention(self):
        pin(self.attempt, image(0))
        self.fill()
        self.assertEqual(self.removed(), [image(1)])
        self.assertEqual(json.loads((self.root / 'integrated-images.json').read_text()),
                         [image(0), image(2), image(3), image(4)])
        self.receipt()
        self.assertTrue(release(self.attempt))
        remember(self.root, image(4))
        self.assertEqual(self.removed(), [image(1), image(0)])

    def test_dead_owner_is_not_cleanup_and_retry_cannot_change_pin(self):
        pin(self.attempt, image(0))
        pin(self.attempt, image(0))
        with self.assertRaisesRegex(ValueError, 'different'):
            pin(self.attempt, image(1))
        self.handle.close()
        self.assertFalse(release(self.attempt))
        self.fill()
        self.assertNotIn(image(0), self.removed())
        self.receipt(name='admission-cleanup.json')
        remember(self.root, image(4))
        self.assertIn(image(0), self.removed())
        self.assertFalse((self.attempt / 'terminal.json').exists())

    def test_live_owner_cannot_be_released_by_external_cleanup_receipt(self):
        pin(self.attempt, image(0))
        self.receipt(name='admission-cleanup.json')
        self.assertFalse(release(self.attempt))
        self.fill()
        self.assertNotIn(image(0), self.removed())

    def test_mismatched_or_unverified_receipt_does_not_release(self):
        pin(self.attempt, image(0))
        self.receipt(identity='b' * 32)
        self.assertFalse(release(self.attempt))
        self.receipt(verified=False)
        self.assertFalse(release(self.attempt))
        self.fill()
        self.assertNotIn(image(0), self.removed())

    def test_missing_attempt_does_not_release_reservation(self):
        pin(self.attempt, image(0))
        self.handle.close()
        (self.attempt / 'attempt.lock').unlink()
        self.attempt.rmdir()
        self.fill()
        self.assertNotIn(image(0), self.removed())

    def test_malformed_late_ledger_entry_prevents_any_deletion(self):
        (self.root / 'integrated-images.json').write_text(json.dumps([image(0), image(1), 'other:tag', image(2)]))
        with self.assertRaises(ValueError):
            remember(self.root, image(4))
        self.docker.assert_not_called()

    def test_malformed_pin_prevents_any_deletion(self):
        pin(self.attempt, image(0))
        (self.root / 'dependency-pins' / ('b' * 32 + '.json')).write_text('{}')
        with self.assertRaises(ValueError):
            remember(self.root, image(4))
        self.docker.assert_not_called()

    def test_unlocked_attempt_cannot_reserve(self):
        self.handle.close()
        with self.assertRaises(ValueError):
            pin(self.attempt, image(0))

    def test_failed_docker_removal_stays_in_ledger(self):
        self.docker.return_value = subprocess.CompletedProcess([], 1)
        self.fill()
        self.assertEqual(json.loads((self.root / 'integrated-images.json').read_text()),
                         [image(n) for n in range(5)])


if __name__ == '__main__':
    unittest.main()
