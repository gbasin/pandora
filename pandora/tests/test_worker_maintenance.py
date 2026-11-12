"""Installer validation must precede any writes or systemctl calls."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/install-worker-maintenance.sh'


@unittest.skipUnless(Path('/usr/bin/python3').exists(), 'service Python required')
class WorkerMaintenanceInstallerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='maintenance-installer-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.cwd = self.root / 'checkout'
        self.cwd.mkdir()
        self.bundle = self.root / 'bundle'
        self.package(self.bundle, 0)
        self.package(self.cwd, 0)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        systemctl = self.bin / 'systemctl'
        systemctl.write_text('#!/bin/sh\necho "$*" >> "$SYSTEMCTL_LOG"\n')
        systemctl.chmod(0o755)
        self.log = self.root / 'systemctl.log'
        self.env = dict(os.environ, HOME=str(self.home),
                        PATH=str(self.bin) + ':' + os.environ['PATH'],
                        SYSTEMCTL_LOG=str(self.log))

    def package(self, directory, exit_code):
        engine = directory / 'pandora/engine'
        engine.mkdir(parents=True, exist_ok=True)
        (directory / 'pandora/__init__.py').touch()
        (directory / 'pandora/.bundle').write_text('synthetic-test-bundle\n')
        (engine / '__init__.py').touch()
        (engine / 'service.py').write_text('raise SystemExit(%d)\n' % exit_code)

    def install(self, bundle=None, root=None):
        return subprocess.run(['sh', str(SCRIPT), str(bundle or self.bundle),
                               str(root or self.root / 'engine')],
                              cwd=self.cwd, env=self.env, capture_output=True, text=True)

    def assert_untouched(self):
        self.assertFalse((self.home / '.config/systemd/user').exists())
        self.assertFalse(self.log.exists())

    def test_relative_bundle_is_rejected_before_writing_units(self):
        result = self.install(bundle='../bundle')
        self.assertNotEqual(result.returncode, 0)
        self.assert_untouched()

    def test_relative_engine_root_is_rejected_before_writing_units(self):
        result = self.install(root='pandora-engine')
        self.assertNotEqual(result.returncode, 0)
        self.assert_untouched()

    def test_checkout_cannot_hide_an_incompatible_selected_bundle(self):
        self.package(self.bundle, 2)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assert_untouched()

    def test_valid_bundle_uses_service_python_and_ignores_checkout(self):
        self.package(self.cwd, 17)
        python = self.bin / 'python3'
        python.write_text('#!/bin/sh\nexit 19\n')
        python.chmod(0o755)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        unit = (self.home / '.config/systemd/user/pandora-retain.service').read_text()
        self.assertIn('WorkingDirectory=' + str(self.bundle), unit)
        self.assertIn('ExecStart=/usr/bin/python3 ', unit)
        self.assertIn('--root ' + str(self.root / 'engine'), unit)
        self.assertIn('enable --now pandora-retain.timer pandora-expire-queued.timer',
                      self.log.read_text())
