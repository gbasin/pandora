"""The engine bundle is an explicit member list, and a module the engine imports
that is not on it fails on the worker, at first use, with an ImportError from a
bundle directory nobody can edit. On 2026-09-22 `engine/writeback.py` was added
to the engine and not to the list, and `pandora worker gc` was the first thing
to notice. This test unpacks the bundle exactly as the worker's bootstrap does
and imports the three entry points in `ENTRY_POINTS` from it in a clean
interpreter. That proves their import-time closure only: a module imported
lazily inside a function, and not by any entry point at import, is not
checked here."""
import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pandora.engine import bundle

ENTRY_POINTS = ('pandora.worker.service', 'pandora.engine.service', 'pandora.engine.runner')


class Bundle(unittest.TestCase):
    def test_every_module_the_entry_points_import_is_shipped(self):
        digest, text = bundle.payload()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, data in json.loads(text).items():
                path = root / 'pandora' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(base64.b64decode(data))
            code = 'import importlib, sys\nsys.path.insert(0, %r)\n' % tmp + ''.join(
                'importlib.import_module(%r)\n' % module for module in ENTRY_POINTS)
            proc = subprocess.run([sys.executable, '-I', '-c', code], cwd='/',
                                  capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0,
                         'the bundle %s cannot be imported on its own:\n%s' % (digest[:12], proc.stderr))

    def test_members_exist(self):
        for name in bundle.MEMBERS:
            self.assertTrue((Path(bundle.__file__).resolve().parents[1] / name).is_file(), name)


if __name__ == '__main__':
    unittest.main()
