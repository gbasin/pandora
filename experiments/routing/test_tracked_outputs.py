import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tracked_outputs
from tracked_outputs import PublicationConflict, publish, read_intent, resolve_with_local_contents, source_matches_intent


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
        self.assertEqual(read_intent(self.output).phase, 'conflicted')
        self.assertTrue(raised.exception.paths[0].exists())

    def test_explicit_resolution_preserves_all_current_values_without_writing(self):
        (self.repo / 'a').write_bytes(b'outside')
        (self.repo / 'b').write_bytes(b'old')
        declarations = {'a': {'base': b'old', 'target': b'new'},
                        'b': {'base': b'old', 'target': b'new'}}
        self.prepare(declarations)
        with self.assertRaises(PublicationConflict):
            publish(self.repo, self.output, declarations)
        # The user can merge one conflict and keep a different, unconflicted
        # declared file. Resolution must accept both current values as-is.
        (self.repo / 'a').write_bytes(b'manual merge')
        self.assertEqual(resolve_with_local_contents(self.repo, self.output, declarations), ('a', 'b'))
        self.assertEqual((self.repo / 'a').read_bytes(), b'manual merge')
        self.assertEqual((self.repo / 'b').read_bytes(), b'old')
        self.assertEqual(read_intent(self.output).phase, 'resolved')
        receipt = json.loads((self.output / 'publication/tracked-resolution.json').read_text())
        self.assertEqual(receipt['kind'], 'keep-local-unvalidated')
        with self.assertRaisesRegex(ValueError, 'resolved'):
            publish(self.repo, self.output, declarations)
        (self.output / 'publication/tracked-resolution.json').unlink()
        with self.assertRaisesRegex(ValueError, 'missing its immutable'):
            resolve_with_local_contents(self.repo, self.output, declarations)

    def test_resolution_resumes_after_receipt_or_intent_crash_without_rereading_source(self):
        (self.repo / 'a').write_bytes(b'outside')
        declarations = {'a': {'base': b'old', 'target': b'new'}}
        self.prepare(declarations)
        with self.assertRaises(PublicationConflict):
            publish(self.repo, self.output, declarations)
        (self.repo / 'a').write_bytes(b'accepted')
        with self.assertRaisesRegex(OSError, 'receipt crash'):
            resolve_with_local_contents(self.repo, self.output, declarations,
                                        lambda point: (_ for _ in ()).throw(OSError('receipt crash'))
                                        if point == 'after_receipt' else None)
        # A later edit must not replace receipt evidence during recovery.
        (self.repo / 'a').write_bytes(b'later local edit')
        self.assertEqual(resolve_with_local_contents(self.repo, self.output, declarations), ('a',))
        self.assertEqual(read_intent(self.output).phase, 'resolved')
        receipt = json.loads((self.output / 'publication/tracked-resolution.json').read_text())
        self.assertEqual(receipt['paths']['a']['accepted_sha256'], hashlib.sha256(b'accepted').hexdigest())

        # A second attempt models the crash after the resolved intent write.
        (self.repo / 'b').write_bytes(b'outside')
        output = self.root / 'attempt-after-intent'
        declarations = {'b': {'base': b'old', 'target': b'new'}}
        self.output = output
        self.prepare(declarations)
        with self.assertRaises(PublicationConflict):
            publish(self.repo, output, declarations)
        with self.assertRaisesRegex(OSError, 'intent crash'):
            resolve_with_local_contents(self.repo, output, declarations,
                                        lambda point: (_ for _ in ()).throw(OSError('intent crash'))
                                        if point == 'after_intent' else None)
        self.assertEqual(read_intent(output).phase, 'resolved')
        self.assertEqual(resolve_with_local_contents(self.repo, output, declarations), ('b',))

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

    def test_v1_intent_remains_readable_and_recovery_upgrades_it(self):
        declarations = {'x': {'base': None, 'target': b'X'}}
        self.prepare(declarations)
        publication = self.output / 'publication'
        publication.mkdir()
        (publication / 'tracked-intent.json').write_text(json.dumps({
            'version': 1,
            'phase': 'applying',
            'declarations': tracked_outputs._encode(declarations),
            'committed': [],
        }))
        self.assertEqual(read_intent(self.output).declarations, declarations)
        self.assertEqual(publish(self.repo, self.output, declarations).committed, ('x',))
        intent = json.loads((publication / 'tracked-intent.json').read_text())
        self.assertEqual(intent['version'], 2)
        self.assertTrue((publication / 'tracked-declarations.json').is_file())

    def test_v2_declaration_digest_detects_tampering(self):
        declarations = {'x': {'base': None, 'target': b'X'}}
        self.prepare(declarations)
        publish(self.repo, self.output, declarations)
        declarations_path = self.output / 'publication' / 'tracked-declarations.json'
        declarations_path.write_bytes(declarations_path.read_bytes() + b' ')
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            read_intent(self.output)

    def test_progress_receipts_do_not_rewrite_declarations(self):
        declarations = {
            f'outputs/{index:03}.txt': {'base': None, 'target': bytes([index]) * 256}
            for index in range(200)
        }
        self.prepare(declarations)
        writes = []
        original = tracked_outputs._atomic_json

        def record(path, value):
            writes.append((Path(path).name, len(tracked_outputs._json_bytes(value))))
            return original(path, value)

        with patch.object(tracked_outputs, '_atomic_json', side_effect=record):
            publish(self.repo, self.output, declarations)

        declaration_writes = [size for name, size in writes if name == 'tracked-declarations.json']
        intent_writes = [size for name, size in writes if name == 'tracked-intent.json']
        self.assertEqual(len(declaration_writes), 1)
        self.assertEqual(len(intent_writes), len(declarations) + 2)
        # The immutable record is written once. Each progress receipt contains
        # only phase, digest, and a compact committed-path bitset, never target bytes.
        self.assertLess(sum(intent_writes), len(declarations) * 2048)
        self.assertLess(sum(size for _, size in writes), declaration_writes[0] + len(declarations) * 2048)

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
