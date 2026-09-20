from pathlib import Path
import tempfile
import unittest
from docker_images import publish, resolve, path


class ImageTests(unittest.TestCase):
    def test_worktree_scoping_and_removal_keep_pinned_value(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            publish(root, 'a' * 64, 'app:test', {'image_id': 'old'})
            publish(root, 'b' * 64, 'app:test', {'image_id': 'other'})
            pinned = resolve(root, 'a' * 64, 'app:test')
            publish(root, 'a' * 64, 'app:test', {'image_id': 'new'})
            self.assertEqual(pinned['image_id'], 'old')
            self.assertEqual(resolve(root, 'b' * 64, 'app:test')['image_id'], 'other')
            path(root, 'a' * 64, 'app:test').unlink()
            with self.assertRaisesRegex(ValueError, 'docker build'):
                resolve(root, 'a' * 64, 'app:test')
            self.assertEqual(pinned['image_id'], 'old')
