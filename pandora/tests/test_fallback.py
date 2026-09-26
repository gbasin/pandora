"""One fallback path, through the local lane, and the accident it prevents.

The daemon is real here: a real socket, a real classifier, a real local queue, a
real supervised process. Only the worker is fake, because the whole subject is
what happens when the worker does not take the job -- and there are four
different ways for that to be true, which is precisely why they must not be four
different code paths.
"""
import base64
import contextlib
import io
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pandora.client import daemon as daemon_module
from pandora.client import enrollment, fallback as policy, shim
from pandora.client.protocol import Reader, VERSION, dump
from pandora.errors import EngineError, SnapshotError, TransferError, WorkerUnreachable

CONFIG = '''
version = 1
[repo]
name = "demo"
entrypoints = ["pnpm"]
[matching]
subdirectory = "reroot"
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
[[jobs]]
id = "fanout"
# medium, on purpose: size alone would admit it to the local lane, so the
# sharded-job refusal below exercises the lane gate, not the size class.
size = "medium"
args = "optional"
forms = [{ prefix = ["fanout"] }]
shards = { strategy = "argv", template = "--shard={i}/{n}", default = 2, max = 4 }
run = { argv = ["sh", "-c", "echo ran-fanout > %(marker)s", "--", "{args}"] }
[worker]
base_image = "images:ubuntu/26.04"
'''


class Submission:
    def __init__(self, run_id='r1'):
        self.run_id = run_id
        self.input_id = 'i1'
        self.same_tree_as = None
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

    collected_into = []

    def collect(self, *a, **k):
        FakeWorker.collected_into.append(k.get('worktree'))
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
            # No notification center pop-ups from a test suite.
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

    def call(self, argv, cwd=None, timeout=60, sock=None):
        answer = Answer()
        if sock is None:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(str(self.daemon.socket_path))
        sock.settimeout(timeout)
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
    ]
    # A busy worker, not a broken path to it: never a fallback (2026-09-24).
    BUSY = [
        ('queue-timeout', EngineError(json.dumps({'code': 'queue-timeout'}))),
        ('admission-refused', EngineError(json.dumps({'code': 'admission-refused'}))),
    ]

    def test_a_busy_worker_never_moves_even_a_small_job_here(self):
        for cause, error in self.BUSY:
            with self.subTest(cause=cause):
                self.marker.unlink(missing_ok=True)
                FakeWorker.raises = error
                answer = self.call(['pnpm', 'unit'])
                self.assertEqual(answer.exit, 70)
                self.assertIsNone(answer.accepted)
                self.assertIn(cause, answer.error['msg'])
                self.assertIn('busy', answer.error['msg'])
                self.assertFalse(self.marker.exists())

    def test_a_busy_worker_refuses_even_a_job_that_declares_local(self):
        FakeWorker.raises = EngineError(json.dumps({'code': 'admission-refused'}))
        answer = self.call(['pnpm', 'insist'])
        self.assertEqual(answer.exit, 70)
        self.assertFalse(self.marker.exists())

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
        # The job could run in the local lane, so the refusal steers there and
        # not to an unmanaged run (2026-09-24).
        self.assertIn('PANDORA_WHERE=local', answer.error['msg'])
        self.assertNotIn('PANDORA_OFF', answer.error['msg'])
        self.assertFalse(self.marker.exists(), 'a large job ran on this Mac')

    def test_a_sharded_job_never_falls_back_at_any_size(self):
        # fanout is medium: the size rule would admit it, but the local lane
        # cannot run a sharded job, so the verdict is refuse (gh-120).
        FakeWorker.raises = TransferError('rsync failed (255): unexpected end of file')
        answer = self.call(['pnpm', 'fanout'])
        self.assertEqual((answer.error['code'], answer.exit), ('fallback-refused', 70))
        self.assertIn('local lane', answer.error['msg'])
        self.assertNotIn('PANDORA_WHERE=local', answer.error['msg'])
        self.assertIn('last resort, PANDORA_OFF=1', answer.error['msg'])
        self.assertFalse(self.marker.exists())

    def test_a_paused_local_lane_says_why_in_the_refused_runs_log(self):
        from pandora.client.pressure import Paused
        FakeWorker.raises = WorkerUnreachable('down')
        reason = 'this Mac has been under memory pressure for 300s (swap growing)'
        with mock.patch.object(self.daemon.budget, 'admit', side_effect=Paused(reason)):
            answer = self.call(['pnpm', 'unit'])
        self.assertEqual((answer.error['code'], answer.exit), ('local-paused', 70))
        local = [path.parent for path in (self.state / 'runs').glob('*/meta.json')
                 if json.loads(path.read_text())['lane'] == 'local']
        self.assertEqual(len(local), 1)
        self.assertEqual(json.loads((local[0] / 'meta.json').read_text())['state'], 'refused')
        frames = [json.loads(line) for line in (local[0] / 'log').read_text().splitlines()]
        said = b''.join(base64.b64decode(frame['b64']) for frame in frames if 'b64' in frame)
        self.assertIn(reason.encode(), said)

    def test_a_large_job_that_declares_local_is_allowed(self):
        FakeWorker.raises = WorkerUnreachable('down')
        answer = self.call(['pnpm', 'insist'])
        self.assertEqual(answer.exit, 0, answer.error)
        self.assertEqual(self.marker.read_text().strip(), 'ran-insist')

    def test_a_declared_cause_list_refuses_every_other_cause(self):
        FakeWorker.raises = TransferError('rsync died')
        answer = self.call(['pnpm', 'picky'])
        self.assertEqual(answer.exit, 70)
        self.assertIn('worker-unreachable', answer.error['msg'])
        self.assertFalse(self.marker.exists())

    def test_a_write_back_run_never_falls_back(self):
        FakeWorker.raises = WorkerUnreachable('down')
        answer = self.call(['pnpm', 'writer', '--update'])
        self.assertEqual(answer.exit, 70)
        self.assertFalse(self.marker.exists())
        # And the refusal does not steer the caller to the local lane: that
        # lane cannot write back either (gh-130).
        self.assertNotIn('PANDORA_WHERE=local', answer.error['msg'])
        self.assertIn('worker is reachable', answer.error['msg'])


class UploadPhases(DaemonCase):
    """A slow submission says where it is: in `ps`, in the log, in its timings."""

    def test_each_step_is_on_disk_and_a_failure_keeps_its_partial_timings(self):
        seen = []
        state = self.state

        def slow(worker, *, phase=None, progress=None, **kwargs):
            for name in ('freeze', 'ship'):
                phase(name)
                [meta] = [json.loads(path.read_text())
                          for path in (state / 'runs').glob('*/meta.json')]
                seen.append((meta['state'], meta['phase']))
            progress('syncing 3 files, 1 KiB')
            error = TransferError('rsync to h failed (255): unexpected end of file')
            error.pre_accept = {'freeze': 0.5, 'ship': 12.0}
            raise error
        with mock.patch.object(FakeWorker, 'submit', slow):
            answer = self.call(['pnpm', 'surface'])
        self.assertEqual(seen, [('queued', 'freeze'), ('queued', 'ship')])
        self.assertEqual(answer.notices.count('syncing 3 files, 1 KiB'), 1)
        [row] = [path.parent for path in (self.state / 'runs').glob('*/meta.json')]
        meta = json.loads((row / 'meta.json').read_text())
        self.assertEqual((meta['state'], meta['phase']), ('refused', 'ship'))
        self.assertEqual(meta['pre_accept'], {'freeze': 0.5, 'ship': 12.0})
        frames = [json.loads(line) for line in (row / 'log').read_text().splitlines()]
        said = [base64.b64decode(frame['b64']) for frame in frames if frame['t'] == 'said']
        self.assertEqual(said, [b'pandora: syncing 3 files, 1 KiB\n'])

    def test_an_accepted_run_streams_the_sync_line_only_once(self):
        def syncing(worker, *, phase=None, progress=None, **kwargs):
            phase('ship')
            progress('syncing 3 files, 1 KiB')
            return Submission()
        with mock.patch.object(FakeWorker, 'submit', syncing):
            answer = self.call(['pnpm', 'unit'])
        self.assertEqual(answer.exit, 0, answer.error)
        self.assertEqual(answer.notices, ['syncing 3 files, 1 KiB'])
        self.assertNotIn(b'syncing', answer.err)
        meta = json.loads((self.state / 'runs' / answer.accepted['run'] / 'meta.json')
                          .read_text())
        self.assertNotEqual(meta['phase'], 'ship')


class ClaimCache(DaemonCase):
    """The daemon derives each worktree's claim cache from that worktree's own config."""

    def setUp(self):
        super().setUp()
        (self.repo / '.git').mkdir(exist_ok=True)
        (self.repo / '.git' / 'pandora-repo').write_text(enrollment.registration_text(
            socket_path=str(self.daemon.socket_path), repo='demo'))
        self.cache = self.repo / '.git' / 'pandora-claims'
        self.settle(self.root / 'config.toml', minutes=2)
        self.settle()

    def settle(self, path=None, minutes=1):
        """Date a config well before now, as if it was edited a minute ago."""
        path = path or self.repo / 'pandora.toml'
        stamp = time.time_ns() - minutes * 60 * 10**9
        os.utime(path, ns=(stamp, stamp))
        return path.stat().st_mtime_ns

    def ask(self, argv, cwd=None):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(30)
        sock.connect(str(self.daemon.socket_path))
        try:
            sock.sendall(dump({'v': VERSION, 'op': 'claims', 'cwd': str(cwd or self.repo),
                               'argv': argv}))
            return Reader(sock).line()
        finally:
            sock.close()

    def test_a_claimed_run_writes_the_cache_dated_as_its_config(self):
        stamp = self.settle()
        answer = self.call(['pnpm', 'unit'])
        self.assertEqual(answer.exit, 0, answer.error)
        cache = enrollment.parse(self.cache.read_text())
        self.assertIn(['unit'], cache['claim'])
        self.assertIn(['surface'], cache['claim'])
        self.assertEqual(cache['sock'], str(self.daemon.socket_path))
        self.assertIsNone(cache['config'])          # the worktree's own pandora.toml
        self.assertEqual(self.cache.stat().st_mtime_ns, stamp)

    def test_a_stale_marker_gets_no_notice_any_more(self):
        (self.repo / '.git' / 'pandora-enrolled').write_text(enrollment.render(
            socket_path=str(self.daemon.socket_path), repo='demo', claims=[['unit']]))
        answer = self.call(['pnpm', 'unit'])
        self.assertEqual(answer.exit, 0, answer.error)
        self.assertFalse(any('stale' in line for line in answer.notices), answer.notices)

    def test_a_changed_config_is_rewritten_on_the_next_classification(self):
        self.call(['pnpm', 'unit'])
        text = (self.repo / 'pandora.toml').read_text()
        (self.repo / 'pandora.toml').write_text(text.replace('prefix = ["surface"]',
                                                             'prefix = ["surface2"]'))
        stamp = self.settle()
        answer = self.ask(['surface2'])
        self.assertEqual((answer['t'], answer['claimed']), ('claims', True))
        cache = enrollment.parse(self.cache.read_text())
        self.assertIn(['surface2'], cache['claim'])
        self.assertNotIn(['surface'], cache['claim'])
        self.assertEqual(self.cache.stat().st_mtime_ns, stamp)

    def test_an_edit_that_has_not_settled_leaves_the_cache_dated_before_it(self):
        path = self.repo / 'pandora.toml'
        path.write_text(path.read_text())         # an edit this second
        self.ask(['unit'])
        self.assertLess(self.cache.stat().st_mtime_ns, path.stat().st_mtime_ns)
        self.assertEqual(enrollment.cache_state(self.repo, self.cache)[0], 'stale')

    def test_the_claims_op_answers_with_the_shims_rule(self):
        self.assertEqual({key: self.ask(['unit', 'x'])[key] for key in ('claimed', 'heavy')},
                         {'claimed': True, 'heavy': False})
        self.assertEqual({key: self.ask(['build'])[key] for key in ('claimed', 'heavy')},
                         {'claimed': False, 'heavy': True})
        self.assertEqual({key: self.ask(['why'])[key] for key in ('claimed', 'heavy')},
                         {'claimed': False, 'heavy': False})
        self.assertEqual(enrollment.cache_state(self.repo, self.cache)[0], 'fresh')

    def test_a_branch_worktree_routes_by_its_own_file_and_nothing_compares_them(self):
        branch = self.root / 'branch'
        branch.mkdir()
        gitdir = self.repo / '.git' / 'worktrees' / 'branch'
        gitdir.mkdir(parents=True)
        (branch / '.git').write_text('gitdir: %s\n' % gitdir)
        text = (self.repo / 'pandora.toml').read_text()
        (branch / 'pandora.toml').write_text(text[:text.index('[[jobs]]\nid = "surface"')]
                                             + text[text.index('[worker]'):])
        self.settle(branch / 'pandora.toml')
        self.assertFalse(self.ask(['surface'], cwd=branch)['claimed'])
        self.assertTrue(self.ask(['surface'])['claimed'])
        mine = enrollment.parse((gitdir / 'pandora-claims').read_text())
        self.assertEqual(mine['claim'], [['unit']])
        answer = self.call(['pnpm', 'unit'], cwd=branch)
        self.assertEqual(answer.exit, 0, answer.error)
        self.assertEqual(answer.notices, [])

    def test_a_repository_without_a_repos_entry_gets_a_cache_that_claims_nothing(self):
        other = self.root / 'other'
        (other / '.git').mkdir(parents=True)
        (other / '.git' / 'pandora-repo').write_text('sock %s\n' % self.daemon.socket_path)
        (other / 'pandora.toml').write_text((self.repo / 'pandora.toml').read_text())
        self.assertFalse(self.ask(['unit'], cwd=other)['claimed'])
        text = (other / '.git' / 'pandora-claims').read_text()
        self.assertIn('# claims nothing: no [[repos]] entry', text)
        self.assertEqual(enrollment.parse(text)['claim'], [])
        self.assertEqual(enrollment.parse(text)['client'], self.daemon.config['source'])

    def test_a_repository_enrolled_with_another_daemon_is_not_repointed(self):
        (self.repo / '.git' / 'pandora-repo').write_text('sock /elsewhere/client.sock\n')
        answer = self.ask(['unit'])
        self.assertTrue(answer['claimed'])            # it still answers
        self.assertFalse(self.cache.exists())
        self.assertEqual(self.call(['pnpm', 'unit']).exit, 0)
        self.assertFalse(self.cache.exists())

    def test_a_deleted_pandora_toml_is_seen_and_the_cache_then_claims_nothing(self):
        self.ask(['unit'])
        (self.repo / 'pandora.toml').unlink()
        self.assertEqual(enrollment.cache_state(self.repo, self.cache)[0], 'stale')
        self.assertFalse(self.ask(['unit'])['claimed'])
        cache = enrollment.parse(self.cache.read_text())
        self.assertEqual((cache['derived'], cache['claim']), ('none', []))

    def test_an_unenrolled_repository_gets_no_cache(self):
        (self.repo / '.git' / 'pandora-repo').unlink()
        self.assertEqual(self.call(['pnpm', 'unit']).exit, 0)
        self.assertFalse(self.cache.exists())


class RestartHygiene(DaemonCase):
    """A restarted daemon settles every row the last one left live."""

    def row(self, run_id, **fields):
        directory = self.state / 'runs' / run_id
        directory.mkdir(parents=True)
        payload = dict({'id': run_id, 'argv': ['pnpm', 'unit'], 'cwd': str(self.repo),
                        'repo': 'demo', 'job': 'unit', 'started': time.time()}, **fields)
        (directory / 'meta.json').write_text(json.dumps(payload))
        (directory / 'log').touch()
        return directory

    def settled(self, run_id, timeout=10):
        path = self.state / 'runs' / run_id / 'meta.json'
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            meta = json.loads(path.read_text())
            if meta['state'] not in ('queued', 'running'):
                return meta
            time.sleep(0.05)
        self.fail('row %s stayed %s' % (run_id, meta['state']))

    def said(self, run_id):
        frames = [json.loads(line) for line in
                  (self.state / 'runs' / run_id / 'log').read_text().splitlines()]
        return b''.join(base64.b64decode(frame['b64']) for frame in frames
                        if 'b64' in frame).decode()

    def test_a_local_run_is_closed_and_its_process_group_stopped(self):
        child = subprocess.Popen(['sleep', '60'], start_new_session=True)
        self.addCleanup(child.wait)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        self.row('loc1', lane='local', state='running', accepted=time.time(),
                 pgid=child.pid, pgid_started=time.time())
        self.row('loc2', lane='local', state='queued')
        self.daemon.resume_interrupted()
        for run_id in ('loc1', 'loc2'):
            meta = self.settled(run_id)
            self.assertEqual((meta['state'], meta['exit_code']), ('infra_failed', 70))
            self.assertIn('daemon restarted during the run', self.said(run_id))
        self.assertEqual(child.wait(timeout=5), -9)
        self.assertIn('stopped its process group %d' % child.pid, self.said('loc1'))

    def test_a_recycled_process_group_is_left_alone(self):
        child = subprocess.Popen(['sleep', '60'], start_new_session=True)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        # Recorded an hour before this process started: not the run's leader.
        self.row('loc3', lane='local', state='running', pgid=child.pid,
                 pgid_started=time.time() - 3600)
        self.daemon.resume_interrupted()
        self.assertEqual(self.settled('loc3')['state'], 'infra_failed')
        self.assertIsNone(child.poll())

    def test_an_unaccepted_row_the_engine_started_is_adopted(self):
        asked = []

        def lookup(worker, request_id, *, plan=None, fence=True):
            asked.append((request_id, plan, fence))
            return {'ok': True, 'found': True, 'spawned': True, 'run_id': 'r77',
                    'state': 'running'}
        self.row('pre1', state='queued', phase='submit')
        with mock.patch.object(FakeWorker, 'lookup', lookup, create=True):
            self.daemon.resume_interrupted()
            meta = self.settled('pre1')
        self.assertEqual(asked, [('pre1:unit', {'repo': 'demo', 'job': 'unit'}, True)])
        self.assertEqual((meta['state'], meta['remote'], meta['exit_code']),
                         ('passed', 'r77', 0))
        self.assertIn('the worker had started it as r77', self.said('pre1'))

    def test_an_unaccepted_row_the_engine_never_saw_is_closed(self):
        self.row('pre2', state='queued')
        with mock.patch.object(FakeWorker, 'lookup', create=True,
                               side_effect=lambda *a, **k: {'ok': True, 'found': False}):
            self.daemon.resume_interrupted()
            meta = self.settled('pre2')
        self.assertEqual((meta['state'], meta['exit_code']), ('infra_failed', 70))
        self.assertIn('the worker never started it', self.said('pre2'))

    def test_an_unaccepted_row_is_closed_when_the_worker_cannot_be_asked(self):
        self.row('pre3', state='queued')
        with mock.patch.object(FakeWorker, 'lookup', create=True,
                               side_effect=WorkerUnreachable('no route')):
            self.daemon.resume_interrupted()
            meta = self.settled('pre3')
        self.assertEqual(meta['state'], 'infra_failed')
        self.assertIn('could not be asked (no route)', self.said('pre3'))

    def test_an_unaccepted_write_back_the_engine_started_is_stopped_not_adopted(self):
        # Its frozen context is saved only at `accepted`, so following it would
        # finish `passed` with nothing written back.
        stopped = []
        self.row('wb1', state='queued', phase='submit', argv=['pnpm', 'writer', '--update'],
                 job='writer')
        with mock.patch.object(FakeWorker, 'lookup', create=True, return_value={
                'ok': True, 'found': True, 'spawned': True, 'run_id': 'r9'}), \
                mock.patch.object(FakeWorker, 'cancel', create=True,
                                  side_effect=lambda run_id: stopped.append(run_id)):
            self.daemon.resume_interrupted()
            meta = self.settled('wb1')
        self.assertEqual(stopped, ['r9'])
        self.assertEqual((meta['state'], meta['exit_code'], meta['remote']),
                         ('infra_failed', 70, None))
        self.assertIn('frozen context was never saved', self.said('wb1'))

    def test_a_row_still_freezing_or_shipping_is_closed_without_asking(self):
        for phase in ('freeze', 'ship'):
            self.row('early-' + phase, state='queued', phase=phase)
        with mock.patch.object(FakeWorker, 'lookup', create=True,
                               side_effect=AssertionError('asked the worker')):
            self.daemon.resume_interrupted()
            for phase in ('freeze', 'ship'):
                meta = self.settled('early-' + phase)
                self.assertEqual(meta['state'], 'infra_failed')
                self.assertIn('still in %s' % phase, self.said('early-' + phase))

    def test_a_row_the_engine_cannot_account_for_is_uncertain_not_rerun(self):
        self.row('unk1', state='queued', phase='submit')
        self.row('unk2', state='queued', phase='submit')
        answers = {'unk1': WorkerUnreachable('no route'),
                   'unk2': {'ok': True, 'found': True, 'spawned': False, 'state': 'claimed'}}

        def lookup(worker, request_id, **kwargs):
            answer = answers[request_id.split(':')[0]]
            if isinstance(answer, Exception):
                raise answer
            return answer
        with mock.patch.object(FakeWorker, 'lookup', lookup, create=True):
            self.daemon.resume_interrupted()
            for run_id in answers:
                self.settled(run_id)
                said = self.said(run_id)
                self.assertIn('check `pandora ps` before retrying', said)
                self.assertNotIn('rerun it', said)

    def test_an_exception_nobody_named_still_ends_the_row(self):
        # `bundle.call` raises TimeoutExpired, which the takeover did not catch:
        # the thread died and the row stayed live with its client waiting.
        self.row('pre4', state='queued', phase='submit')
        self.row('acc4', state='running', remote='r4', accepted=time.time())
        timeout = subprocess.TimeoutExpired(['ssh'], 60)
        with mock.patch.object(FakeWorker, 'lookup', create=True, side_effect=timeout), \
                mock.patch.object(FakeWorker, 'follow', side_effect=timeout):
            self.daemon.resume_interrupted()
            for run_id in ('pre4', 'acc4'):
                meta = self.settled(run_id)
                self.assertEqual((meta['state'], meta['exit_code']), ('infra_failed', 70))
                self.assertIn('TimeoutExpired', self.said(run_id))

    def test_threads_that_ask_at_once_share_one_worker(self):
        built = []

        class Slow(FakeWorker):
            def __init__(self, host, **kwargs):
                time.sleep(0.1)
                built.append(self)
        self.daemon.worker_factory = Slow
        with self.daemon.workers_lock:   # the health thread may be building one
            self.daemon.workers.clear()
        threads = [threading.Thread(target=self.daemon.worker_for, args=({},))
                   for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(built), 1)

    def test_a_local_run_records_its_process_group(self):
        # `unit` is remote; placed locally, the supervisor spawns it here.
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(30)
        sock.connect(str(self.daemon.socket_path))
        sock.sendall(dump({'v': VERSION, 'op': 'run', 'cwd': str(self.repo),
                           'argv': ['pnpm', 'unit'], 'env': {}, 'tty': False, 'where': 'local'}))
        reader = Reader(sock)
        run_id = None
        while True:
            frame = reader.line()
            if frame is None or frame.get('t') == 'exit':
                break
            if frame.get('t') == 'accepted':
                run_id = frame['run']
        sock.close()
        meta = json.loads((self.state / 'runs' / run_id / 'meta.json').read_text())
        self.assertIsInstance(meta['pgid'], int)
        self.assertAlmostEqual(meta['pgid_started'], meta['started'], delta=30)


class OrphanedRows(DaemonCase):
    """A live row whose daemon has exited never leaves a client waiting forever."""

    row = RestartHygiene.row
    settled = RestartHygiene.settled
    said = RestartHygiene.said

    def attach(self, run_id, timeout=10):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(self.daemon.socket_path))
        sock.sendall(dump({'v': VERSION, 'op': 'attach', 'run': run_id, 'from': 0}))
        reader = Reader(sock)
        first = reader.line()
        code = None
        while True:
            frame = reader.line()
            if frame is None:
                break
            if frame.get('t') == 'exit':
                code = frame['code']
                break
        sock.close()
        return first, code

    def cancel(self, run_id):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(str(self.daemon.socket_path))
        sock.sendall(dump({'v': VERSION, 'op': 'cancel', 'run': run_id}))
        answer = Reader(sock).line()
        sock.close()
        return answer

    def test_attaching_to_a_local_row_nobody_supervises_ends_it(self):
        self.row('orph1', lane='local', state='running', accepted=time.time())
        first, code = self.attach('orph1')
        self.assertEqual((first['t'], first['owned'], code), ('accepted', True, 70))
        self.assertEqual(self.settled('orph1')['state'], 'infra_failed')

    def test_cancel_of_a_local_row_nobody_supervises_ends_it_canceled(self):
        self.row('orph2', lane='local', state='running', accepted=time.time())
        self.assertEqual(self.cancel('orph2')['t'], 'ok')
        meta = self.settled('orph2')
        self.assertEqual((meta['state'], meta['exit_code']), ('cancelled', 130))
        self.assertIn('canceled: the daemon that supervised it has exited', self.said('orph2'))
        # A client attaching afterward replays the exit rather than waiting.
        self.assertEqual(self.attach('orph2')[1], 130)

    def test_cancel_of_a_remote_row_nobody_follows_cancels_it_on_the_worker(self):
        cancels = []

        def follow(worker, run_id, *, should_cancel=None, **kwargs):
            cancels.append((run_id, should_cancel()))
            return {'outcome': 'cancelled', 'cli_exit': 130}, 0
        self.row('orph3', state='running', remote='r55', accepted=time.time())
        with mock.patch.object(FakeWorker, 'follow', follow):
            self.cancel('orph3')
            meta = self.settled('orph3')
        self.assertEqual(cancels, [('r55', True)])
        self.assertEqual((meta['state'], meta['exit_code']), ('cancelled', 130))

    def test_a_row_saved_by_this_daemon_is_not_taken_over(self):
        # Before `accepted` a row is live, not in `runs`, and still driven by
        # its connection thread: attaching must not close it.
        self.row('mine1', state='queued', owner=daemon_module.OWNER)
        self.assertFalse(self.daemon.orphaned(json.loads(
            (self.state / 'runs' / 'mine1' / 'meta.json').read_text())))
        self.assertTrue(self.daemon.orphaned(dict(json.loads(
            (self.state / 'runs' / 'mine1' / 'meta.json').read_text()), owner='gone')))


class OwnPreAccept(DaemonCase):
    """`pandora cancel` and `attach` reach this daemon's own run before `accepted`."""

    cancel = OrphanedRows.cancel
    attach = OrphanedRows.attach

    def queued_id(self, lane, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for path in (self.state / 'runs').glob('*/meta.json'):
                meta = json.loads(path.read_text())
                if meta.get('lane') == lane and meta['state'] == 'queued':
                    return meta['id']
            time.sleep(0.02)
        self.fail('no queued %s row' % lane)

    def test_cancel_of_a_queued_local_run_reaches_the_real_one(self):
        def admit(run_id, *, canceled=None, **kwargs):
            while not canceled():
                time.sleep(0.02)
            return None
        answers = []
        with mock.patch.object(self.daemon.budget, 'admit', side_effect=admit):
            caller = threading.Thread(target=lambda: answers.append(
                self.call(['pnpm', 'unit'])))
            FakeWorker.raises = WorkerUnreachable('down')      # small: falls back to local
            caller.start()
            run_id = self.queued_id('local')
            self.assertEqual(self.cancel(run_id)['t'], 'ok')
            caller.join(10)
        [answer] = answers
        self.assertEqual((answer.error['code'], answer.exit), ('canceled', 130))
        meta = json.loads((self.state / 'runs' / run_id / 'meta.json').read_text())
        self.assertEqual((meta['state'], meta['exit_code']), ('cancelled', 130))
        self.assertNotIn(run_id, self.daemon.runs)       # no stand-in left behind

    def test_cancel_of_a_remote_run_still_submitting_reaches_it_after_accepted(self):
        release, asked = threading.Event(), []

        def submit(worker, **kwargs):
            release.wait(10)
            return Submission('r-late')

        def follow(worker, run_id, *, should_cancel=None, **kwargs):
            asked.append(should_cancel())
            return {'outcome': 'cancelled', 'cli_exit': 130}, 0
        answers = []
        with mock.patch.object(FakeWorker, 'submit', submit), \
                mock.patch.object(FakeWorker, 'follow', follow):
            caller = threading.Thread(target=lambda: answers.append(self.call(['pnpm', 'unit'])))
            caller.start()
            run_id = self.queued_id('remote')
            self.cancel(run_id)
            release.set()
            caller.join(10)
        self.assertEqual(asked, [True])
        self.assertEqual(answers[0].exit, 130)
        self.assertIs(self.daemon.runs[run_id].done.is_set(), True)

    def test_a_live_row_of_ours_that_nothing_holds_is_not_waited_on(self):
        RestartHygiene.row(self, 'mine2', state='running', owner=daemon_module.OWNER)
        first, code = self.attach('mine2')
        self.assertEqual((first['owned'], code), (False, None))
        self.assertNotIn('mine2', self.daemon.runs)


class Unfollowed(unittest.TestCase):
    """A client told that nothing follows its run exits 70 with one line."""

    def serve(self, frame):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / 's.sock')
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(4)
        self.addCleanup(server.close)

        def answer():
            while True:
                try:
                    conn, _ = server.accept()
                except OSError:
                    return
                Reader(conn).line()
                conn.sendall(dump(frame))
                conn.close()
        threading.Thread(target=answer, daemon=True).start()
        return path

    def idle(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        return sock

    def test_the_shim_stops_reattaching_and_says_so(self):
        path = self.serve({'v': VERSION, 't': 'accepted', 'run': 'x1', 'owned': False})
        stream = shim.Stream(path, 'x1', Reader(self.idle()), None)
        with mock.patch.object(shim, 'REATTACH_PAUSE', 0):
            self.assertFalse(stream.reattach())
        self.assertIn('not following', stream.unfollowed)

    def test_the_shim_stops_on_a_run_the_daemon_does_not_have(self):
        path = self.serve({'v': VERSION, 't': 'error', 'code': 'rejected',
                           'msg': 'no such run x2'})
        stream = shim.Stream(path, 'x2', Reader(self.idle()), None)
        with mock.patch.object(shim, 'REATTACH_PAUSE', 0):
            self.assertFalse(stream.reattach())
        self.assertEqual(stream.unfollowed, 'no such run x2')

    def test_pandora_wait_exits_70_at_once(self):
        from pandora import cli
        path = self.serve({'v': VERSION, 't': 'accepted', 'run': 'x3', 'owned': False})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.attach(path, 'x3'), 70)
        self.assertIn('the daemon is not following it', err.getvalue())


class DaemonLog(DaemonCase):
    """Every line the daemon writes to its log starts with a UTC time.

    Each check picks its line out of the captured stderr first: anything else
    in the process may write there meanwhile (a `ResourceWarning` from the
    garbage collector, a daemon thread of another test), and a `^` anchored on
    the whole buffer would miss a well-stamped line that came second.
    """

    STAMP = r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ '

    def test_the_helper_stamps_each_line(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            daemon_module.log('worker down: no route')
        [line] = [line for line in err.getvalue().splitlines(keepends=True)
                  if 'worker down' in line]
        self.assertRegex(line, self.STAMP + 'worker down: no route\n$')

    def test_a_refusal_is_logged_with_its_run_and_cause(self):
        FakeWorker.raises = TransferError('rsync to h failed (255): unexpected end of file')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            answer = self.call(['pnpm', 'surface'])
        [line] = [line for line in err.getvalue().splitlines() if 'refused' in line]
        self.assertRegex(line, self.STAMP + 'run [0-9a-f]{12} refused: transfer-failed: '
                         'rsync to h failed')
        self.assertEqual(answer.exit, 70)

    def test_a_restart_logs_what_it_decided(self):
        directory = self.state / 'runs' / 'loc9'
        directory.mkdir(parents=True)
        (directory / 'meta.json').write_text(json.dumps(
            {'id': 'loc9', 'state': 'running', 'lane': 'local', 'argv': ['pnpm', 'unit']}))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.daemon.resume_interrupted()
        [line] = [line for line in err.getvalue().splitlines() if 'loc9' in line]
        self.assertRegex(line, self.STAMP + 'resume: local run loc9 closed as infra_failed')


class NoRowStaysQueued(DaemonCase):
    """Issue #86: a submission that never reached `accepted` still ends its row.

    The remote path saves a `queued` row before it submits. Every way out
    before `accepted` must finish that row with a state that says what
    happened, or `pandora ps` shows a queued run with no exit forever.
    """

    def metas(self):
        return {meta['id']: meta for meta in
                (json.loads(path.read_text())
                 for path in (self.state / 'runs').glob('*/meta.json'))}

    def test_an_admission_refusal_that_is_not_fallen_back_ends_refused(self):
        FakeWorker.raises = EngineError(json.dumps({'code': 'admission-refused'}))
        answer = self.call(['pnpm', 'surface'])
        self.assertEqual(answer.error['code'], 'fallback-refused')
        rows = list(self.metas().values())
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual((rows[0]['state'], rows[0]['exit_code']), ('refused', 70))

    def test_a_refusal_that_falls_back_names_the_local_run(self):
        FakeWorker.raises = TransferError('rsync died')
        answer = self.call(['pnpm', 'unit'])
        self.assertEqual(answer.exit, 0, answer.error)
        local = answer.accepted['run']
        rows = self.metas()
        self.assertEqual(len(rows), 2, rows)
        remote = next(row for row in rows.values() if row['id'] != local)
        self.assertEqual(remote['state'], 'fell_back')
        self.assertEqual(remote['fell_back_to'], local)
        self.assertIsNotNone(remote['exit_code'])
        self.assertEqual(rows[local]['lane'], 'local')
        # Counted once, by the local run that carries the fallback reason.
        from pandora.client import stats
        report = stats.build(self.state)
        self.assertEqual(report['runs'], 1)
        self.assertEqual(report['fallbacks'], [{'reason': 'transfer-failed', 'count': 1}])

    def test_an_explicit_remote_request_ends_refused(self):
        FakeWorker.raises = WorkerUnreachable('down')
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(30)
        sock.connect(str(self.daemon.socket_path))
        sock.sendall(dump({'v': VERSION, 'op': 'run', 'cwd': str(self.repo),
                           'argv': ['pnpm', 'unit'], 'env': {}, 'tty': False,
                           'where': 'remote'}))
        reader = Reader(sock)
        while True:
            frame = reader.line()
            if frame is None or frame.get('t') == 'error':
                break
        sock.close()
        self.assertEqual(frame['code'], 'placement-unavailable')
        rows = list(self.metas().values())
        self.assertEqual([row['state'] for row in rows], ['refused'])

    def test_an_unexpected_error_before_accepted_ends_infra_failed(self):
        FakeWorker.raises = RuntimeError('a bug nobody anticipated')
        with mock.patch.object(daemon_module.Daemon, 'deny', lambda *a, **k: None):
            self.call(['pnpm', 'unit'], timeout=10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            rows = list(self.metas().values())
            if rows and rows[0]['state'] != 'queued':
                break
            time.sleep(0.05)
        self.assertEqual([(row['state'], row['exit_code']) for row in rows],
                         [('infra_failed', 70)])


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


class LocalCallerGoneWhileQueued(DaemonCase):
    """A caller that leaves while its local job waits for the budget.

    Nothing may start, and nothing may stay held: the reservation and the
    one-run-per-worktree hold are released, and the row says what happened
    rather than staying `running` until the daemon restarts.
    """

    def leave_while_queued(self):
        admit, left = self.daemon.budget.admit, threading.Event()

        def admit_after_the_caller_left(*args, **kwargs):
            left.wait(10)
            return admit(*args, **kwargs)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10)
        sock.connect(str(self.daemon.socket_path))
        with mock.patch.object(self.daemon.budget, 'admit', admit_after_the_caller_left):
            sock.sendall(dump({'v': VERSION, 'op': 'run', 'cwd': str(self.repo),
                               'argv': ['pnpm', 'unit'], 'env': {}, 'tty': False,
                               'where': 'local'}))
            reader = Reader(sock)
            while (reader.line() or {}).get('t') != 'queued':
                pass
            sock.close()
            left.set()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                metas = [json.loads(path.read_text())
                         for path in (self.state / 'runs').glob('*/meta.json')]
                if metas and metas[0]['state'] not in ('queued', 'running'):
                    return metas[0]
                time.sleep(0.05)
        self.fail('the run never left queued/running: %s' % metas)

    def assert_released(self, meta):
        self.assertEqual(meta['state'], 'withdrawn')
        snapshot = self.daemon.budget.snapshot()
        self.assertEqual((snapshot['held_mib'], snapshot['running'], snapshot['worktrees']),
                         (0, [], {}))
        self.assertFalse(self.marker.exists(), 'the job ran for a caller that had gone')

    def test_the_accept_path_checks_the_caller_first(self):
        self.assert_released(self.leave_while_queued())

    def test_a_failed_accepted_send_releases_the_budget(self):
        # The peek can say "alive" and the send still fail: the peer may close
        # between the two. That path must release as well.
        with mock.patch.object(daemon_module, 'client_alive', lambda conn: True):
            self.assert_released(self.leave_while_queued())


class SubdirectoryInvocations(DaemonCase):
    def test_a_subdirectory_is_re_rooted_when_no_argument_names_a_path(self):
        FakeWorker.raises = WorkerUnreachable('down')
        answer = self.call(['pnpm', 'unit', 'fast'], cwd=self.repo / 'sub' / 'deep')
        self.assertEqual(answer.exit, 0, answer.error)
        self.assertTrue(any('worktree root' in line for line in answer.notices),
                        answer.notices)

    def test_a_re_rooted_remote_run_brings_its_outputs_home_to_the_worktree_root(self):
        FakeWorker.collected_into = []
        answer = self.call(['pnpm', 'unit', 'fast'], cwd=self.repo / 'sub' / 'deep')
        self.assertEqual(answer.exit, 0, answer.error)
        self.assertEqual(FakeWorker.collected_into, [str(self.repo)])

    def test_an_argument_that_names_a_path_here_is_refused_and_nothing_runs(self):
        (self.repo / 'sub' / 'x.test.ts').write_text('')
        answer = self.call(['pnpm', 'unit', 'x.test.ts'], cwd=self.repo / 'sub')
        self.assertEqual((answer.error['code'], answer.exit), ('subdirectory', 64))
        self.assertEqual(answer.error['msg'], 'run from the repo root to route')
        self.assertFalse(self.marker.exists(), 'a subdirectory invocation ran')

    def test_a_slash_in_an_argument_is_refused_without_touching_the_disk(self):
        answer = self.call(['pnpm', 'unit', 'src/nothing.test.ts'], cwd=self.repo / 'sub')
        self.assertEqual((answer.error['code'], answer.exit), ('subdirectory', 64))

    def test_a_root_only_repository_passes_a_subdirectory_through(self):
        # Neither re-rooted nor refused: the client runs it as if unclaimed.
        (self.repo / 'pandora.toml').write_text(
            (CONFIG % {'marker': self.marker}).replace('"reroot"', '"passthrough"'))
        answer = self.call(['pnpm', 'unit', 'src/x.test.ts'], cwd=self.repo / 'sub')
        self.assertEqual(answer.error['code'], 'passthrough')
        self.assertIn('worktree root', answer.error['msg'])
        self.assertFalse(self.marker.exists(), 'the daemon ran it')

    def test_an_unenrolled_directory_passes_through_rather_than_refusing(self):
        answer = self.call(['pnpm', 'unit'], cwd=self.root)
        self.assertEqual(answer.error['code'], 'passthrough')
        self.assertIn('enrolled', answer.error['msg'])


class RefusalsExitWithUsage(DaemonCase):
    """#124: a claimed command the job will not take is exit 64, not exit 1.

    Exit 1 would be indistinguishable from the command having run and failed.
    """

    def test_every_classification_refusal_is_exit_64(self):
        for argv in (['pnpm', 'insist', 'extra'],                  # takes no arguments
                     ['pnpm', 'writer', '--update', '--update'],   # one option, twice
                     ['pnpm', 'unit', '../outside']):              # escapes the worktree
            answer = self.call(list(argv))
            self.assertEqual((answer.error['code'], answer.exit), ('rejected', 64), argv)
        self.assertFalse(self.marker.exists(), 'a refused command ran')


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

    def enroll(self, policies, subdirectory=None):
        """A git repository with a marker, and this process inside it."""
        git = self.root / 'repo' / '.git'
        git.mkdir(parents=True)
        (git / 'pandora-enrolled').write_text(enrollment.render(
            socket_path=str(self.state / 'client.sock'), repo='demo',
            claims=[item['prefix'] for item in policies], policies=policies,
            subdirectory=subdirectory))
        os.chdir(self.root / 'repo')

    def run_shim(self, command):
        # A socket path that is not there: the daemon is the missing thing.
        real = self.root / 'fake-pnpm'
        real.write_text('#!/bin/sh\necho ran > %s\n' % self.ran)
        real.chmod(0o755)
        return shim.main(['--sock', str(self.state / 'client.sock'),
                          '--real', str(real), '--state', str(self.state),
                          '--', *command])

    def test_a_small_job_runs_here_when_the_daemon_is_gone(self):
        self.enroll([{'prefix': ['unit'], 'size': 'small', 'fallback': 'auto',
                     'writeback': False}])
        self.assertEqual(self.run_shim(['unit']), 0)
        self.assertTrue(self.ran.exists())
        log = (self.state / 'passthrough.jsonl').read_text()
        self.assertIn('daemon-unreachable', log)

    def test_a_path_typed_in_a_subdirectory_is_refused_without_the_daemon_too(self):
        self.enroll([{'prefix': ['unit'], 'size': 'small', 'fallback': 'auto',
                     'writeback': False}])
        (self.root / 'repo' / 'apps').mkdir()
        os.chdir(self.root / 'repo' / 'apps')
        self.assertEqual(self.run_shim(['unit', 'src/x.test.ts']), 64)
        self.assertFalse(self.ran.exists())
        self.assertEqual(self.run_shim(['unit', 'fast']), 0)
        self.assertTrue(self.ran.exists())

    def test_a_root_only_claim_passes_through_below_the_root_before_any_socket(self):
        # `pandora run` does not come through the POSIX shim, so the client
        # applies its rule. The notice names why; no daemon is mentioned.
        self.enroll([{'prefix': ['unit'], 'size': 'small', 'fallback': 'auto',
                     'writeback': False}], subdirectory='passthrough')
        (self.root / 'repo' / 'apps').mkdir()
        os.chdir(self.root / 'repo' / 'apps')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.run_shim(['unit', 'src/x.test.ts']), 0)
        self.assertTrue(self.ran.exists())
        self.assertIn('worktree root', err.getvalue())
        self.assertNotIn('daemon', err.getvalue())
        [row] = [json.loads(line) for line in
                 (self.state / 'passthrough.jsonl').read_text().splitlines()]
        self.assertEqual(row['reason'], 'passthrough')

    def test_a_root_only_claim_below_the_root_refuses_remote_like_any_unclaimed(self):
        self.enroll([{'prefix': ['unit'], 'size': 'small', 'fallback': 'auto',
                     'writeback': False}], subdirectory='passthrough')
        (self.root / 'repo' / 'apps').mkdir()
        os.chdir(self.root / 'repo' / 'apps')
        os.environ['PANDORA_WHERE'] = 'remote'
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self.run_shim(['unit']), 70)
        finally:
            os.environ.pop('PANDORA_WHERE', None)
        self.assertFalse(self.ran.exists())

    def test_a_large_job_runs_here_too_because_no_daemon_means_no_pandora(self):
        # The owner's rule for a machine without Pandora is "run directly", and
        # a dead daemon is that machine. Refusing here cost an engineer their
        # `pnpm journey` on 2026-09-23 (acme #1536).
        self.enroll([{'prefix': ['surface'], 'size': 'large', 'fallback': 'auto',
                     'writeback': False}])
        self.assertEqual(self.run_shim(['surface']), 0)
        self.assertTrue(self.ran.exists())
        log = (self.state / 'passthrough.jsonl').read_text()
        self.assertIn('daemon-unreachable', log)

    def test_a_marker_without_policies_passes_through_as_well(self):
        self.enroll([])
        self.assertEqual(self.run_shim(['surface']), 0)
        self.assertTrue(self.ran.exists())

    def test_update_is_refused_without_a_daemon(self):
        # --update asks the worker to write files back; with no daemon there
        # is no run at all, and writing in place here skips every check.
        self.enroll([{'prefix': ['unit'], 'size': 'small', 'fallback': 'local',
                     'writeback': True}])
        self.assertEqual(self.run_shim(['unit', '--update']), 70)
        self.assertFalse(self.ran.exists())
        # The same job without the option is an ordinary run and still passes.
        self.assertEqual(self.run_shim(['unit']), 0)
        self.assertTrue(self.ran.exists())

    def test_an_explicit_remote_request_is_still_refused_without_a_daemon(self):
        self.enroll([{'prefix': ['unit'], 'size': 'small', 'fallback': 'auto',
                     'writeback': False}])
        os.environ['PANDORA_WHERE'] = 'remote'
        try:
            self.assertEqual(self.run_shim(['unit']), 70)
        finally:
            os.environ.pop('PANDORA_WHERE', None)
        self.assertFalse(self.ran.exists())

    def test_the_marker_round_trips_through_the_shell_readable_format(self):
        policies = [{'prefix': ['test:unit'], 'size': 'medium', 'fallback': 'refuse',
                     'writeback': True}]
        text = enrollment.render(socket_path='/s', repo='demo',
                                claims=[['test:unit']], policies=policies)
        marker = enrollment.parse(text)
        self.assertEqual(marker['policy'], policies)
        self.assertEqual(enrollment.policy_for(['test:unit', 'x'], marker)['size'], 'medium')
        self.assertIsNone(enrollment.policy_for(['other'], marker))


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

    def test_the_default_is_the_old_behavior_exactly(self):
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

    def test_a_refusal_steers_to_the_local_queue_when_the_job_can_run_there(self):
        for kwargs in ({'size': 'large'},
                       {'size': 'small', 'declared': {'action': 'local', 'on': ['queue-timeout']}}):
            reason = policy.decide(cause='transfer-failed', **kwargs)['reason']
            self.assertIn('PANDORA_WHERE=local', reason, kwargs)
            self.assertNotIn('PANDORA_OFF', reason, kwargs)

    def test_a_write_back_refusal_steers_to_the_worker_not_the_local_queue(self):
        reason = policy.decide(cause='worker-unreachable', size='small',
                               writeback=True)['reason']
        self.assertNotIn('PANDORA_WHERE=local', reason)
        self.assertNotIn('PANDORA_OFF', reason)
        self.assertIn('worker is reachable', reason)

    def test_a_job_the_local_lane_cannot_take_is_refused_at_any_size(self):
        # A medium job falls back on size alone; a sharded one cannot run in
        # the lane at all, so the gate turns the verdict into refuse (gh-120).
        verdict = policy.decide(cause='worker-unreachable', size='medium', local_lane=False)
        self.assertEqual(verdict['action'], 'refuse')
        self.assertIn('local lane', verdict['reason'])
        # The gate applies to a declared fallback = "local" too.
        declared = {'action': 'local', 'on': list(policy.CAUSES)}
        verdict = policy.decide(cause='worker-unreachable', size='small', declared=declared,
                                local_lane=False)
        self.assertEqual(verdict['action'], 'refuse')

    def test_pandora_off_is_named_only_when_the_local_lane_cannot_take_it(self):
        reason = policy.decide(cause='transfer-failed', size='large', local_lane=False)['reason']
        self.assertNotIn('PANDORA_WHERE=local', reason)
        self.assertIn('last resort, PANDORA_OFF=1', reason)

    def test_an_unknown_cause_is_refused_rather_than_guessed(self):
        self.assertEqual(policy.decide(cause='cosmic-ray', size='small')['action'], 'refuse')

    def test_a_declaration_beats_the_size_both_ways(self):
        big = {'action': 'local', 'on': list(policy.CAUSES)}
        small = {'action': 'refuse', 'on': list(policy.CAUSES)}
        self.assertEqual(policy.decide(cause='transfer-failed', size='large',
                                       declared=big)['action'], 'local')
        self.assertEqual(policy.decide(cause='transfer-failed', size='small',
                                       declared=small)['action'], 'refuse')

    def test_a_busy_worker_outranks_size_and_declaration(self):
        big = {'action': 'local', 'on': list(policy.CAUSES)}
        for cause in ('admission-refused', 'queue-timeout'):
            for kwargs in ({'size': 'small'}, {'size': 'large', 'declared': big}):
                verdict = policy.decide(cause=cause, **kwargs)
                self.assertEqual(verdict['action'], 'refuse', (cause, kwargs))
                self.assertIn('PANDORA_WHERE=local', verdict['reason'])

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
        self.assertTrue(answer.err.rstrip().splitlines()[-1].startswith(b'pandora: hint: '),
                        'the hint is the final stderr line')
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
        result = self.result_of(answer.accepted['run'])
        self.assertIn('[sync] include', result['hint'])
        self.assertEqual(result['hint_rule'], 'gitignored')

    def test_a_finished_run_leaves_a_perfetto_trace_beside_its_result(self):
        original = FakeWorker.follow

        def follow(self, run_id, **kwargs):
            return {'outcome': 'passed', 'cli_exit': 0,
                    'durations': {'queue': 0.1, 'execute': 2.0}}, 0

        FakeWorker.follow = follow
        self.addCleanup(setattr, FakeWorker, 'follow', original)
        answer = self.call(['pnpm', 'unit'])
        run_dir = self.state / 'runs' / answer.accepted['run']
        self.assertTrue((run_dir / 'trace.json').exists())
        import json as json_module
        events = json_module.loads((run_dir / 'trace.json').read_text())['traceEvents']
        spans = {event['name']: event for event in events}
        self.assertEqual(spans['execute']['dur'], 2_000_000)
        self.assertTrue(all(event['ph'] == 'X' for event in events))

    def test_a_path_the_snapshot_shipped_is_not_blamed(self):
        ignored = self.repo / 'tmp' / 'fixture.json'
        ignored.parent.mkdir()
        ignored.write_text('{}')
        (self.repo / '.gitignore').write_text('tmp/\n')
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        original_submit, original_follow = FakeWorker.submit, FakeWorker.follow

        def submit(self, **kwargs):
            submission = Submission()
            submission.shipped = frozenset({'tmp/fixture.json'})   # a [sync] include
            return submission

        def follow(self, run_id, *, on_log=None, **k):
            on_log(b"Error: Cannot find module 'tmp/fixture.json'\n")
            return {'outcome': 'command_failed', 'cli_exit': 1, 'job': 'unit',
                    'hint': None}, 0

        FakeWorker.submit, FakeWorker.follow = submit, follow
        self.addCleanup(setattr, FakeWorker, 'submit', original_submit)
        self.addCleanup(setattr, FakeWorker, 'follow', original_follow)
        answer = self.call(['pnpm', 'unit'])
        self.assertEqual(answer.exit, 1)
        self.assertNotIn(b'pandora: hint', answer.err)

    def test_a_passing_run_says_nothing(self):
        answer = self.call(['pnpm', 'unit'])
        self.assertEqual(answer.exit, 0)
        self.assertNotIn(b'pandora: hint', answer.err)
        self.assertIsNone(self.result_of(answer.accepted['run']).get('hint'))


class StatsOverTheSocket(DaemonCase):
    """`pandora stats --since` reaches the daemon and comes back as one report."""

    def ask(self, request):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(30)
        sock.connect(str(self.daemon.socket_path))
        sock.sendall(dump(dict({'v': VERSION}, **request)))
        try:
            return Reader(sock).line()
        finally:
            sock.close()

    def test_a_windowed_report_counts_a_run_and_carries_the_worker(self):
        self.assertEqual(self.call(['pnpm', 'unit']).exit, 0)
        frame = self.ask({'op': 'stats', 'since': '24h'})
        data = frame['data']
        self.assertEqual(data['window'], '24h')
        self.assertEqual(data['runs'], 1)
        self.assertEqual(data['by_job'][0]['job'], 'unit')
        self.assertEqual(data['worker']['worker'], 'reachable')
        self.assertEqual(data['queue_wait_seconds']['remote']['n'], 1)
