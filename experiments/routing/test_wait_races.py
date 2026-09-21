"""Process-level races around explicit wait recovery.

These use the same advisory locks as the router.  Network and publication
edges are replaced, but allocation and the local evidence/owner fences are not.
"""
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import route
import wait


COMMAND = ['test:surface', 'borrower-web']


def _hold_lock(path, ready, release):
    """Keep a real flock open in another process until the parent releases it."""
    handle = route.locked(Path(path))
    ready.set()
    release.wait(10)
    handle.close()


def _retrieve_once(output_name, attempt, count_name, ready):
    """A separate follower with a deliberately slow, fake remote transfer."""
    import transport
    from pathlib import Path
    from unittest.mock import patch

    output = Path(output_name)
    count = Path(count_name)

    def rsync(command, **_kwargs):
        with count.open('a') as recorded:
            recorded.write('transfer\n')
        stage = Path(command[-1])
        if len(command) > 2 and command[2] == '--files-from=-':
            (stage / 'result.txt').write_text('result')
        else:
            ready.set()
            time.sleep(.25)
            (stage / 'artifacts.json').write_text(json.dumps({'result.txt': 'ignored'}))
            (stage / 'terminal.json').write_text(json.dumps({'attempt': attempt, 'exit_code': 0,
                                                              'cleanup_verified': True}))

    with patch('transport.subprocess.run', side_effect=rsync), \
         patch('transport.query', return_value={'artifact_sizes': {'result.txt': 6},
                                                 'artifact_total_bytes': 6}), \
         patch('transport.validate_evidence', return_value={'attempt': attempt, 'exit_code': 0}):
        transport.retrieve('unused', output, attempt)


class WaitRaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        self.state_root = self.repo / 'state'
        self.state = self.state_root / hashlib.sha256(str(self.repo).encode()).hexdigest()
        self.state.mkdir(parents=True)

    def active(self, attempt, *, terminal=False):
        output = self.state / attempt
        output.mkdir(parents=True, exist_ok=True)
        (output / 'submission.json').write_text(json.dumps({'attempt': attempt, 'source_digest': 'same'}))
        if terminal:
            (output / 'terminal.json').write_text(json.dumps({'attempt': attempt, 'exit_code': 1,
                                                               'cleanup_verified': True}))
            (output / 'artifacts.json').write_text('{}')
        record = {'state': 'active', 'protocol': 2, 'tool': 'pnpm', 'output': str(output),
                  'command': COMMAND, 'host': 'unused', 'attempt': attempt}
        route.write(self.state / 'active.json', record)
        return output, record

    def routing(self, **extra):
        environment = {'PANDORA_STATE': str(self.state_root), 'PANDORA_HOST': 'unused',
                       'PANDORA_SESSION': 'test'}
        environment.update(extra)
        return patch.dict(os.environ, environment), \
            patch.object(sys, 'argv', ['route.py', *COMMAND]), \
            patch('route.subprocess.check_output', return_value=str(self.repo)), \
            patch('route.Path.cwd', return_value=self.repo)

    def test_stalled_original_holds_per_attempt_owner_lock(self):
        output, _ = self.active('a' * 32)
        ready, release = multiprocessing.Event(), multiprocessing.Event()
        owner = multiprocessing.Process(target=_hold_lock, args=(route.owner_path(output), ready, release))
        owner.start(); self.addCleanup(lambda: (release.set(), owner.join(10)))
        self.assertTrue(ready.wait(5))
        env, argv, git, cwd = self.routing()
        with env, argv, git, cwd, patch('route.subprocess.Popen', side_effect=AssertionError('must not recover')):
            self.assertEqual(route.main(), 75)

    def test_explicit_wait_finalizes_verified_terminal_while_original_owner_lives(self):
        attempt = 'b' * 32
        output, _ = self.active(attempt, terminal=True)
        ready, release = multiprocessing.Event(), multiprocessing.Event()
        owner = multiprocessing.Process(target=_hold_lock, args=(route.owner_path(output), ready, release))
        owner.start(); self.addCleanup(lambda: (release.set(), owner.join(10)))
        self.assertTrue(ready.wait(5))
        env = {'PANDORA_STATE': str(self.state_root), 'PANDORA_HOST': 'unused', 'PANDORA_SESSION': 'test'}
        with patch.dict(os.environ, env), patch.object(sys, 'argv', ['wait.py', attempt]), \
             patch('wait.subprocess.check_output', return_value=str(self.repo)), patch('wait.Path.cwd', return_value=self.repo), \
             patch('route.subprocess.check_output', return_value=str(self.repo)), patch('route.Path.cwd', return_value=self.repo), \
             patch('route.current_digest', return_value='same'), \
             patch('route.validate_evidence', return_value={'attempt': attempt, 'exit_code': 1, 'cleanup_verified': True}):
            self.assertEqual(wait.main(), 1)
        self.assertEqual(json.loads((self.state / 'active.json').read_text())['state'], 'terminal')

    def test_normal_command_allocates_after_wait_finalizes_while_old_owner_is_alive(self):
        attempt = 'c' * 32
        output, _ = self.active(attempt, terminal=True)
        ready, release = multiprocessing.Event(), multiprocessing.Event()
        owner = multiprocessing.Process(target=_hold_lock, args=(route.owner_path(output), ready, release))
        owner.start(); self.addCleanup(lambda: (release.set(), owner.join(10)))
        self.assertTrue(ready.wait(5))
        env = {'PANDORA_STATE': str(self.state_root), 'PANDORA_HOST': 'unused', 'PANDORA_SESSION': 'test'}
        with patch.dict(os.environ, env), patch.object(sys, 'argv', ['wait.py', attempt]), \
             patch('wait.subprocess.check_output', return_value=str(self.repo)), patch('wait.Path.cwd', return_value=self.repo), \
             patch('route.subprocess.check_output', return_value=str(self.repo)), patch('route.Path.cwd', return_value=self.repo), \
             patch('route.current_digest', return_value='same'), \
             patch('route.validate_evidence', return_value={'attempt': attempt, 'exit_code': 1, 'cleanup_verified': True}):
            self.assertEqual(wait.main(), 1)
        child = type('Child', (), {'wait': lambda self: 75})()
        def launch(command, **_kwargs):
            fresh_output = Path(command[command.index('--output') + 1])
            fresh_output.mkdir()
            (fresh_output / 'submission.json').write_text(json.dumps({'attempt': command[command.index('--attempt') + 1],
                                                                       'source_digest': 'same'}))
            return child
        env, argv, git, cwd = self.routing()
        with env, argv, git, cwd, patch('route.subprocess.Popen', side_effect=launch) as launched:
            self.assertEqual(route.main(), 75)
        new = json.loads((self.state / 'active.json').read_text())
        self.assertEqual(new['state'], 'active')
        self.assertNotEqual(new['attempt'], attempt)
        self.assertIn('warm.py', launched.call_args.args[0][2])

    def test_late_original_finalizer_cannot_publish_or_replace_new_active_attempt(self):
        old = 'd' * 32
        output, _ = self.active(old)
        fresh_output = self.state / ('e' * 32)
        fresh_output.mkdir()
        (fresh_output / 'submission.json').write_text(json.dumps({'attempt': 'e' * 32, 'source_digest': 'same'}))
        new = {'state': 'active', 'protocol': 2, 'tool': 'pnpm', 'output': str(fresh_output),
               'command': COMMAND, 'host': 'unused', 'attempt': 'e' * 32}
        # This models the old client after its child returned: it must re-read the
        # state fence before it validates evidence or calls delivery.
        def finished_after_replacement():
            (output / 'terminal.json').write_text(json.dumps({'attempt': old, 'exit_code': 0,
                                                               'cleanup_verified': True}))
            (output / 'artifacts.json').write_text('{}')
            route.write(self.state / 'active.json', new)
            return 0
        child = type('Child', (), {'wait': lambda self: finished_after_replacement()})()
        env, argv, git, cwd = self.routing()
        with env, argv, git, cwd, patch('route.subprocess.Popen', return_value=child), \
             patch('route.deliver', side_effect=AssertionError('late owner must not publish')):
            self.assertEqual(route.main(), 75)
        self.assertEqual(json.loads((self.state / 'active.json').read_text()), new)

    def test_late_original_cancel_cannot_replace_new_active_attempt(self):
        old = '0' * 32
        _, _ = self.active(old)
        fresh_output = self.state / ('9' * 32)
        fresh_output.mkdir()
        (fresh_output / 'submission.json').write_text(json.dumps({'attempt': '9' * 32, 'source_digest': 'same'}))
        new = {'state': 'active', 'protocol': 2, 'tool': 'pnpm', 'output': str(fresh_output),
               'command': COMMAND, 'host': 'unused', 'attempt': '9' * 32}
        child = type('Child', (), {'wait': lambda self: (_ for _ in ()).throw(KeyboardInterrupt),
                                   'poll': lambda self: 0})()
        def cancelled(*_args):
            route.write(self.state / 'active.json', new)
            return {'cleanup_verified': True}
        env, argv, git, cwd = self.routing()
        with env, argv, git, cwd, patch('route.subprocess.Popen', return_value=child), \
             patch('route.control', side_effect=cancelled), patch('route.time.sleep'):
            self.assertEqual(route.main(), 130)
        self.assertEqual(json.loads((self.state / 'active.json').read_text()), new)

    def test_concurrent_followers_retrieve_evidence_once_under_evidence_lock(self):
        attempt = 'f' * 32
        output = self.state / attempt
        output.mkdir()
        (output / 'submission.json').write_text(json.dumps({'attempt': attempt}))
        count = self.state / 'transfers.txt'
        context = multiprocessing.get_context('fork')
        ready = context.Event()
        second_ready = context.Event()
        first = context.Process(target=_retrieve_once, args=(str(output), attempt, str(count), ready))
        second = context.Process(target=_retrieve_once, args=(str(output), attempt, str(count), second_ready))
        first.start(); self.assertTrue(ready.wait(5)); second.start()
        first.join(10); second.join(10)
        self.assertEqual(first.exitcode, 0); self.assertEqual(second.exitcode, 0)
        # One retrieval is two rsync transfers.  The second follower sees promoted terminal evidence.
        self.assertEqual(count.read_text().splitlines(), ['transfer', 'transfer'])

    def test_dead_normal_owner_recovers_existing_attempt_without_resubmission(self):
        attempt = '1' * 32
        _, _ = self.active(attempt)
        child = type('Child', (), {'wait': lambda self: 75})()
        env, argv, git, cwd = self.routing()
        with env, argv, git, cwd, patch('route.subprocess.Popen', return_value=child) as launched:
            self.assertEqual(route.main(), 75)
        command = launched.call_args.args[0]
        self.assertIn('transport.py', command[2])
        self.assertEqual(json.loads((self.state / 'active.json').read_text())['attempt'], attempt)

    def test_observer_interrupt_never_cancels_original_attempt(self):
        attempt = '2' * 32
        output, _ = self.active(attempt)
        env = {'PANDORA_STATE': str(self.state_root), 'PANDORA_HOST': 'unused'}
        with patch.dict(os.environ, env), patch.object(sys, 'argv', ['wait.py', attempt]), \
             patch('wait.subprocess.check_output', return_value=str(self.repo)), patch('wait.Path.cwd', return_value=self.repo), \
             patch('wait.follow', side_effect=KeyboardInterrupt), patch('route.control') as cancel:
            with self.assertRaises(KeyboardInterrupt):
                wait.main()
        cancel.assert_not_called()
        self.assertEqual(json.loads((self.state / 'active.json').read_text())['state'], 'active')


if __name__ == '__main__':
    unittest.main()
