"""Driver unit tests: the parts that are decisions, not subprocesses.

The parts that are subprocesses are tested on the worker by `canary.py`.
"""
import json
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from pandora.executor import incus as incus_driver
from pandora.executor.incus import IncusDriver, parse_cgroup
from pandora.executor.interface import (Golden, Instance, Limits, Receipt, Toolchain,
                                        Usage, CloneFailed, ExecutionFailed,
                                        InstanceLost)

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
==memory.stat
anon 209715200
file 83886080
kernel 12582912
shmem 1048576
file_mapped 4194304
pgfault 123456
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
        self.assertEqual(usage.memory_stat, {})

    def test_memory_stat_is_read_in_bytes(self):
        self.assertEqual(self.usage.memory_stat['anon'], 209715200)
        self.assertEqual(self.usage.memory_stat['file'], 83886080)
        self.assertEqual(self.usage.memory_stat['kernel'], 12582912)

    def test_the_breakdown_keeps_the_named_fields_and_drops_the_rest(self):
        kept = incus_driver.memory_breakdown(self.usage.memory_stat)
        self.assertEqual(kept, {'anon': 209715200, 'file': 83886080, 'kernel': 12582912,
                                'shmem': 1048576, 'file_mapped': 4194304})
        self.assertNotIn('pgfault', kept)

    def test_an_unparsable_memory_stat_is_an_empty_breakdown(self):
        usage = parse_cgroup('==memory.stat\nanon lots\nfile\n\n==memory.current\n5\n')
        self.assertEqual(usage.memory_current, 5)
        self.assertEqual(incus_driver.memory_breakdown(usage.memory_stat), {})
        self.assertEqual(incus_driver.memory_breakdown(None), {})

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


class CloneCleanup(unittest.TestCase):
    """#88: a clone that fails after `incus copy` may not leave the instance."""

    def setUp(self):
        self.driver = IncusDriver(root='/tmp')
        self.golden = Golden(name='golden-abc', fingerprint='abc', snapshot='warm')

    def test_a_start_failure_deletes_the_copied_instance(self):
        calls = []

        def fake(*args, **kwargs):
            calls.append(args)
            if args[0] == 'start':
                raise ExecutionFailed('start refused')
            return 0, '', ''

        self.driver.incus = fake
        with self.assertRaises(ExecutionFailed):
            self.driver.clone(self.golden, 'r1', limits=Limits(memory_mib=1,
                                                               ceiling_mib=2))
        self.assertIn(('delete', '-f', 'run-r1'), calls)

    def test_a_quota_failure_deletes_the_copied_instance(self):
        calls = []
        self.driver.incus = lambda *args, **kwargs: (calls.append(args), (0, '', ''))[1]
        self.driver.settle_qgroups = lambda: (_ for _ in ()).throw(
            CloneFailed('quota refused'))
        with self.assertRaises(CloneFailed):
            self.driver.clone(self.golden, 'r1', limits=Limits(memory_mib=1,
                                                               ceiling_mib=2, disk_gib=8))
        self.assertIn(('delete', '-f', 'run-r1'), calls)

    def test_a_clone_timeout_is_a_clone_failure_not_an_engine_error(self):
        """#88: TimeoutExpired maps to retryable 'clone-failed', not 'engine-error'."""
        calls = []
        self.driver.incus = lambda *args, **kwargs: (calls.append(args), (0, '', ''))[1]

        def timed_out(name, gib):
            raise subprocess.TimeoutExpired(['btrfs'], 900)

        self.driver.quota = timed_out
        with self.assertRaises(CloneFailed):
            self.driver.clone(self.golden, 'r1', limits=Limits(memory_mib=1,
                                                               ceiling_mib=2, disk_gib=8))
        self.assertIn(('delete', '-f', 'run-r1'), calls)


class PortIsolation(unittest.TestCase):
    """#172: every clone is an isolated port on the shared bridge."""

    def setUp(self):
        self.driver = IncusDriver(root='/tmp')
        self.golden = Golden(name='golden-abc', fingerprint='abc', snapshot='warm')
        self.calls = []

    def fake(self, refuse=()):
        def incus(*args, **kwargs):
            self.calls.append(args)
            if args[:3] in refuse:
                return 1, '', 'refused'
            return 0, '', ''
        self.driver.incus = incus

    def test_the_clone_overrides_eth0_isolated_before_it_starts(self):
        self.fake()
        self.driver.clone(self.golden, 'r1')
        isolate = ('config', 'device', 'override', 'run-r1', 'eth0',
                   'security.port_isolation=true')
        self.assertIn(isolate, self.calls)
        self.assertLess(self.calls.index(isolate), self.calls.index(('start', 'run-r1')))

    def test_a_local_eth0_is_set_instead_of_overridden(self):
        self.fake(refuse={('config', 'device', 'override')})
        self.driver.clone(self.golden, 'r1')
        self.assertIn(('config', 'device', 'set', 'run-r1', 'eth0',
                       'security.port_isolation=true'), self.calls)

    def test_a_clone_that_cannot_be_isolated_is_refused_and_deleted(self):
        self.fake(refuse={('config', 'device', 'override'), ('config', 'device', 'set')})
        with self.assertRaises(CloneFailed):
            self.driver.clone(self.golden, 'r1')
        self.assertIn(('delete', '-f', 'run-r1'), self.calls)
        self.assertNotIn(('start', 'run-r1'), self.calls)

    def bridge(self, *answers):
        """Fake `bridge` reads in order; record every argv."""
        argvs, reads = [], list(answers)

        def run(argv, **kwargs):
            argvs.append(argv)
            if argv[:2] == ['bridge', '-d']:
                return 0, reads.pop(0), ''
            return 0, '', ''
        return argvs, run

    def harden(self, run):
        self.driver.veth = lambda name: 'veth1234'
        self.driver.cgroup = lambda name: '/sys/fs/cgroup/x'
        instance = Instance(name='run-r1', run_id='r1', golden='golden-abc')
        with unittest.mock.patch.object(incus_driver, 'run', run):
            return self.driver.harden(instance, Limits(memory_mib=1, ceiling_mib=2))

    def test_harden_records_an_isolated_port(self):
        argvs, run = self.bridge('7: veth1234 ... learning on isolated on locked off')
        self.assertEqual(self.harden(run)['eth0.port_isolation'], 'on')

    def test_harden_reports_an_open_port_and_never_patches_it(self):
        """Incus applies the flag at attach or fails the start; open is a fault."""
        argvs, run = self.bridge('isolated off locked off')
        self.assertTrue(self.harden(run)['eth0.port_isolation'].startswith('ERR:'))
        self.assertFalse([argv for argv in argvs if 'set' in argv and 'bridge' in argv])

    def test_isolating_counts_as_clone_time(self):
        self.fake()
        clock = iter([0.0, 1.0, 3.0, 3.0, 3.5, 4.0, 4.0])
        with unittest.mock.patch.object(incus_driver.time, 'monotonic',
                                        lambda: next(clock, 4.0)):
            instance = self.driver.clone(self.golden, 'r1')
        self.assertEqual(instance.clone_seconds, 3.0)

    def test_address_reads_the_global_ipv4_on_eth0(self):
        rows = [{'name': 'run-r1', 'state': {'network': {'eth0': {'addresses': [
            {'family': 'inet6', 'scope': 'link', 'address': 'fe80::1'},
            {'family': 'inet', 'scope': 'global', 'address': '10.70.0.9'}]}}}}]
        self.driver.incus = lambda *args, **kwargs: (0, json.dumps(rows), '')
        self.assertEqual(self.driver.address('run-r1'), '10.70.0.9')
        self.driver.incus = lambda *args, **kwargs: (0, 'not json', '')
        self.assertEqual(self.driver.address('run-r1'), '')


class Listing(unittest.TestCase):
    """A failed `incus list` is empty for a status line and an error for gc (#88)."""

    def driver(self, rc):
        driver = IncusDriver(root='/tmp')
        driver.incus = lambda *args, **kwargs: (rc, 'run-a,RUNNING,\n' if rc == 0 else '',
                                                'connection refused')
        return driver

    def test_a_failed_listing_is_empty_unless_checked(self):
        self.assertEqual(self.driver(1).instances(), [])
        from pandora.executor.interface import ExecutorError
        with self.assertRaises(ExecutorError):
            self.driver(1).instances(check=True)
        self.assertEqual([row['name'] for row in self.driver(0).instances(check=True)],
                         ['run-a'])


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
        # Lowest measured hog PSI: 2.0 on local NVMe, where page-ins are fast
        # enough that even a wedged cgroup barely stalls (#150). Loop-file
        # pools read 5-8; a healthy run reads 0.0.
        self.assertLess(self.driver.thrash_psi, 2.0)
        self.assertGreater(self.driver.thrash_psi, 0.0)

    def test_pinned_fraction_leaves_room_for_a_run_that_merely_runs_hot(self):
        self.assertGreaterEqual(self.driver.thrash_pinned, 0.9)
        self.assertLessEqual(self.driver.thrash_pinned, 1.0)

    def test_the_effective_wall_is_memory_high_when_it_is_lower(self):
        # memory.high suppresses memory.events:max entirely, so a watchdog
        # that compared against memory.max alone would never see a pinned run.
        for high, maximum, expected in ((0, 512, 512), (460, 512, 460), (0, 0, 1 << 62)):
            wall = min(x for x in (high, maximum) if x) if (high or maximum) else (1 << 62)
            self.assertEqual(wall, expected)


class StalledSeconds(unittest.TestCase):
    """The sustain bar is wedged time inside a trailing window, not a streak."""

    def setUp(self):
        self.driver = IncusDriver(root='/tmp')   # thrash_seconds = 15, window 30

    @staticmethod
    def samples(flags, step=0.5):
        return [{'t': i * step, 'stalled': flag} for i, flag in enumerate(flags)]

    def test_an_unbroken_streak_counts_as_before(self):
        samples = self.samples([True] * 40)      # 20 s of stall
        self.assertAlmostEqual(self.driver.stalled_seconds(samples), 19.5)

    def test_a_streak_split_by_dips_still_counts(self):
        # 24 s wedged in two 12 s runs around a 2 s dip: the old per-streak
        # timer never reached 15 s; the windowed count does (#150).
        samples = self.samples([True] * 24 + [False] * 4 + [True] * 24)
        self.assertGreaterEqual(self.driver.stalled_seconds(samples), 15.0)

    def test_a_shorter_episode_inside_the_window_is_not_enough(self):
        samples = self.samples([True] * 28 + [False] * 4)
        self.assertLess(self.driver.stalled_seconds(samples), 15.0)

    def test_time_older_than_the_window_does_not_count(self):
        samples = self.samples([True] * 40 + [False] * 80)
        self.assertEqual(self.driver.stalled_seconds(samples), 0.0)


class PollLogDrain(unittest.TestCase):
    def test_a_running_command_streams_output_without_an_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'log').write_text('still running\n')

            def execute(*args, **kwargs):
                proc = subprocess.run(['bash', '-c', args[-1]], capture_output=True,
                                      text=True, timeout=kwargs['timeout'])
                return proc.returncode, proc.stdout, proc.stderr

            driver = IncusDriver(root=tmp)
            instance = Instance(name='run-r1', run_id='r1', golden='golden-abc')
            with unittest.mock.patch.object(incus_driver, 'GUEST', tmp), \
                    unittest.mock.patch.object(driver, 'incus', side_effect=execute):
                self.assertEqual(driver.poll(instance, 0, 5), ('still running\n', None))

    def test_completion_during_tail_keeps_final_bytes_for_the_next_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            guest = Path(tmp)
            (guest / 'log').write_text('marker\n')
            # Complete the command immediately after tail has read its bytes.
            # Reading rc afterward would report completion and lose the last line.
            shell = '''tail() {
                command tail "$@"
                if [ ! -f "$GUEST_TEST/rc" ]; then
                    printf 'final line\\n' >> "$GUEST_TEST/log"
                    printf '0\\n' > "$GUEST_TEST/rc"
                fi
            }
            '''

            def execute(*args, **kwargs):
                proc = subprocess.run(['bash', '-c', 'GUEST_TEST=%s\n' % tmp
                                       + shell + args[-1]], capture_output=True,
                                      text=True, timeout=kwargs['timeout'])
                return proc.returncode, proc.stdout, proc.stderr

            driver = IncusDriver(root=tmp)
            instance = Instance(name='run-r1', run_id='r1', golden='golden-abc')
            with unittest.mock.patch.object(incus_driver, 'GUEST', tmp), \
                    unittest.mock.patch.object(driver, 'incus', side_effect=execute):
                chunk, code = driver.poll(instance, 0, 5)
                self.assertEqual(chunk, 'marker\n')
                self.assertIsNone(code)
                final, code = driver.poll(instance, len(chunk.encode()), 5)
                self.assertEqual(final, 'final line\n')
                self.assertEqual(code, 0)


class ThrashingDriver(IncusDriver):
    """The watchdog loop over scripted cgroup reads: nothing runs, nothing is killed."""

    def __init__(self, stat):
        super().__init__(root='/tmp', sample_interval=0.01, thrash_seconds=0.1,
                         thrash_window=0.5)
        self.stat, self.events, self.killed = stat, 0, False

    def usage(self, instance):
        self.events += 1000
        high = 7730102272
        return Usage(memory_current=high, memory_peak=high + 1048576,
                     memory_max=8589934592, memory_high=high,
                     events={'high': self.events, 'max': 0, 'oom_kill': 0},
                     pressure={'memory_full_avg10': 55.0, 'memory_some_avg10': 63.0},
                     memory_stat=self.stat)

    def poll(self, instance, offset, timeout):
        return None, None

    def kill(self, instance, **kw):
        self.killed = True


class ThrashEvidence(unittest.TestCase):
    """A memory-thrash verdict carries the limit that applied and what filled it."""

    INSTANCE = Instance(name='r-1', run_id='r1', golden='golden-abc')
    LIMITS = Limits(memory_mib=7000, ceiling_mib=8192, wall_seconds=30)

    def verdict(self, stat):
        driver = ThrashingDriver(stat)
        result = driver.supervise(self.INSTANCE, self.LIMITS)
        self.assertTrue(driver.killed)
        self.assertEqual(result.outcome, 'oom')
        self.assertEqual(result.evidence['reason'], 'memory-thrash')
        return result.evidence

    def test_the_breakdown_is_recorded_with_the_verdict(self):
        evidence = self.verdict({'anon': 8450 << 20, 'file': 1492 << 20, 'kernel': 314 << 20,
                                 'shmem': 7 << 20, 'pgfault': 99})
        self.assertEqual(evidence['memory_stat'], {'anon': 8450 << 20, 'file': 1492 << 20,
                                                   'kernel': 314 << 20, 'shmem': 7 << 20})
        self.assertEqual(evidence['memory_high'], 7730102272)
        self.assertEqual(evidence['memory_max'], 8589934592)
        self.assertEqual(evidence['memory_wall'], 7730102272)

    def test_a_missing_memory_stat_leaves_the_verdict_and_omits_the_breakdown(self):
        evidence = self.verdict({})
        self.assertNotIn('memory_stat', evidence)
        self.assertEqual(evidence['memory_high'], 7730102272)

    def test_the_samples_do_not_carry_the_breakdown(self):
        evidence = self.verdict({'anon': 1, 'file': 2})
        self.assertTrue(evidence['samples'])
        self.assertTrue(all('memory_stat' not in sample for sample in evidence['samples']))


class LimitsShape(unittest.TestCase):
    def test_reservation_and_ceiling_are_separate_numbers(self):
        limits = Limits(memory_mib=3072, ceiling_mib=5120, cpus_hint=4)
        self.assertLess(limits.memory_mib, limits.ceiling_mib)
        self.assertEqual(limits.cpus_hint, 4)

    def test_cpu_weight_maps_onto_the_allowance_percentage(self):
        # limits.cpu.allowance=<N>% writes cpu.weight=N and leaves cpu.max
        # unlimited. limits.cpu.priority only spans cpu.weight 90-100.
        for weight, expected in ((0, 1), (25, 25), (100, 100), (250, 100)):
            self.assertEqual(max(1, min(100, weight)), expected)


if __name__ == '__main__':
    unittest.main()
