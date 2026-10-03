"""Pinning a run to whole physical cores, from a fake sibling map (#201)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pandora.engine import runner
from pandora.engine.ledger import Ledger
from pandora.executor import cpuset
from pandora.executor.incus import IncusDriver
from pandora.executor.interface import Limits
from pandora.tests.test_engine import PLAN, FakeDriver, claim


def smt_host(cores=16):
    """Linux's usual numbering: CPU n and n + cores share a core."""
    return {cpu: '%d,%d' % (cpu % cores, cpu % cores + cores) for cpu in range(2 * cores)}


def flat_host(cores=8):
    return {cpu: str(cpu) for cpu in range(cores)}


class ListsTest(unittest.TestCase):
    def test_a_kernel_list_parses_to_cpus(self):
        self.assertEqual(cpuset.parse_list('0-3,8,10-11\n'), [0, 1, 2, 3, 8, 10, 11])
        self.assertEqual(cpuset.parse_list(''), [])

    def test_garbage_is_refused(self):
        for text in ('a', '3-1', '1-', '-2'):
            with self.assertRaises(ValueError):
                cpuset.parse_list(text)

    def test_a_list_formats_as_ranges_and_round_trips(self):
        cpus = [0, 1, 2, 3, 16, 17, 18, 19, 21]
        self.assertEqual(cpuset.format_list(cpus), '0-3,16-19,21')
        self.assertEqual(cpuset.parse_list(cpuset.format_list(cpus)), cpus)

    def test_a_lone_cpu_is_never_a_bare_number_incus_would_read_as_a_count(self):
        self.assertEqual(cpuset.format_list([5]), '5-5')
        self.assertEqual(cpuset.format_list([0, 16]), '0,16')


class CoresTest(unittest.TestCase):
    def test_smt_pairs_become_two_thread_cores(self):
        cores = cpuset.cores_of(smt_host(16))
        self.assertEqual(len(cores), 16)
        self.assertEqual(cores[0], (0, 16))
        self.assertEqual(cores[15], (15, 31))

    def test_no_smt_is_one_thread_per_core(self):
        self.assertEqual(cpuset.cores_of(flat_host(4)), ((0,), (1,), (2,), (3,)))

    def test_offline_cpus_are_left_out(self):
        cores = cpuset.cores_of(smt_host(2), online=[0, 1, 2])
        self.assertEqual(cores, ((0, 2), (1,)))

    def test_a_map_that_disagrees_with_itself_is_refused(self):
        with self.assertRaises(ValueError):
            cpuset.cores_of({0: '0,1', 1: '1,2', 2: '1,2'})
        with self.assertRaises(ValueError):
            cpuset.cores_of({0: '0,1'})


class AllocateTest(unittest.TestCase):
    def test_eight_threads_on_smt_are_four_whole_cores(self):
        cores = cpuset.cores_of(smt_host(16))
        chosen = cpuset.allocate(cores, 8)
        self.assertEqual(chosen, [0, 1, 2, 3, 16, 17, 18, 19])
        self.assertEqual(cpuset.physical_cores(cores, chosen), 4)

    def test_without_smt_threads_and_cores_are_one_number(self):
        cores = cpuset.cores_of(flat_host(8))
        chosen = cpuset.allocate(cores, 2)
        self.assertEqual(chosen, [0, 1])
        self.assertEqual(cpuset.physical_cores(cores, chosen), 2)

    def test_runs_spread_over_disjoint_cores_until_the_host_is_full(self):
        cores = cpuset.cores_of(smt_host(16))
        held = []
        for _ in range(4):
            held.append(cpuset.allocate(cores, 8, held))
        everything = [cpu for chosen in held for cpu in chosen]
        self.assertEqual(sorted(everything), list(range(32)))
        for chosen in held:
            self.assertEqual(cpuset.physical_cores(cores, chosen), 4)

    def test_more_runs_than_cores_share_the_least_used_whole_cores(self):
        cores = cpuset.cores_of(smt_host(4))
        held = [cpuset.allocate(cores, 4)]                 # cores 0, 1
        held.append(cpuset.allocate(cores, 4, held))       # cores 2, 3
        held.append(cpuset.allocate(cores, 4, held))       # all loaded once: 0, 1 again
        self.assertEqual(held[1], [2, 3, 6, 7])
        self.assertEqual(held[2], [0, 1, 4, 5])
        held.append(cpuset.allocate(cores, 4, held))       # now 2, 3 are least used
        self.assertEqual(held[3], [2, 3, 6, 7])
        for chosen in held:
            self.assertEqual(cpuset.physical_cores(cores, chosen), 2)

    def test_a_finished_run_frees_its_cores_for_the_next(self):
        cores = cpuset.cores_of(smt_host(4))
        first = cpuset.allocate(cores, 4)
        second = cpuset.allocate(cores, 4, [first])
        self.assertEqual(cpuset.allocate(cores, 4, [second]), first)

    def test_an_odd_width_on_smt_rounds_up_to_whole_cores(self):
        cores = cpuset.cores_of(smt_host(16))
        chosen = cpuset.allocate(cores, 7)
        self.assertEqual(chosen, [0, 1, 2, 3, 16, 17, 18, 19])
        self.assertEqual(cpuset.physical_cores(cores, chosen), 4)

    def test_a_width_below_one_core_is_one_whole_core(self):
        cores = cpuset.cores_of(smt_host(4))
        self.assertEqual(cpuset.allocate(cores, 1), [0, 4])
        self.assertEqual(cpuset.physical_cores(cores, [0, 4]), 1)

    def test_only_whole_cores_count_as_physical_cores(self):
        cores = cpuset.cores_of(smt_host(16))
        self.assertEqual(cpuset.physical_cores(cores, [0, 1, 2, 3, 16, 17, 18]), 3)

    def test_the_width_is_clamped_to_the_host(self):
        cores = cpuset.cores_of(smt_host(2))
        self.assertEqual(cpuset.allocate(cores, 99), [0, 1, 2, 3])
        self.assertEqual(cpuset.allocate(cores, 0), [0, 2])

    def test_mixed_core_sizes_fill_with_whole_cores(self):
        # Two two-thread cores and two single-thread cores.
        cores = cpuset.cores_of({0: '0,1', 1: '0,1', 2: '2,3', 3: '2,3', 4: '4', 5: '5'})
        chosen = cpuset.allocate(cores, 5)
        self.assertEqual(chosen, [0, 1, 2, 3, 4])
        self.assertEqual(cpuset.physical_cores(cores, chosen), 3)


class RebalanceTest(unittest.TestCase):
    def test_two_runs_left_sharing_cores_are_spread_onto_idle_ones(self):
        cores = cpuset.cores_of(smt_host(16))
        held = []
        for _ in range(5):
            held.append(cpuset.allocate(cores, 8, held))
        self.assertEqual(held[0], held[4])            # runs 1 and 5 share cores 0-3
        moves = cpuset.rebalance(cores, [('r1', held[0]), ('r5', held[4])])
        self.assertEqual(list(moves), ['r5'])         # the newest moves
        self.assertEqual(moves['r5'], [4, 5, 6, 7, 20, 21, 22, 23])
        self.assertEqual(cpuset.physical_cores(cores, moves['r5']), 4)

    def test_runs_that_share_nothing_stay(self):
        cores = cpuset.cores_of(smt_host(16))
        first = cpuset.allocate(cores, 8)
        second = cpuset.allocate(cores, 8, [first])
        self.assertEqual(cpuset.rebalance(cores, [('a', first), ('b', second)]), {})

    def test_a_run_moves_only_when_its_whole_pin_fits_on_idle_cores(self):
        cores = cpuset.cores_of(smt_host(4))
        # a and b share cores 0-1, c holds 2, nothing idle is big enough.
        placed = [('a', [0, 1, 4, 5]), ('b', [0, 1, 4, 5]), ('c', [2, 6])]
        self.assertEqual(cpuset.rebalance(cores, placed), {})

    def test_when_the_newest_cannot_move_the_older_run_sharing_its_cores_does(self):
        cores = cpuset.cores_of(smt_host(16))
        same = [0, 1, 2, 3, 16, 17, 18, 19]
        tried = []

        def apply(key, cpus):
            tried.append(key)
            return key != 'a'                          # a is not cloned, or its repin failed

        moves = cpuset.rebalance(cores, [('b', same), ('a', same)], apply=apply)
        self.assertEqual(tried, ['a', 'b'])
        self.assertEqual(list(moves), ['b'])
        self.assertFalse(set(moves['b']) & set(same))

    def test_moves_never_land_two_runs_on_the_same_idle_cores(self):
        cores = cpuset.cores_of(smt_host(8))
        same = [0, 1, 8, 9]
        moves = cpuset.rebalance(cores, [('a', same), ('b', same), ('c', same)])
        self.assertEqual(sorted(moves), ['b', 'c'])
        self.assertFalse(set(moves['b']) & set(moves['c']))
        self.assertFalse(set(moves['b']) & set(same))


class HostTopologyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, online, siblings, name='thread_siblings_list'):
        (self.root / 'online').write_text(online + '\n')
        for cpu, text in siblings.items():
            topology = self.root / ('cpu%d' % cpu) / 'topology'
            topology.mkdir(parents=True)
            (topology / name).write_text(text + '\n')

    def test_sysfs_reads_as_cores(self):
        self.write('0-3', smt_host(2))
        self.assertEqual(cpuset.host_topology(self.root), ((0, 2), (1, 3)))

    def test_the_newer_file_name_is_read_too(self):
        self.write('0-1', flat_host(2), name='core_cpus_list')
        self.assertEqual(cpuset.host_topology(self.root), ((0,), (1,)))

    def test_anything_unreadable_is_none_so_the_count_pin_stays(self):
        self.assertIsNone(cpuset.host_topology(self.root))
        self.write('0-3', {0: '0,2', 1: '1,3', 2: '0,2'})
        self.assertIsNone(cpuset.host_topology(self.root))


class TopologyDriver(FakeDriver):
    def __init__(self, cores, repin_fails=False, **kwargs):
        super().__init__(**kwargs)
        self.cores, self.repin_fails, self.repinned = cores, repin_fails, []

    def repin(self, name, cpus):
        if self.repin_fails is True or self.repin_fails == name:
            raise RuntimeError('incus said no')
        self.repinned.append((name, cpus))

    def topology(self):
        return self.cores

    def clone(self, golden, run_id, limits=None):
        self.cloned_with = limits
        return super().clone(golden, run_id, limits)


class SupervisedPinTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.paths = runner.Paths(self.root).ensure()
        self.ledger = Ledger(self.paths.ledger)
        for index in (1, 2):
            run_id = 'r%d' % index
            claim(self.ledger, request_id='req-%d' % index, run_id=run_id)
            self.paths.attempt(run_id).mkdir(parents=True, exist_ok=True)
            (self.paths.attempt(run_id) / 'toolchain.json').write_text(
                json.dumps(PLAN['worker']))
            self.ledger.update(run_id, state='admitted', reservation_mib=2048,
                               ceiling_mib=4096, cpus_hint=8)
        Path(self.paths.root, 'cpus_per_run').write_text('8\n')
        patcher = mock.patch.dict(os.environ, {'PANDORA_BUDGET_MIB': '8192'})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('PANDORA_CPUS_PER_RUN', None)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_a_run_on_smt_is_told_its_physical_cores(self):
        driver = TopologyDriver(cpuset.cores_of(smt_host(16)))
        result = runner.supervise(self.root, 'r1', driver=driver)
        self.assertEqual(result['outcome'], 'passed')
        self.assertEqual(driver.cloned_with.cpuset, '0-3,16-19')
        self.assertEqual(driver.limits.cpus_hint, 4)
        self.assertEqual(driver.limits.cpu_threads, 8)
        self.assertEqual(result['evidence']['cpuset'],
                         {'cpus': '0-3,16-19', 'threads': 8, 'cores': 4})
        self.assertEqual(self.ledger.get('r1')['cpus_hint'], 4)

    def test_a_second_live_run_gets_other_cores(self):
        self.ledger.update('r1', state='running', cpuset='0-3,16-19')
        driver = TopologyDriver(cpuset.cores_of(smt_host(16)))
        runner.supervise(self.root, 'r2', driver=driver)
        self.assertEqual(driver.cloned_with.cpuset, '4-7,20-23')

    def test_no_topology_keeps_the_count_pin(self):
        driver = TopologyDriver(None)
        runner.supervise(self.root, 'r1', driver=driver)
        self.assertEqual(driver.cloned_with.cpuset, '')
        self.assertEqual(driver.limits.cpus_hint, 8)
        self.assertIsNone(self.ledger.get('r1')['cpuset'])

    def test_a_run_that_ends_spreads_the_runs_left_sharing_its_neighbors_cores(self):
        # r2 and r3 are live and share cores 0-3; r1 ends on cores 4-7.
        claim(self.ledger, request_id='req-13', run_id='r3x')
        self.ledger.update('r2', state='running', cpuset='0-3,16-19', instance='run-r2',
                           admitted_at=1.0)
        self.ledger.update('r3x', state='running', cpuset='0-3,16-19', instance='run-r3x',
                           admitted_at=2.0)
        driver = TopologyDriver(cpuset.cores_of(smt_host(16)))
        result = runner.supervise(self.root, 'r1', driver=driver)
        self.assertEqual(result['outcome'], 'passed')
        self.assertEqual(driver.cloned_with.cpuset, '4-7,20-23')
        # r1 is finished, so its cores are idle again: r3x, the newest, takes them.
        self.assertEqual(driver.repinned, [('run-r3x', '4-7,20-23')])
        self.assertEqual(self.ledger.get('r3x')['cpuset'], '4-7,20-23')
        self.assertEqual(self.ledger.get('r2')['cpuset'], '0-3,16-19')

    def test_when_the_newest_repin_fails_the_older_sharer_moves(self):
        claim(self.ledger, request_id='req-13', run_id='r3x')
        self.ledger.update('r2', state='running', cpuset='0-3,16-19', instance='run-r2',
                           admitted_at=1.0)
        self.ledger.update('r3x', state='running', cpuset='0-3,16-19', instance='run-r3x',
                           admitted_at=2.0)
        driver = TopologyDriver(cpuset.cores_of(smt_host(16)), repin_fails='run-r3x')
        runner.supervise(self.root, 'r1', driver=driver)
        self.assertEqual(driver.repinned, [('run-r2', '4-7,20-23')])
        self.assertEqual(self.ledger.get('r2')['cpuset'], '4-7,20-23')
        self.assertEqual(self.ledger.get('r3x')['cpuset'], '0-3,16-19')

    def test_a_run_not_cloned_yet_stays_and_the_older_sharer_moves(self):
        claim(self.ledger, request_id='req-13', run_id='r3x')
        self.ledger.update('r2', state='running', cpuset='0-3,16-19', instance='run-r2',
                           admitted_at=1.0)
        self.ledger.update('r3x', state='running', cpuset='0-3,16-19', admitted_at=2.0)
        driver = TopologyDriver(cpuset.cores_of(smt_host(16)))
        runner.supervise(self.root, 'r1', driver=driver)
        self.assertEqual(driver.repinned, [('run-r2', '4-7,20-23')])
        self.assertEqual(self.ledger.get('r3x')['cpuset'], '0-3,16-19')

    def test_a_failed_repin_keeps_the_old_list(self):
        claim(self.ledger, request_id='req-13', run_id='r3x')
        self.ledger.update('r2', state='running', cpuset='0-3,16-19', instance='run-r2',
                           admitted_at=1.0)
        self.ledger.update('r3x', state='running', cpuset='0-3,16-19', instance='run-r3x',
                           admitted_at=2.0)
        driver = TopologyDriver(cpuset.cores_of(smt_host(16)), repin_fails=True)
        self.assertEqual(runner.supervise(self.root, 'r1', driver=driver)['outcome'], 'passed')
        self.assertEqual(self.ledger.get('r3x')['cpuset'], '0-3,16-19')


class IncusPinTest(unittest.TestCase):
    def config_for(self, limits):
        driver = IncusDriver(root=tempfile.gettempdir())
        calls = []
        driver.incus = lambda *args, **kwargs: calls.append(args) or (0, '', '')
        driver.default_disk_gib = lambda: 0
        driver.apply('run-x', limits)
        return [item for item in calls[0] if item.startswith('limits.cpu=')]

    def test_a_cpuset_is_written_as_the_list(self):
        limits = Limits(memory_mib=1024, ceiling_mib=2048, cpus_hint=4,
                        cpuset='0-3,16-19', cpu_threads=8)
        self.assertEqual(self.config_for(limits), ['limits.cpu=0-3,16-19'])

    def test_without_a_cpuset_the_count_pin_is_unchanged(self):
        limits = Limits(memory_mib=1024, ceiling_mib=2048, cpus_hint=1)
        with mock.patch('os.cpu_count', return_value=32):
            self.assertEqual(self.config_for(limits), ['limits.cpu=1'])

    def test_the_run_is_told_both_numbers(self):
        driver = IncusDriver(root=tempfile.gettempdir())
        seen = {}
        driver.start = lambda name, argv, env, cwd: seen.update(env)
        driver.supervise = lambda *args, **kwargs: None
        from pandora.executor.interface import Instance
        driver.execute(Instance(name='run-x', run_id='x', golden='g'), ['true'],
                       limits=Limits(memory_mib=1, ceiling_mib=2, cpus_hint=4,
                                     cpuset='0-3,16-19', cpu_threads=8))
        self.assertEqual((seen['PANDORA_CPUS'], seen['PANDORA_CPU_THREADS']), ('4', '8'))


class WorkerStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = runner.Paths(Path(self.tmp.name)).ensure()
        Path(self.paths.root, 'cpus_per_run').write_text('8\n')

    def tearDown(self):
        self.tmp.cleanup()

    def status(self, cores):
        from pandora.worker.cli import render_status
        from pandora.worker.service import cpu_pin_of
        pin = cpu_pin_of(self.paths, TopologyDriver(cores))
        lines = [line for line in render_status({'cpu_pin': pin}).splitlines()
                 if line.startswith('cpu pin')]
        return pin, lines[0]

    def test_status_says_threads_and_the_physical_cores_a_run_is_told(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PANDORA_CPUS_PER_RUN', None)
            pin, line = self.status(cpuset.cores_of(smt_host(16)))
        self.assertEqual(pin['pandora_cpus'], 4)
        self.assertIn('8 thread(s) per run', line)
        self.assertIn('whole cores of 16 core(s), 32 thread(s), PANDORA_CPUS 4', line)

    def test_status_says_when_the_pin_is_a_count(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('PANDORA_CPUS_PER_RUN', None)
            pin, line = self.status(None)
        self.assertNotIn('pandora_cpus', pin)
        self.assertIn('by count, topology unread', line)


class SelftestAgreementTest(unittest.TestCase):
    def test_physical_cores_may_be_fewer_than_threads_but_threads_must_be_nproc(self):
        from pandora.client.selftest import pin_agrees
        self.assertTrue(pin_agrees(8, 4, 8))
        self.assertTrue(pin_agrees(2, 2, 2))
        self.assertFalse(pin_agrees(8, 8, 4))
        self.assertFalse(pin_agrees(8, 9, 8))
        self.assertFalse(pin_agrees(8, 1, 8))           # fewer than half: wrong
        self.assertFalse(pin_agrees(8, 3, 8))

    def test_an_older_worker_pins_by_count_and_nproc_is_pandora_cpus(self):
        from pandora.client.selftest import pin_agrees
        self.assertTrue(pin_agrees(8, 8, None))
        self.assertFalse(pin_agrees(8, 4, None))


if __name__ == '__main__':
    unittest.main()
