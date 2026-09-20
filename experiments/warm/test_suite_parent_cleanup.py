import fcntl
import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import suite_parent_cleanup


def hold_lock(path, ready, release):
    with Path(path).open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        ready.set()
        release.wait(5)


class SuiteParentCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.parent = self.root / 'runs' / ('a' * 32)
        self.parent.mkdir(parents=True)
        (self.parent / 'suite-cleanup.pending').touch()

    def tearDown(self):
        self.temp.cleanup()

    def registry(self, children):
        value = {'version': 1, 'parent_attempt': self.parent.name, 'children': children}
        (self.parent / 'children.json').write_text(json.dumps(value))
        return value

    def child(self, identity):
        path = self.root / 'runs' / identity
        path.mkdir()
        return path

    def terminal(self, child, *, cleanup=True, identity=None):
        (child / 'terminal.json').write_text(json.dumps({
            'attempt': child.name if identity is None else identity,
            'cleanup_verified': cleanup,
        }))

    def test_validate_registry_requires_exact_unique_nonparent_ids(self):
        child = 'b' * 32
        self.assertEqual(suite_parent_cleanup.validate_registry(self.parent, self.registry([child])), [child])
        for value in (
                {'version': 1, 'parent_attempt': self.parent.name, 'children': [child], 'extra': True},
                {'version': True, 'parent_attempt': self.parent.name, 'children': [child]},
                {'version': 1, 'parent_attempt': self.parent.name, 'children': [child, child]},
                {'version': 1, 'parent_attempt': self.parent.name, 'children': [self.parent.name]},
                {'version': 1, 'parent_attempt': 'c' * 32, 'children': [child]},):
            with self.subTest(value=value), self.assertRaises(ValueError):
                suite_parent_cleanup.validate_registry(self.parent, value)

    def test_missing_registry_is_unresolved_when_ownership_is_pending(self):
        self.assertFalse(suite_parent_cleanup.cleanup(self.parent))
        self.assertTrue((self.parent / 'suite-cleanup.pending').exists())
        (self.parent / 'suite-cleanup.pending').unlink()
        self.assertTrue(suite_parent_cleanup.cleanup(self.parent))

    def test_live_child_fails_closed_after_cancelling_every_staged_child(self):
        live, stopped = self.child('b' * 32), self.child('c' * 32)
        self.registry([live.name, stopped.name])
        ready = multiprocessing.Event()
        release = multiprocessing.Event()
        child = multiprocessing.Process(target=hold_lock, args=(str(live / 'attempt.lock'), ready, release))
        child.start()
        self.assertTrue(ready.wait(3))
        try:
            with patch('suite_parent_cleanup.service_cleanup.cleanup') as services, \
                    patch('suite_parent_cleanup.docker_cleanup.cleanup') as containers, \
                    patch('suite_parent_cleanup.admission.record_cleanup', return_value=True):
                self.assertFalse(suite_parent_cleanup.cleanup(self.parent))
            services.assert_called_once_with(stopped)
            containers.assert_called_once_with(stopped)
            self.assertTrue((live / 'cancel.request').exists())
            self.assertTrue((stopped / 'cancel.request').exists())
            self.assertTrue((self.parent / 'suite-cleanup.pending').exists())
        finally:
            release.set()
            child.join(3)

    def test_nonlive_children_are_cleaned_and_parent_pending_clears_only_when_all_receipts_verify(self):
        first, second = self.child('b' * 32), self.child('c' * 32)
        self.registry([first.name, second.name])
        self.terminal(first)
        self.terminal(second)
        with patch('suite_parent_cleanup.service_cleanup.cleanup', return_value=True) as services, \
                patch('suite_parent_cleanup.docker_cleanup.cleanup', return_value=True) as containers, \
                patch('suite_parent_cleanup.admission.record_cleanup', side_effect=[True, False]) as receipts:
            self.assertFalse(suite_parent_cleanup.cleanup(self.parent))
        self.assertEqual(services.call_args_list, [((first,),), ((second,),)])
        self.assertEqual(containers.call_args_list, [((first,),), ((second,),)])
        self.assertEqual(receipts.call_args_list, [((first, True),), ((second, True),)])
        self.assertTrue((self.parent / 'suite-cleanup.pending').exists())

    def test_staged_child_without_a_terminal_releases_resources_but_keeps_parent_pending(self):
        child = self.child('b' * 32)
        self.registry([child.name])
        with patch('suite_parent_cleanup.service_cleanup.cleanup', return_value=True) as services, \
                patch('suite_parent_cleanup.docker_cleanup.cleanup', return_value=True) as containers, \
                patch('suite_parent_cleanup.admission.record_cleanup', return_value=True) as receipt:
            self.assertFalse(suite_parent_cleanup.cleanup(self.parent))
        services.assert_called_once_with(child)
        containers.assert_called_once_with(child)
        receipt.assert_called_once_with(child, True)
        self.assertTrue((child / 'cancel.request').exists())
        self.assertTrue((self.parent / 'suite-cleanup.pending').exists())

    def test_invalid_child_terminal_keeps_parent_pending(self):
        child = self.child('b' * 32)
        self.registry([child.name])
        self.terminal(child, identity='c' * 32)
        with patch('suite_parent_cleanup.service_cleanup.cleanup', return_value=True), \
                patch('suite_parent_cleanup.docker_cleanup.cleanup', return_value=True), \
                patch('suite_parent_cleanup.admission.record_cleanup', return_value=True):
            self.assertFalse(suite_parent_cleanup.cleanup(self.parent))
        self.assertTrue((self.parent / 'suite-cleanup.pending').exists())

    def test_nonexistent_reserved_child_is_safe_and_never_mints_a_terminal(self):
        missing = 'b' * 32
        self.registry([missing])
        with patch('suite_parent_cleanup.service_cleanup.cleanup') as services, \
                patch('suite_parent_cleanup.docker_cleanup.cleanup') as containers, \
                patch('suite_parent_cleanup.admission.record_cleanup') as receipts:
            self.assertTrue(suite_parent_cleanup.cleanup(self.parent))
        services.assert_not_called()
        containers.assert_not_called()
        receipts.assert_not_called()
        self.assertFalse((self.root / 'runs' / missing / 'terminal.json').exists())
        self.assertFalse((self.parent / 'suite-cleanup.pending').exists())

    def test_verified_terminal_and_cleanup_receipt_clear_parent_pending(self):
        child = self.child('b' * 32)
        self.registry([child.name])
        self.terminal(child)
        with patch('suite_parent_cleanup.service_cleanup.cleanup', return_value=True), \
                patch('suite_parent_cleanup.docker_cleanup.cleanup', return_value=True), \
                patch('suite_parent_cleanup.admission.record_cleanup', return_value=True):
            self.assertTrue(suite_parent_cleanup.cleanup(self.parent))
        self.assertFalse((self.parent / 'suite-cleanup.pending').exists())

    def test_nested_parent_is_refused(self):
        child = self.child('b' * 32)
        (child / 'children.json').write_text('{}')
        self.registry([child.name])
        self.assertFalse(suite_parent_cleanup.cleanup(self.parent))
        self.assertTrue((child / 'cancel.request').exists())
        self.assertTrue((self.parent / 'suite-cleanup.pending').exists())


if __name__ == '__main__':
    unittest.main()
