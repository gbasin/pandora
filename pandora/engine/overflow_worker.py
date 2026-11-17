"""Experimental frozen-packet worker entrypoint, without production routing.

A trusted dispatcher must hold an exclusive compute lease through execution,
collection and cleanup, and supply an isolated engine root and frozen source.
This module does not transfer source, authorize a signer, or bypass readiness.
Crashes leave uncertain attempts; this entrypoint never reexecutes one.
"""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import time

from ..errors import SnapshotError
from ..snapshot import freeze
from . import admission, runner
from .ledger import Ledger
from .scheduler import gate
from .service import WORKER_ONLY


class PacketRejected(ValueError):
    pass


class AttemptUncertain(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.overflow-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(canonical(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate(packet, source, root, allowed):
    """Verify the complete immutable source before claiming an attempt."""
    if not isinstance(packet, dict):
        raise PacketRejected('packet must be an object')
    run_id = packet.get('run_id')
    if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', run_id):
        raise PacketRejected('invalid original run_id')
    request = packet.get('request')
    plan = request.get('plan') if isinstance(request, dict) else None
    if not isinstance(plan, dict) or (plan.get('repo'), plan.get('job')) not in allowed:
        raise PacketRejected('repo/job is not explicitly allowed')
    if not isinstance(request.get('request_id'), str) or not request['request_id']:
        raise PacketRejected('original request_id is required')
    if (packet.get('role', 'single') != 'single' or request.get('role', 'single') != 'single'
            or any(plan.get(k) or request.get(k) for k in
                   ('shards', 'parent', 'session', 'singleton', 'batchqueue', 'want_shards'))):
        raise PacketRejected('only ordinary single attempts are supported')
    if (plan.get('writeback') or request.get('writeback')
            or any(o.get('kind') == 'writeback' for o in plan.get('outputs', []))):
        raise PacketRejected('writeback is not supported')
    if plan.get('where', 'remote') != 'remote':
        raise PacketRejected('only remote jobs are supported')
    limits = packet.get('limits')
    keys = ('reservation_mib', 'ceiling_mib', 'cpus_hint')
    if not isinstance(limits, dict) or any(type(limits.get(k)) is not int for k in keys):
        raise PacketRejected('integer OVH admission limits are required')
    if not (1 <= limits['reservation_mib'] <= limits['ceiling_mib'] <= 16384 - 4096
            and 1 <= limits['cpus_hint'] <= 8):
        raise PacketRejected('limits do not fit the 16 GiB worker with host headroom')
    if limits['ceiling_mib'] not in admission.CLASSES.values():
        raise PacketRejected('ceiling must name an existing Pandora size class')
    if plan.get('size') not in admission.CLASSES:
        raise PacketRejected('unknown declared size class')
    if limits['ceiling_mib'] < admission.ceiling_for(plan['size']):
        raise PacketRejected('frozen ceiling cannot be below the declared size class')
    override = os.environ.get('PANDORA_CPUS_PER_RUN')
    if override and override != str(limits['cpus_hint']):
        raise PacketRejected('worker CPU override conflicts with frozen admission')
    source, root = Path(source).resolve(), Path(root).resolve()
    if (not source.is_dir() or not source.is_relative_to(root / 'src')
            or source == root / 'src'):
        raise PacketRejected('source must be a directory strictly below engine_root/src')
    manifest = packet.get('manifest')
    if not isinstance(manifest, list):
        raise PacketRejected('frozen manifest is required')
    names = set()
    for record in manifest:
        name = record.get('path') if isinstance(record, dict) else None
        if (not isinstance(name, str) or not name or '\x00' in name
                or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts
                or str(PurePosixPath(name)) != name or name == '.' or name in names):
            raise PacketRejected('manifest paths must be unique canonical relative paths')
        if not (source / name).resolve().is_relative_to(source):
            raise PacketRejected('manifest path escapes the frozen source')
        names.add(name)
    if freeze.input_id(manifest) != request.get('input_id'):
        raise PacketRejected('manifest does not match the original input_id')
    if request.get('git_marks', freeze.git_marks(manifest)) != freeze.git_marks(manifest):
        raise PacketRejected('synthetic git marks do not match the frozen manifest')
    try:
        freeze.verify(source, manifest)
    except (OSError, SnapshotError) as error:
        raise PacketRejected('frozen source verification failed: %s' % error) from None
    return source, root


def finished_result(paths, ledger, run_id, request):
    """The persisted wire result is authoritative, including JSON value types."""
    row = ledger.get(run_id)
    if row is None or row['state'] != 'finished':
        raise AttemptUncertain('original attempt is unfinished; never reexecuting')
    if row['request_id'] != request['request_id'] or row['input_id'] != request['input_id']:
        raise PacketRejected('finished ledger identity does not match the frozen packet')
    try:
        result = json.loads(paths.result(run_id).read_text())
    except (OSError, ValueError):
        raise AttemptUncertain('finished result is missing or unreadable') from None
    if (not isinstance(result, dict) or result.get('run_id') != run_id
            or result.get('request_id') != request['request_id']
            or result.get('input_id') != request['input_id']
            or result.get('outcome') != row['outcome']):
        raise PacketRejected('persisted result does not match the original attempt')
    return result


def execute(packet, *, source, root, allowed, supervise=None):
    """Claim the original IDs once, then use the existing engine result layout.

    Trace: verify -> persist packet -> ledger claim/admit -> supervise.
    A crash between any of those writes is not permission to execute again.
    Exact duplicates return only a finished result. Unfinished records and
    partial intents require dispatcher reconciliation, never automatic retry.
    """
    source, root = validate(packet, source, root, allowed)
    paths = runner.Paths(root).ensure()
    run_id, request, limits = packet['run_id'], dict(packet['request']), packet['limits']
    request['source_path'] = str(source)
    request['git_marks'] = freeze.git_marks(packet['manifest'])
    identity = {'packet': packet, 'source': str(source)}
    plan = request['plan']
    with gate(root):
        ledger = Ledger(paths.ledger)
        try:
            row = ledger.get(run_id)
            by_request = ledger.by_request(request['request_id'])
            attempt = paths.attempt(run_id)
            marker = attempt / 'overflow-packet.json'
            if marker.exists():
                if canonical(json.loads(marker.read_text())) != canonical(identity):
                    raise PacketRejected('original identity has a conflicting frozen packet')
                return finished_result(paths, ledger, run_id, request)
            if row is not None or by_request is not None:
                raise PacketRejected('original run_id or request_id is already owned')
            if ledger.live():
                raise PacketRejected('engine root has another live attempt; an exclusive root is required')
            attempt.mkdir(parents=True, exist_ok=False)
            atomic_json(marker, identity)
            atomic_json(attempt / 'request.json', request)
            atomic_json(attempt / 'toolchain.json',
                        {k: v for k, v in plan['worker'].items() if k not in WORKER_ONLY})
            paths.log(run_id).touch()
            size = next(k for k, v in admission.CLASSES.items() if v == limits['ceiling_mib'])
            ledger.claim(request['request_id'], run_id, repo=plan['repo'], job=plan['job'],
                         input_id=request['input_id'], source_path=str(source), argv=plan['argv'],
                         env=plan['env'], cwd=plan['cwd'], outputs=plan['outputs'],
                         size_class=plan['size'], role='single', client=request.get('client'),
                         retry_of=request.get('retry_of'))
            ledger.update(run_id, state='admitted', size_class=size, admitted_at=time.time(),
                          **{k: limits[k] for k in ('reservation_mib', 'ceiling_mib', 'cpus_hint')})
            # The engine rereads this worker setting before cloning/execution.
            # This root is private to the exclusive lease, never a shared worker.
            (root / 'cpus_per_run').write_text(str(limits['cpus_hint']))
        finally:
            ledger.close()
    (supervise or runner.supervise)(root, run_id)
    with gate(root):
        ledger = Ledger(paths.ledger)
        try:
            return finished_result(paths, ledger, run_id, request)
        finally:
            ledger.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('packet', 'source', 'root'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--allow', action='append', required=True, metavar='REPO:JOB')
    args = parser.parse_args(argv)
    allowed = set()
    for value in args.allow:
        pair = value.split(':', 1)
        if len(pair) != 2 or not all(pair):
            parser.error('--allow requires REPO:JOB')
        allowed.add(tuple(pair))
    result = execute(json.loads(args.packet.read_text()), source=args.source,
                     root=args.root, allowed=allowed)
    print(json.dumps(result))
    return int(result['cli_exit'])


if __name__ == '__main__':
    raise SystemExit(main())
