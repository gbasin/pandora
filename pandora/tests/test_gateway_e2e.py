"""The gateway as a real process against a real engine root.

`test_gateway.py` checks the filter in-process; this file runs
`pandora/worker/gateway.py` the way sshd does: SSH_ORIGINAL_COMMAND in the
environment, `sh -c` on the other side, and a real `pandora.engine.service`
under a fake bundle (the checkout's own package, symlinked). Only sshd's
`restrict,command=` line itself is absent -- its rendering is covered by
`test_worker.py::AuthorizedKeys`.
"""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pandora.engine import bundle as bundle_module
from pandora.engine.ledger import Ledger
from pandora.snapshot import transfer

PANDORA = Path(__file__).resolve().parents[1]       # the package dir itself
GATEWAY = PANDORA / 'worker' / 'gateway.py'
DIGEST = 'a' * 64


def engine_call(bundle, root, argv):
    """The wire shape `Remote.call` builds, as one shell command."""
    return 'cd %s && PYTHONPATH=%s python3 -m pandora.engine.service --root %s %s' % (
        shlex.quote(str(bundle)), shlex.quote(str(bundle)), shlex.quote(str(root)),
        ' '.join(shlex.quote(item) for item in argv))


class GatewaySubprocess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.engine_root = base / 'engine'
        self.worker_root = base / 'worker'
        self.bundle = self.engine_root / 'bundles' / DIGEST
        # A "bundle" is a directory importable as the pandora package.
        self.bundle.mkdir(parents=True)
        os.symlink(PANDORA, self.bundle / 'pandora')
        self.worker_root.mkdir()
        (self.engine_root / 'feeds.allow').write_text(bundle_module.feed_manifest())

    def through(self, command, *, stdin=None):
        """Run the gateway as sshd would: the command in the environment."""
        env = dict(os.environ, SSH_ORIGINAL_COMMAND=command)
        return subprocess.run(
            [sys.executable, str(GATEWAY), '--name', 'bob@laptop',
             '--engine-root', str(self.engine_root),
             '--worker-root', str(self.worker_root)],
            env=env, input=stdin, capture_output=True, text=True, timeout=120)

    def test_a_real_engine_call_passes_and_answers(self):
        proc = self.through(engine_call(self.bundle, str(self.engine_root), ['stats']))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        answer = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(answer['ok'])

    def test_the_pin_reaches_the_ledger(self):
        # Alice's request, Bob's key: the row is Bob's.
        fence = engine_call(self.bundle, str(self.engine_root),
                            ['lookup', '--request-id', 't:fence', '--fence',
                             '--repo', 'demo', '--job', 'check',
                             '--client', 'alice@studio'])
        proc = self.through(fence)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ledger = Ledger(self.engine_root / 'ledger.db')
        try:
            row = ledger.by_request('t:fence')
            self.assertIsNotNone(row)
            self.assertEqual(row['client'], 'bob@laptop')
        finally:
            ledger.close()

    def test_a_foreign_status_is_not_yours_over_the_wire(self):
        ledger = Ledger(self.engine_root / 'ledger.db')
        ledger.claim('alice:run', 'ralice', repo='demo', job='check', input_id='i',
                     source_path='/tmp', argv=[], env={}, cwd='', outputs=[],
                     size_class='small', client='alice@studio')
        ledger.close()
        status = engine_call(self.bundle, str(self.engine_root),
                             ['status', '--run', 'ralice', '--client', 'alice@studio'])
        proc = self.through(status)
        self.assertEqual(proc.returncode, 0)
        answer = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(answer['code'], 'not-yours')

    def test_an_allowlisted_feed_runs_and_a_stranger_does_not(self):
        feed = 'python3 -c ' + shlex.quote(transfer.FEEDS['probe']) + ' ' + \
            shlex.quote(str(self.engine_root / 'src' / 'demo')) + ' ' + \
            shlex.quote(str(self.engine_root / 'src' / 'demo' / 'x'))
        proc = self.through(feed)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), 'absent')
        proc = self.through("python3 -c 'import os; print(os.getuid())'")
        self.assertEqual(proc.returncode, 1)
        self.assertIn('not on the feed allowlist', proc.stderr)

    def test_a_shell_is_refused_before_it_runs(self):
        for command in ('id', "sh -c 'echo hi'", 'python3 -m pandora.engine.service',
                        'rsync --server -a . /etc/'):
            with self.subTest(command=command):
                proc = self.through(command)
                self.assertEqual(proc.returncode, 1)
                self.assertIn('pandora-gateway: refused as bob@laptop', proc.stderr)


if __name__ == '__main__':
    unittest.main()
