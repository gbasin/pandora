#!/usr/bin/env python3
"""Actual Mac-to-worker scratch transfer measurement. Run only when authorized."""
import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import resource
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time

SPEC = importlib.util.spec_from_file_location(
    'network_profile', Path(__file__).parents[1] / 'transfer-profile' / 'benchmark.py')
PROFILE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROFILE)
CASES = ('cold', 'warm_unchanged', 'small_delta', 'tiny_large_edit')
METHODS = ('rsync', 'cas_trusted', 'cas_rehash')
HYBRID_METHODS = ('cas_hybrid_trusted', 'cas_hybrid_rehash')
EXTRA_CASES = ('full_large_rewrite', 'mode_change', 'new_path')
ALLOCATE = '''import json, os, pathlib, tempfile, uuid
root = pathlib.Path(tempfile.mkdtemp(prefix='pandora-cas-network-', dir=pathlib.Path.home()))
os.chmod(root, 0o700)
token = uuid.uuid4().hex
(root / '.owner').write_text(token)
print(json.dumps({'root': str(root), 'token': token}))
'''
UPLOAD = '''import pathlib, sys, tarfile
root = pathlib.Path(sys.argv[1])
if root.is_symlink() or not root.name.startswith('pandora-cas-network-') or root.parent.resolve() != pathlib.Path.home().resolve() or (root / '.owner').read_text() != sys.argv[2]:
 raise ValueError('not an owned scratch root')
allowed = {'experiments/cas-network/remote.py', 'experiments/cas-poc/cas.py', 'experiments/transfer-profile/benchmark.py'}
with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as archive:
 for item in archive:
  if item.name not in allowed or not item.isfile(): raise ValueError('unexpected helper archive member')
  path = root / 'code' / item.name
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open('wb') as out: out.write(archive.extractfile(item).read())
(root / 'baseline').mkdir()
'''
EMERGENCY_CLEANUP = '''import pathlib, shutil, sys
root = pathlib.Path(sys.argv[1])
if root.is_symlink() or not root.name.startswith('pandora-cas-network-') or root.parent.resolve() != pathlib.Path.home().resolve() or (root / '.owner').read_text() != sys.argv[2]:
 raise ValueError('not an owned scratch root')
shutil.rmtree(root)
'''


def cpu(who=resource.RUSAGE_CHILDREN):
    value = resource.getrusage(who)
    return {'user_seconds': value.ru_utime, 'system_seconds': value.ru_stime,
            'filesystem_input_blocks': value.ru_inblock, 'filesystem_output_blocks': value.ru_oublock}


class Link:
    def __init__(self, host, control):
        self.host = host
        self.options = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                        '-o', 'ControlMaster=auto', '-o', 'ControlPath=' + str(control / 'ssh-%C'),
                        '-o', 'ControlPersist=10m', '-o', 'ServerAliveInterval=15',
                        '-o', 'ServerAliveCountMax=3']
        self.root = self.token = None
        self.deadline = None
        self.closed = False

    @property
    def rsh(self):
        return shlex.join(['ssh', *self.options])

    def command(self, argv, payload=b'', timeout=1800):
        timeout = self.timeout(timeout)
        proc = subprocess.run(['ssh', *self.options, self.host, shlex.join(argv)],
                              input=payload, capture_output=True, timeout=timeout)
        if proc.returncode:
            raise RuntimeError('scratch SSH failed: ' + proc.stderr.decode('utf-8', 'replace'))
        return proc.stdout

    def timeout(self, limit):
        if self.deadline is None:
            return limit
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('network sample elapsed its timeout')
        return min(limit, remaining)

    def call(self, action, request):
        payload = json.dumps(request, separators=(',', ':')).encode()
        response = self.command(['python3', self.root + '/code/experiments/cas-network/remote.py',
                                 self.root, self.token, action], payload)
        return json.loads(response), len(payload), len(response)

    def close(self):
        self.closed = True
        try:
            proc = subprocess.run(['ssh', *self.options, '-O', 'exit', self.host],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  check=False, timeout=20)
            return {'closed': proc.returncode == 0, 'exit': proc.returncode}
        except (OSError, subprocess.TimeoutExpired) as error:
            return {'closed': False, 'error': str(error)}


def code_archive():
    root = Path(__file__).parents[2]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for name in ('experiments/cas-network/remote.py', 'experiments/cas-poc/cas.py',
                     'experiments/transfer-profile/benchmark.py'):
            archive.add(root / name, arcname=name)
    return buffer.getvalue()


def rsync_send(link, rsync, source, target, records, *, sample=None, baseline=False,
               checksum=False, hybrid=False):
    argv = [rsync, '-a', '--no-times', '--stats', '--files-from=-', '--from0', '-e', link.rsh]
    if checksum:
        argv += ['--checksum', '--delete']
    if hybrid:
        # Missing CAS identities must transfer even when size/mtime match.
        # Let rsync use a separate readonly basis; never prelink old bytes into
        # the stage or mutate a shared inode with --inplace/--append.
        argv += ['--ignore-times', '--no-whole-file']
    if baseline:
        argv += ['--link-dest=' + link.root + '/baseline']
    if sample:
        argv += ['--rsync-path=' + shlex.join(['python3', link.root + '/code/experiments/cas-network/remote.py',
                                              link.root, link.token, 'receive', sample])]
    names = b'\0'.join(record['path'].encode() for record in records) + b'\0'
    argv += [str(source) + '/', '%s:%s/' % (link.host, shlex.quote(target))]
    proc = subprocess.run(argv, input=names, capture_output=True, timeout=link.timeout(1800),
                          env={**os.environ, 'LC_ALL': 'C'})
    if proc.returncode:
        raise RuntimeError('scratch rsync failed: ' + proc.stderr.decode('utf-8', 'replace'))
    return PROFILE.parse_stats(proc.stdout.decode('utf-8', 'replace'))


def fixtures(root, mib, files, *, cases=None):
    baseline = root / 'baseline'
    details = PROFILE.fixture(baseline, mib, files)
    for path in baseline.rglob('*'):
        if path.is_file() and not path.is_symlink():
            path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
    small, tiny = root / 'small', root / 'tiny'
    PROFILE.clone_links(baseline, small)
    PROFILE.clone_links(baseline, tiny)
    PROFILE.replace_payload(small / details['odd_paths'][0], 700001, True)
    path = tiny / 'large/075.bin'
    old = path.stat()
    replacement = path.with_name(path.name + '.replacement')
    shutil.copyfile(path, replacement)
    with replacement.open('r+b') as handle:
        handle.seek(path.stat().st_size // 2)
        original = handle.read(16)
        if len(original) != 16:
            raise ValueError('tiny-edit fixture must hold sixteen edited bytes')
        handle.seek(path.stat().st_size // 2)
        handle.write(bytes(value ^ 0x55 for value in original))
    replacement.chmod(0o444)
    os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
    os.replace(replacement, path)
    sources = {'cold': baseline, 'warm_unchanged': baseline, 'small_delta': small, 'tiny_large_edit': tiny}
    selected = set(CASES if cases is None else cases)
    if 'full_large_rewrite' in selected:
        rewritten = root / 'rewritten'
        PROFILE.clone_links(baseline, rewritten)
        path = rewritten / 'large/075.bin'
        PROFILE.replace_payload(path, 900075, False, mtime=path.stat().st_mtime_ns)
        sources['full_large_rewrite'] = rewritten
    if 'mode_change' in selected:
        changed_mode = root / 'changed-mode'
        PROFILE.clone_links(baseline, changed_mode)
        path = changed_mode / 'small/00000.txt'
        old = path.stat()
        replacement = path.with_name(path.name + '.replacement')
        shutil.copyfile(path, replacement)
        replacement.chmod(0o444)
        os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
        os.replace(replacement, path)
        sources['mode_change'] = changed_mode
    if 'new_path' in selected:
        added = root / 'added'
        PROFILE.clone_links(baseline, added)
        path = added / 'small/new-path.txt'
        PROFILE.write_payload(path, 128, 800001, True)
        path.chmod(0o444)
        sources['new_path'] = added
    began = time.monotonic()
    manifests = {key: PROFILE.manifest(source) for key, source in sources.items()}
    details['source_capture_seconds'] = time.monotonic() - began
    details['tiny_edit_bytes'] = len(original)
    details['tiny_edit_file_bytes'] = (tiny / 'large/075.bin').stat().st_size
    details['manifest_sha256'] = {key: hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
                                 for key, records in manifests.items()}
    return sources, manifests, details


def selection(*, cases=None, hybrid=False, warm_only=False):
    selected = list(CASES if cases is None else cases)
    if not selected or len(set(selected)) != len(selected):
        raise ValueError('select at least one case without duplicates')
    if any(case not in (*CASES, *EXTRA_CASES) for case in selected):
        raise ValueError('unknown benchmark case')
    if warm_only:
        if cases is not None and 'cold' in selected:
            raise ValueError('an explicit cold case conflicts with --warm-only')
        selected = [case for case in selected if case != 'cold']
    return selected, [*METHODS, *HYBRID_METHODS] if hybrid else list(METHODS)


def run(*, host, rounds=3, mib=375, files=5000, rsync=None, sample_timeout=600,
        hybrid=False, cases=None, warm_only=False):
    if rounds < 1:
        raise ValueError('rounds must be positive')
    if sample_timeout <= 0:
        raise ValueError('sample timeout must be positive')
    selected_cases, selected_methods = selection(cases=cases, hybrid=hybrid, warm_only=warm_only)
    rsync = rsync or shutil.which('rsync')
    if not rsync:
        raise RuntimeError('rsync required')
    with tempfile.TemporaryDirectory(prefix='cas-network-src-') as local, \
            tempfile.TemporaryDirectory(prefix='cas-net-', dir='/tmp') as control:
        sources, manifests, fixture = fixtures(Path(local), mib, files, cases=selected_cases)
        link = Link(host, Path(control))
        cleanup = None
        try:
            began = time.monotonic()
            allocated = json.loads(link.command(['python3', '-c', ALLOCATE]))
            link.root, link.token = allocated['root'], allocated['token']
            startup_seconds = time.monotonic() - began
            link.command(['python3', '-c', UPLOAD, link.root, link.token], code_archive())
            rsync_send(link, rsync, sources['cold'], link.root + '/baseline', manifests['cold'])
            initialized, _, _ = link.call('initialize', {'records': manifests['cold']})
            samples, priming = [], []
            for round_index in range(-1, rounds):
                rotation = max(round_index, 0)
                cases = list(selected_cases)
                cases = cases[rotation % len(cases):] + cases[:rotation % len(cases)]
                pairs = []
                for case in cases:
                    methods = list(selected_methods)
                    methods = methods[rotation % len(methods):] + methods[:rotation % len(methods)]
                    pairs.extend((case, method) for method in methods)
                for position, (case, method) in enumerate(pairs):
                    sample = 'sample_%d_%d' % (round_index + 1, position)
                    setup, _, _ = link.call('setup', {'sample': sample, 'method': method, 'warm': case != 'cold'})
                    request = {'sample': sample, 'method': method}
                    if method != 'rsync':
                        request['records'] = manifests[case]
                    before, before_self, began = cpu(), cpu(resource.RUSAGE_SELF), time.monotonic()
                    link.deadline = began + sample_timeout
                    phase = time.monotonic()
                    planned, req_bytes, resp_bytes = link.call('plan', request)
                    steps = {'plan': time.monotonic() - phase}
                    requested = manifests[case]
                    if method != 'rsync':
                        missing = set(planned['missing_paths'])
                        requested = [record for record in manifests[case] if record['path'] in missing]
                    stats = {}
                    if method == 'rsync' or requested:
                        phase = time.monotonic()
                        stats = rsync_send(link, rsync, sources[case], planned['stage'], requested,
                                           sample=sample,
                                           baseline=(method == 'rsync' or method in HYBRID_METHODS) and case != 'cold',
                                           checksum=method == 'rsync', hybrid=method in HYBRID_METHODS)
                        steps['rsync'] = time.monotonic() - phase
                    phase = time.monotonic()
                    finalized, final_request_bytes, final_response_bytes = link.call('finalize', {'sample': sample})
                    steps['finalize'] = time.monotonic() - phase
                    wall = time.monotonic() - began
                    after, after_self = cpu(), cpu(resource.RUSAGE_SELF)
                    link.deadline = None
                    phase = time.monotonic()
                    audit, _, _ = link.call('audit', {'sample': sample, 'records': manifests[case]})
                    audit_wall = time.monotonic() - phase
                    result = {'case': case, 'method': method, 'round': round_index, 'order': position,
                              'wall_seconds': wall, 'steps': steps,
                              'sender_children': {key: after[key] - before[key] for key in before},
                              'sender_self': {key: after_self[key] - before_self[key] for key in before_self},
                              'plan': planned, 'finalize': finalized, 'stats': stats,
                              'manifest_request_bytes': len(json.dumps(request.get('records', []), separators=(',', ':')).encode()) if method != 'rsync' else 0,
                              'control_request_bytes': req_bytes + final_request_bytes,
                              'control_response_bytes': resp_bytes + final_response_bytes,
                              'missing_files': len(requested) if method != 'rsync' else None,
                              'missing_bytes': sum((sources[case] / record['path']).stat().st_size for record in requested) if method != 'rsync' else None,
                              'verification_seconds': audit['output_audit_seconds'],
                              'audit_transaction_seconds': audit_wall, 'audit': audit,
                              'verified': audit['verified'], 'load_average': setup['load_average']}
                    # Paths disclose only owned scratch, but remove manifest path
                    # lists from final evidence after measuring their wire size.
                    result['plan'] = {key: value for key, value in planned.items() if key not in ('stage', 'missing_paths')}
                    (priming if round_index == -1 else samples).append(result)
                    print('%s round=%d method=%s wall=%.3fs audited' %
                          (case, round_index, method, wall), flush=True)
            immutable, _, _ = link.call('baseline_audit', {'records': manifests['cold']})
            checked_sources = set()
            for case, source in sources.items():
                if source not in checked_sources:
                    PROFILE.verify(source, manifests[case])
                    checked_sources.add(source)
            cleanup, _, _ = link.call('cleanup', {})
            link.root = None
            return {'schema': 1, 'cases': selected_cases, 'methods': selected_methods, 'rounds': rounds,
                    'priming_rounds_excluded': 1, 'sample_timeout_seconds': sample_timeout,
                    'scope': 'actual Mac-to-worker SSH transport in private disk scratch; no production state',
                    'source_variants_immutable': True,
                    'fixture': fixture, 'samples': samples, 'priming': priming,
                    'setup': initialized, 'baseline_audit': immutable, 'cleanup': cleanup,
                    'machine': {'sender_platform': platform.platform(), 'sender_python': platform.python_version()},
                    'rsync': {'sender_path': rsync, 'receiver_path': initialized['rsync_path'],
                              'sender': subprocess.check_output([rsync, '--version'], text=True)},
                    'transport': {'host': host, 'control_master': 'one isolated private warmed master; auto/persist10m',
                                  'startup_seconds': startup_seconds, 'master_close': link.close()},
                    'limitations': ['Private SSH master CPU is not included in sender waited-child CPU.',
                                    'Serial samples share worker load and warm filesystem caches; no caches dropped.',
                                    'Cold cases start empty; warm methods retain the same baseline content only.',
                                    'Audit/copy is outside transfer wall and reported separately; receiver helper metrics are self-only.',
                                    'Receiver helper CPU excludes interpreter startup/imports and response serialization; full wall includes them.',
                                    'SSH server CPU and private master CPU are not attributed to individual transactions.',
                                    'Receiver logical child I/O bytes are unknown; raw filesystem blocks are not byte throughput.']
                    + (['Measured rotations do not fully balance all five method positions; hybrid results are exploratory.']
                       if hybrid and rounds % len(selected_methods) else [])}
        finally:
            link.deadline = None
            try:
                if link.root:
                    link.command(['python3', '-c', EMERGENCY_CLEANUP, link.root, link.token], timeout=120)
            finally:
                if not link.closed:
                    link.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='ubuntu@5.135.138.35')
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--mib', type=float, default=375)
    parser.add_argument('--files', type=int, default=5000)
    parser.add_argument('--sample-timeout', type=float, default=600)
    parser.add_argument('--hybrid', action='store_true', help='add missing-file delta CAS policies')
    parser.add_argument('--case', action='append', choices=(*CASES, *EXTRA_CASES), help='select a case; repeat')
    parser.add_argument('--warm-only', action='store_true', help='omit the default cold case')
    parser.add_argument('--out', type=Path, default=Path('artifacts/cas-network.json'))
    args = parser.parse_args(argv)
    try:
        selection(cases=args.case, hybrid=args.hybrid, warm_only=args.warm_only)
    except ValueError as error:
        parser.error(str(error))
    report = run(host=args.host, rounds=args.rounds, mib=args.mib, files=args.files,
                 sample_timeout=args.sample_timeout, hybrid=args.hybrid, cases=args.case,
                 warm_only=args.warm_only)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')


if __name__ == '__main__':
    main()
