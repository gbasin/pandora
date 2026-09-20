"""Exercise real zsh startup and manual PATH changes without starting agents."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


class EnvironmentTests(unittest.TestCase):
    def test_launcher_preserves_artifact_and_suite_settings_for_codex_shells(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = root / 'codex'
            fake.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
            fake.chmod(0o755)
            env = dict(os.environ)
            env['PANDORA_REAL_CODEX'] = str(fake)
            env['PANDORA_REAL_PNPM'] = '/usr/bin/true'
            result = subprocess.check_output([
                'python3', str(ROOT / 'launch.py'), '--host', 'unused', '--state', str(root / 'state'),
                '--artifact-delivery-limit-bytes', '123', '--suite-shards', '7', '--',
                'codex', 'exec', '--add-dir', '/existing/root', 'prompt',
            ], env=env, text=True)
            argv = json.loads(result)
            self.assertEqual(argv[0:2], ['exec', '--add-dir'])
            self.assertEqual(Path(argv[2]).resolve(), (root / 'state').resolve())
            self.assertEqual(argv[-3:], ['--add-dir', '/existing/root', 'prompt'])
            self.assertFalse(any('shell_environment_policy.set.PATH=' in arg for arg in argv))
            self.assertFalse(any('writable_roots' in arg for arg in argv))
            self.assertIn('shell_environment_policy.set.PANDORA_QUEUE_TIMEOUT_SECONDS="900"', argv)
            self.assertIn('shell_environment_policy.set.PANDORA_ARTIFACT_DELIVERY_LIMIT_BYTES="123"', argv)
            self.assertIn('shell_environment_policy.set.PANDORA_SUITE_SHARDS="7"', argv)

    def test_newer_shim_accepts_an_older_launcher_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fake = root / 'codex'
            fake.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
            fake.chmod(0o755)
            env = dict(os.environ)
            for key in ['PANDORA_HOST', 'PANDORA_STATE', 'PANDORA_SESSION',
                        'PANDORA_REAL_PNPM', 'PANDORA_TREATMENT', 'PANDORA_QUEUE_TIMEOUT_SECONDS', 'ZDOTDIR', 'PANDORA_ORIGINAL_ZDOTDIR']:
                env[key] = str(root / key)
            env['PANDORA_REAL_CODEX'] = str(fake)
            env.pop('PANDORA_ARTIFACT_DELIVERY_LIMIT_BYTES', None)
            env.pop('PANDORA_SUITE_SHARDS', None)
            argv = json.loads(subprocess.check_output(
                ['python3', str(ROOT / 'bin/codex'), 'exec', 'prompt'], env=env, text=True))
            self.assertFalse(any('PANDORA_ARTIFACT_DELIVERY_LIMIT_BYTES' in arg for arg in argv))
            self.assertFalse(any('PANDORA_SUITE_SHARDS' in arg for arg in argv))

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
