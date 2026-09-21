import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from delivery import deliver, OUTPUTS


class Delivery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (self.repo / '.gitignore').write_text('dist/\n')
        self.output = self.root / 'attempt'
        files = {}
        for name in OUTPUTS:
            relative = 'results/outputs/' + name + '/index.html'
            path = self.output / relative
            path.parent.mkdir(parents=True)
            path.write_text(name)
            files[relative] = hashlib.sha256(name.encode()).hexdigest()
        (self.output / 'artifacts.json').write_text(json.dumps(files))

    def test_partial_multi_root_publication_recovers_without_swap_back(self):
        for name in OUTPUTS:
            path = self.repo / name
            path.mkdir(parents=True)
            (path / 'obsolete').write_text('old')
        def crash(point):
            if point == 'after_exchange':
                raise OSError('client died')
        with self.assertRaises(OSError):
            deliver(self.repo, self.output, crash)
        deliver(self.repo, self.output)
        deliver(self.repo, self.output)
        for index, name in enumerate(OUTPUTS):
            self.assertEqual((self.repo / name / 'index.html').read_text(), name)
            self.assertFalse((self.repo / name / 'obsolete').exists())
            self.assertEqual((self.output / 'publication' / str(index) / 'generation/obsolete').read_text(), 'old')

    def test_symlink_ancestor_and_tracked_output_are_rejected(self):
        (self.repo / 'apps').symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            deliver(self.repo, self.output)
        (self.repo / 'apps').unlink()
        target = self.repo / OUTPUTS[0]
        target.mkdir(parents=True)
        (target / 'source').write_text('keep')
        subprocess.run(['git', '-C', str(self.repo), 'add', '-f', str(target / 'source')], check=True)
        with self.assertRaisesRegex(ValueError, 'tracked'):
            deliver(self.repo, self.output)
        self.assertEqual((target / 'source').read_text(), 'keep')

    def test_parent_journal_publishes_outputs_from_reserved_child(self):
        child_outputs = self.output / 'results/attempts' / ('a' * 32) / 'results/outputs'
        manifest = {}
        for name in OUTPUTS:
            path = child_outputs / name / 'index.html'
            path.parent.mkdir(parents=True)
            path.write_text('child-' + name)
            manifest[str(path.relative_to(self.output))] = hashlib.sha256(path.read_bytes()).hexdigest()
        (self.output / 'artifacts.json').write_text(json.dumps(manifest))
        deliver(self.repo, self.output, source_outputs=child_outputs)
        for index, name in enumerate(OUTPUTS):
            self.assertEqual((self.repo / name / 'index.html').read_text(), 'child-' + name)
            self.assertTrue((self.output / 'publication' / str(index)).is_dir())


if __name__ == '__main__':
    unittest.main()
