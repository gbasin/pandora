#!/usr/bin/env python3
"""Exercise the warm surface adapter without a browser or container."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name('in-container.sh')


class SurfaceRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.source = self.workspace / 'source'
        (self.source / 'tools').mkdir(parents=True)
        (self.root / 'bin').mkdir()
        self.cgroup = self.root / 'cgroup'
        self.cgroup.mkdir()
        for name, content in {
            'memory.peak': '1048576\n',
            'memory.events': 'oom 0\n',
            'cpu.stat': 'usage_usec 3\n',
        }.items():
            (self.cgroup / name).write_text(content)
        self.args = self.root / 'pnpm-args'
        self.junit = self.root / 'junit-path'
        (self.root / 'bin' / 'node').write_text('#!/bin/sh\nexit "${PANDORA_NODE_STATUS:-0}"\n')
        (self.root / 'bin' / 'pnpm').write_text(
            '#!/bin/sh\n'
            'printf "%s\\n" "$@" > "$PANDORA_PNPM_ARGS"\n'
            'printf "%s" "$PLAYWRIGHT_JUNIT_OUTPUT_FILE" > "$PANDORA_JUNIT_PATH"\n'
            'case "$2" in\n'
            "  @acme/web) app='web' ;;\n"
            "  @acme/desk) app='desk' ;;\n"
            '  *) exit 91 ;;\n'
            'esac\n'
            'mkdir -p "$PANDORA_WORKSPACE_ROOT/results/playwright"\n'
            'printf artifact > "$PANDORA_WORKSPACE_ROOT/results/playwright/result.txt"\n'
            'mkdir -p "$PWD/apps/$app/dist" "$PWD/apps/$app/e2e/dist"\n'
            'printf production > "$PWD/apps/$app/dist/index.html"\n'
            'printf fixture > "$PWD/apps/$app/e2e/dist/index.html"\n'
            'exit "${PANDORA_PNPM_STATUS:-0}"\n'
        )
        for executable in (self.root / 'bin').iterdir():
            executable.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def run_adapter(self, *selectors, **overrides):
        env = dict(os.environ)
        env.update({
            'PATH': str(self.root / 'bin') + os.pathsep + env['PATH'],
            'PANDORA_CGROUP_ROOT': str(self.cgroup),
            'PANDORA_JUNIT_PATH': str(self.junit),
            'PANDORA_NODE_BIN': str(self.root / 'bin/node'),
            'PANDORA_PNPM_ARGS': str(self.args),
            'PANDORA_PNPM_BIN': str(self.root / 'bin/pnpm'),
            'PANDORA_WORKSPACE_ROOT': str(self.workspace),
        })
        env.update(overrides)
        return subprocess.run(['bash', str(SCRIPT), *selectors], env=env,
                              text=True, capture_output=True)

    def test_default_web_web_preserves_selectors_and_collects_outputs(self):
        result = self.run_adapter('e2e/loan.spec.ts', '--grep', 'document access')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.workspace / 'results/surface.json').read_text()),
                         {'app': 'web'})
        self.assertEqual(self.args.read_text().splitlines(), [
            '--filter', '@acme/web', 'test:e2e', 'e2e/loan.spec.ts',
            '--grep', 'document access', '--workers=1', '--reporter=line,junit',
            '--output=' + str(self.workspace / 'results/playwright'),
        ])
        self.assertEqual(self.junit.read_text(), str(self.workspace / 'results/junit.xml'))
        self.assertEqual((self.workspace / 'results/playwright/result.txt').read_text(), 'artifact')
        self.assertEqual((self.workspace / 'results/outputs/apps/web/dist/index.html').read_text(),
                         'production')
        self.assertEqual((self.workspace / 'results/outputs/apps/web/e2e/dist/index.html').read_text(),
                         'fixture')
        self.assertEqual((self.workspace / 'results/memory-peak-bytes').read_text(), '1048576\n')

    def test_desk_selects_its_package_and_both_build_outputs(self):
        result = self.run_adapter('e2e/desk.spec.ts', PANDORA_SURFACE_APP='desk')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads((self.workspace / 'results/surface.json').read_text()), {'app': 'desk'})
        self.assertEqual(self.args.read_text().splitlines()[0:3],
                         ['--filter', '@acme/desk', 'test:e2e'])
        self.assertTrue((self.workspace / 'results/outputs/apps/desk/dist/index.html').is_file())
        self.assertTrue((self.workspace / 'results/outputs/apps/desk/e2e/dist/index.html').is_file())

    def test_failure_still_records_surface_and_resource_evidence(self):
        result = self.run_adapter('--grep', 'fails', PANDORA_SURFACE_APP='desk',
                                  PANDORA_PNPM_STATUS='17')

        self.assertEqual(result.returncode, 17)
        self.assertEqual(json.loads((self.workspace / 'results/surface.json').read_text()), {'app': 'desk'})
        self.assertEqual((self.workspace / 'results/exit-code').read_text(), '17\n')
        self.assertEqual((self.workspace / 'results/phase').read_text(), 'finished\n')
        self.assertEqual((self.workspace / 'results/cpu-stat').read_text(), 'usage_usec 3\n')
        self.assertFalse((self.workspace / 'results/outputs').exists())

    def test_rejects_unknown_surface_before_running_commands(self):
        result = self.run_adapter(PANDORA_SURFACE_APP='api')

        self.assertEqual(result.returncode, 64)
        self.assertIn('Unsupported PANDORA_SURFACE_APP: api', result.stderr)
        self.assertFalse(self.args.exists())


if __name__ == '__main__':
    unittest.main()
