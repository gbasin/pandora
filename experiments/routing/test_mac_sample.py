import importlib.util
from pathlib import Path
import unittest
from unittest import mock


SOURCE = Path(__file__).with_name('mac-sample.py')
SPEC = importlib.util.spec_from_file_location('mac_sample', SOURCE)
mac_sample = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mac_sample)


class MacSampleTests(unittest.TestCase):
    def test_memory_free_percent_parses_query_output(self):
        output = 'The system has 10 pages.\nSystem-wide memory free percentage: 64%\n'
        self.assertEqual(mac_sample.memory_free_percent(output), 64)
        self.assertIsNone(mac_sample.memory_free_percent('unavailable'))

    def test_vm_stat_pages_uses_only_current_page_counters(self):
        output = '''Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               10,022.
Pages active:                            195916.
Pageins:                                  437085466.
Swapins:                                   62894987.
Swapouts:                                  69058251.
Compressions:                            4138671402.
Decompressions:                          3653927704.
'''
        self.assertEqual(mac_sample.vm_stat_pages(output), {
            'free': 10022,
            'active': 195916,
        })
        self.assertEqual(mac_sample.vm_stat_page_size_bytes(output), 16384)
        self.assertEqual(mac_sample.vm_stat_counters(output), {
            'swapins': 62894987,
            'swapouts': 69058251,
            'compressions': 4138671402,
            'decompressions': 3653927704,
        })

    def test_missing_read_only_tool_becomes_sample_error(self):
        with mock.patch.object(mac_sample.subprocess, 'run', side_effect=FileNotFoundError('missing')):
            result = mac_sample.command_sample(['memory_pressure', '-Q'])
        self.assertEqual(result['error'], 'FileNotFoundError')
        self.assertIn('missing', result['message'])


if __name__ == '__main__':
    unittest.main()
