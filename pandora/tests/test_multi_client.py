"""Two client daemons, two Macs, one worker: nothing collides, and every run says whose it is.

Issue #73. The owner plus a few trusted engineers share one worker and one
engine root, each Mac running its own client daemon. What must hold, tested
here against a real engine (`service.main` on a scratch root, the supervisor
spawn stubbed) and the real source-cache and turbo-cache writers:

* ledger rows, attempt directories and results never collide or overwrite each
  other, even for one tree submitted by both;
* a request id one client used is never attached to by another;
* memory admission counts every client's runs against one budget;
* a cancel, a lookup or a retry from one client never touches the other's run;
* source staging and the turbo cache stay content-addressed and atomic under
  concurrent writers;
* cleanup (reconcile, retention) acts on each row's facts, not on who owns it.

Fair share between clients is out of scope (#73 keeps it open).
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from pandora import cli
from pandora.client import settings
from pandora.client import worker as worker_module
from pandora.engine import fanout, runner, service
from pandora.engine.ledger import Ledger
from pandora.engine.turbocache import Store
from pandora.errors import ConfigError, EngineError
from pandora.snapshot import transfer
from pandora.tests.test_engine import PLAN, FakeDriver, claim
from pandora.tests.test_fallback import DaemonCase
from pandora.tests.test_lost_submit import SUBMIT_PLAN, LossyWorker
from pandora.tests.test_transfer_concurrency import _REAL_RUN, LocalLink

ALICE, BOB = 'alice@studio', 'bob@laptop'


def client(root, name):
    """A real engine behind a transport that never loses anything, as `name`."""
    worker = ConcurrentClient(root, lose=None)
    worker.client = name
    return worker


REPLIES = threading.local()
EMIT = service.emit


def emit(payload):
    """`service.emit`, also kept per thread: two threads cannot share one redirected stdout."""
    REPLIES.last = payload
    return EMIT(payload)


class ConcurrentClient(LossyWorker):
    """`submit` runs the engine's own body on this thread with its own ledger connection.

    On a worker each engine call is its own process. In one test process two
    threads would fight over `sys.stdin` and `sys.stdout`, so `submit` skips the
    CLI wrapper and reads its reply per thread. Every other verb goes through
    `service.main` as before.
    """

    def engine(self, argv, stdin=None, **kwargs):
        if argv[0] != 'submit':
            return super().engine(argv, stdin=stdin, **kwargs)
        self.calls.append('submit')
        import argparse
        import sys
        paths, ledger = service.open_ledger(str(self.root_dir))
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                service.submit(argparse.Namespace(root=str(self.root_dir),
                                                  python=sys.executable),
                               paths, ledger, json.loads(stdin))
        finally:
            ledger.close()
        return REPLIES.last


class TwoClientsOneEngine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'engine'
        self.tree = Path(self.tmp.name) / 'tree'
        self.tree.mkdir()
        self.spawned, self.waiters = [], []
        lock = threading.Lock()

        def spawn(root, run_id, python=None):
            with lock:
                self.spawned.append(run_id)
            return 4242

        def spawn_waiter(root, run_id, python=None):
            with lock:
                self.waiters.append(run_id)
            return 4343
        for patch in (
                mock.patch.dict(os.environ, {'PANDORA_BUDGET_MIB': '8192'}),
                mock.patch.object(runner, 'spawn', spawn),
                mock.patch.object(runner, 'spawn_waiter', spawn_waiter),
                mock.patch.object(runner, 'disk_headroom', lambda paths, driver=None: {'ok': True}),
                mock.patch.object(worker_module.snapshot, 'freeze',
                                  lambda *a, **k: ([{'path': 'a'}], [], 'input-a')),
                mock.patch.object(worker_module.transfer, 'send',
                                  lambda *a, **k: {'path': str(self.tree), 'reused': True}),
                mock.patch.object(service, 'emit', emit)):
            patch.start()
            self.addCleanup(patch.stop)
        self.alice, self.bob = client(self.root, ALICE), client(self.root, BOB)

    def submit(self, worker, request_id, plan=SUBMIT_PLAN):
        return worker.submit(plan=plan, worktree=str(self.tree), request_id=request_id)

    def rows(self):
        ledger = Ledger(runner.Paths(self.root).ensure().ledger)
        try:
            return {row['run_id']: dict(row) for row in ledger.recent(limit=100)}
        finally:
            ledger.close()

    def small(self):
        return dict(SUBMIT_PLAN, size='small')

    def test_concurrent_submissions_of_one_tree_get_their_own_rows_and_attempts(self):
        results, errors = {}, []

        def go(worker, request_id):
            try:
                results[worker.client] = self.submit(worker, request_id, self.small())
            except BaseException as error:              # noqa: BLE001 - reported below
                errors.append(error)
        threads = [threading.Thread(target=go, args=(self.alice, 'a1:suite')),
                   threading.Thread(target=go, args=(self.bob, 'b1:suite'))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        alice, bob = results[ALICE].run_id, results[BOB].run_id
        self.assertNotEqual(alice, bob)
        self.assertEqual(sorted(self.spawned), sorted([alice, bob]))
        rows = self.rows()
        self.assertEqual((rows[alice]['client'], rows[bob]['client']), (ALICE, BOB))
        self.assertEqual(rows[alice]['input_id'], rows[bob]['input_id'])   # one tree, shared
        paths = runner.Paths(self.root)
        for run_id, name in ((alice, ALICE), (bob, BOB)):
            request = json.loads((paths.attempt(run_id) / 'request.json').read_text())
            self.assertEqual(request['client'], name)

    def test_a_request_id_another_client_holds_is_refused_never_attached(self):
        mine = self.submit(self.alice, 'same:suite')
        with self.assertRaises(EngineError) as caught:
            self.submit(self.bob, 'same:suite')
        self.assertEqual(json.loads(str(caught.exception))['code'], 'request-collision')
        self.assertEqual(self.spawned, [mine.run_id])
        # The owner resubmitting its own id is still the idempotent duplicate.
        again = self.submit(self.alice, 'same:suite')
        self.assertTrue(again.duplicate)
        self.assertEqual(again.run_id, mine.run_id)

    def test_memory_admission_caps_the_total_across_clients(self):
        # 8 GiB budget; a cold medium job reserves its 4 GiB ceiling. The third
        # run queues behind both -- whoever's they are -- and starts nothing.
        self.submit(self.alice, 'a1:suite')
        self.submit(self.alice, 'a2:suite')
        queued = self.submit(self.bob, 'b1:suite')
        self.assertEqual(queued.state, 'queued')
        self.assertEqual(queued.queued['position'], 1)
        self.assertEqual(queued.queued['running'], 2)
        self.assertEqual(len(self.spawned), 2)
        self.assertEqual(self.waiters, [queued.run_id])
        self.assertEqual(self.rows()[queued.run_id]['client'], BOB)

    def test_a_cancel_from_one_client_never_cancels_the_others_run(self):
        run = self.submit(self.bob, 'b1:suite').run_id
        answer = self.alice.cancel(run)
        self.assertEqual((answer['ok'], answer['code'], answer['client']),
                         (False, 'not-yours', BOB))
        self.assertEqual(self.rows()[run]['cancel_requested'], 0)
        anonymous = client(self.root, None)             # a client that names no one
        self.assertEqual(anonymous.cancel(run)['code'], 'not-yours')
        self.assertEqual(self.rows()[run]['cancel_requested'], 0)
        self.assertTrue(self.bob.cancel(run)['requested'])
        self.assertEqual(self.rows()[run]['cancel_requested'], 1)

    def test_a_row_from_before_attribution_is_anyones_to_cancel(self):
        ledger = Ledger(runner.Paths(self.root).ensure().ledger)
        claim(ledger, request_id='old:suite', run_id='rold')
        ledger.close()
        self.assertTrue(self.alice.cancel('rold')['requested'])

    def test_a_lookup_never_attaches_to_or_fences_another_clients_request(self):
        self.submit(self.bob, 'b1:suite')
        before = len(self.rows())
        answer = self.alice.lookup('b1:suite', plan=SUBMIT_PLAN)
        self.assertEqual((answer['ok'], answer['code']), (False, 'request-collision'))
        self.assertEqual(len(self.rows()), before)
        mine = self.bob.lookup('b1:suite', plan=SUBMIT_PLAN)
        self.assertTrue(mine['found'])

    def test_a_retry_of_another_clients_run_is_refused(self):
        run = self.submit(self.bob, 'b1:suite').run_id
        ledger = Ledger(runner.Paths(self.root).ensure().ledger)
        ledger.finish(run, outcome='infra_failed', exit_code=None)
        ledger.close()
        with self.assertRaises(EngineError) as caught:
            self.alice.resubmit(run, request_id='a:retry')
        self.assertEqual(json.loads(str(caught.exception))['code'], 'not-yours')
        retried = self.bob.resubmit(run, request_id='b1:suite:retry')
        self.assertEqual(self.rows()[retried.run_id]['client'], BOB)

    def test_a_gateway_pin_outranks_the_name_the_client_sent(self):
        # The forced command's whole point: the key decides, not the request.
        with mock.patch.dict(os.environ, {'PANDORA_GATEWAY_CLIENT': BOB}):
            mine = self.submit(self.alice, 'a1:suite', self.small())
        self.assertEqual(self.rows()[mine.run_id]['client'], BOB)
        # And reads scope to the pin: under it, Alice's own earlier run reads
        # as someone else's.
        other = self.submit(self.alice, 'a2:suite', self.small()).run_id
        with mock.patch.dict(os.environ, {'PANDORA_GATEWAY_CLIENT': BOB}):
            answer = self.alice.cancel(other)
        self.assertEqual(answer['code'], 'not-yours')
        self.assertEqual(self.rows()[other]['cancel_requested'], 0)

    def test_a_read_of_another_clients_run_is_not_yours(self):
        run = self.submit(self.bob, 'b1:suite', self.small()).run_id
        paths = runner.Paths(self.root)
        (paths.log(run).parent.mkdir(parents=True, exist_ok=True))
        paths.log(run).write_text('bob output\n')
        for verb in ('status', 'result', 'wait'):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = service.main(['--root', str(self.root), verb,
                                     '--run', run, '--client', ALICE])
            self.assertEqual(code, 0, verb)        # the refusal is still JSON
            self.assertEqual(json.loads(out.getvalue())['code'], 'not-yours', verb)
        # `logs` is a byte stream: its refusal is a nonzero exit and stderr.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = service.main(['--root', str(self.root), 'logs',
                                 '--run', run, '--client', ALICE])
        self.assertEqual(code, 1)
        self.assertIn('not-yours', err.getvalue())
        # Bob's own reads pass, and a nameless caller cannot read either.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            service.main(['--root', str(self.root), 'status',
                          '--run', run, '--client', BOB])
        self.assertTrue(json.loads(out.getvalue())['ok'])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = service.main(['--root', str(self.root), 'logs', '--run', run])
        self.assertEqual(code, 1)
        self.assertIn('not-yours', err.getvalue())

    def test_a_run_from_before_attribution_is_anyones_to_read(self):
        ledger = Ledger(runner.Paths(self.root).ensure().ledger)
        claim(ledger, request_id='old:suite', run_id='rold')
        ledger.finish('rold', outcome='passed', exit_code=0)
        ledger.close()
        paths = runner.Paths(self.root)
        paths.attempt('rold').mkdir(parents=True, exist_ok=True)
        paths.result('rold').write_text(json.dumps({'run_id': 'rold', 'outcome': 'passed'}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            service.main(['--root', str(self.root), 'result',
                          '--run', 'rold', '--client', ALICE])
        self.assertTrue(json.loads(out.getvalue())['ok'])

    def test_a_worker_floor_refuses_engines_older_than_it(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'min_engine_version').write_text('%d\n' % (service.ENGINE_VERSION + 1))
        with self.assertRaises(EngineError) as caught:
            self.submit(self.alice, 'a1:suite', self.small())
        self.assertEqual(json.loads(str(caught.exception))['code'], 'engine-version')
        # A fence is a claim: below the floor it does not write either.
        answer = self.alice.lookup('never:seen', plan=SUBMIT_PLAN)
        self.assertEqual(answer['code'], 'engine-version')
        self.assertEqual(self.rows(), {})
        # At the floor, admitted; above it the engine simply runs.
        (self.root / 'min_engine_version').write_text(str(service.ENGINE_VERSION))
        mine = self.submit(self.alice, 'a2:suite', self.small())
        self.assertTrue(mine.run_id)

    def test_a_retry_of_a_legacy_run_belongs_to_whoever_retried_it(self):
        ledger = Ledger(runner.Paths(self.root).ensure().ledger)
        claim(ledger, request_id='old:suite', run_id='rold', source_path=str(self.tree))
        ledger.finish('rold', outcome='infra_failed', exit_code=None)
        ledger.close()
        paths = runner.Paths(self.root)
        paths.attempt('rold').mkdir(parents=True, exist_ok=True)
        (paths.attempt('rold') / 'request.json').write_text(json.dumps({
            'request_id': 'old:suite', 'input_id': 'input-a', 'source_path': str(self.tree),
            'plan': SUBMIT_PLAN}))
        retried = self.alice.resubmit('rold', request_id='old:suite:retry')
        self.assertEqual(self.rows()[retried.run_id]['client'], ALICE)
        self.assertEqual(self.bob.cancel(retried.run_id)['code'], 'not-yours')

    def test_the_engine_counts_runs_by_client(self):
        self.submit(self.alice, 'a1:suite', self.small())
        self.submit(self.bob, 'b1:suite', self.small())
        self.submit(self.bob, 'b2:suite', self.small())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            service.main(['--root', str(self.root), 'stats'])
        stats = json.loads(out.getvalue())
        self.assertEqual(stats['by_client'], {ALICE: 1, BOB: 2})
        self.assertEqual(stats['engine'], service.ENGINE_VERSION)


class OpeningOneLedgerAtOnce(unittest.TestCase):
    def test_two_processes_opening_a_fresh_ledger_both_succeed(self):
        import subprocess
        import sys
        import time
        code = ('import sys, time\n'
                'from pandora.engine.ledger import Ledger\n'
                'while time.time() < float(sys.argv[2]): pass\n'
                'Ledger(sys.argv[1]).close()\n')
        here = str(Path(__file__).resolve().parents[2])
        for _ in range(5):
            with tempfile.TemporaryDirectory() as tmp:
                start = str(time.time() + 0.3)
                procs = [subprocess.Popen([sys.executable, '-c', code, str(Path(tmp) / 'l.db'),
                                           start], cwd=here, stderr=subprocess.PIPE, text=True)
                         for _ in range(3)]
                errors = [proc.communicate(timeout=60)[1] for proc in procs]
                self.assertEqual([proc.returncode for proc in procs], [0, 0, 0], errors)


class ReceiptsAndChildren(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = runner.Paths(self.root).ensure()
        self.ledger = Ledger(self.paths.ledger)
        self.addCleanup(self.ledger.close)

    def admitted(self, request_id, run_id, name):
        claim(self.ledger, request_id=request_id, run_id=run_id, client=name)
        self.paths.attempt(run_id).mkdir(parents=True, exist_ok=True)
        (self.paths.attempt(run_id) / 'toolchain.json').write_text(json.dumps(PLAN['worker']))
        self.ledger.update(run_id, state='admitted', reservation_mib=1024, ceiling_mib=4096,
                           cpus_hint=2)

    def test_each_result_records_its_own_client(self):
        self.admitted('a:suite', 'ra', ALICE)
        self.admitted('b:suite', 'rb', BOB)
        with mock.patch.dict(os.environ, {'PANDORA_BUDGET_MIB': '8192'}):
            for run_id in ('ra', 'rb'):
                runner.supervise(self.root, run_id, driver=FakeDriver())
        for run_id, name in (('ra', ALICE), ('rb', BOB)):
            result = json.loads(self.paths.result(run_id).read_text())
            self.assertEqual((result['run_id'], result['client']), (run_id, name))
            self.assertTrue(self.paths.log(run_id).is_file())

    def test_a_shard_belongs_to_its_parents_client(self):
        self.admitted('b:suite', 'rparent', BOB)
        plan = {'repo': 'demo', 'job': 'suite', 'input_id': 'input-a', 'source_path': '/src',
                'cwd': '.', 'size_class': 'medium'}
        child = fanout.start_child(self.paths, self.ledger, plan, 'rparent', role='shard',
                                   argv=['x'], env={}, outputs=[], request_suffix='shard-1',
                                   index=1, total=2)
        self.assertEqual(self.ledger.get(child)['client'], BOB)

    def test_reconcile_settles_every_clients_orphans_and_adopts_every_live_run(self):
        claim(self.ledger, request_id='a:suite', run_id='ra', client=ALICE)
        claim(self.ledger, request_id='b:suite', run_id='rb', client=BOB, input_id='input-b')
        self.ledger.update('ra', state='running', supervisor_pid=999999, instance='run-ra')
        self.ledger.update('rb', state='running', supervisor_pid=os.getpid())
        answer = runner.reconcile(self.root, driver=FakeDriver())
        self.assertEqual((answer['infra_failed'], answer['adopted']), (['ra'], ['rb']))
        self.assertEqual(self.ledger.get('rb')['state'], 'running')

    def test_retention_keeps_a_live_run_whoever_owns_it(self):
        import time
        claim(self.ledger, request_id='a:suite', run_id='ra', client=ALICE)
        claim(self.ledger, request_id='b:suite', run_id='rb', client=BOB, input_id='input-b')
        for run_id in ('ra', 'rb'):
            self.paths.attempt(run_id).mkdir(parents=True, exist_ok=True)
        self.ledger.finish('ra', outcome='passed', exit_code=0)
        self.ledger.db.execute('UPDATE attempts SET finished=?, created=? WHERE run_id IN '
                               "('ra','rb')", (time.time() - 200000, time.time() - 200000))
        answer = runner.retain(self.root, keep_seconds=3600, keep_failed_seconds=3600)
        self.assertEqual(answer['removed'], ['ra'])
        self.assertTrue(self.paths.attempt('rb').is_dir())

    def test_live_counts_by_client(self):
        claim(self.ledger, request_id='a:suite', run_id='ra', client=ALICE)
        claim(self.ledger, request_id='b:suite', run_id='rb', client=BOB, input_id='input-b')
        claim(self.ledger, request_id='old:suite', run_id='ro', input_id='input-c')
        self.ledger.finish('ra', outcome='passed', exit_code=0)
        self.assertEqual(self.ledger.by_client(live=True), {BOB: 1, None: 1})
        self.assertEqual(service.ledger_clients(self.ledger),
                         {ALICE: 1, BOB: 1, '(unknown)': 1})


class SharedCachesUnderTwoWriters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def source(self, name, text):
        path = self.root / name
        path.mkdir()
        (path / 'file.txt').write_text(text)
        return path

    def test_two_macs_uploading_different_trees_at_once_each_get_their_own(self):
        link = LocalLink()
        release = threading.Barrier(2, timeout=10)

        def rsync(argv, **kwargs):
            if argv[0] != 'rsync':
                return _REAL_RUN(argv, **kwargs)
            release.wait()                       # both stages exist before either publishes
            source, target = Path(argv[-2]), Path(argv[-1].split(':', 1)[1])
            shutil.copyfile(source / 'file.txt', target / 'file.txt')
            return subprocess.CompletedProcess(argv, 0, b'', b'')
        trees = {'alice': self.source('alice', 'alice tree'),
                 'bob': self.source('bob', 'bob tree')}
        results, errors = {}, []

        def send(name):
            try:
                results[name] = transfer.send(link, [{'path': 'file.txt'}], worktree=trees[name],
                                              root=str(self.root / 'worker'), repo='demo',
                                              input_id='input-' + name)
            except BaseException as error:              # noqa: BLE001 - reported below
                errors.append(error)
        with mock.patch.object(transfer.subprocess, 'run', side_effect=rsync):
            threads = [threading.Thread(target=send, args=(name,)) for name in trees]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(20)
        self.assertEqual(errors, [])
        for name in trees:
            self.assertEqual((Path(results[name]['path']) / 'file.txt').read_text(),
                             '%s tree' % name)
        base = Path(transfer.cache_paths(str(self.root / 'worker'), 'demo', 'x')['base'])
        self.assertEqual(list(base.glob('*.partial*')), [])

    def test_two_writers_of_one_turbo_entry_leave_one_whole_entry(self):
        store = Store(self.root / 'turbo')
        body = [b'x' * 65536] * 8
        barrier = threading.Barrier(2, timeout=10)

        def chunks():
            barrier.wait()
            yield from body
        threads = [threading.Thread(target=store.write, args=('demo', 'team', 'hash1',
                                                               {'from': name}, chunks()))
                   for name in ('alice', 'bob')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        path = store.path('demo', 'team', 'hash1')
        header, _offset, size = store.read_header(path)
        self.assertIn(header['from'], ('alice', 'bob'))
        self.assertEqual(size, 65536 * 8)
        self.assertEqual([item.name for item in path.parent.iterdir()], ['hash1'])


class ClientIdentity(unittest.TestCase):
    def test_the_default_is_user_at_short_host(self):
        with mock.patch.object(settings.getpass, 'getuser', return_value='gary'), \
                mock.patch.object(settings.socket, 'gethostname', return_value='studio.local'):
            self.assertEqual(settings.client_name({}), 'gary@studio')

    def test_a_configured_name_wins(self):
        config = settings.normalize({'client': {'name': 'gary-desk'}})
        self.assertEqual(settings.client_name(config), 'gary-desk')

    def test_a_name_the_engine_would_drop_is_refused_at_load(self):
        for bad in ('has space', '/slash', 'x' * 65, 7):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                settings.normalize({'client': {'name': bad}})

    def test_a_misspelled_table_is_refused_at_load(self):
        # #128: `[wroker]` used to load silently and leave worker.host empty.
        with self.assertRaises(ConfigError) as caught:
            settings.normalize({'wroker': {'host': 'ubuntu@10.0.0.1'}})
        self.assertIn('wroker', str(caught.exception))
        self.assertIn('worker', str(caught.exception))

    def test_the_dead_fallback_keys_are_refused_not_ignored(self):
        # #127: both were accepted and never read.
        for key in ('fallback_slots', 'fallback_wait_seconds'):
            with self.subTest(key=key):
                with self.assertRaises(ConfigError) as caught:
                    settings.normalize({'client': {key: 2}})
                self.assertIn(key, str(caught.exception))

    def test_odd_characters_in_the_default_are_replaced(self):
        with mock.patch.object(settings.getpass, 'getuser', return_value='gary basin'), \
                mock.patch.object(settings.socket, 'gethostname', return_value='my mac.lan'):
            self.assertEqual(settings.client_name({}), 'gary-basin@my-mac')

    def test_the_engine_drops_an_identity_it_does_not_accept(self):
        self.assertIsNone(service.client_of('a b'))
        self.assertIsNone(service.client_of(None))
        self.assertEqual(service.client_of(ALICE), ALICE)


class TheDaemonAttributesItsRuns(DaemonCase):
    def setUp(self):
        super().setUp()
        text = (self.root / 'config.toml').read_text().replace(
            '[client]\n', '[client]\nname = "%s"\n' % ALICE)
        (self.root / 'config.toml').write_text(text)
        self.daemon.refresh()

    def test_the_row_and_the_worker_carry_the_client(self):
        answer = self.call(['pnpm', 'unit'])
        self.assertEqual(answer.exit, 0, answer.error)
        run = answer.accepted['run']
        meta = json.loads((self.state / 'runs' / run / 'meta.json').read_text())
        self.assertEqual(meta['client'], ALICE)
        self.assertEqual(self.daemon.any_worker().client, ALICE)

    def test_ps_and_stats_name_this_client(self):
        self.call(['pnpm', 'unit'])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(['--state', str(self.state), 'ps'])
        self.assertIn('as %s' % ALICE, out.getvalue().splitlines()[0])
        self.assertIn(ALICE, self.daemon.stats()['client'])


class WhatThePersonReads(unittest.TestCase):
    def test_the_worker_line_names_other_clients_live_runs(self):
        line = cli.worker_line({'worker': 'reachable',
                                'health': {'live_by_client': {ALICE: 1, BOB: 2}}}, ALICE)
        self.assertIn('live from other clients: %s 2' % BOB, line)
        self.assertNotIn('%s 1' % ALICE, line)
        self.assertTrue(line.endswith('as ' + ALICE))

    def test_a_result_names_its_client(self):
        text = cli.render_result('r1', {'outcome': 'passed', 'cli_exit': 0, 'client': BOB})
        self.assertIn('client ' + BOB, text)

    def test_stats_list_every_client_the_worker_has_seen(self):
        from pandora.client import stats as statistics
        lines = statistics.render_worker({'worker': 'reachable', 'health': {
            'by_client': {ALICE: 3, BOB: 5}, 'live_by_client': {BOB: 1}}})
        clients = [line for line in lines if 'clients:' in line]
        self.assertEqual(clients, ['  clients: %s 5 run(s), 1 live, %s 3 run(s)' % (BOB, ALICE)])


if __name__ == '__main__':
    unittest.main()
