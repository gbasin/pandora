import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pandora.client.execution_reuse import input_inventory
from pandora.engine.source_integrity import SCRIPT, CLEAR_SCRIPT


class SourceIntegrityTests(unittest.TestCase):
    def test_mutation_mode_new_source_and_symlink_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.check_call(['git', 'init', '-q', folder])
            file = root / 'adapter.py'
            file.write_text('original')
            subprocess.check_call(['git', '-C', folder, 'add', '-A'])
            tree = subprocess.check_output(['git', '-C', folder, 'write-tree']).decode().strip()
            inventory = input_inventory(root, tree)

            def verify():
                return subprocess.check_output([sys.executable, '-c', SCRIPT, folder],
                                               input=json.dumps(inventory).encode()).strip()
            self.assertEqual(verify(), b'verified')
            file.write_text('mutated')
            self.assertEqual(verify(), b'unverified')
            file.write_text('original')
            file.chmod(0o755)
            self.assertEqual(verify(), b'unverified')
            file.chmod(0o644)
            extra = root / 'extra.py'
            extra.write_text('new')
            self.assertEqual(verify(), b'unverified')
            subprocess.check_call(['git', '-C', folder, 'add', 'extra.py'])
            self.assertEqual(verify(), b'unverified')
            subprocess.check_call(['git', '-C', folder, 'rm', '-q', '--cached', 'extra.py'])
            extra.unlink()
            file.unlink()
            file.symlink_to('/tmp/missing')
            self.assertEqual(verify(), b'unverified')

    def test_lfs_materialization_requires_exact_pointer_hash_and_size(self):
        import hashlib
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.check_call(['git', 'init', '-q', folder])
            data = b'materialized data'
            pointer = ('version https://git-lfs.github.com/spec/v1\noid sha256:%s\nsize %s\n'
                       % (hashlib.sha256(data).hexdigest(), len(data)))
            file = root / 'fixture.bin'
            file.write_text(pointer)
            subprocess.check_call(['git', '-C', folder, 'add', '-A'])
            tree = subprocess.check_output(['git', '-C', folder, 'write-tree']).decode().strip()
            inventory = input_inventory(root, tree)
            file.write_bytes(data)
            argv = [sys.executable, '-c', SCRIPT, folder]
            context = json.dumps(inventory).encode()
            self.assertEqual(subprocess.check_output(argv, input=context).strip(), b'verified')
            file.write_bytes(b'changed data')
            self.assertEqual(subprocess.check_output(argv, input=context).strip(), b'unverified')

    def test_rewritten_pointer_object_does_not_attest_changed_materialization(self):
        import hashlib
        import zlib
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subprocess.check_call(['git', 'init', '-q', folder])
            initial = b'original'
            changed = b'modified'
            def pointer(data):
                return ('version https://git-lfs.github.com/spec/v1\noid sha256:%s\nsize %s\n'
                        % (hashlib.sha256(data).hexdigest(), len(data))).encode()
            file = root / 'fixture.bin'
            file.write_bytes(pointer(initial))
            subprocess.check_call(['git', '-C', folder, 'add', '-A'])
            tree = subprocess.check_output(['git', '-C', folder, 'write-tree']).decode().strip()
            inventory = input_inventory(root, tree)
            oid = inventory['entries']['fixture.bin'][2]
            forged = pointer(changed)
            object_path = root / '.git/objects' / oid[:2] / oid[2:]
            object_path.chmod(0o644)
            object_path.write_bytes(zlib.compress(b'blob ' + str(len(forged)).encode() + b'\0' + forged))
            file.write_bytes(changed)
            result = subprocess.check_output([sys.executable, '-c', SCRIPT, folder],
                                             input=json.dumps(inventory).encode()).strip()
            self.assertEqual(result, b'unverified')

    def test_clearing_rejects_symlink_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'tmp').symlink_to(root)
            proc = subprocess.run([sys.executable, '-c', CLEAR_SCRIPT, folder], capture_output=True)
            self.assertNotEqual(proc.returncode, 0)
