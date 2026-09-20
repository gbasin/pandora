import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import worker_config


def config(**changes):
    scheduler = {
        'version': 1, 'cpu_millis': 3500, 'memory_mib': 7296,
        'max_running': 2, 'policy': 'fair', 'disk_mib': 2000,
        'disk_floor_mib': 200,
    }
    value = {
        'version': 1, 'scheduler': scheduler, 'max_parallel': 2,
        'execution_seconds': 3600, 'workspace_mib': 1000,
        'limits': worker_config.LEGACY,
    }
    return value | changes


class WorkerConfigTests(unittest.TestCase):
    def test_cold_build_reserves_the_larger_of_journey_and_builder_and_is_exclusive(self):
        submitted = {'worker_config': config(), 'workflow': 'journey'}

        self.assertEqual(worker_config.demand(submitted, cold=True), {
            'cpu_millis': 3500, 'memory_mib': 7296, 'disk_mib': 1000,
            'exclusive': ['dependency-builder'],
        })

    def test_journey_demand_sums_all_long_lived_services(self):
        submitted = {'worker_config': config(), 'workflow': 'journey'}

        self.assertEqual(worker_config.demand(submitted), {
            'cpu_millis': 3500, 'memory_mib': 7296, 'disk_mib': 1000,
            'exclusive': [],
        })

    def test_docker_build_uses_builder_reservation_and_exclusive_gate(self):
        submitted = {
            'worker_config': config(), 'workflow': 'docker',
            'docker': {'request': {'kind': 'build'}},
        }

        self.assertEqual(worker_config.demand(submitted, cold=True), {
            'cpu_millis': 2000, 'memory_mib': 6144, 'disk_mib': 1000,
            'exclusive': ['dependency-builder', 'docker-builder'],
        })

    def test_validate_rejects_capacity_that_cannot_run_a_journey_or_builder(self):
        with self.assertRaisesRegex(ValueError, 'cannot fit'):
            worker_config.validate(config(scheduler=config()['scheduler'] | {'memory_mib': 7295}))
        with self.assertRaisesRegex(ValueError, 'disk reservations'):
            worker_config.validate(config(scheduler={
                key: value for key, value in config()['scheduler'].items()
                if key != 'disk_floor_mib'
            }))

    def test_load_rejects_cpu_and_memory_that_leave_no_hardware_headroom(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'worker-config.json').write_text(json.dumps(config()))
            with patch.object(worker_config.os, 'cpu_count', return_value=3):
                with self.assertRaisesRegex(ValueError, 'CPU exceeds'):
                    worker_config.load(root)

            memory = config(scheduler=config()['scheduler'] | {'memory_mib': 7296})
            (root / 'worker-config.json').write_text(json.dumps(memory))

            class MeminfoPath:
                def exists(self):
                    return True

                def read_text(self):
                    return 'MemTotal:        8000000 kB\\n'

            with patch.object(worker_config, 'Path', return_value=MeminfoPath()):
                with self.assertRaisesRegex(ValueError, '1 GiB RAM'):
                    worker_config.load(root)


if __name__ == '__main__':
    unittest.main()
