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


if __name__ == '__main__':
    unittest.main()
