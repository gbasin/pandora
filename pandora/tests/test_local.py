"""The local lane, with fake commands.

Everything here runs real processes, because the properties being tested are
about processes: that a cancel reaches a grandchild, that a peak is observed,
that two worktrees queue behind one budget. Nothing here needs a worker, a
container or a repository -- the commands are `sh -c` one-liners.
"""
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pandora.client import local, pressure
from pandora.client.local import Budget, Busy, LocalExecutor, Supervisor, cli_exit
from pandora.client.pressure import Gate, Paused
from pandora.engine.admission import Store


def plan_for(argv, **extra):
    plan = {'argv': list(argv), 'cwd': '.', 'env': {}, 'env_unset': [], 'env_passthrough': [],
            'secrets_exclude_globs': [], 'timeout_minutes': 5, 'args': [], 'outputs': []}
    plan.update(extra)
    return plan


class FakeRun:
    """Enough of the daemon's `Run` for the executor: an id, a log, a flag."""

    def __init__(self, directory, run_id='r0'):
        self.id = run_id
        self.dir = Path(directory)
        self.canceled = threading.Event()
        self.chunks = []
        self.notes = []
        self.started = time.time()

    def stream_local(self, which, chunk):
        self.chunks.append((which, chunk))

    def note(self, text):
        self.notes.append(text)

    def output(self):
        return b''.join(chunk for _which, chunk in self.chunks).decode()


def budget(gate=None, **config):
    settings = {'budget_mib': 4096, 'max_running': 4, 'one_active_per_worktree': True}
    settings.update(config)
    return Budget(settings, store=Store(':memory:'),
                  gate=gate if gate is not None else Gate({'enabled': False}))


class Fake:
    """A host that says whatever the test needs, as many times as it needs."""

    def __init__(self, *readings):
        self.readings = list(readings)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.readings[min(self.calls - 1, len(self.readings) - 1)]


def healthy(**extra):
    reading = {'platform': 'linux', 'swap_used_mib': 100.0, 'free_percent': 60.0,
               'psi_full_avg10': 0.0, 'load_per_cpu': 0.5}
    reading.update(extra)
    return reading


class BudgetRules(unittest.TestCase):
    def test_one_active_per_worktree_refuses_rather_than_queues(self):
        pool = budget()
        pool.reserve('a', repo='eichler', job='check', worktree='.', singleton=False)
        with self.assertRaises(Busy) as caught:
            pool.reserve('b', repo='eichler', job='unit', worktree='.', singleton=False)
        self.assertIn('already has an active local job', str(caught.exception))
        self.assertIn('a', str(caught.exception))

    def test_the_rule_is_configurable_off(self):
        pool = budget(one_active_per_worktree=False)
        pool.reserve('a', repo='eichler', job='check', worktree='.', singleton=False)
        pool.reserve('b', repo='eichler', job='unit', worktree='.', singleton=False)

    def test_two_worktrees_are_independent(self):
        pool = budget()
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            pool.reserve('a', repo='eichler', job='check', worktree=one, singleton=False)
            pool.reserve('b', repo='eichler', job='check', worktree=two, singleton=False)

    def test_a_singleton_owns_the_machine(self):
        pool = budget()
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            pool.reserve('a', repo='eichler', job='dev-stack', worktree=one, singleton=True)
            with self.assertRaises(Busy) as caught:
                pool.reserve('b', repo='eichler', job='dev-stack', worktree=two, singleton=True)
        self.assertIn('pandora cancel a', str(caught.exception))

    def test_a_singleton_does_not_take_its_worktrees_slot(self):
        pool = budget()
        pool.reserve('stack', repo='eichler', job='dev-stack', worktree='.', singleton=True)
        pool.reserve('a', repo='eichler', job='check', worktree='.', singleton=False)
        with self.assertRaises(Busy) as caught:
            pool.reserve('b', repo='eichler', job='unit', worktree='.', singleton=False)
        self.assertIn('active local job (a)', str(caught.exception))
        with self.assertRaises(Busy):
            pool.reserve('c', repo='eichler', job='dev-stack', worktree='.', singleton=True)
        # Releasing the singleton leaves the slot it never took with its owner.
        pool.finish('stack', 100, 'ok')
        with self.assertRaises(Busy):
            pool.reserve('d', repo='eichler', job='unit', worktree='.', singleton=False)

    def test_releasing_frees_both_holds(self):
        pool = budget()
        pool.reserve('a', repo='eichler', job='dev-stack', worktree='.', singleton=True)
        pool.admit('a', repo='eichler', job='dev-stack')
        pool.finish('a', 100, 'ok')
        pool.reserve('b', repo='eichler', job='dev-stack', worktree='.', singleton=True)

    def test_a_cold_job_reserves_its_whole_class_and_a_second_waits(self):
        # A cold `medium` reserves its whole 4096 MiB ceiling, so a 4096 MiB
        # budget holds exactly one of them and the second has to wait.
        pool = budget(budget_mib=4096)
        pool.reserve('a', repo='eichler', job='check', worktree='/a', singleton=False)
        pool.reserve('b', repo='eichler', job='check', worktree='/b', singleton=False)
        self.assertIsNotNone(pool.admit('a', repo='eichler', job='check'))
        self.assertIsNone(pool.admit('b', repo='eichler', job='check', timeout=0.2))

    def test_a_release_wakes_the_waiter(self):
        pool = budget(budget_mib=4096)
        pool.reserve('a', repo='eichler', job='check', worktree='/a', singleton=False)
        pool.reserve('b', repo='eichler', job='check', worktree='/b', singleton=False)
        pool.admit('a', repo='eichler', job='check')
        admitted = []
        waiter = threading.Thread(
            target=lambda: admitted.append(
                pool.admit('b', repo='eichler', job='check', timeout=10)))
        waiter.start()
        time.sleep(0.3)
        self.assertEqual(admitted, [])
        pool.finish('a', 512, 'ok')
        waiter.join(timeout=10)
        self.assertTrue(admitted[0]['admitted'])

    def test_the_budget_defaults_to_ram_minus_the_reserve(self):
        total = local.total_memory_mib()
        self.assertEqual(local.budget_from({'reserve_mib': 1024}), total - 1024)
        self.assertEqual(local.budget_from({'budget_mib': 777}), 777)

    def test_the_declared_size_class_is_the_ceiling(self):
        pool = budget()
        pool.reserve('a', repo='eichler', job='validate-node', worktree='/a',
                     singleton=False, size='small')
        admitted = pool.admit('a', repo='eichler', job='validate-node')
        self.assertEqual(admitted['size_class'], 'small')
        self.assertEqual(admitted['ceiling_mib'], 1024)
        self.assertEqual(admitted['reservation_mib'], 1024)

    def test_a_job_that_can_never_fit_is_refused_rather_than_queued(self):
        pool = budget(budget_mib=1024)
        pool.reserve('a', repo='eichler', job='check', worktree='/a', singleton=False)
        with self.assertRaises(Busy) as caught:
            pool.admit('a', repo='eichler', job='check')
        self.assertIn('budget_mib', str(caught.exception))

    def test_a_canceled_wait_is_not_an_admission(self):
        pool = budget(budget_mib=4096)
        pool.reserve('a', repo='eichler', job='check', worktree='/a', singleton=False)
        pool.reserve('b', repo='eichler', job='check', worktree='/b', singleton=False)
        pool.admit('a', repo='eichler', job='check')
        self.assertIsNone(pool.admit('b', repo='eichler', job='check',
                                     canceled=lambda: True, timeout=10))


class PauseGate(unittest.TestCase):
    """The machine's own gate: evidence, waiting, and a refusal that is not a run."""

    def test_a_healthy_host_is_not_evidence_of_anything(self):
        self.assertIsNone(Gate(reader=Fake(healthy())).closed())

    def test_a_memory_stall_pauses_the_lane(self):
        gate = Gate(reader=Fake(healthy(psi_full_avg10=41.0)))
        self.assertIn('PSI full avg10 41.0', gate.closed())

    def test_swap_growth_not_swap_level_is_the_signal(self):
        clock = [0.0]
        # 4 GiB of swap, sitting still: a Mac that swapped yesterday.
        gate = Gate(reader=Fake(healthy(swap_used_mib=4096.0)),
                    clock=lambda: clock[0])
        self.assertIsNone(gate.closed())
        clock[0] = 60.0
        self.assertIsNone(gate.sample())

    def test_swap_climbing_fast_pauses_the_lane(self):
        clock = [0.0]
        gate = Gate(reader=Fake(healthy(swap_used_mib=100.0), healthy(swap_used_mib=900.0)),
                    clock=lambda: clock[0])
        self.assertIsNone(gate.closed())
        clock[0] = 60.0
        self.assertIn('swap growing 800 MiB/min', gate.sample())

    def test_a_starved_mac_pauses_on_free_percentage(self):
        gate = Gate(reader=Fake(healthy(free_percent=1.5, psi_full_avg10=None)))
        self.assertIn('1.5% of memory free', gate.closed())

    def test_the_gate_can_be_turned_off_entirely(self):
        gate = Gate({'enabled': False}, reader=Fake(healthy(psi_full_avg10=99.0)))
        self.assertIsNone(gate.closed())

    def test_a_reading_is_reused_until_it_is_stale(self):
        reader = Fake(healthy())
        gate = Gate({'sample_seconds': 30}, reader=reader, clock=lambda: 5.0)
        gate.closed()
        gate.closed()
        self.assertEqual(reader.calls, 1)

    def test_a_pause_that_clears_admits_the_waiting_job(self):
        reader = Fake(healthy(psi_full_avg10=50.0), healthy())
        pool = budget(gate=Gate({'sample_seconds': 0, 'max_wait_seconds': 30}, reader=reader))
        notes = []
        pool.reserve('a', repo='eichler', job='check', worktree='/a', singleton=False)
        admitted = pool.admit('a', repo='eichler', job='check', poll=0.01, note=notes.append)
        self.assertTrue(admitted['admitted'])
        self.assertTrue(any('local lane paused' in note for note in notes), notes)
        self.assertTrue(any('resumed' in note for note in notes), notes)

    def test_a_pause_that_never_clears_refuses_rather_than_running_anyway(self):
        pool = budget(gate=Gate({'sample_seconds': 0, 'max_wait_seconds': 0.2},
                                reader=Fake(healthy(psi_full_avg10=50.0))))
        pool.reserve('a', repo='eichler', job='check', worktree='/a', singleton=False)
        with self.assertRaises(Paused) as caught:
            pool.admit('a', repo='eichler', job='check', poll=0.01)
        self.assertIn('PSI', str(caught.exception))
        self.assertIn('memory pressure', str(caught.exception))
        self.assertIn('Do not bypass this with PANDORA_OFF=1', str(caught.exception))
        self.assertNotIn('PANDORA_WHERE', str(caught.exception))

    def test_the_counters_are_what_pandora_stats_prints(self):
        gate = Gate({'sample_seconds': 0, 'max_wait_seconds': 0.1},
                    reader=Fake(healthy(psi_full_avg10=50.0)))
        pool = budget(gate=gate)
        pool.reserve('a', repo='eichler', job='check', worktree='/a', singleton=False)
        with self.assertRaises(Paused):
            pool.admit('a', repo='eichler', job='check', poll=0.01)
        state = pool.snapshot()['pause']
        self.assertTrue(state['paused'])
        self.assertEqual(state['episodes'], 1)
        self.assertEqual(state['jobs_delayed'], 1)
        self.assertEqual(state['jobs_refused'], 1)
        self.assertIn('PSI', state['last_evidence'])

    def test_counters_survive_a_daemon_restart(self):
        with tempfile.TemporaryDirectory() as home:
            store = Path(home) / 'pause.json'
            first = Gate({'sample_seconds': 0}, reader=Fake(healthy(psi_full_avg10=50.0)),
                         store=store)
            first.closed()
            first.refused()
            second = Gate({'sample_seconds': 0}, reader=Fake(healthy()), store=store)
            self.assertEqual(second.state()['episodes'], 1)
            self.assertEqual(second.state()['jobs_refused'], 1)

    def test_this_machine_can_be_read_without_raising(self):
        # Not an assertion about this Mac's health -- an assertion that the
        # platform probes parse whatever this platform actually prints.
        reading = pressure.read_host()
        self.assertIn(reading['platform'], ('darwin', 'linux'))
        self.assertIsNotNone(reading['load_per_cpu'])


class SupervisorBehavior(unittest.TestCase):
    def test_it_streams_both_pipes_and_reports_the_exit(self):
        seen = []
        supervisor = Supervisor(['sh', '-c', 'echo out; echo err >&2; exit 3'],
                                cwd='.', env=dict(os.environ), timeout_seconds=30,
                                on_log=lambda which, chunk: seen.append((which, chunk)))
        outcome, code = supervisor.run(lambda: False)
        self.assertEqual((outcome, code), ('command_failed', 3))
        text = b''.join(chunk for _which, chunk in seen).decode()
        self.assertIn('out', text)
        self.assertIn('err', text)
        self.assertEqual({which for which, _chunk in seen}, {'out', 'err'})

    def test_a_zero_exit_is_the_only_pass(self):
        supervisor = Supervisor(['sh', '-c', 'exit 0'], cwd='.', env=dict(os.environ),
                                timeout_seconds=30, on_log=lambda *_: None)
        self.assertEqual(supervisor.run(lambda: False), ('passed', 0))

    def test_a_missing_command_is_infrastructure_not_a_failing_test(self):
        seen = []
        supervisor = Supervisor(['/nonexistent/pandora-test'], cwd='.', env=dict(os.environ),
                                timeout_seconds=30,
                                on_log=lambda which, chunk: seen.append(chunk))
        outcome, code = supervisor.run(lambda: False)
        self.assertEqual(outcome, 'infra_failed')
        self.assertIsNone(code)
        self.assertEqual(cli_exit(outcome, code), 70)

    def test_a_cancel_reaches_the_whole_process_group(self):
        with tempfile.TemporaryDirectory() as home:
            marker = Path(home) / 'grandchild-alive'
            # The child backgrounds a grandchild and waits. Killing only the
            # child would leave the grandchild writing to the marker forever.
            script = ('sh -c \'while :; do date >> %s; sleep 0.1; done\' & '
                      'echo started; wait' % marker)
            supervisor = Supervisor(['sh', '-c', script], cwd=home, env=dict(os.environ),
                                    timeout_seconds=60, on_log=lambda *_: None)
            stop = threading.Event()
            threading.Timer(1.0, stop.set).start()
            outcome, _code = supervisor.run(stop.is_set)
            self.assertEqual(outcome, 'cancelled')
            self.assertTrue(marker.is_file())
            size = marker.stat().st_size
            time.sleep(0.6)
            self.assertEqual(marker.stat().st_size, size,
                             'the grandchild outlived the cancel')

    def test_the_job_chooses_the_signal_and_the_grace(self):
        with tempfile.TemporaryDirectory() as home:
            seen = Path(home) / 'caught'
            # Traps SIGINT, writes what it caught, and then leaves.
            script = ('trap \'echo int > %s; exit 7\' INT; echo up; '
                      'while :; do sleep 0.05; done' % seen)
            supervisor = Supervisor(['sh', '-c', script], cwd=home, env=dict(os.environ),
                                    timeout_seconds=60, on_log=lambda *_: None,
                                    cancel={'signal': 'SIGINT', 'grace_ms': 5000})
            stop = threading.Event()
            threading.Timer(0.8, stop.set).start()
            outcome, _code = supervisor.run(stop.is_set)
            self.assertEqual(outcome, 'cancelled')
            self.assertEqual(seen.read_text().strip(), 'int')

    def test_a_grace_that_expires_escalates_to_sigkill(self):
        # A job may take as long as its grace to clean up; it may not decline.
        script = 'trap "" TERM INT; echo up; while :; do sleep 0.05; done'
        supervisor = Supervisor(['sh', '-c', script], cwd='.', env=dict(os.environ),
                                timeout_seconds=60, on_log=lambda *_: None,
                                cancel={'signal': 'SIGTERM', 'grace_ms': 300})
        stop = threading.Event()
        threading.Timer(0.5, stop.set).start()
        started = time.time()
        outcome, _code = supervisor.run(stop.is_set)
        self.assertEqual(outcome, 'cancelled')
        self.assertLess(time.time() - started, 15, 'the 15 s default grace was used')

    def test_an_unknown_signal_name_falls_back_to_sigterm(self):
        supervisor = Supervisor(['true'], cwd='.', env={}, timeout_seconds=1,
                                on_log=lambda *_: None,
                                cancel={'signal': 'SIGNOPE', 'grace_ms': 1})
        self.assertEqual(supervisor.cancel_signal, signal.SIGTERM)

    def test_a_timeout_is_not_a_command_failure(self):
        supervisor = Supervisor(['sh', '-c', 'sleep 30'], cwd='.', env=dict(os.environ),
                                timeout_seconds=0.5, on_log=lambda *_: None)
        outcome, _code = supervisor.run(lambda: False)
        self.assertEqual(outcome, 'timed_out')
        self.assertEqual(cli_exit(outcome, -15), 70)

    def test_a_peak_is_observed_for_the_group(self):
        # ~120 MiB held for a second, which the one-second sampler must see.
        # Random and kept touched: macOS compresses or pages out idle memory
        # within a second on a busy host, and the group's RSS then reads ~11 MiB.
        script = ("python3 -c \"import os, time; x=bytearray(os.urandom(120*1024*1024)); "
                  "[x[::4096] for _ in range(250) if not time.sleep(0.01)]; print(len(x))\"")
        supervisor = Supervisor(['sh', '-c', script], cwd='.', env=dict(os.environ),
                                timeout_seconds=60, on_log=lambda *_: None)
        outcome, _code = supervisor.run(lambda: False)
        self.assertEqual(outcome, 'passed')
        self.assertGreater(supervisor.peak_mib, 80)

    # pid ppid pgid rss(KiB). 100 is the child; 102 called setsid and left its
    # group; 104 stayed in the group but lost its parent; 200 is someone else's.
    TABLE = ('100 1 100 2048\n101 100 100 1024\n102 101 102 4096\n103 102 102 2048\n'
             '104 1 100 1024\n200 1 200 999999\nbroken\n')

    def fake_ps(self, *args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=self.TABLE, stderr='')

    def test_tree_rss_follows_descendants_that_left_the_group(self):
        supervisor = self.supervised(None)
        supervisor.sample(local.process_table(self.fake_ps))
        self.assertEqual(supervisor.peak_mib, 10)

    def test_the_tree_is_listed_deepest_first(self):
        order = local.tree_of(local.process_table(self.fake_ps), 100, 100)
        self.assertEqual(set(order), {100, 101, 102, 103, 104})
        self.assertLess(order.index(103), order.index(102))
        self.assertLess(order.index(102), order.index(101))
        self.assertEqual(order[-1], 100)

    def supervised(self, returncode):
        supervisor = Supervisor(['true'], cwd='.', env={}, timeout_seconds=1,
                                on_log=lambda *_: None)
        supervisor.proc = mock.Mock(pid=100, returncode=returncode)
        now = time.monotonic()
        # 102 was seen 30 s ago and is 40 s old: ours. 103 was seen 30 s ago
        # but is 5 s old: its pid was reused, so it is left alone.
        supervisor.seen = {102: now - 30, 103: now - 30}
        supervisor.ps = lambda argv, **k: subprocess.CompletedProcess(
            argv, 0, stdout=self.TABLE if 'pid=,ppid=,pgid=,rss=' in argv else
            '102 00:40\n103 00:05\n', stderr='')
        return supervisor

    def signalled(self, supervisor, **kwargs):
        sent = []
        with mock.patch.object(local.os, 'killpg', lambda pid, sig: sent.append(('group', pid))), \
                mock.patch.object(local.os, 'kill', lambda pid, sig: sent.append(('pid', pid))):
            supervisor.signal_group(signal.SIGINT, **kwargs)
        return sent

    def test_each_process_is_signalled_once(self):
        # A second SIGINT makes pnpm, vitest and playwright force-quit.
        sent = self.signalled(self.supervised(None), orphans=True)
        self.assertEqual(sent[0], ('group', 100))
        self.assertEqual(sorted(sent[1:]), [('pid', 102), ('pid', 103)])

    def test_after_the_child_exits_only_old_enough_orphans_are_kept(self):
        sent = self.signalled(self.supervised(0), orphans=True)
        self.assertEqual(sent, [('group', 100), ('pid', 102)])

    def test_a_run_that_ended_on_its_own_leaves_its_orphans_alone(self):
        # A turbo or watchman daemon it meant to leave behind.
        self.assertEqual(self.signalled(self.supervised(0)), [('group', 100)])

    def recorded(self, table, ages, started):
        def ps(argv, **kwargs):
            out = table if 'pid=,ppid=,pgid=,rss=' in argv else ages
            return subprocess.CompletedProcess(argv, 0, stdout=out, stderr='')
        groups, pids = [], []
        killed = local.kill_recorded(100, started, run=ps, clock=lambda: 1000.0,
                                     kill=lambda pid, sig: groups.append(pid),
                                     kill_one=lambda pid, sig: pids.append(pid))
        return killed, groups, pids

    def test_a_group_whose_leader_died_is_still_killed_when_it_is_the_runs(self):
        # 100 died of EPIPE; 101 and 104 are left in its group, 102 left it.
        table = '101 1 100 1024\n102 101 102 1024\n104 1 100 1024\n'
        killed, groups, pids = self.recorded(table, '101 01:30\n104 00:10\n', started=900.0)
        self.assertEqual((killed, groups, pids), (True, [100], [102]))

    def test_a_leaderless_group_older_than_the_run_is_left_alone(self):
        table = '101 1 100 1024\n'
        self.assertEqual(self.recorded(table, '101 10:00\n', started=900.0),
                         (False, [], []))

    def test_a_live_leader_must_have_started_with_the_run(self):
        table = '100 1 100 1024\n'
        self.assertTrue(self.recorded(table, '100 01:40\n', started=900.0)[0])
        self.assertFalse(self.recorded(table, '100 00:10\n', started=900.0)[0])

    def test_a_cancel_reaches_a_descendant_that_called_setsid(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / 'pid'
            script = ('import os, time\n'
                      'if os.fork() == 0:\n'
                      '    os.setsid()\n'
                      '    open(%r, "w").write(str(os.getpid()))\n'
                      '    time.sleep(60)\n'
                      '    os._exit(0)\n'
                      'time.sleep(60)\n' % str(record))
            asked = {'at': None}

            def canceled():
                if asked['at'] is None and record.exists() and record.read_text():
                    asked['at'] = time.monotonic()
                return asked['at'] is not None
            supervisor = Supervisor([sys.executable, '-c', script], cwd='.',
                                    env=dict(os.environ), timeout_seconds=30,
                                    on_log=lambda *_: None,
                                    cancel={'signal': 'SIGTERM', 'grace_ms': 500})
            outcome, _code = supervisor.run(canceled)
            self.assertEqual(outcome, 'cancelled')
            escaped = int(record.read_text())
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    os.kill(escaped, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                os.kill(escaped, 9)
                self.fail('the setsid descendant %d survived the cancel' % escaped)


class ChildEnvironment(unittest.TestCase):
    def test_the_recursion_guard_is_always_set(self):
        env = local.child_environment(plan_for(['x']), {}, cpus_hint=2,
                                      run_id='r1', directory='/tmp/r1')
        self.assertEqual(env['PANDORA_ROUTE_DEPTH'], '1')
        self.assertEqual(env['PANDORA_CPUS'], '2')
        self.assertEqual(env['PANDORA_LOCAL'], '1')

    def test_the_job_declares_and_unsets(self):
        plan = plan_for(['x'], env={'CI': 'true', 'KEEP': '1'}, env_unset=['KEEP'],
                        env_passthrough=['TZ'])
        env = local.child_environment(plan, {'TZ': 'UTC', 'SECRET': 'no'}, cpus_hint=1,
                                      run_id='r1', directory='/tmp/r1')
        self.assertEqual(env['CI'], 'true')
        self.assertNotIn('KEEP', env)
        self.assertEqual(env['TZ'], 'UTC')
        self.assertNotIn('SECRET', env)


class ExecutorEndToEnd(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.root = Path(self.home.name)

    def git_worktree(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'config', 'user.email', 'a@b.c'], check=True)
        subprocess.run(['git', '-C', str(self.root), 'config', 'user.name', 'a'], check=True)
        (self.root / 'file.txt').write_text('one\n')
        subprocess.run(['git', '-C', str(self.root), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(self.root), 'commit', '-qm', 'one'], check=True)
        return self.root

    def execute(self, argv, *, drift='warn', worktree=None, run_id='r0', drift_job=None, **plan):
        # `drift` is the machine's setting; `drift_job` is the job's override.
        worktree = worktree or self.root
        if drift_job is not None:
            plan['drift'] = drift_job
        pool = budget()
        executor = LocalExecutor(pool, drift=drift)
        run = FakeRun(self.root / 'run', run_id)
        run.dir.mkdir(parents=True, exist_ok=True)
        pool.reserve(run.id, repo='eichler', job='fake', worktree=worktree, singleton=False)
        admission = pool.admit(run.id, repo='eichler', job='fake')
        result = executor.execute(run, plan_for(argv, **plan), repo='eichler', job='fake',
                                  worktree=worktree, request_env={}, admission=admission,
                                  note=run.note)
        return run, result, pool

    def test_a_passing_command_publishes_a_clean_receipt(self):
        run, result, pool = self.execute(['sh', '-c', 'echo hello'])
        self.assertEqual(result['outcome'], 'passed')
        self.assertEqual(result['cli_exit'], 0)
        self.assertEqual(result['lane'], 'local')
        self.assertIn('hello', run.output())
        self.assertEqual(pool.snapshot()['held_mib'], 0)
        self.assertIsNotNone(result['learned'])

    def test_a_failing_command_keeps_its_own_exit(self):
        _run, result, _pool = self.execute(['sh', '-c', 'exit 42'])
        self.assertEqual(result['outcome'], 'command_failed')
        self.assertEqual(result['cli_exit'], 42)

    def test_drift_is_a_note_by_default(self):
        self.git_worktree()
        _run, result, _pool = self.execute(
            ['sh', '-c', 'echo two >> file.txt'], drift='warn')
        self.assertTrue(result['drifted'])
        self.assertEqual(result['outcome'], 'passed')
        self.assertEqual(result['cli_exit'], 0)

    def test_drift_can_be_made_a_refusal(self):
        self.git_worktree()
        run, result, _pool = self.execute(
            ['sh', '-c', 'echo two >> file.txt'], drift='fail')
        self.assertTrue(result['drifted'])
        self.assertEqual(result['outcome'], 'drifted')
        self.assertEqual(result['cli_exit'], 75)
        self.assertTrue(any('no longer exists' in note for note in run.notes))

    def test_a_still_tree_never_drifts(self):
        self.git_worktree()
        _run, result, _pool = self.execute(['sh', '-c', 'true'], drift='fail')
        self.assertFalse(result['drifted'])
        self.assertIsNotNone(result['source_before'])
        self.assertEqual(result['source_before'], result['source_after'])

    def test_drift_off_skips_the_freeze_entirely(self):
        self.git_worktree()
        _run, result, _pool = self.execute(['sh', '-c', 'true'], drift='off')
        self.assertIsNone(result['source_before'])
        self.assertFalse(result['drifted'])

    def test_a_non_git_worktree_is_not_reported_as_drifted(self):
        _run, result, _pool = self.execute(['sh', '-c', 'true'], drift='fail')
        self.assertIsNone(result['source_before'])
        self.assertFalse(result['drifted'])

    def test_the_job_overrides_the_machines_drift_setting(self):
        self.git_worktree()
        # The machine says `fail`; this job says `off`, which is the
        # recommendation for `small` jobs whose run is shorter than the freeze.
        _run, result, _pool = self.execute(['sh', '-c', 'echo two >> file.txt'],
                                           drift='fail', drift_job='off')
        self.assertEqual(result['drift'], 'off')
        self.assertIsNone(result['source_before'])
        self.assertEqual(result['outcome'], 'passed')

    def test_a_job_can_ask_for_fail_on_a_warning_machine(self):
        self.git_worktree()
        _run, result, _pool = self.execute(['sh', '-c', 'echo two >> file.txt'],
                                           drift='warn', drift_job='fail')
        self.assertEqual(result['outcome'], 'drifted')
        self.assertEqual(result['cli_exit'], 75)

    def test_declared_evidence_paths_are_recorded_in_the_receipt(self):
        # The answer to EICHLER_VALIDATION_DIRECTORY: the job says where it
        # writes, and the receipt says what was there afterward.
        outputs = [{'kind': 'evidence', 'paths': ['cleanup-required', 'logs/*.txt']}]
        _run, result, _pool = self.execute(
            ['sh', '-c', 'mkdir -p logs && echo x > logs/one.txt && echo y > cleanup-required'],
            outputs=outputs)
        found = {item['path']: item for item in result['outputs']['evidence']}
        self.assertTrue(found['cleanup-required']['present'])
        self.assertTrue(found['logs/one.txt']['present'])
        self.assertEqual(found['logs/one.txt']['bytes'], 2)

    def test_a_declared_path_that_is_absent_is_a_fact_not_a_silence(self):
        outputs = [{'kind': 'evidence', 'paths': ['cleanup-required']}]
        _run, result, _pool = self.execute(['sh', '-c', 'true'], outputs=outputs)
        self.assertEqual(result['outputs']['evidence'],
                         [{'path': 'cleanup-required', 'present': False, 'bytes': None}])

    def test_a_local_run_records_the_cancel_contract_it_ran_under(self):
        _run, result, _pool = self.execute(['sh', '-c', 'true'],
                                           cancel={'signal': 'SIGINT', 'grace_ms': 240000})
        self.assertEqual(result['cancel'], {'signal': 'SIGINT', 'grace_ms': 240000})

    def test_a_canceled_run_exits_130_and_releases_its_holds(self):
        pool = budget()
        executor = LocalExecutor(pool, drift='off')
        run = FakeRun(self.root / 'run')
        run.dir.mkdir(parents=True, exist_ok=True)
        pool.reserve(run.id, repo='eichler', job='fake', worktree=self.root, singleton=False)
        admission = pool.admit(run.id, repo='eichler', job='fake')
        threading.Timer(0.8, run.canceled.set).start()
        result = executor.execute(run, plan_for(['sh', '-c', 'sleep 30']), repo='eichler',
                                  job='fake', worktree=self.root, request_env={},
                                  admission=admission, note=run.note)
        self.assertEqual(result['outcome'], 'cancelled')
        self.assertEqual(result['cli_exit'], 130)
        self.assertEqual(pool.snapshot()['running'], [])


class ExitContract(unittest.TestCase):
    def test_no_outcome_without_a_verdict_ever_exits_zero(self):
        for outcome in ('cancelled', 'timed_out', 'infra_failed', 'drifted', 'oom'):
            self.assertNotEqual(cli_exit(outcome, 0), 0, outcome)

    def test_a_command_verdict_is_passed_through(self):
        self.assertEqual(cli_exit('passed', 0), 0)
        self.assertEqual(cli_exit('command_failed', 7), 7)
        self.assertEqual(cli_exit('command_failed', None), 70)


if __name__ == '__main__':
    unittest.main()
