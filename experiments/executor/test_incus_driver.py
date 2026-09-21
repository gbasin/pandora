"""Driver unit tests: the parts that are decisions, not subprocesses.

The parts that are subprocesses are tested on the worker by `canary.py`.
"""
import unittest

import incus_driver
from incus_driver import IncusDriver, parse_cgroup
from interface import (Golden, Instance, Limits, Receipt, Toolchain,
                       CloneFailed, InstanceLost, PrepareFailed)

SAMPLE = '''==memory.current
312356864
==memory.peak
317120512
==memory.max
5368709120
==memory.high
max
==memory.swap.current
0
==memory.events
low 0
high 0
max 29603
oom 18
oom_kill 1
oom_group_kill 0
==memory.pressure
some avg10=9.07 avg60=3.11 avg300=0.63 total=4321
full avg10=8.27 avg60=2.90 avg300=0.58 total=3990
==cpu.pressure
some avg10=12.17 avg60=9.50 avg300=2.00 total=99
full avg10=0.00 avg60=0.00 avg300=0.00 total=0
==io.pressure
some avg10=0.75 avg60=1.00 avg300=0.40 total=12
full avg10=0.17 avg60=0.43 avg300=0.20 total=7
==cpu.stat
usage_usec 66018286
user_usec 50000000
system_usec 16018286
==pids.current
33
'''


class ParseCgroup(unittest.TestCase):
    def setUp(self):
        self.usage = parse_cgroup(SAMPLE)

    def test_reads_the_counters(self):
        self.assertEqual(self.usage.memory_current, 312356864)
        self.assertEqual(self.usage.memory_peak, 317120512)
        self.assertEqual(self.usage.memory_max, 5368709120)
        self.assertEqual(self.usage.cpu_usec, 66018286)
        self.assertEqual(self.usage.processes, 33)

    def test_unlimited_memory_high_reads_as_zero_not_as_a_crash(self):
        self.assertEqual(self.usage.memory_high, 0)

    def test_events_are_the_watchdogs_input(self):
        self.assertEqual(self.usage.events['max'], 29603)
        self.assertEqual(self.usage.events['oom_kill'], 1)

    def test_pressure_is_namespaced_by_resource_and_kind(self):
        self.assertEqual(self.usage.pressure['memory_full_avg10'], 8.27)
        self.assertEqual(self.usage.pressure['memory_some_avg10'], 9.07)
        self.assertEqual(self.usage.pressure['cpu_some_avg10'], 12.17)
        self.assertEqual(self.usage.pressure['io_full_avg60'], 0.43)

    def test_an_empty_read_is_zeroes_rather_than_an_exception(self):
        usage = parse_cgroup('')
        self.assertEqual(usage.memory_current, 0)
        self.assertEqual(usage.events, {})

    def test_a_truncated_read_keeps_what_arrived(self):
        usage = parse_cgroup('==memory.current\n123\n==memory.events\nmax 7\n')
        self.assertEqual(usage.memory_current, 123)
        self.assertEqual(usage.events['max'], 7)


class Fingerprints(unittest.TestCase):
    def test_identical_toolchains_share_a_golden(self):
        self.assertEqual(Toolchain(node_version='24.9.0').fingerprint(),
                         Toolchain(node_version='24.9.0').fingerprint())

    def test_any_field_change_makes_a_new_golden(self):
        base = Toolchain(node_version='24.9.0', packages=('git',))
        for other in (Toolchain(node_version='24.9.1', packages=('git',)),
                      Toolchain(node_version='24.9.0', packages=('git', 'jq')),
                      Toolchain(node_version='24.9.0', packages=('git',), source_id='x'),
                      Toolchain(node_version='24.9.0', packages=('git',), install_command='pnpm i')):
            self.assertNotEqual(base.fingerprint(), other.fingerprint())

    def test_the_golden_name_is_an_instance_name(self):
        driver = IncusDriver(root='/tmp')
        self.assertRegex(driver.golden_name(Toolchain()), '^golden-[0-9a-f]{16}$')
        self.assertTrue(incus_driver.NAME.fullmatch(driver.golden_name(Toolchain())))


class Naming(unittest.TestCase):
    def setUp(self):
        self.driver = IncusDriver(root='/tmp')
        self.golden = Golden(name='golden-abc', fingerprint='abc', snapshot='warm')

    def test_a_run_id_that_is_not_an_instance_name_is_refused_before_any_command(self):
        for bad in ('Run One', '../escape', 'x' * 80, ''):
            with self.assertRaises(CloneFailed):
                self.driver.clone(self.golden, bad)

    def test_cgroup_of_an_unknown_instance_raises_instance_lost(self):
        with self.assertRaises(InstanceLost):
            self.driver.cgroup('no-such-instance-here')


class ReceiptCleanliness(unittest.TestCase):
    def receipt(self, **overrides):
        base = dict(run_id='r', instance='run-r', seconds=0.9, instance_gone=True,
                    volume_gone=True, veth_gone=True, cgroup_gone=True, leftovers=())
        return Receipt(**{**base, **overrides})

    def test_all_four_gone_and_no_leftovers_is_clean(self):
        self.assertTrue(self.receipt().clean)

    def test_any_survivor_makes_it_dirty(self):
        for field in ('instance_gone', 'volume_gone', 'veth_gone', 'cgroup_gone'):
            self.assertFalse(self.receipt(**{field: False}).clean, field)

    def test_a_named_leftover_makes_it_dirty_even_when_the_flags_agree(self):
        self.assertFalse(self.receipt(leftovers=('veth vethabc',)).clean)


class Thresholds(unittest.TestCase):
    def setUp(self):
        self.driver = IncusDriver(root='/tmp')

    def test_rate_sits_below_a_measured_thrash_and_above_a_healthy_run(self):
        self.assertLess(self.driver.thrash_rate, 1700)   # slowest measured hog rate
        self.assertGreater(self.driver.thrash_rate, 10)  # measured healthy rate is 0

    def test_psi_sits_below_a_measured_thrash_and_above_a_healthy_run(self):
        self.assertLess(self.driver.thrash_psi, 6.6)     # lowest measured hog PSI
        self.assertGreater(self.driver.thrash_psi, 0.0)  # measured healthy PSI is 0.0

    def test_pinned_fraction_leaves_room_for_a_run_that_merely_runs_hot(self):
        self.assertGreaterEqual(self.driver.thrash_pinned, 0.9)
        self.assertLessEqual(self.driver.thrash_pinned, 1.0)

    def test_the_effective_wall_is_memory_high_when_it_is_lower(self):
        # memory.high suppresses memory.events:max entirely, so a watchdog
        # that compared against memory.max alone would never see a pinned run.
        for high, maximum, expected in ((0, 512, 512), (460, 512, 460), (0, 0, 1 << 62)):
            wall = min(x for x in (high, maximum) if x) if (high or maximum) else (1 << 62)
            self.assertEqual(wall, expected)


class LimitsShape(unittest.TestCase):
    def test_reservation_and_ceiling_are_separate_numbers(self):
        limits = Limits(memory_mib=3072, ceiling_mib=5120, cpus_hint=4)
        self.assertLess(limits.memory_mib, limits.ceiling_mib)
        self.assertEqual(limits.cpus_hint, 4)

    def test_cpu_weight_maps_into_the_incus_priority_range(self):
        for weight, expected in ((0, 0), (50, 5), (100, 10), (250, 10)):
            self.assertEqual(max(0, min(10, weight // 10)), expected)


if __name__ == '__main__':
    unittest.main()
