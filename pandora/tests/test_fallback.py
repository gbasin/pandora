"""One fallback path, through the local lane, and the accident it prevents.

The daemon is real here: a real socket, a real classifier, a real local queue, a
real supervised process. Only the worker is fake, because the whole subject is
what happens when the worker does not take the job -- and there are four
different ways for that to be true, which is precisely why they must not be four
different code paths.
"""
import base64
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from pandora.client import daemon as daemon_module
from pandora.client import enrolment, fallback as policy, shim
from pandora.client.protocol import Reader, VERSION, dump
from pandora.errors import EngineError, SnapshotError, TransferError, WorkerUnreachable

CONFIG = '''
version = 1
[repo]
name = "demo"
entrypoints = ["pnpm"]
[matching]
subdirectory = "local"
[[jobs]]
id = "unit"
size = "small"
args = "optional"
forms = [{ prefix = ["unit"] }]
run = { argv = ["sh", "-c", "echo ran-unit > %(marker)s; echo hello", "--", "{args}"] }
[[jobs]]
id = "surface"
size = "large"
args = "optional"
forms = [{ prefix = ["surface"] }]
run = { argv = ["sh", "-c", "echo ran-surface > %(marker)s", "--", "{args}"] }
[[jobs]]
id = "insist"
size = "large"
args = "none"
fallback = "local"
forms = [{ prefix = ["insist"] }]
run = { argv = ["sh", "-c", "echo ran-insist > %(marker)s"] }
[[jobs]]
id = "picky"
size = "small"
args = "none"
fallback = { action = "local", on = ["worker-unreachable"] }
forms = [{ prefix = ["picky"] }]
run = { argv = ["sh", "-c", "echo ran-picky > %(marker)s"] }
[[jobs]]
id = "writer"
size = "small"
args = "none"
forms = [{ prefix = ["writer"] }]
options = [{ name = "--update", sets = "update", forward = true, writeback = true }]
outputs = [{ kind = "writeback", requires_option = "update", paths = ["out"] }]
run = { argv = ["sh", "-c", "echo ran-writer > %(marker)s"] }
[worker]
base_image = "images:ubuntu/26.04"
'''


class Submission:
    def __init__(self, run_id='r1'):
        self.run_id = run_id
        self.input_id = 'i1'
        self.same_input_as = None
        self.admission = {'reservation_mib': 100, 'cpus_hint': 1}
        self.source = {'reused': False}
        self.durations = {}


class FakeWorker:
    """A worker that fails in one named way, or accepts and then vanishes."""

    raises = None
    follow_raises = None

    def __init__(self, host, **kwargs):
        self.host = host

    def submit(self, **kwargs):
        if FakeWorker.raises is not None:
            raise FakeWorker.raises
        return Submission()

    def follow(self, run_id, **kwargs):
        if FakeWorker.follow_raises is not None:
            raise FakeWorker.follow_raises
        return {'outcome': 'passed', 'cli_exit': 0}, 0

    def collect(self, *a, **k):
        return {'fetched': True, 'missing': []}

    def stats(self):
        return {'ok': False}

    def health(self, **kwargs):
        """Answers by default. A worker that fails `submit` may still be up, and
        the tests below are about `submit`; the one that is about a worker being
        gone sets `health_raises` so both calls agree."""
        if FakeWorker.health_raises is not None:
            raise FakeWorker.health_raises
        return {'ok': True, 'capacity': {'ok': True, 'free_gib': 40.0, 'floor_gib': 4},
                'goldens': ['golden-abc'], 'state': 'ready', 'canary': {'ok': True},
                'kernel_drift': False, 'scheduler': {}, 'outcomes': []}

    def close(self):
        pass


FakeWorker.health_raises = None


class Answer:
    """What one client invocation saw: notices, output, the error, the exit."""

    def __init__(self):
        self.notices = []
        self.out = b''
        self.err = b''
        self.error = None
        self.accepted = None
        self.exit = None


class DaemonCase(unittest.TestCase):
    def setUp(self):
        FakeWorker.raises = None
        FakeWorker.follow_raises = None
        FakeWorker.health_raises = None
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.root = Path(self.home.name)
        self.repo = self.root / 'repo'
        (self.repo / 'sub' / 'deep').mkdir(parents=True)
        self.marker = self.root / 'marker'
        (self.repo / 'pandora.toml').write_text(CONFIG % {'marker': self.marker})
        self.state = self.root / 'state'
        config = self.root / 'config.toml'
        config.write_text(
            '[client]\nstate = "%s"\n[worker]\nhost = "fake@nowhere"\n'
            # No notification centre pop-ups from a test suite.
            '[notify]\nenabled = false\n'
            '[local]\nbudget_mib = 16384\nqueue_timeout_seconds = 20\ndrift = "off"\n'
            '[local.pause]\nenabled = false\n'
            '[[repos]]\nname = "demo"\nroot = "%s"\n' % (self.state, self.repo))
        self.daemon = daemon_module.Daemon(config_path=str(config))
        self.daemon.worker_factory = FakeWorker
        self.daemon.start()
        self.addCleanup(self.daemon.stop)
        self.thread = threading.Thread(target=self.daemon.serve, daemon=True)
        self.thread.start()

    # -- a client, without the shim's process ------------------------------

    def call(self, argv, cwd=None, timeout=60):
        answer = Answer()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(self.daemon.socket_path))
        sock.sendall(dump({'v': VERSION, 'op': 'run', 'cwd': str(cwd or self.repo),
                           'argv': argv, 'env': {}, 'tty': False}))
        reader = Reader(sock)
        try:
            while True:
                frame = reader.line()
                if frame is None:
                    return answer
                kind = frame.get('t')
                if kind == 'notice':
                    answer.notices.append(frame['msg'])
                elif kind == 'error':
                    answer.error = frame
                    answer.exit = frame.get('exit')
                    return answer
                elif kind == 'accepted':
                    answer.accepted = frame
                elif kind == 'log':
                    data = base64.b64decode(frame['b64'])
                    if frame.get('s') == 'err':
                        answer.err += data
                    else:
                        answer.out += data
                elif kind == 'exit':
                    answer.exit = frame['code']
                    return answer
        finally:
            sock.close()

    def result_of(self, run_id):
        return json.loads((self.state / 'runs' / run_id / 'result.json').read_text())


class OneFallbackPath(DaemonCase):
    CAUSES = [
        ('worker-unreachable', WorkerUnreachable('no route to host')),
        ('snapshot-failed', SnapshotError('the tree moved under the freeze')),
        ('transfer-failed', TransferError('rsync died')),
        ('engine-error', EngineError(json.dumps({'code': 'rejected', 'detail': None}))),
        ('queue-timeout', EngineError(json.dumps({'code': 'queue-timeout'}))),
        ('admission-refused', EngineError(json.dumps({'code': 'admission-refused'}))),
    ]

    def test_every_cause_admits_a_small_job_into_the_local_lane(self):
        for cause, error in self.CAUSES:
            with self.subTest(cause=cause):
                self.marker.unlink(missing_ok=True)
                FakeWorker.raises = error
                answer = self.call(['pnpm', 'unit'])
                self.assertEqual(answer.exit, 0, answer.error)
                self.assertEqual(answer.accepted['lane'], 'local')
                self.assertEqual(answer.accepted['reason'], 'fallback:' + cause)
                self.assertTrue(any(cause in line for line in answer.notices), answer.notices)
                self.assertEqual(self.marker.read_text().strip(), 'ran-unit')

    def test_the_receipt_names_the_lane_and_the_reason(self):
        FakeWorker.raises = WorkerUnreachable('down')
        answer = self.call(['pnpm', 'unit'])
        result = self.result_of(answer.accepted['run'])
        self.assertEqual(result['lane'], 'local')
        self.assertEqual(result['reason'], 'fallback:worker-unreachable')
        meta = json.loads((self.state / 'runs' / answer.accepted['run'] / 'meta.json').read_text())
        self.assertEqual(meta['lane'], 'local')
        self.assertEqual(meta['reason'], 'fallback:worker-unreachable')

    def test_a_fallback_holds_the_local_budget_like_any_other_local_job(self):
        # The point of admitting rather than exec'ing: the reservation is real,
        # the size class is the job's, and `pandora ps` has a row for it.
        FakeWorker.raises = WorkerUnreachable('down')
        answer = self.call(['pnpm', 'unit'])
        result = self.result_of(answer.accepted['run'])
        self.assertEqual(result['size_class'], 'small')
        self.assertEqual(result['reservation_mib'], 1024)
        self.assertEqual(self.daemon.budget.snapshot()['held_mib'], 0)

    def test_a_large_job_is_refused_rather_than_run_here(self):
        # This is the accident of 2026-09-22, in one test: the worker refuses a
        # submission of a 302-test browser suite, and nothing runs on this Mac.
        FakeWorker.raises = EngineError(json.dumps({'code': 'rejected'}))
        answer = self.call(['pnpm', 'surface'])
        self.assertEqual(answer.exit, 70)
        self.assertEqual(answer.error['code'], 'fallback-refused')
        self.assertIn('size large', answer.error['msg'])
        self.assertIn('PANDORA_OFF=1', answer.error['msg'])
        self.assertFalse(self.marker.exists(), 'a large job ran on this Mac')

    def test_a_large_job_that_declares_local_is_allowed(self):
        FakeWorker.raises = WorkerUnreachable('down')
        answer = self.call(['pnpm', 'insist'])
        self.assertEqual(answer.exit, 0, answer.error)
        self.assertEqual(self.marker.read_text().strip(), 'ran-insist')

    def test_a_declared_cause_list_refuses_every_other_cause(self):
        FakeWorker.raises = EngineError(json.dumps({'code': 'admission-refused'}))
        answer = self.call(['pnpm', 'picky'])
        self.assertEqual(answer.exit, 70)
        self.assertIn('worker-unreachable', answer.error['msg'])
        self.assertFalse(self.marker.exists())

    def test_a_write_back_run_never_falls_back(self):
        FakeWorker.raises = WorkerUnreachable('down')
        answer = self.call(['pnpm', 'writer', '--update'])
        self.assertEqual(answer.exit, 70)
        self.assertFalse(self.marker.exists())


class AfterAcceptedNeverFallsBack(DaemonCase):
    def test_a_worker_lost_after_acceptance_is_an_infrastructure_failure(self):
        # The line the whole design hangs on, pinned. Before `accepted` every
        # failure is a fallback decision; after it, none of them are, because the
        # command may be executing on the worker right now.
        self.daemon.ATTEMPTS, self.daemon.BACKOFF = 2, 0.01
        FakeWorker.follow_raises = WorkerUnreachable('the worker went away')
        answer = self.call(['pnpm', 'unit'])
        self.assertIsNotNone(answer.accepted)
        self.assertEqual(answer.accepted.get('lane'), None)
        self.assertEqual(answer.exit, 70)
        self.assertFalse(self.marker.exists(), 'a run fell back after acceptance')
        self.assertIn(b'lost the worker', answer.err)


class SubdirectoryInvocations(DaemonCase):
    def test_a_subdirectory_is_re_rooted_when_no_argument_names_a_path(self):
        FakeWorker.raises = WorkerUnreachable('down')
        answer = self.call(['pnpm', 'unit', 'fast'], cwd=self.repo / 'sub' / 'deep')
        self.assertEqual(answer.exit, 0, answer.error)
        self.assertTrue(any('worktree root' in line for line in answer.notices),
                        answer.notices)

    def test_an_argument_that_names_a_path_here_passes_through(self):
        (self.repo / 'sub' / 'x.test.ts').write_text('')
        answer = self.call(['pnpm', 'unit', 'x.test.ts'], cwd=self.repo / 'sub')
        self.assertEqual(answer.error['code'], 'passthrough')
        self.assertEqual(answer.error['msg'], 'run from the repo root to route')

    def test_a_slash_in_an_argument_passes_through_without_touching_the_disk(self):
        answer = self.call(['pnpm', 'unit', 'src/nothing.test.ts'], cwd=self.repo / 'sub')
        self.assertEqual(answer.error['code'], 'passthrough')

    def test_an_unenrolled_directory_passes_through_rather_than_refusing(self):
        answer = self.call(['pnpm', 'unit'], cwd=self.root)
        self.assertEqual(answer.error['code'], 'passthrough')
        self.assertIn('enrolled', answer.error['msg'])


class WithoutADaemon(unittest.TestCase):
    """The one decision the client still makes, and the marker it makes it with."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.root = Path(self.home.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.ran = self.root / 'ran'
        self.cwd = os.getcwd()
        self.addCleanup(os.chdir, self.cwd)

    def enrol(self, policies):
        """A git repository with a marker, and this process inside it."""
        git = self.root / 'repo' / '.git'
        git.mkdir(parents=True)
        (git / 'pandora-enrolled').write_text(enrolment.render(
            socket_path=str(self.state / 'client.sock'), repo='demo',
            claims=[item['prefix'] for item in policies], policies=policies))
        os.chdir(self.root / 'repo')

    def run_shim(self, command):
        # A socket path that is not there: the daemon is the missing thing.
        real = self.root / 'fake-pnpm'
        real.write_text('#!/bin/sh\necho ran > %s\n' % self.ran)
        real.chmod(0o755)
        return shim.main(['--sock', str(self.state / 'client.sock'),
                          '--real', str(real), '--state', str(self.state),
                          '--', *command])

    def test_a_small_job_runs_here_under_the_slot_budget(self):
        self.enrol([{'prefix': ['unit'], 'size': 'small', 'fallback': 'auto',
                     'writeback': False}])
        self.assertEqual(self.run_shim(['unit']), 0)
        self.assertTrue(self.ran.exists())
        log = (self.state / 'passthrough.jsonl').read_text()
        self.assertIn('daemon-unreachable', log)

    def test_a_large_job_is_refused_rather_than_run_here(self):
        self.enrol([{'prefix': ['surface'], 'size': 'large', 'fallback': 'auto',
                     'writeback': False}])
        self.assertEqual(self.run_shim(['surface']), 70)
        self.assertFalse(self.ran.exists())

    def test_a_marker_without_policies_is_unknown_and_therefore_refused(self):
        # An enrolment written before this rule existed. Unknown size is decided
        # as `large`: a client that cannot say how big a job is has not earned
        # the right to start it on this Mac.
        self.enrol([])
        self.assertEqual(self.run_shim(['surface']), 70)
        self.assertFalse(self.ran.exists())

    def test_update_is_never_run_here_whatever_the_marker_says(self):
        self.enrol([{'prefix': ['unit'], 'size': 'small', 'fallback': 'local',
                     'writeback': False}])
        self.assertEqual(self.run_shim(['unit', '--update']), 70)
        self.assertFalse(self.ran.exists())

    def test_the_marker_round_trips_through_the_shell_readable_format(self):
        policies = [{'prefix': ['test:unit'], 'size': 'medium', 'fallback': 'refuse',
                     'writeback': True}]
        text = enrolment.render(socket_path='/s', repo='demo',
                                claims=[['test:unit']], policies=policies)
        marker = enrolment.parse(text)
        self.assertEqual(marker['policy'], policies)
        self.assertEqual(enrolment.policy_for(['test:unit', 'x'], marker)['size'], 'medium')
        self.assertIsNone(enrolment.policy_for(['other'], marker))


class RemoteCancelContract(unittest.TestCase):
    """The job's cancel contract reaches the worker's executor, or nothing does.

    A remote cancel is an instance destroy: the run dies whatever happens, and
    the receipt proves the machine is gone. What the contract buys is the window
    before that -- one signal of the job's choosing, then its grace, then
    SIGKILL. Checked here at both seams, because the seam in the middle is a
    file the client writes and the engine reads.
    """

    def test_the_plan_carries_the_contract_to_the_worker(self):
        from pandora.config import classify, loader
        config = loader.validate(__import__('tomllib').loads(
            'version = 1\n[repo]\nname = "d"\nentrypoints = ["pnpm"]\n'
            '[worker]\nbase_image = "i"\n[[jobs]]\nid = "s"\n'
            'cancel = { signal = "SIGINT", grace_ms = 240000 }\n'
            'forms = [{ prefix = ["s"] }]\nrun = { argv = ["true"] }\n'))
        plan = classify.classify(config, ['pnpm', 's'])['plan']
        self.assertEqual(plan['cancel'], {'signal': 'SIGINT', 'grace_ms': 240000})

    def test_the_engine_reads_it_from_the_submitted_request(self):
        from pandora.engine import runner
        with tempfile.TemporaryDirectory() as home:
            paths = runner.Paths(home).ensure()
            paths.attempt('r1').mkdir(parents=True, exist_ok=True)
            (paths.attempt('r1') / 'request.json').write_text(json.dumps(
                {'plan': {'cancel': {'signal': 'SIGINT', 'grace_ms': 240000}}}))
            self.assertEqual(runner.submitted_plan(paths, 'r1')['cancel']['grace_ms'], 240000)
            self.assertIsNone(runner.submitted_plan(paths, 'r2'))

    def test_the_driver_sends_the_signal_then_escalates(self):
        from pandora.executor.incus import IncusDriver
        from pandora.executor.interface import Instance
        driver = IncusDriver(root='/tmp/none', sudo=False)
        seen = []
        driver.incus = lambda *args, **kw: (seen.append((args, kw)) or (0, '', ''))
        driver.kill(Instance(name='i1', run_id='r1', golden='g'),
                    signal='SIGINT', grace_ms=240000)
        script = seen[0][0][-1]
        self.assertIn('kill -SIGINT', script)
        self.assertIn('+ 240 ', script)
        self.assertIn('kill -9', script)
        self.assertGreater(seen[0][1]['timeout'], 240)

    def test_the_default_is_the_old_behaviour_exactly(self):
        from pandora.executor.incus import IncusDriver
        from pandora.executor.interface import Instance
        driver = IncusDriver(root='/tmp/none', sudo=False)
        seen = []
        driver.incus = lambda *args, **kw: (seen.append(args) or (0, '', ''))
        driver.kill(Instance(name='i1', run_id='r1', golden='g'))
        self.assertIn('kill -SIGKILL', seen[0][-1])
        self.assertIn('+ 0 ', seen[0][-1])


class Policy(unittest.TestCase):
    """The decision itself, without a daemon around it."""

    def test_size_decides_when_nothing_is_declared(self):
        for size, action in (('small', 'local'), ('medium', 'local'),
                             ('large', 'refuse'), ('xlarge', 'refuse')):
            self.assertEqual(policy.decide(cause='worker-unreachable', size=size)['action'],
                             action, size)

    def test_an_unknown_size_is_treated_as_large(self):
        self.assertEqual(policy.decide(cause='worker-unreachable')['action'], 'refuse')

    def test_writeback_outranks_everything(self):
        verdict = policy.decide(cause='worker-unreachable', size='small', writeback=True,
                                declared={'action': 'local', 'on': list(policy.CAUSES)})
        self.assertEqual(verdict['action'], 'refuse')
        self.assertIn('write-back', verdict['reason'])

    def test_an_unknown_cause_is_refused_rather_than_guessed(self):
        self.assertEqual(policy.decide(cause='cosmic-ray', size='small')['action'], 'refuse')

    def test_a_declaration_beats_the_size_both_ways(self):
        big = {'action': 'local', 'on': list(policy.CAUSES)}
        small = {'action': 'refuse', 'on': list(policy.CAUSES)}
        self.assertEqual(policy.decide(cause='queue-timeout', size='large',
                                       declared=big)['action'], 'local')
        self.assertEqual(policy.decide(cause='queue-timeout', size='small',
                                       declared=small)['action'], 'refuse')

    def test_every_cause_the_loader_knows_is_a_cause_this_module_knows(self):
        from pandora.config import loader
        self.assertEqual(set(loader.FAULTS), set(policy.CAUSES))


if __name__ == '__main__':
    unittest.main()


class WorkerKnownDown(DaemonCase):
    """The health poll's one job: a known-down worker costs no SSH timeout."""

    def test_a_known_down_worker_falls_back_without_submitting(self):
        FakeWorker.health_raises = WorkerUnreachable('ssh: connect timed out')
        self.daemon.health.poll()
        self.assertTrue(self.daemon.health.known_down())
        FakeWorker.raises = AssertionError('submit must not be called for a known-down worker')
        started = time.monotonic()
        answer = self.call(['pnpm', 'unit'])
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(answer.exit, 0)
        self.assertTrue(self.marker.exists())
        self.assertTrue(any('worker-down' in note for note in answer.notices), answer.notices)
        meta = json.loads((self.state / 'runs' / answer.accepted['run'] / 'meta.json').read_text())
        self.assertEqual(meta['lane'], 'local')
        self.assertEqual(meta['reason'], 'fallback:worker-down')

    def test_ps_carries_the_worker_state(self):
        FakeWorker.health_raises = WorkerUnreachable('gone')
        self.daemon.health.poll()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(self.daemon.socket_path))
        sock.sendall(dump({'v': VERSION, 'op': 'ps'}))
        frame = Reader(sock).line()
        sock.close()
        self.assertEqual(frame['worker']['worker'], 'down')
        self.assertIn('gone', frame['worker']['reason'])

    def test_a_refused_submission_asks_for_a_recheck_rather_than_declaring_down(self):
        before = self.daemon.health.state()['polls']
        FakeWorker.raises = WorkerUnreachable('no route to host')
        self.call(['pnpm', 'unit'])
        deadline = time.monotonic() + 5
        while self.daemon.health.state()['polls'] == before and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertGreater(self.daemon.health.state()['polls'], before, 'the nudge polled')
        # The health call still answers, so a refused submit is not a down worker.
        self.assertEqual(self.daemon.health.state()['worker'], 'reachable')


class Hints(DaemonCase):
    """The last line a caller sees names the next action, and only from evidence."""

    def test_an_oom_result_carries_the_engines_hint_to_the_caller(self):
        original = FakeWorker.follow
        FakeWorker.follow = lambda self, run_id, **k: (
            {'outcome': 'oom', 'cli_exit': 137, 'job': 'unit', 'peak_mib': 4096,
             'ceiling_mib': 4096, 'evidence': {'reason': 'memory-thrash'},
             'hint': 'watchdog killed for file-cache thrash; likely a large build or install '
                     '-- declare size large for job unit (peak 4096 MiB of a 4096 MiB ceiling)'},
            0)
        self.addCleanup(setattr, FakeWorker, 'follow', original)
        answer = self.call(['pnpm', 'unit'])
        self.assertEqual(answer.exit, 137)
        self.assertIn(b'pandora: hint: watchdog killed for file-cache thrash', answer.err)
        result = self.result_of(answer.accepted['run'])
        self.assertIn('declare size large', result['hint'])

    def test_a_client_side_rule_fills_in_when_the_engine_has_none(self):
        ignored = self.repo / 'tmp' / 'fixture.json'
        ignored.parent.mkdir()
        ignored.write_text('{}')
        (self.repo / '.gitignore').write_text('tmp/\n')
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        original = FakeWorker.follow

        def follow(self, run_id, *, on_log=None, **k):
            on_log(b"Error: Cannot find module 'tmp/fixture.json'\n")
            return {'outcome': 'command_failed', 'cli_exit': 1, 'job': 'unit',
                    'hint': None}, 0

        FakeWorker.follow = follow
        self.addCleanup(setattr, FakeWorker, 'follow', original)
        answer = self.call(['pnpm', 'unit'])
        self.assertEqual(answer.exit, 1)
        self.assertIn(b'tmp/fixture.json exists locally but is gitignored', answer.err)
        self.assertIn('[sync] include', self.result_of(answer.accepted['run'])['hint'])

    def test_a_passing_run_says_nothing(self):
        answer = self.call(['pnpm', 'unit'])
        self.assertEqual(answer.exit, 0)
        self.assertNotIn(b'pandora: hint', answer.err)
        self.assertIsNone(self.result_of(answer.accepted['run']).get('hint'))
