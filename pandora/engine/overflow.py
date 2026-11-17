"""Opt-in primary-worker relay. Ownership is sticky across every failure.

Only the operator's engine-root overflow.toml enables this path. No client can
supply an endpoint, credential, allowlist or destination. A claimed attempt is
never returned to local admission, including when the broker is unreachable.
"""
from contextlib import closing
import fcntl
import hashlib
import io
import json
import math
import os
import tempfile
from pathlib import Path, PurePosixPath
import tarfile
import time
import tomllib
import urllib.error
import urllib.request

from . import admission, runner, verdict
from .ledger import Ledger
from .scheduler import Scheduler, gate
from ..snapshot import freeze
from ..errors import SnapshotError

MAX_ARCHIVE = 256 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024


def durable_bytes(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.overflow-')
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
        # Persist directory creation and rename through the engine root as
        # well as file contents before acknowledging remote custody.
        for directory in (path.parent, *path.parent.parents):
            descriptor = os.open(directory,os.O_RDONLY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def durable_json(path, value):
    durable_bytes(path,json.dumps(value,sort_keys=True).encode())


def config(paths):
    filename = paths.root / 'overflow.toml'
    if not filename.exists():
        return None
    data = tomllib.loads(filename.read_text())
    if data.get('enabled') is not True:
        return None
    try:
        floor = int((paths.root / 'min_engine_version').read_text())
    except (OSError, ValueError):
        floor = 0
    if floor < 7:
        raise ValueError('Overflow requires the primary worker engine-version floor at least 7')
    from urllib.parse import urlparse
    url = urlparse(data['url'])
    if (url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in
                                     ('127.0.0.1', 'localhost'))):
        raise ValueError('Overflow requires HTTPS, except on loopback')
    if url.username or url.password or url.query or url.fragment:
        raise ValueError('Overflow URL cannot contain credentials, query or fragment')
    allowed = data.get('allow', [])
    if (not isinstance(allowed, list) or not allowed
            or any(not isinstance(x, str) or len(x.split(':')) != 2 for x in allowed)):
        raise ValueError('Overflow requires an explicit repo:job allowlist')
    if type(data.get('after_seconds', 60)) is not int or data.get('after_seconds', 60) < 0:
        raise ValueError('Invalid overflow queue age')
    if type(data.get('max_claims',2)) is not int or not 1 <= data.get('max_claims',2) <= 16:
        raise ValueError('Invalid maximum outstanding overflow claims')
    cold = data.get('cold_sizes',{})
    if (not isinstance(cold,dict) or any(key not in allowed or not isinstance(value,str) or value not in admission.CLASSES
                                      for key,value in cold.items())):
        raise ValueError('Cold size floors must name allowlisted jobs and known size classes')
    token = Path(data['token_file'])
    if not token.is_absolute() or not token.read_text().strip():
        raise ValueError('Overflow requires an absolute, nonempty token file')
    return data


def claim_if_eligible(paths, ledger, row, *, now=None):
    """Called only under admission gate, before local admission, with no I/O side effect."""
    settings = config(paths)
    if not settings or row['destination'] or row['state'] != 'queued' or row['cancel_requested']:
        return False
    now = time.time() if now is None else now() if callable(now) else now
    if sum(item['destination']=='boat' for item in ledger.live()) >= settings.get('max_claims',2):
        return False
    if (not row['queued_at'] or now - row['queued_at'] < settings.get('after_seconds', 60)
            or now >= (row['queue_deadline'] or 0) or (row['role'] or 'single') != 'single'):
        return False
    request = runner.submitted_request(paths, row['run_id']) or {}
    plan = request.get('plan') or {}
    if plan.get('size') not in admission.CLASSES or plan.get('where', 'remote') != 'remote':
        return False
    if f"{row['repo']}:{row['job']}" not in settings['allow'] or 'manifest' not in request:
        return False
    if (any(plan.get(k) or request.get(k) for k in
            ('shards', 'parent', 'session', 'singleton', 'batchqueue', 'want_shards', 'writeback'))
            or any(x.get('kind') == 'writeback' for x in plan.get('outputs', []))):
        return False
    wall = int(plan.get('env', {}).get('PANDORA_WALL_SECONDS') or
               60 * int(plan.get('timeout_minutes') or 30))
    if not 1 <= wall <= 11700:
        return False  # Leave 30 minutes for preparation, collection and teardown.
    with closing(admission.Store(str(paths.peaks))) as store:
        scheduler = Scheduler(ledger, store, budget_mib=runner.budget_of(paths),
                              cpus_per_run=runner.cpus_per_run_of(paths)[0])
        reservation, ceiling, size, _ = scheduler.reservation(row['repo'], row['job'], plan['size'])
        cpus = scheduler.cpus_hint()
    cold_size = settings.get('cold_sizes',{}).get(f"{row['repo']}:{row['job']}")
    if cold_size and admission.ceiling_for(cold_size) > ceiling:
        size = cold_size
        ceiling = admission.ceiling_for(size)
        # A warm OVH observation does not reserve a smaller cold Boat footprint.
        # The primary chooses this explicit floor before claiming destination;
        # the worker still receives immutable limits and never enlarges them.
        reservation = ceiling
    if ceiling > 12288 or cpus > 8:
        return False
    # Pin the entire operator policy with the attempt. Changes to the default
    # endpoint must not redirect an already submitted attempt after restart.
    durable_json(paths.attempt(row['run_id']) / 'overflow-policy.json', settings)
    ledger.update(row['run_id'], destination='boat', state='admitted',
                  reservation_mib=reservation, ceiling_mib=ceiling, cpus_hint=cpus,
                  size_class=size, admitted_at=now)
    return True


class Broker:
    def __init__(self, policy, identity):
        self.policy = policy
        self.identity = identity

    def call(self, run_id, action='', *, body=None, method='GET'):
        url = self.policy['url'].rstrip('/') + '/v1/pandora/' + run_id
        if action:
            url += '/' + action
        request = urllib.request.Request(url, data=body, method=method,
            headers={'Authorization': 'Bearer ' + Path(self.policy['token_file']).read_text().strip(),
                     'User-Agent': 'pandora-overflow/1',
                     'Content-Type': 'application/octet-stream',
                     'X-Pandora-Identity':self.identity})
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read(MAX_ARCHIVE + 1)
        if len(raw) > MAX_ARCHIVE:
            raise ValueError('Overflow response exceeds archive limit')
        return raw


def packet_of(paths,row):
    request = runner.submitted_request(paths,row['run_id'])
    request['client'] = row['client']
    return {'run_id':row['run_id'],'request':request,'manifest':request['manifest'],
            'limits':{k:row[k] for k in ('reservation_mib','ceiling_mib','cpus_hint')}}


def input_archive(paths, row):
    packet = packet_of(paths,row)
    manifest = packet['manifest']
    source = Path(row['source_path']).resolve()
    if not source.is_relative_to(paths.src.resolve()) or source == paths.src.resolve():
        raise ValueError('Overflow source outside cache')
    freeze.verify(source, manifest)
    if freeze.input_id(manifest) != row['input_id']:
        raise ValueError('Overflow manifest identity mismatch')
    from .bundle import MEMBERS
    package = Path(__file__).resolve().parents[1]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        data = json.dumps(packet, sort_keys=True).encode()
        member = tarfile.TarInfo('packet.json'); member.size = len(data)
        tar.addfile(member, io.BytesIO(data))
        for name in MEMBERS:
            tar.add(package / name, arcname='engine/pandora/' + name, recursive=False)
        digest = verdict.engine_id().encode()
        marker = tarfile.TarInfo('engine/pandora/.bundle');marker.size = len(digest)
        tar.addfile(marker,io.BytesIO(digest))
        for record in manifest:
            name = record['path']
            if (not (source / name).resolve().is_relative_to(source)
                    or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts):
                raise ValueError('Overflow source path escapes cache')
            tar.add(source / name, arcname='source/' + name, recursive=False)
    freeze.verify(source, manifest)
    raw = buffer.getvalue()
    if len(raw) > MAX_ARCHIVE:
        raise ValueError('Overflow input exceeds archive limit')
    return raw


def receive(paths, ledger, run_id, raw, policy):
    """Validate identities before importing outputs; publish finished last."""
    row = ledger.get(run_id)
    files, total = {}, 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as tar:
        for member in tar:
            name = member.name
            if member.isdir():
                continue
            if (not member.isfile() or name in files or PurePosixPath(name).is_absolute()
                    or '..' in PurePosixPath(name).parts or str(PurePosixPath(name)) != name
                    or (name not in ('result.json', 'log') and not name.startswith('outputs/'))):
                raise ValueError('Unsafe overflow output archive')
            total += member.size
            if total > MAX_EXPANDED:
                raise ValueError('Overflow outputs exceed expanded limit')
            files[name] = tar.extractfile(member).read()
    result = json.loads(files['result.json'])
    if not isinstance(result, dict):
        raise ValueError('Overflow result must be an object')
    if result.get('receipt') is not None and not isinstance(result['receipt'], dict):
        raise ValueError('Overflow receipt must be an object')
    if result.get('verdict') is not None and not isinstance(result['verdict'], dict):
        raise ValueError('Overflow verdict must be an object')
    for key in ('run_id', 'request_id', 'input_id', 'repo', 'job', 'reservation_mib',
                'ceiling_mib', 'cpus_hint', 'client'):
        if result.get(key) != row[key]:
            raise ValueError('Overflow result identity mismatch: ' + key)
    if result.get('argv') != json.loads(row['argv']) or result.get('size_used') != row['size_class']:
        raise ValueError('Overflow result command or size differs from admission')
    if result['outcome'] not in ('passed', 'command_failed', 'oom', 'timed_out', 'cancelled', 'infra_failed'):
        raise ValueError('Invalid overflow outcome')
    observed = result.get('observed_exit')
    if observed is not None and (type(observed) is not int or not -(2**63) <= observed < 2**63):
        raise ValueError('Overflow observed exit must be an integer')
    for key in ('peak_mib', 'finished'):
        value = result.get(key)
        if value is not None and (type(value) not in (int, float)
                                  or not -1e18 < value < 1e18 or not math.isfinite(value)):
            raise ValueError('Overflow ' + key + ' must be a finite number')
    if (result['outcome'] == 'passed' and (type(observed) is not int or observed != 0)
            or result['outcome'] == 'command_failed' and (type(observed) is not int or observed == 0)):
        raise ValueError('Overflow outcome contradicts observed command exit')
    if (type(result.get('cli_exit')) is not int
            or result['cli_exit'] != runner.cli_exit(result['outcome'], observed)):
        raise ValueError('Invalid overflow exit code')
    if result['outcome'] == 'passed' and not (result.get('receipt') or {}).get('clean'):
        raise ValueError('Passing overflow result has no clean receipt')
    signature = result.get('verdict')
    if signature:
        signer = policy.get('signer')
        if (not signer or signature.get('signer') != signer
                or not isinstance(signature.get('payload'), str)
                or not isinstance(signature.get('signature'), str)
                or not verdict.verify(signature['payload'], signature['signature'], signer)):
            raise ValueError('Unexpected or invalid overflow signer')
        signed = json.loads(signature['payload'])
        if not isinstance(signed, dict):
            raise ValueError('Overflow signed payload must be an object')
        for key in ('run_id', 'input_id', 'repo', 'job', 'outcome', 'tree', 'argv'):
            if signed.get(key) != result.get(key):
                raise ValueError('Overflow signature does not bind result')
        if (signed.get('env_digest') != verdict.env_digest(json.loads(row['env']))
                or signed.get('cwd') != row['cwd']):
            raise ValueError('Overflow signature does not bind command environment')
    if ledger.get(run_id)['cancel_requested'] and result['outcome'] == 'passed':
        # Cancellation won the race with delivery. Do not publish its signature.
        result.update(outcome='cancelled', cli_exit=130, verdict=None,
                      verdict_skipped='cancelled_before_collection')
    for name, data in files.items():
        if name == 'result.json':
            continue
        target = paths.attempt(run_id) / name
        if not target.resolve().is_relative_to(paths.attempt(run_id).resolve()):
            raise ValueError('Overflow output path escapes attempt')
        target.parent.mkdir(parents=True, exist_ok=True)
        durable_bytes(target,data)
    durable_json(paths.result(run_id),result)
    durable_bytes(paths.attempt(run_id) / 'overflow-ack.pending',b'')
    ledger.finish(run_id, outcome=result['outcome'], exit_code=result.get('observed_exit'),
                  peak_mib=result.get('peak_mib'), durations=result.get('durations'),
                  evidence=result.get('evidence'), receipt=result.get('receipt'),
                  finished=result.get('finished'),overflow=True)
    return result


def supervise(root, run_id, *, broker=None, sleep=time.sleep):
    paths = runner.Paths(root).ensure()
    # Reconcile may start a second relay after a crash between spawn and PID
    # recording. Only one relay may collect or settle an attempt at a time.
    with (paths.attempt(run_id) / 'overflow-relay.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        policy = json.loads((paths.attempt(run_id) / 'overflow-policy.json').read_text())
        with closing(Ledger(paths.ledger)) as identity_ledger:
            identity = hashlib.sha256(verdict.canonical(packet_of(paths,identity_ledger.get(run_id)))).hexdigest()
        broker = broker or Broker(policy,identity)
        with closing(Ledger(paths.ledger)) as ledger:
            raw = None
            while True:
                row = ledger.get(run_id)
                try:
                    if row['state'] == 'finished' and paths.result(run_id).is_file():
                        broker.call(run_id, 'ack', method='POST', body=b'')
                        (paths.attempt(run_id) / 'overflow-ack.pending').unlink(missing_ok=True)
                        return json.loads(paths.result(run_id).read_text())
                    failure = paths.attempt(run_id) / 'overflow-failure.json'
                    if row['cancel_requested'] or failure.exists():
                        broker.call(run_id, 'cancel', method='POST', body=b'')
                    try:
                        state = json.loads(broker.call(run_id))
                    except urllib.error.HTTPError as error:
                        if error.code != 404:
                            raise
                        error.close()
                        if time.time() >= (row['queue_deadline'] or float('inf')):
                            durable_json(failure,{'cause':'overflow-dispatch-timeout'})
                            continue
                        try:
                            if raw is None:
                                try:raw = input_archive(paths,row)
                                except (OSError,SnapshotError,ValueError,tarfile.TarError):
                                    durable_json(failure,{'cause':'overflow-staging-failed'})
                                    continue
                            state = json.loads(broker.call(run_id, body=raw, method='PUT'))
                        except (ValueError, SnapshotError, tarfile.TarError):
                            durable_json(failure,{'cause':'overflow-staging-failed'})
                            continue
                        except urllib.error.HTTPError as refused:
                            if refused.code not in (400,403,409,413):raise
                            refused.close()
                            durable_json(failure,{'cause':'overflow-submit-refused','status':refused.code})
                            continue
                    if (state.get('identity') != identity
                            and not (state.get('phase') == 'canceled' and state.get('identity') is None)):
                        raise urllib.error.HTTPError('overflow',412,'packet identity conflict',{},None)
                    raw = None  # The broker now owns input custody; release staging RAM.
                    if (state.get('phase') == 'waiting'
                            and time.time() >= (row['queue_deadline'] or float('inf'))):
                        durable_json(failure,{'cause':'overflow-dispatch-timeout'})
                        continue
                    if state.get('result_ready') and not failure.exists():
                        data = broker.call(run_id, 'result')
                        with gate(paths.root):
                            try:
                                result = receive(paths, ledger, run_id, data, policy)
                            except (ValueError, KeyError, tarfile.TarError):
                                durable_json(failure,{'cause':'overflow-result-rejected'})
                                # Invalid collected evidence cannot become a pass.
                                # Stop first; only an observed terminal task may
                                # settle an attempt without a valid result.
                                broker.call(run_id, 'cancel', method='POST', body=b'')
                                raise
                        broker.call(run_id, 'ack', method='POST', body=b'')
                        (paths.attempt(run_id) / 'overflow-ack.pending').unlink(missing_ok=True)
                        return result
                    if state.get('terminal'):
                        durable_bytes(paths.attempt(run_id) / 'overflow-ack.pending',b'')
                        failed_result = runner.write_result(paths, ledger, run_id,
                            outcome='cancelled' if row['cancel_requested'] else 'infra_failed',
                            layer='engine', exit_code=None, peak_mib=0, durations={},
                            evidence={'cause': 'overflow-no-result', 'broker': state}, receipt=None,overflow=True)
                        durable_json(paths.result(run_id),failed_result)
                        continue
                    log = broker.call(run_id, 'log')
                    paths.log(run_id).write_bytes(log)
                    ledger.update(run_id, state='running')
                except urllib.error.HTTPError as conflict:
                    conflict.close()
                    if conflict.code == 412:
                        # This named broker task belongs to another immutable
                        # packet. Never cancel, acknowledge or follow it.
                        result = runner.write_result(paths,ledger,run_id,outcome='infra_failed',
                            layer='engine',exit_code=None,peak_mib=0,durations={},
                            evidence={'cause':'overflow-identity-conflict'},receipt=None,overflow=True)
                        durable_json(paths.result(run_id),result)
                        return result
                except (OSError, ValueError, KeyError, tarfile.TarError):
                    # A network error is uncertainty, never permission to replay
                    # locally or fabricate a final result. Recovery keeps polling.
                    pass
                sleep(2)
