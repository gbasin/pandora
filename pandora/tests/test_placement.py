"""`--local`/`--remote` and `PANDORA_WHERE`: an override that keeps Pandora in the loop.

The daemon is real, the local lane is real, the classifier is real; the worker
is the recording fake from `test_fallback`, so a test can say what was
submitted and what was not. Every refusal test also asserts the marker file is
absent, because "refused" means nothing ran anywhere.
"""
import contextlib
import io
import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pandora import cli
from pandora.client import enrolment, placement, shim, stats as statistics
from pandora.client.protocol import Reader, VERSION, dump
from pandora.errors import Refused, WorkerUnreachable
from pandora.exits import INFRA
from pandora.tests.test_fallback import Answer, DaemonCase, FakeWorker, Submission

HERE = Path(__file__).resolve().parents[2]

CONFIG = '''
version = 1
[repo]
name = "demo"
entrypoints = ["pnpm"]
[[jobs]]
id = "unit"
size = "small"
args = "optional"
forms = [{ prefix = ["unit"] }]
run = { argv = ["sh", "-c", "echo ran-unit > %(marker)s", "--", "{args}"] }
[[jobs]]
id = "journey"
size = "large"
fallback = "refuse"
args = "optional"
forms = [{ prefix = ["journey"] }]
options = [{ name = "--update", sets = "update", forward = true, writeback = true }]
outputs = [
  { kind = "artifacts", paths = ["reports"] },
  { kind = "writeback", requires_option = "update", paths = ["fixtures"] },
]
run = { argv = ["sh", "-c", "echo ran-journey \\"$*\\" > %(marker)s", "--", "{args}"] }
[[jobs]]
id = "node"
where = "local"
size = "small"
args = "optional"
forms = [{ prefix = ["node"] }]
run = { argv = ["sh", "-c", "echo ran-node > %(marker)s", "--", "{args}"] }
[[jobs]]
id = "evidence"
where = "local"
size = "small"
forms = [{ prefix = ["evidence"] }]
outputs = [{ kind = "evidence", paths = ["tmp/result.json"] }]
run = { argv = ["sh", "-c", "echo ran-evidence > %(marker)s"] }
[[jobs]]
id = "stack"
where = "local"
singleton = true
size = "large"
forms = [{ prefix = ["dev:stack"] }]
run = { argv = ["sh", "-c", "echo ran-stack > %(marker)s"] }
[[jobs]]
id = "surface"
size = "medium"
args = "optional"
forms = [{ prefix = ["surface"] }]
run = { argv = ["sh", "-c", "echo ran-surface > %(marker)s", "--", "{args}"] }
[jobs.shards]
strategy = "argv"
template = "--shard={i}/{n}"
default = 2
[worker]
base_image = "images:ubuntu/26.04"
'''


class RecordingWorker(FakeWorker):
    """Remembers every plan it was handed, so "nothing was submitted" is checkable."""

    plans = []

    def submit(self, **kwargs):
        RecordingWorker.plans.append(kwargs['plan'])
        if FakeWorker.raises is not None:
            raise FakeWorker.raises
        return Submission()


class PlacementCase(DaemonCase):
    def setUp(self):
        super().setUp()
        RecordingWorker.plans = []
        (self.repo / 'pandora.toml').write_text(CONFIG % {'marker': self.marker})
        self.daemon.worker_factory = RecordingWorker
        self.daemon.workers.clear()

    def place(self, argv, where):
        """`call`, with the placement field the shim lifts out of PANDORA_WHERE."""
        answer = Answer()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(60)
        sock.connect(str(self.daemon.socket_path))
        sock.sendall(dump({'v': VERSION, 'op': 'run', 'cwd': str(self.repo), 'argv': argv,
                           'env': {}, 'tty': False, 'where': where}))
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
                    answer.error, answer.exit = frame, frame.get('exit')
                    return answer
                elif kind == 'accepted':
                    answer.accepted = frame
                elif kind == 'exit':
                    answer.exit = frame['code']
                    return answer
        finally:
            sock.close()

    def meta_of(self, run_id):
        return json.loads((self.state / 'runs' / run_id / 'meta.json').read_text())


class LocalOverride(PlacementCase):
    def test_a_remote_job_runs_in_the_local_lane_and_says_why(self):
        answer = self.place(['pnpm', 'unit'], 'local')
        self.assertEqual(answer.exit, 0, answer.error)
        self.assertEqual(answer.accepted['lane'], 'local')
        self.assertEqual(answer.accepted['reason'], 'override:local')
        self.assertEqual(self.marker.read_text().strip(), 'ran-unit')
        self.assertEqual(RecordingWorker.plans, [], 'a --local run was submitted')
        meta = self.meta_of(answer.accepted['run'])
        self.assertEqual(meta['lane'], 'local')
        self.assertEqual(meta['placement'], {'where': 'local', 'declared': 'remote',
                                             'override': 'local', 'overridden': True})
        result = self.result_of(answer.accepted['run'])
        self.assertTrue(result['placement']['overridden'])
        self.assertEqual(result['reason'], 'override:local')
        # The local lane's own accounting, not a bare exec: the size class and
        # the reservation are the job's.
        self.assertEqual(result['size_class'], 'small')

    def test_update_runs_locally_and_writes_in_place(self):
        # `journey` is large and declares fallback = "refuse"; neither applies,
        # because this is not a fallback, and --update is fine where it writes in place.
        answer = self.place(['pnpm', 'journey', 'S0-01', '--update'], 'local')
        self.assertEqual(answer.exit, 0, answer.error)
        self.assertEqual(answer.accepted['lane'], 'local')
        self.assertEqual(self.marker.read_text().strip(), 'ran-journey S0-01 --update')
        self.assertEqual(RecordingWorker.plans, [])
        self.assertEqual(self.result_of(answer.accepted['run'])['size_class'], 'large')

    def test_a_sharded_job_is_refused_with_64_and_nothing_runs(self):
        answer = self.place(['pnpm', 'surface', 'desk'], 'local')
        self.assertEqual((answer.error['code'], answer.exit), ('placement', 64))
        self.assertIn('sharded', answer.error['msg'])
        self.assertIn('PANDORA_OFF=1', answer.error['msg'])
        self.assertFalse(self.marker.exists())
        self.assertEqual(RecordingWorker.plans, [])

    def test_local_on_a_local_job_changes_nothing_but_is_recorded(self):
        answer = self.place(['pnpm', 'node', 'x'], 'local')
        self.assertEqual(answer.exit, 0, answer.error)
        self.assertEqual(answer.accepted['reason'], '')
        self.assertEqual(self.meta_of(answer.accepted['run'])['placement'],
                         {'where': 'local', 'declared': 'local', 'override': 'local',
                          'overridden': False})


class RemoteOverride(PlacementCase):
    def test_a_local_job_is_submitted_with_a_synthetic_repository(self):
        answer = self.place(['pnpm', 'node', 'x'], 'remote')
        self.assertEqual(answer.exit, 0, answer.error)
        self.assertIsNone(answer.accepted.get('lane'))
        [plan] = RecordingWorker.plans
        self.assertEqual((plan['job'], plan['where'], plan['git']), ('node', 'remote', 'synthetic'))
        meta = self.meta_of(answer.accepted['run'])
        self.assertEqual((meta['lane'], meta['reason']), ('remote', 'override:remote'))
        self.assertTrue(meta['placement']['overridden'])
        self.assertTrue(self.result_of(answer.accepted['run'])['placement']['overridden'])
        self.assertFalse(self.marker.exists(), 'the fake worker does not run anything')

    def test_evidence_outputs_are_refused_with_64(self):
        answer = self.place(['pnpm', 'evidence'], 'remote')
        self.assertEqual((answer.error['code'], answer.exit), ('placement', 64))
        self.assertIn('evidence', answer.error['msg'])
        self.assertEqual(RecordingWorker.plans, [])
        self.assertFalse(self.marker.exists())

    def test_a_singleton_is_refused_with_64(self):
        answer = self.place(['pnpm', 'dev:stack'], 'remote')
        self.assertEqual((answer.error['code'], answer.exit), ('placement', 64))
        self.assertIn('singleton', answer.error['msg'])
        self.assertFalse(self.marker.exists())

    def test_an_unreachable_worker_is_an_error_never_a_fallback(self):
        # `unit` is small, so without the override this exact failure admits it
        # into the local lane (test_fallback). With it, nothing runs anywhere.
        FakeWorker.raises = WorkerUnreachable('no route to host')
        answer = self.place(['pnpm', 'unit'], 'remote')
        self.assertEqual((answer.error['code'], answer.exit), ('placement-unavailable', 70))
        self.assertIn('--remote was asked for', answer.error['msg'])
        self.assertFalse(self.marker.exists(), 'an explicit --remote fell back to this Mac')

    def test_a_worker_known_down_is_the_same_error_without_submitting(self):
        FakeWorker.health_raises = WorkerUnreachable('gone')
        self.daemon.health.poll()
        answer = self.place(['pnpm', 'node'], 'remote')
        self.assertEqual((answer.error['code'], answer.exit), ('placement-unavailable', 70))
        self.assertEqual(RecordingWorker.plans, [])
        self.assertFalse(self.marker.exists())

    def test_remote_on_a_remote_job_moves_nothing_and_still_never_falls_back(self):
        FakeWorker.raises = WorkerUnreachable('down')
        answer = self.place(['pnpm', 'unit'], 'remote')
        self.assertEqual(answer.exit, 70)
        self.assertFalse(self.marker.exists())

    def test_no_override_is_recorded_as_none(self):
        answer = self.call(['pnpm', 'unit'])
        self.assertEqual(answer.exit, 0, answer.error)
        self.assertEqual(self.meta_of(answer.accepted['run'])['placement'],
                         {'where': 'remote', 'declared': 'remote', 'override': None,
                          'overridden': False})


class Decide(unittest.TestCase):
    JOB = {'id': 'j', 'where': 'remote', 'shards': None, 'singleton': False, 'outputs': []}

    def test_parse_accepts_two_words_and_empty(self):
        self.assertEqual(placement.parse('local'), 'local')
        self.assertEqual(placement.parse(' remote '), 'remote')
        self.assertIsNone(placement.parse(''))
        self.assertIsNone(placement.parse(None))
        with self.assertRaises(ValueError):
            placement.parse('Local')

    def test_a_refusal_carries_64(self):
        job = dict(self.JOB, shards={'plan': ['x']})
        with self.assertRaises(Refused) as caught:
            placement.decide(job, {'where': 'remote'}, 'local')
        self.assertEqual((caught.exception.code, caught.exception.exit), ('placement', 64))
        self.assertIn('plan step', str(caught.exception))


def capture(function, *args):
    out, err = (io.TextIOWrapper(io.BytesIO(), encoding='utf-8') for _ in range(2))
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = function(*args)
    out.flush()
    err.flush()
    return code, out.buffer.getvalue().decode(), err.buffer.getvalue().decode()


class TheClient(PlacementCase):
    """The flag, the variable, and which one wins -- through `pandora run` and the shim client."""

    def setUp(self):
        super().setUp()
        here = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, here)
        self.real = self.root / 'fake-pnpm'
        self.real.write_text('#!/bin/sh\necho "real $*" > %s\n' % self.marker)
        self.real.chmod(0o755)

    def pandora(self, *argv, env=None):
        with mock.patch.dict(os.environ, env or {}, clear=False):
            if 'PANDORA_WHERE' not in (env or {}):
                os.environ.pop('PANDORA_WHERE', None)
            return capture(cli.main, ['--state', str(self.state),
                                      '--config', str(self.root / 'config.toml'), *argv])

    def test_run_local_flag(self):
        code, _, err = self.pandora('run', '--local', '--real', str(self.real), '--', 'unit')
        self.assertEqual(code, 0, err)
        self.assertIn('in the local lane', err)
        self.assertEqual(self.marker.read_text().strip(), 'ran-unit')

    def test_the_variable_means_the_same(self):
        code, _, err = self.pandora('run', '--real', str(self.real), '--', 'unit',
                                    env={'PANDORA_WHERE': 'local'})
        self.assertEqual(code, 0, err)
        self.assertIn('in the local lane', err)

    def test_the_flag_wins_over_the_variable_even_a_bad_one(self):
        code, _, err = self.pandora('run', '--local', '--real', str(self.real), '--', 'unit',
                                    env={'PANDORA_WHERE': 'somewhere'})
        self.assertEqual(code, 0, err)

    def test_a_bad_variable_is_64_and_nothing_is_asked(self):
        code, _, err = self.pandora('run', '--real', str(self.real), '--', 'unit',
                                    env={'PANDORA_WHERE': 'laptop'})
        self.assertEqual(code, 64)
        self.assertIn('PANDORA_WHERE=laptop is not a placement', err)
        self.assertFalse(self.marker.exists())
        self.assertFalse((self.state / 'runs').exists() and any((self.state / 'runs').iterdir()))

    def test_the_two_flags_exclude_each_other(self):
        with self.assertRaises(SystemExit):
            self.pandora('run', '--local', '--remote', '--', 'unit')

    def test_a_remote_override_on_an_unclaimed_command_never_runs_locally(self):
        code, _, err = self.pandora('run', '--remote', '--real', str(self.real), '--',
                                    'why', 'react')
        self.assertEqual(code, INFRA, err)
        self.assertIn('cannot pass through to local execution', err)
        self.assertFalse(self.marker.exists())
        self.assertEqual(statistics.read_passthrough(self.state), [])

    def test_stats_counts_overrides_by_direction(self):
        self.pandora('run', '--local', '--real', str(self.real), '--', 'unit')
        self.pandora('run', '--local', '--real', str(self.real), '--', 'node')
        self.pandora('run', '--remote', '--real', str(self.real), '--', 'node')
        report = statistics.build(self.state)
        self.assertEqual(report['overrides']['local'], {'asked': 2, 'moved': 1, 'unclaimed': 0})
        self.assertEqual(report['overrides']['remote'], {'asked': 1, 'moved': 1, 'unclaimed': 0})
        self.assertIn('overrides: local x2 (1 moved, 0 on unclaimed), remote x1',
                      statistics.render(report))

    def test_result_says_it_was_placed_by_override(self):
        code, _, err = self.pandora('run', '--local', '--detach', '--real', str(self.real),
                                    '--', 'unit')
        self.assertEqual(code, 0, err)
        run_id = [line for line in err.splitlines() if 'local lane' in line][0].split()[2]
        self.pandora('wait', run_id)
        code, out, _ = self.pandora('result', run_id)
        self.assertIn('placed local by override; the job says remote', out)


class Help(unittest.TestCase):
    def test_the_override_is_on_the_one_screen(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(['--help'])
        text = out.getvalue()
        self.assertLessEqual(len(text.splitlines()), 50)
        self.assertIn('PANDORA_WHERE=local|remote', text)
        self.assertIn('never a fallback', text)


class WithoutADaemon(unittest.TestCase):
    def test_remote_is_refused_and_nothing_runs(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            git = root / 'repo' / '.git'
            git.mkdir(parents=True)
            (git / 'pandora-enrolled').write_text(enrolment.render(
                socket_path=str(root / 'nothing.sock'), repo='demo', claims=[['unit']],
                policies=[{'prefix': ['unit'], 'size': 'small', 'fallback': 'auto',
                           'writeback': False}]))
            ran = root / 'ran'
            real = root / 'pnpm'
            real.write_text('#!/bin/sh\necho ran > %s\n' % ran)
            real.chmod(0o755)
            here = os.getcwd()
            os.chdir(root / 'repo')
            try:
                code, _, err = capture(shim.main, ['--sock', str(root / 'nothing.sock'),
                                                   '--real', str(real), '--state', str(root),
                                                   '--where', 'remote', '--', 'unit'])
            finally:
                os.chdir(here)
            self.assertEqual(code, 70)
            self.assertIn('--remote was asked for', err)
            self.assertFalse(ran.exists(), 'a --remote with no daemon ran here')


class ThroughThePosixShim(unittest.TestCase):
    """The shell half: an unclaimed command with PANDORA_WHERE set is logged, not routed."""

    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        root = Path(home.name)
        self.repo, self.state, fake = root / 'repo', root / 'state', root / 'fake'
        for directory in (self.repo, self.state, fake):
            directory.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (fake / 'pnpm').write_text('#!/bin/sh\necho "real $*"\n')
        (fake / 'pnpm').chmod(0o755)
        (self.repo / '.git' / 'pandora-enrolled').write_text(enrolment.render(
            socket_path=str(self.state / 'client.sock'), repo='demo',
            claims=[['journey']], heavy=enrolment.heavy_forms([['journey']]),
            home=str(HERE)))
        self.env = dict(os.environ, PATH='%s:%s' % (HERE / 'bin', fake) + ':/usr/bin:/bin')
        for name in ('PANDORA_OFF', 'PANDORA_ROUTE_DEPTH', 'PANDORA_WHERE'):
            self.env.pop(name, None)

    def pnpm(self, *argv, where=None):
        env = dict(self.env, **({'PANDORA_WHERE': where} if where is not None else {}))
        return subprocess.run(['sh', str(HERE / 'bin' / 'pnpm'), *argv], cwd=self.repo,
                              env=env, capture_output=True, text=True, timeout=30)

    def rows(self):
        path = self.state / 'passthrough.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] \
            if path.exists() else []

    def test_a_light_command_with_an_override_is_logged_as_ignored(self):
        proc = self.pnpm('why', 'react', where='local')
        self.assertEqual((proc.returncode, proc.stdout), (0, 'real why react\n'))
        [row] = self.rows()
        self.assertEqual((row['reason'], row['override']), ('override-ignored', 'local'))
        report = statistics.build(self.state)
        self.assertEqual(report['overrides']['local']['unclaimed'], 1)
        # Not a heavy command, so not in the "local, not routed" table.
        self.assertEqual(report['passthrough'], [])

    def test_a_heavy_command_with_an_override_keeps_its_reason(self):
        self.pnpm('build', where='remote')
        [row] = self.rows()
        self.assertEqual((row['reason'], row['override']), ('unclaimed', 'remote'))

    def test_a_bad_value_is_64(self):
        proc = self.pnpm('why', where='lcoal')
        self.assertEqual(proc.returncode, 64)
        self.assertIn('PANDORA_WHERE=lcoal', proc.stderr)
        self.assertEqual(proc.stdout, '')

    def test_without_the_variable_a_light_command_costs_nothing(self):
        self.pnpm('why')
        self.assertEqual(self.rows(), [])


if __name__ == '__main__':
    unittest.main()
