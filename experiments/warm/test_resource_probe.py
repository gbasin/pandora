import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('resource_probe', Path(__file__).parent.parent / 'scheduler/probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProbeTests(unittest.TestCase):
    def test_failed_creation_with_verified_absence_records_failure_and_releases_lease(self):
        with tempfile.TemporaryDirectory() as temp:
            lease = Mock()
            scheduler = Mock(claim=Mock(return_value=lease))
            with patch.object(probe, 'Scheduler', return_value=scheduler), \
                 patch.object(probe, 'docker', side_effect=subprocess.CalledProcessError(1, ['docker','run'])), \
                 patch.object(probe, 'remove_container') as remove:
                with self.assertRaises(subprocess.CalledProcessError):
                    probe.worker(temp, 'boot', 'b'*32, 'a'*32, 'image', 'probe', Mock(), Mock())
            terminal = json.loads((Path(temp)/'runs'/('a'*32)/'terminal.json').read_text())
            self.assertEqual(terminal['exit_code'], 70)
            self.assertTrue(terminal['cleanup_verified'])
            remove.assert_called_once()
            scheduler.settle.assert_called_once_with('a'*32)
            lease.close.assert_called_once()

    def test_no_such_container_is_verified_instead_of_becoming_unknown_cleanup(self):
        results = [subprocess.CompletedProcess([],1,'','No such container: owned'),
                   subprocess.CompletedProcess([],0,'','')]
        with patch.object(probe.subprocess, 'run', side_effect=results):
            probe.remove_container('owned')

    def test_unknown_cleanup_keeps_missing_terminal_but_always_closes_process_lease(self):
        with tempfile.TemporaryDirectory() as temp:
            lease = Mock()
            scheduler = Mock(claim=Mock(return_value=lease))
            with patch.object(probe, 'Scheduler', return_value=scheduler), \
                 patch.object(probe, 'docker', side_effect=subprocess.CalledProcessError(1,['docker','run'])), \
                 patch.object(probe, 'remove_container', side_effect=RuntimeError('unresolved')):
                with self.assertRaisesRegex(RuntimeError, 'unresolved'):
                    probe.worker(temp,'boot','b'*32,'a'*32,'image','probe',Mock(),Mock())
            self.assertFalse((Path(temp)/'runs'/('a'*32)/'terminal.json').exists())
            scheduler.settle.assert_not_called()
            lease.close.assert_called_once()


if __name__ == '__main__': unittest.main()
