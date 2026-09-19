"""Exercise real zsh startup and manual PATH changes without starting agents."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


class EnvironmentTests(unittest.TestCase):
    def test_startup_order_manual_path_and_original_pnpm(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / 'original'
            alternate = root / 'alternate'
            zdot = root / 'zdot'
            for path in [original, alternate, zdot]:
                path.mkdir()
            for path, label in [(original, 'original'), (alternate, 'alternate')]:
                exe = path / 'pnpm'
                exe.write_text(f'#!/bin/sh\necho {label}\n')
                exe.chmod(0o755)
            (zdot / '.zshenv').write_text(f'export PATH="{alternate}:$PATH"\nexport PANDORA_TEST_STARTUP=loaded\n')
            env = {k: v for k, v in os.environ.items() if not k.startswith('PANDORA_')}
            env.update(PATH=str(original) + ':' + env['PATH'], ZDOTDIR=str(zdot))
            launch = ['python3', str(ROOT / 'launch.py'), '--host', 'unused', '--state', str(root / 'state'), '--']
            result = subprocess.run([*launch, 'zsh', '-c',
                                     'command -v pnpm; pnpm --version; echo "$PANDORA_TEST_STARTUP"; '
                                     f'export PATH="{alternate}:$PATH"; pnpm --version'],
                                    env=env, text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout.splitlines(), [str(ROOT / 'bin/pnpm'), 'original', 'loaded', 'alternate'])
            self.assertEqual((zdot / '.zshenv').read_text(), f'export PATH="{alternate}:$PATH"\nexport PANDORA_TEST_STARTUP=loaded\n')

    def test_login_only_initialization_is_absent_in_nonlogin_shell(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            zdot = root / 'zdot'
            zdot.mkdir()
            (zdot / '.zprofile').write_text('export PANDORA_TEST_LOGIN_ONLY=loaded\n')
            env = {k: v for k, v in os.environ.items() if not k.startswith('PANDORA_')}
            env['ZDOTDIR'] = str(zdot)
            launch = ['python3', str(ROOT / 'launch.py'), '--host', 'unused', '--state', str(root / 'state'), '--']
            probe = 'echo "${PANDORA_TEST_LOGIN_ONLY-unset}"'
            normal = subprocess.check_output([*launch, 'zsh', '-c', probe], env=env, text=True)
            login = subprocess.check_output([*launch, 'zsh', '-lc', probe], env=env, text=True)
            self.assertEqual(normal.strip(), 'unset')
            self.assertEqual(login.strip(), 'loaded')


if __name__ == '__main__':
    unittest.main()
