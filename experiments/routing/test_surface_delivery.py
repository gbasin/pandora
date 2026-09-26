import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from surface_delivery import deliver_surface


class SurfaceDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.repo = self.root / 'repo'; self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (self.repo / '.gitignore').write_text('dist/\n')
        self.output = self.root / 'parent'; self.output.mkdir()
        self.parent = 'a' * 32; self.planner = 'b' * 32; self.shard = 'c' * 32
        (self.output / 'children.json').write_text(json.dumps({'version': 1, 'parent_attempt': self.parent,
                                                                'children': [self.planner, self.shard]}))
        self.submitted = {'attempt': self.parent, 'surface_suite': {'app': 'web'}}

    def add(self, identity, kind, relative, value, files):
        path = self.output / 'results/attempts' / identity / kind / relative
        path.parent.mkdir(parents=True, exist_ok=True); path.write_text(value)
        files[str(path.relative_to(self.output))] = hashlib.sha256(value.encode()).hexdigest()

    def test_merges_planner_compiled_and_shard_generated_outputs_and_resumes(self):
        files = {}
        self.add(self.planner, 'results/outputs', 'apps/web/dist/index.html', 'compiled', files)
        self.add(self.planner, 'results/outputs', 'apps/web/e2e/dist/report.html', 'report', files)
        self.add(self.shard, 'results/generated', 'apps/web/e2e/dist/screenshot.png', 'shot', files)
        (self.output / 'artifacts.json').write_text(json.dumps(files))
        deliver_surface(self.repo, self.output, self.submitted)
        deliver_surface(self.repo, self.output, self.submitted)
        self.assertEqual((self.repo / 'apps/web/dist/index.html').read_text(), 'compiled')
        self.assertEqual((self.repo / 'apps/web/e2e/dist/screenshot.png').read_text(), 'shot')

    def test_rejects_conflicting_generated_writes(self):
        second = 'd' * 32; files = {}
        self.add(self.planner, 'results/outputs', 'apps/web/dist/index.html', 'compiled', files)
        self.add(self.planner, 'results/outputs', 'apps/web/e2e/dist/report.html', 'report', files)
        self.add(self.shard, 'results/generated', 'apps/web/e2e/dist/screenshot.png', 'one', files)
        self.add(second, 'results/generated', 'apps/web/e2e/dist/screenshot.png', 'two', files)
        (self.output / 'children.json').write_text(json.dumps({'version': 1, 'parent_attempt': self.parent,
                                                                'children': [self.planner, self.shard, second]}))
        (self.output / 'artifacts.json').write_text(json.dumps(files))
        with self.assertRaisesRegex(ValueError, 'disagree'):
            deliver_surface(self.repo, self.output, self.submitted)


if __name__ == '__main__':
    unittest.main()
