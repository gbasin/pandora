"""Unclaimed heavy-looking commands run unchanged here and leave one log line.

Through the real POSIX shim, so the `heavy` marker key, the hand-off and the
exit code are all the shipped ones.
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from pandora.client import enrolment

HERE = Path(__file__).resolve().parents[2]


class HeavyForms(unittest.TestCase):
    def test_a_claimed_form_is_never_also_heavy(self):
        forms = enrolment.heavy_forms([['build'], ['journey']])
        self.assertNotIn(['build'], forms)
        self.assertIn(['lint'], forms)


class ThroughTheShim(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        root = Path(home.name)
        self.repo, self.state, fake = root / 'repo', root / 'state', root / 'fake'
        for directory in (self.repo, self.state, fake):
            directory.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (fake / 'pnpm').write_text('#!/bin/sh\necho "real $*"\nexit 3\n')
        (fake / 'pnpm').chmod(0o755)
        (self.repo / '.git' / 'pandora-enrolled').write_text(enrolment.render(
            socket_path=str(self.state / 'client.sock'), repo='demo',
            claims=[['journey']], heavy=enrolment.heavy_forms([['journey']]),
            home=str(HERE)))
        self.env = dict(os.environ, PATH='%s:%s' % (HERE / 'bin', fake) + ':/usr/bin:/bin')
        for name in ('PANDORA_OFF', 'PANDORA_ROUTE_DEPTH'):
            self.env.pop(name, None)

    def pnpm(self, *argv):
        return subprocess.run(['sh', str(HERE / 'bin' / 'pnpm'), *argv], cwd=self.repo,
                              env=self.env, capture_output=True, text=True, timeout=30)

    def rows(self):
        path = self.state / 'passthrough.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] \
            if path.exists() else []

    def test_a_heavy_command_runs_unchanged_and_is_logged(self):
        proc = self.pnpm('build', '--filter', 'x')
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(proc.stdout, 'real build --filter x\n')
        self.assertEqual(proc.stderr, '')
        [row] = self.rows()
        self.assertEqual((row['kind'], row['reason'], row['argv'], row['exit']),
                         ('passthrough', 'unclaimed', ['build', '--filter', 'x'], 3))
        self.assertIn('duration_ms', row)

    def test_a_light_command_is_not_logged(self):
        self.assertEqual(self.pnpm('why', 'react').returncode, 3)
        self.assertEqual(self.rows(), [])


if __name__ == '__main__':
    unittest.main()
