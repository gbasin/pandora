"""Deterministic publication semantics tests on a temporary local filesystem."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from publish import publish


class Publication(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifacts = self.root / 'artifacts'
        (self.artifacts / 'dist').mkdir(parents=True)
        (self.artifacts / 'dist/report.json').write_text('new')
        self.manifest = {'report.json': hashlib.sha256(b'new').hexdigest()}

    def old(self):
        (self.root / 'dist').mkdir()
        (self.root / 'dist/report.json').write_text('old')
        (self.root / 'dist/obsolete').write_text('retain only in backup')

    def test_first_publication(self):
        receipt = publish(self.root, self.artifacts, self.manifest)
        self.assertEqual((self.root / 'dist/report.json').read_text(), 'new')
        self.assertIsNone(receipt['retained_previous'])

    def test_existing_directory_replaced_and_retained(self):
        self.old()
        receipt = publish(self.root, self.artifacts, self.manifest)
        self.assertEqual((self.root / 'dist/report.json').read_text(), 'new')
        self.assertFalse((self.root / 'dist/obsolete').exists())
        self.assertEqual((Path(receipt['retained_previous']) / 'report.json').read_text(), 'old')
        self.assertTrue((Path(receipt['retained_previous']) / 'obsolete').exists())

    def test_process_exit_before_and_after_exchange(self):
        for point in ['after_prepare', 'after_exchange']:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); artifacts = root/'artifacts'
                (artifacts/'dist').mkdir(parents=True)
                (artifacts/'dist/report.json').write_text('new')
                (root/'dist').mkdir(); (root/'dist/report.json').write_text('old')
                pid = os.fork()
                if pid == 0:
                    publish(root, artifacts, self.manifest,
                            lambda step: os._exit(86) if step == point else None)
                    os._exit(1)
                self.assertEqual(os.waitpid(pid, 0)[1], 86 << 8)
                publish(root, artifacts, self.manifest)
                publish(root, artifacts, self.manifest)
                self.assertEqual((root/'dist/report.json').read_text(), 'new')
                self.assertEqual((artifacts/'generation/report.json').read_text(), 'old')

    def test_open_writer_survives_in_retained_generation(self):
        self.old()
        with (self.root/'dist/report.json').open('w') as writer:
            def fault(point):
                if point == 'after_prepare':
                    writer.write('before-'); writer.flush()
                if point == 'after_exchange':
                    writer.write('after'); writer.flush()
            receipt = publish(self.root, self.artifacts, self.manifest, fault)
        self.assertEqual((Path(receipt['retained_previous'])/'report.json').read_text(), 'before-after')
        self.assertEqual((self.root/'dist/report.json').read_text(), 'new')

    def test_corrupt_download_leaves_previous_output(self):
        self.old(); (self.artifacts/'dist/report.json').write_text('corrupt')
        with self.assertRaises(ValueError):publish(self.root,self.artifacts,self.manifest)
        self.assertEqual((self.root/'dist/report.json').read_text(),'old')

    def test_destination_replaced_before_exchange_stops(self):
        self.old()
        def fault(point):
            if point == 'after_prepare':
                (self.root/'dist').rename(self.root/'externally-moved')
                (self.root/'dist').mkdir(); (self.root/'dist/report.json').write_text('external')
        with self.assertRaises(ValueError):publish(self.root,self.artifacts,self.manifest,fault)
        self.assertEqual((self.root/'dist/report.json').read_text(),'external')

    def test_symlink_destination_is_rejected(self):
        other=self.root/'other';other.mkdir();(other/'report.json').write_text('untouched')
        (self.root/'dist').symlink_to(other,target_is_directory=True)
        with self.assertRaises(ValueError):publish(self.root,self.artifacts,self.manifest)
        self.assertEqual((other/'report.json').read_text(),'untouched')


if __name__ == '__main__':
    unittest.main(verbosity=2)
