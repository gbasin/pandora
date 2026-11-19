"""Primary-worker ownership and custody recovery, with real ledger/files."""
from contextlib import closing
import copy
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from pandora.engine import admission, overflow, runner, service
from pandora.engine.ledger import Ledger
from pandora.engine.scheduler import Scheduler
from pandora.snapshot import freeze
from pandora.tests.test_engine import PLAN


def packed(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name);info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class OverflowRouting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.paths = runner.Paths(Path(self.tmp.name) / 'engine').ensure()
        self.source = self.paths.src / 'demo/frozen';self.source.mkdir(parents=True)
        (self.source / 'hello.py').write_text('print("hello")\n')
        self.manifest = [freeze.entry(self.source, 'hello.py')]
        self.plan = copy.deepcopy(PLAN)
        self.plan.update(repo='demo',job='suite',size='medium',where='remote')
        self.request = {'request_id':'request-original','input_id':freeze.input_id(self.manifest),
                        'manifest':self.manifest,'source_path':str(self.source),
                        'plan':self.plan,'client':'owner'}
        self.ledger = Ledger(self.paths.ledger);self.addCleanup(self.ledger.close)
        self.ledger.claim('request-original','r-original',repo='demo',job='suite',
            input_id=self.request['input_id'],source_path=str(self.source),
            argv=self.plan['argv'],env=self.plan['env'],cwd=self.plan['cwd'],
            outputs=self.plan['outputs'],size_class='medium',client='owner')
        self.ledger.update('r-original',queued_at=100,queue_deadline=600)
        self.attempt = self.paths.attempt('r-original');self.attempt.mkdir()
        runner.write_json(self.attempt / 'request.json',self.request)
        (self.paths.root / 'cpus_per_run').write_text('4')
        (self.paths.root / 'min_engine_version').write_text('7')
        self.token = Path(self.tmp.name) / 'token';self.token.write_text('scoped-primary-token')
        self.policy = {'enabled':True,'url':'http://127.0.0.1:18081',
                       'token_file':str(self.token),'after_seconds':10,'allow':['demo:suite']}
        self.enable()

    def enable(self):
        (self.paths.root / 'overflow.toml').write_text(
            'enabled = true\nurl = "http://127.0.0.1:18081"\n'
            f'token_file = "{self.token}"\nafter_seconds = 10\nallow = ["demo:suite"]\n')

    def claim(self):
        return overflow.claim_if_eligible(self.paths,self.ledger,
                                          self.ledger.get('r-original'),now=120)

    def result(self):
        row = self.ledger.get('r-original')
        result = {k:row[k] for k in ('run_id','request_id','input_id','repo','job',
                  'reservation_mib','ceiling_mib','cpus_hint','client')}
        result.update(argv=self.plan['argv'],size_used=row['size_class'],outcome='passed',
                      cli_exit=0,observed_exit=0,receipt={'clean':True},verdict=None,
                      peak_mib=123,durations={},evidence={},finished=200)
        return result

    def state(self,**values):
        identity = overflow.hashlib.sha256(overflow.verdict.canonical(
            overflow.packet_of(self.paths,self.ledger.get('r-original')))).hexdigest()
        return json.dumps(dict(identity=identity,**values)).encode()

    def blob(self, result=None, **files):
        return packed({'result.json':json.dumps(result or self.result()).encode(),
                       'log':b'actual command output\n','outputs/artifact.txt':b'evidence',**files})

    def test_disabled_default_and_age(self):
        self.assertFalse(overflow.claim_if_eligible(self.paths,self.ledger,
            self.ledger.get('r-original'),now=105))
        (self.paths.root / 'overflow.toml').unlink()
        self.assertFalse(self.claim())

    def test_sticky_claim_cannot_consume_or_reenter_ovh(self):
        self.assertTrue(self.claim());self.assertFalse(self.claim())
        with closing(admission.Store(str(self.paths.peaks))) as store:
            scheduler = Scheduler(self.ledger,store,budget_mib=8192)
            self.assertEqual(scheduler.held_mib(),0)
            self.assertEqual(scheduler.queue(now=120),[])
            self.assertEqual(scheduler.admit('r-original','demo','suite')['reason'],'destination')
        with patch('pandora.engine.overflow.supervise',return_value='remote') as remote:
            self.assertEqual(runner.supervise(self.paths.root,'r-original'),'remote')
        remote.assert_called_once()

    def test_large_ceiling_and_unsupported_jobs_stay_on_ovh(self):
        self.plan['size'] = 'future'
        runner.write_json(self.attempt / 'request.json',self.request)
        with patch.dict(admission.CLASSES, {'future':24576}):
            self.assertFalse(self.claim())
        self.plan['size'] = 'medium';self.plan['session'] = {'name':'server'}
        runner.write_json(self.attempt / 'request.json',self.request)
        self.assertFalse(self.claim())

    def test_freezes_fully_verified_source_and_engine(self):
        self.assertTrue(self.claim())
        raw = overflow.input_archive(self.paths,self.ledger.get('r-original'))
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            packet = json.load(tar.extractfile('packet.json'))
            self.assertEqual(packet['request']['input_id'],self.request['input_id'])
            self.assertEqual(packet['manifest'],self.manifest)
            self.assertNotIn('manifest',packet['request'])
            self.assertIn('engine/pandora/engine/overflow.py',tar.getnames())
            self.assertIn('engine/pandora/engine/bundle.py',tar.getnames())
        (self.source / 'hello.py').write_text('changed')
        with self.assertRaises(Exception):overflow.input_archive(self.paths,self.ledger.get('r-original'))

    def test_worker_accepts_the_archive_the_primary_builds(self):
        from pandora.engine import overflow_worker
        self.assertTrue(self.claim())
        raw = overflow.input_archive(self.paths,self.ledger.get('r-original'))
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            packet = json.load(tar.extractfile('packet.json'))
        root = Path(self.tmp.name).resolve() / 'boat-engine'
        source = root / 'src' / 'repo' / 'frozen-input'
        shutil.copytree(self.source, source)
        plan = packet['request']['plan']
        validated, _ = overflow_worker.validate(packet, source, root, {(plan['repo'], plan['job'])})
        self.assertEqual(validated, source)
        # Each manifest entry crosses the wire once.
        self.assertEqual(json.dumps(packet).count(self.manifest[0]['sha256']), 1)

    def test_collection_preserves_original_identity_and_outputs(self):
        self.claim()
        result = overflow.receive(self.paths,self.ledger,'r-original',self.blob(),self.policy)
        self.assertEqual(self.ledger.get('r-original')['outcome'],'passed')
        self.assertEqual((self.attempt / 'outputs/artifact.txt').read_bytes(),b'evidence')
        self.assertEqual(json.loads(self.paths.result('r-original').read_text()),result)
        self.assertTrue((self.attempt / 'overflow-ack.pending').exists())

    def test_wrong_identity_bad_receipt_and_unsafe_output_never_pass(self):
        self.claim()
        for changes in ({'input_id':'different'},{'receipt':{'clean':False}},
                        {'argv':['different']},{'ceiling_mib':123}):
            with self.subTest(changes=changes):
                result = self.result();result.update(changes)
                with self.assertRaises(ValueError):
                    overflow.receive(self.paths,self.ledger,'r-original',self.blob(result),self.policy)
                self.assertNotEqual(self.ledger.get('r-original')['state'],'finished')
        with self.assertRaises(ValueError):
            overflow.receive(self.paths,self.ledger,'r-original',
                             self.blob(**{'outputs/../../escape':b'bad'}),self.policy)
        self.assertFalse(self.paths.result('r-original').exists())

    def test_cancel_before_collection_wins_without_publishing_pass(self):
        self.claim();self.ledger.request_cancel('r-original')
        result = overflow.receive(self.paths,self.ledger,'r-original',self.blob(),self.policy)
        self.assertEqual(result['outcome'],'cancelled');self.assertEqual(result['cli_exit'],130)
        self.assertIsNone(result['verdict'])

    def test_reconcile_adopts_boat_and_never_fabricates_supervisor_gone(self):
        self.claim()
        with patch.object(runner,'spawn',return_value=123) as spawn:
            result = runner.reconcile(self.paths.root,driver=object())
        spawn.assert_called_once();self.assertEqual(result['infra_failed'],[])
        self.assertIsNone(self.ledger.get('r-original')['outcome'])

    def test_relay_retries_ack_without_recollecting_or_reexecuting(self):
        self.claim()
        raw = self.blob();calls = []
        class Fake:
            def call(inner, run_id, action='', **kwargs):
                calls.append(action)
                if action == 'ack' and calls.count('ack') == 1:raise OSError('lost ack')
                if action == 'result':return raw
                if action == 'ack':return b'{}'
                return self.state(result_ready=True,terminal=False)
        result = overflow.supervise(self.paths.root,'r-original',broker=Fake(),sleep=lambda _:None)
        self.assertEqual(result['outcome'],'passed')
        self.assertEqual(calls.count('result'),1);self.assertEqual(calls.count('ack'),2)
        self.assertFalse((self.attempt / 'overflow-ack.pending').exists())

    def test_cancel_api_does_not_withdraw_boat_as_unexecuted(self):
        self.claim()
        with patch.object(service,'emit',side_effect=lambda x:x):
            result = service.cmd_cancel(type('Args',(),{'root':self.paths.root,
                'run':'r-original','client':'owner','queued_only':False})())
        self.assertTrue(result['requested']);self.assertNotIn('withdrawn',result)

    def test_contradictory_exit_outcomes_are_rejected(self):
        self.claim()
        for outcome,observed,cli in [('command_failed',0,0),('passed',7,7),('passed',True,True)]:
            result = self.result();result.update(outcome=outcome,observed_exit=observed,cli_exit=cli)
            with self.assertRaises(ValueError):
                overflow.receive(self.paths,self.ledger,'r-original',self.blob(result),self.policy)
        self.assertNotEqual(self.ledger.get('r-original')['state'],'finished')

    def test_permanent_staging_failure_fences_broker_before_finishing(self):
        import urllib.error
        from pandora.errors import SnapshotError
        self.claim();calls = []
        class Fake:
            canceled = False
            def call(inner, run_id, action='', **kwargs):
                calls.append(action)
                if action == 'cancel':inner.canceled = True;return b'{}'
                if action == 'ack':return b'{}'
                if inner.canceled:return self.state(terminal=True,result_ready=False)
                raise urllib.error.HTTPError('https://test',404,'unknown',{},None)
        with patch.object(overflow,'input_archive',side_effect=SnapshotError('source changed')):
            result = overflow.supervise(self.paths.root,'r-original',broker=Fake(),sleep=lambda _:None)
        self.assertEqual(result['outcome'],'infra_failed')
        self.assertIn('cancel',calls);self.assertIn('ack',calls)
        self.assertEqual(self.ledger.get('r-original')['destination'],'boat')

    def test_dispatch_timeout_preserves_terminal_broker_evidence(self):
        self.claim()
        self.ledger.update('r-original', queue_deadline=0.1)
        calls = []

        class Fake:
            canceled = False

            def call(inner, run_id, action='', **kwargs):
                calls.append(action)
                if action == 'cancel':
                    inner.canceled = True
                    return b'{}'
                if action == 'ack':
                    return b'{}'
                if action:
                    self.fail('unexpected broker action: ' + action)
                return self.state(phase='canceled' if inner.canceled else 'waiting',
                                  terminal=inner.canceled, result_ready=False,
                                  dispatched=False)

        result = overflow.supervise(self.paths.root, 'r-original', broker=Fake(),
                                    sleep=lambda _: None)
        self.assertEqual(result['outcome'], 'infra_failed')
        self.assertEqual(result['cli_exit'], 70)
        self.assertEqual(result['evidence']['cause'], 'overflow-dispatch-timeout')
        self.assertIs(result['evidence']['broker']['dispatched'], False)
        self.assertEqual(calls, ['', 'cancel', '', 'ack'])

    def test_malformed_result_fences_before_terminal_settlement(self):
        self.claim();calls = []
        raw = packed({'result.json':b'[]'})
        class Fake:
            canceled = False
            def call(inner, run_id, action='', **kwargs):
                calls.append(action)
                if action == 'cancel':inner.canceled = True;return b'{}'
                if action == 'ack':
                    self.assertTrue(inner.canceled)
                    self.assertEqual(self.ledger.get(run_id)['state'],'finished')
                    return b'{}'
                if action == 'result':return raw
                return self.state(terminal=inner.canceled,result_ready=True)
        result = overflow.supervise(self.paths.root,'r-original',broker=Fake(),sleep=lambda _:None)
        self.assertEqual(result['outcome'],'infra_failed')
        self.assertLess(calls.index('cancel'),calls.index('ack'))
        self.assertEqual(calls.count('result'),1)
        self.assertEqual(json.loads((self.attempt / 'overflow-failure.json').read_text()),
                         {'cause':'overflow-result-rejected'})

    def test_malformed_receipt_and_verdict_are_rejected(self):
        self.claim()
        for field in ('receipt','verdict'):
            for value in ([],True,'invalid'):
                with self.subTest(field=field,value=value):
                    result = self.result();result[field] = value
                    with self.assertRaises(ValueError):
                        overflow.receive(self.paths,self.ledger,'r-original',self.blob(result),self.policy)

    def test_malformed_result_scalars_are_rejected_before_publication(self):
        self.claim()
        for field in ('observed_exit','peak_mib','finished'):
            values = ([],{},True,'invalid',float('inf'),float('nan'),10**100,-10**100)
            for value in values:
                with self.subTest(field=field,value=value):
                    result = self.result();result.update(outcome='infra_failed',cli_exit=70)
                    result[field] = value
                    with self.assertRaises(ValueError):
                        overflow.receive(self.paths,self.ledger,'r-original',self.blob(result),self.policy)
                    self.assertFalse(self.paths.result('r-original').exists())
                    self.assertNotEqual(self.ledger.get('r-original')['state'],'finished')

    def test_missing_finished_result_is_recollected_before_ack(self):
        self.claim();raw = self.blob()
        overflow.receive(self.paths,self.ledger,'r-original',raw,self.policy)
        self.paths.result('r-original').unlink()
        calls = []
        class Fake:
            def call(inner, run_id, action='', **kwargs):
                calls.append(action)
                if action == 'ack':
                    self.assertTrue(self.paths.result('r-original').is_file())
                    return b'{}'
                if action == 'result':return raw
                return self.state(terminal=True,result_ready=True)
        result = overflow.supervise(self.paths.root,'r-original',broker=Fake(),sleep=lambda _:None)
        self.assertEqual(result['outcome'],'passed')
        self.assertLess(calls.index('result'),calls.index('ack'))

    def test_detached_waiter_preserves_owner_attribution(self):
        with patch.object(runner.subprocess,'Popen') as spawn:
            spawn.return_value.pid = 123
            runner.spawn_waiter(self.paths.root,'r-original',python='python3')
        argv = spawn.call_args.args[0]
        self.assertEqual(argv[-2:],['--client','owner'])

    def test_foreign_identity_is_failed_without_cancellation_or_ack(self):
        self.claim();calls = []
        class Fake:
            def call(inner, run_id, action='', **kwargs):
                calls.append(action)
                return b'{"identity":"foreign-packet","phase":"submitted"}'
        result = overflow.supervise(self.paths.root,'r-original',broker=Fake(),sleep=lambda _:None)
        self.assertEqual(result['outcome'],'infra_failed')
        self.assertEqual(result['evidence']['cause'],'overflow-identity-conflict')
        self.assertNotIn('cancel',calls);self.assertNotIn('ack',calls)

    def test_stale_bundle_cannot_settle_an_owned_boat_attempt(self):
        import sqlite3
        self.claim()
        # Simulate the SQL a version-6 Ledger.finish would issue, even if it
        # opened this database before another process installed the fence.
        old = sqlite3.connect(self.paths.ledger)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError,'overflow relay'):
                old.execute("UPDATE attempts SET state='finished',outcome='infra_failed' WHERE run_id='r-original'")
            old.rollback()
        finally:old.close()
        self.assertEqual(self.ledger.get('r-original')['state'],'admitted')
        overflow.receive(self.paths,self.ledger,'r-original',self.blob(),self.policy)
        self.assertEqual(self.ledger.get('r-original')['outcome'],'passed')

    def test_retention_keeps_unacknowledged_custody_and_ignores_removed_history(self):
        self.claim();overflow.receive(self.paths,self.ledger,'r-original',self.blob(),self.policy)
        with patch.object(runner,'gc_sources',return_value=[]):
            runner.retain(self.paths.root,keep_seconds=0)
        self.assertTrue(self.paths.result('r-original').exists())
        (self.attempt / 'overflow-ack.pending').unlink()
        with patch.object(runner,'gc_sources',return_value=[]):
            runner.retain(self.paths.root,keep_seconds=0)
        with patch.object(runner,'spawn') as spawn:
            runner.reconcile(self.paths.root,driver=object())
        spawn.assert_not_called()

    def test_ovh_capacity_freed_before_dispatch_prevents_paid_overflow(self):
        from pandora.engine import waitlist
        with patch.object(waitlist,'below_floor',return_value=None), \
             patch.object(runner,'budget_of',return_value=8192), \
             patch.object(runner,'spawn',return_value=123):
            self.assertEqual(waitlist.wait(self.paths.root,'r-original',clock=lambda:120),'admitted')
        self.assertIsNone(self.ledger.get('r-original')['destination'])

    def test_cold_floor_is_chosen_on_primary_and_never_shrinks_existing_ceiling(self):
        with (self.paths.root / 'overflow.toml').open('a') as f:
            f.write('\n[cold_sizes]\n"demo:suite"="large"\n')
        self.assertTrue(self.claim())
        row = self.ledger.get('r-original')
        self.assertEqual(row['size_declared'],'medium')
        self.assertEqual(row['size_class'],'large')
        self.assertEqual(row['reservation_mib'],8192)
        self.assertEqual(row['ceiling_mib'],8192)
        packet = overflow.packet_of(self.paths,row)
        self.assertEqual(packet['limits']['ceiling_mib'],8192)
        self.assertEqual(packet['request']['plan']['size'],'medium')

    def test_cold_floor_cannot_lower_an_ovh_learned_class(self):
        with (self.paths.root / 'overflow.toml').open('a') as f:
            f.write('\n[cold_sizes]\n"demo:suite"="large"\n')
        with closing(admission.Store(str(self.paths.peaks))) as store:
            store.set_class('demo','suite','xlarge',declared='medium')
        self.assertTrue(self.claim())
        self.assertEqual(self.ledger.get('r-original')['ceiling_mib'],12288)
