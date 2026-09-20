import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from tracked_outputs import PublicationConflict, publish, read_intent, source_matches_intent


class TrackedOutputs(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'; self.repo.mkdir()
        self.output = self.root / 'attempt'

    def prepare(self, declarations):
        manifest = {}
        for name, item in declarations.items():
            if item['target'] is not None:
                path = self.output / 'results' / 'updates' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(item['target'])
                manifest['results/updates/' + name] = hashlib.sha256(item['target']).hexdigest()
        self.output.mkdir(exist_ok=True)
        (self.output / 'artifacts.json').write_text(json.dumps(manifest))

    def test_add_change_delete_and_preserve_unrelated_file(self):
        (self.repo / 'change').write_bytes(b'old')
        (self.repo / 'delete').write_bytes(b'gone')
        (self.repo / 'unrelated').write_bytes(b'keep')
        declarations = {'add': {'base': None, 'target': b'new'},
                        'change': {'base': b'old', 'target': b'newer'},
                        'delete': {'base': b'gone', 'target': None}}
        self.prepare(declarations)
        receipt = publish(self.repo, self.output, declarations)
        self.assertEqual(receipt.committed, ('add', 'change', 'delete'))
        self.assertEqual((self.repo / 'add').read_bytes(), b'new')
        self.assertEqual((self.repo / 'change').read_bytes(), b'newer')
        self.assertFalse((self.repo / 'delete').exists())
        self.assertEqual((self.repo / 'unrelated').read_bytes(), b'keep')
        self.assertEqual((self.output / 'publication/backups/change').read_bytes(), b'old')
        self.assertEqual(read_intent(self.output).phase, 'published')

    def test_preflight_conflict_writes_no_target_or_intent(self):
        (self.repo / 'a').write_bytes(b'outside')
        declarations = {'a': {'base': b'old', 'target': b'new'}, 'b': {'base': None, 'target': b'new'}}
        self.prepare(declarations)
        with self.assertRaises(PublicationConflict) as raised:
            publish(self.repo, self.output, declarations)
        self.assertEqual((self.repo / 'a').read_bytes(), b'outside')
        self.assertFalse((self.repo / 'b').exists())
        self.assertIsNone(read_intent(self.output))
        self.assertTrue(raised.exception.paths[0].exists())

    def test_retry_after_write_before_receipt_recognizes_target(self):
        declarations = {'x': {'base': None, 'target': b'X'}, 'y': {'base': None, 'target': b'Y'}}
        self.prepare(declarations)
        with self.assertRaisesRegex(OSError, 'interrupted'):
            publish(self.repo, self.output, declarations,
                    lambda point: (_ for _ in ()).throw(OSError('interrupted')) if point == 'after_write' else None)
        self.assertEqual((self.repo / 'x').read_bytes(), b'X')
        self.assertEqual(read_intent(self.output).committed, ())
        self.assertTrue(source_matches_intent(self.repo, self.output))
        self.assertEqual(publish(self.repo, self.output, declarations).committed, ('x', 'y'))

    def test_abrupt_pre_replace_staging_leftover_is_outside_repo(self):
        declarations = {'x': {'base': None, 'target': b'X'}}
        self.prepare(declarations)
        pid = os.fork()
        if pid == 0:
            publish(self.repo, self.output, declarations,
                    lambda point: os._exit(86) if point == 'before_replace' else None)
            os._exit(1)
        self.assertEqual(os.waitpid(pid, 0)[1], 86 << 8)
        self.assertFalse((self.repo / 'x').exists())
        self.assertFalse(list(self.repo.rglob('tracked-output-*')))
        self.assertTrue(list((self.output / 'publication/staging').glob('tracked-output-*')))
        self.assertTrue(source_matches_intent(self.repo, self.output))
        self.assertEqual(publish(self.repo, self.output, declarations).committed, ('x',))

    def test_noop_target_is_allowed_and_receipted(self):
        (self.repo / 'route-manifest').write_bytes(b'unchanged')
        declarations = {'route-manifest': {'base': b'unchanged', 'target': b'unchanged'}}
        self.prepare(declarations)
        self.assertEqual(publish(self.repo, self.output, declarations).committed, ('route-manifest',))
        self.assertEqual((self.repo / 'route-manifest').read_bytes(), b'unchanged')

    def test_conflict_after_partial_preserves_local_target_and_stops(self):
        declarations = {'x': {'base': None, 'target': b'X'}, 'y': {'base': None, 'target': b'Y'}, 'z': {'base': None, 'target': b'Z'}}
        self.prepare(declarations)
        with self.assertRaises(OSError):
            publish(self.repo, self.output, declarations,
                    lambda point: (_ for _ in ()).throw(OSError('stop')) if point == 'after_write' else None)
        (self.repo / 'y').write_bytes(b'local')
        with self.assertRaises(PublicationConflict) as raised:
            publish(self.repo, self.output, declarations)
        self.assertEqual((self.repo / 'x').read_bytes(), b'X')
        self.assertEqual((self.repo / 'y').read_bytes(), b'local')
        self.assertFalse((self.repo / 'z').exists())
        self.assertTrue(raised.exception.paths[0].exists())

    def test_committed_target_restored_to_base_is_a_conflict(self):
        declarations = {'x': {'base': b'old', 'target': b'new'}}
        (self.repo / 'x').write_bytes(b'old')
        self.prepare(declarations)
        publish(self.repo, self.output, declarations)
        (self.repo / 'x').write_bytes(b'old')
        with self.assertRaises(PublicationConflict):
            publish(self.repo, self.output, declarations)

    def test_unsafe_paths_are_rejected_without_touching_target(self):
        outside = self.root / 'outside'; outside.write_bytes(b'keep')
        declarations = {'../outside': {'base': b'keep', 'target': b'bad'}}
        self.prepare(declarations)
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            publish(self.repo, self.output, declarations)
        self.assertEqual(outside.read_bytes(), b'keep')
        (self.repo / 'link').symlink_to(self.root, target_is_directory=True)
        declarations = {'link/file': {'base': None, 'target': b'bad'}}
        self.prepare(declarations)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            publish(self.repo, self.output, declarations)

    def test_noncanonical_and_overlapping_paths_are_rejected(self):
        for name in ('./x', 'a//b', '.'):
            with self.subTest(name=name):
                declarations = {name: {'base': None, 'target': b'bad'}}
                self.output.mkdir(exist_ok=True)
                (self.output / 'artifacts.json').write_text('{}')
                with self.assertRaisesRegex(ValueError, 'Unsafe'):
                    publish(self.repo, self.output, declarations)
        declarations = {'a': {'base': None, 'target': b'one'},
                        'a/b': {'base': None, 'target': b'two'}}
        self.output.mkdir(exist_ok=True)
        (self.output / 'artifacts.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'Overlapping'):
            publish(self.repo, self.output, declarations)


if __name__ == '__main__':
    unittest.main(verbosity=2)
