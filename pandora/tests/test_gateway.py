"""The forced command a teammate's key runs through, instead of a shell.

`pandora.worker.gateway.check` decides which `SSH_ORIGINAL_COMMAND` shapes a
key pinned `restrict,command="gateway --name ..."` may run: the client's wire
protocol and nothing else. These tests pin the admitted shapes down exactly --
a refused shape is a feature, not a bug, so each one is named.
"""
import hashlib
import io
import os
import shlex
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from pandora.engine import bundle as bundle_module
from pandora.snapshot import transfer
from pandora.worker import gateway

ER = '/home/ubuntu/pandora-engine'
WR = '/home/ubuntu/pandora'
DIGEST = 'a' * 64
BUNDLE = ER + '/bundles/' + DIGEST


def check(command, engine_root=ER):
    return gateway.check(command, engine_root=engine_root, worker_root=WR)


class AdmittedShapes(unittest.TestCase):
    """Every command the real client sends must be admitted verbatim."""

    def test_the_home_probe(self):
        self.assertIsNone(check("sh -c 'cd \"$HOME\" && pwd'"))

    def test_the_bundle_presence_check(self):
        self.assertIsNone(check(
            "sh -c 'cat %s/bundles/%s/pandora/.bundle 2>/dev/null || true'" % (ER, DIGEST)))

    def test_each_engine_verb_a_client_calls(self):
        for verb in sorted(gateway.USER_ENGINE):
            command = ('cd %s && PYTHONPATH=%s python3 -m pandora.engine.service '
                       '--root %s %s' % (BUNDLE, BUNDLE, ER, verb))
            with self.subTest(verb=verb):
                self.assertIsNone(check(command))

    def test_engine_verbs_with_arguments(self):
        self.assertIsNone(check(
            'cd %s && PYTHONPATH=%s python3 -m pandora.engine.service --root %s '
            'cancel --run r0123 --client bob --queued-only' % (BUNDLE, BUNDLE, ER)))
        self.assertIsNone(check(
            'cd %s && PYTHONPATH=%s python3 -m pandora.engine.service --root %s '
            'logs --run r0123 --offset 4096 --client bob' % (BUNDLE, BUNDLE, ER)))

    def test_each_read_only_worker_verb(self):
        for verb in sorted(gateway.USER_WORKER):
            command = ('cd %s && PYTHONPATH=%s python3 -m pandora.worker.service '
                       '--root %s --engine-root %s %s' % (BUNDLE, BUNDLE, WR, ER, verb))
            with self.subTest(verb=verb):
                self.assertIsNone(check(command))

    def test_rsync_push_and_pull(self):
        push = ('rsync --server -a --no-times --checksum --delete --files-from=- --from0 '
                '--link-dest=%s/src/acme/aaa . %s/src/acme/bbb.partial/'
                % (ER, ER))
        self.assertIsNone(check(push))
        pull = 'rsync --server --sender -a . %s/runs/r0123/outputs/' % ER
        self.assertIsNone(check(pull))

    def test_every_feed_the_client_ships(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'engine'
            (root / 'bundles').mkdir(parents=True)
            (root / 'feeds.allow').write_text(bundle_module.feed_manifest())
            for name, script in sorted(transfer.FEEDS.items()):
                with self.subTest(feed=name):
                    # Quoting as the client does, through Link.feed.
                    argv = ['python3', '-c', script,
                            root.as_posix() + '/src/demo', root.as_posix() + '/src/demo/x']
                    command = ' '.join(shlex.quote(item) for item in argv)
                    self.assertIsNone(check(command, engine_root=root.as_posix()))
            script = bundle_module.BOOTSTRAP
            argv = ['python3', '-c', script, DIGEST, root.as_posix()]
            command = ' '.join(shlex.quote(item) for item in argv)
            self.assertIsNone(check(command, engine_root=root.as_posix()))

    def test_a_bundle_feeds_file_is_also_an_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'engine'
            mark = root / 'bundles' / DIGEST / 'pandora'
            mark.mkdir(parents=True)
            (mark / '.feeds').write_text(bundle_module.feed_manifest())
            argv = ['python3', '-c', transfer.FEEDS['clean'],
                    root.as_posix() + '/src/demo/x.partial']
            command = ' '.join(shlex.quote(item) for item in argv)
            self.assertIsNone(check(command, engine_root=root.as_posix()))


class RefusedShapes(unittest.TestCase):
    """The point of the gateway: everything else dies on the way in."""

    def test_a_shell_and_a_bare_command(self):
        self.assertIsNotNone(check(''))
        self.assertIsNotNone(check('ls -la'))
        self.assertIsNotNone(check('bash'))
        self.assertIsNotNone(check('sh -i'))

    def test_sh_c_other_than_the_two_probes(self):
        self.assertIsNotNone(check("sh -c 'rm -rf ~'"))
        self.assertIsNotNone(check("sh -c 'cat /etc/shadow 2>/dev/null || true'"))
        # The .bundle shape, but pointed outside the bundles directory.
        self.assertIsNotNone(check(
            "sh -c 'cat /etc/hostname/pandora/.bundle 2>/dev/null || true'"))
        self.assertIsNotNone(check("sh -c 'cat /etc/passwd'"))

    def test_python3_c_is_allowlisted_or_nothing(self):
        self.assertIsNotNone(check("python3 -c 'import os; os.system(\"id\")'"))
        self.assertIsNotNone(check('python3 -c "print(1)"'))

    def test_an_allowed_feed_with_a_hostile_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'engine'
            (root / 'bundles').mkdir(parents=True)
            (root / 'feeds.allow').write_text(bundle_module.feed_manifest())
            argv = ['python3', '-c', transfer.FEEDS['clean'], '/home/ubuntu']
            command = ' '.join(shlex.quote(item) for item in argv)
            self.assertIsNotNone(check(command, engine_root=root.as_posix()))
            self.assertIn('outside the engine root',
                          check(command, engine_root=root.as_posix()))

    def test_module_calls_outside_a_bundle(self):
        self.assertIsNotNone(check(
            'cd /tmp && PYTHONPATH=/tmp python3 -m pandora.engine.service --root %s submit' % ER))
        self.assertIsNotNone(check(
            'cd %s/bundles/../../etc && PYTHONPATH=%s python3 -m pandora.engine.service '
            '--root %s submit' % (ER, BUNDLE, ER)))
        self.assertIsNotNone(check(
            'cd %s/bundles/nothex && PYTHONPATH=%s/bundles/nothex python3 -m '
            'pandora.engine.service --root %s submit' % (ER, ER, ER)))

    def test_admin_verbs_are_not_for_user_keys(self):
        for verb in ('retain', 'cache-clear', 'canary', 'supervise'):
            command = ('cd %s && PYTHONPATH=%s python3 -m pandora.engine.service '
                       '--root %s %s' % (BUNDLE, BUNDLE, ER, verb))
            with self.subTest(verb=verb):
                self.assertIsNotNone(check(command))
        for verb in ('gc', 'canary', 'ready'):
            command = ('cd %s && PYTHONPATH=%s python3 -m pandora.worker.service '
                       '--root %s --engine-root %s %s' % (BUNDLE, BUNDLE, WR, ER, verb))
            with self.subTest(verb=verb):
                self.assertIsNotNone(check(command))

    def test_module_flags_must_name_the_provisioned_roots(self):
        self.assertIsNotNone(check(
            'cd %s && PYTHONPATH=%s python3 -m pandora.engine.service --root /tmp '
            'submit' % (BUNDLE, BUNDLE)))
        self.assertIsNotNone(check(
            'cd %s && PYTHONPATH=%s python3 -m pandora.worker.service --root /tmp '
            '--engine-root %s status' % (BUNDLE, BUNDLE, ER)))
        # `--python` names the interpreter a spawn would run: never a teammate's.
        self.assertIsNotNone(check(
            'cd %s && PYTHONPATH=%s python3 -m pandora.engine.service --root %s '
            '--python /tmp/evil submit' % (BUNDLE, BUNDLE, ER)))

    def test_python_m_without_the_bundle_chdir(self):
        self.assertIsNotNone(check('python3 -m pandora.engine.service --root %s submit' % ER))
        self.assertIsNotNone(check(
            'PYTHONPATH=%s python3 -m pandora.engine.service --root %s submit' % (BUNDLE, ER)))

    def test_rsync_outside_the_engine_root(self):
        self.assertIsNotNone(check('rsync --server --sender -a . /home/ubuntu/.ssh/'))
        self.assertIsNotNone(check('rsync --server -a . /tmp/stage/'))
        self.assertIsNotNone(check(
            'rsync --server -a --link-dest=/etc . %s/src/demo/x/' % ER))
        self.assertIsNotNone(check('rsync -a . %s/src/demo/x/' % ER))

    def test_garbage_and_misquoting(self):
        self.assertIsNotNone(check('python3 -c "unterminated'))
        self.assertIsNotNone(check('cd %s && PYTHONPATH=%s' % (BUNDLE, BUNDLE)))


class ThePinIsTheIdentity(unittest.TestCase):
    def test_an_admitted_command_runs_with_the_pinned_client(self):
        command = "sh -c 'cd \"$HOME\" && pwd'"
        env = {'SSH_ORIGINAL_COMMAND': command}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(os, 'execvpe') as execvpe:
            code = gateway.main(['--name', 'sterling@laptop',
                                 '--engine-root', ER, '--worker-root', WR])
            # Copy inside the block: patch.dict restores os.environ on exit.
            passed = dict(execvpe.call_args[0][2])
        self.assertEqual(code, 127)          # execvpe is mocked: it returns
        self.assertEqual(execvpe.call_args[0][0], 'sh')
        self.assertEqual(passed['PANDORA_GATEWAY_CLIENT'], 'sterling@laptop')

    def test_a_refused_command_execs_nothing_and_says_so(self):
        env = {'SSH_ORIGINAL_COMMAND': 'cat /etc/shadow'}
        err = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(os, 'execvpe') as execvpe, \
                redirect_stderr(err):
            code = gateway.main(['--name', 'sterling@laptop',
                                 '--engine-root', ER, '--worker-root', WR])
        self.assertEqual(code, 1)
        execvpe.assert_not_called()
        self.assertIn('sterling@laptop', err.getvalue())

    def test_no_command_at_all_is_a_refusal(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), redirect_stderr(err):
            code = gateway.main(['--name', 'sterling@laptop',
                                 '--engine-root', ER, '--worker-root', WR])
        self.assertEqual(code, 1)


class TheManifestCarriesTheFeeds(unittest.TestCase):
    def test_the_bundle_embeds_every_feeds_digest(self):
        digests = set(bundle_module.feed_digests())
        expected = {hashlib.sha256(text.encode()).hexdigest()
                    for text in [bundle_module.BOOTSTRAP] + list(transfer.FEEDS.values())}
        self.assertEqual(digests, expected)
        self.assertEqual(set(bundle_module.feed_manifest().splitlines()), digests)


if __name__ == '__main__':
    unittest.main()
