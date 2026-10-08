#!/usr/bin/env python3
"""Isolated warmed-SSH rsync phase diagnostics. No CAS store or production state."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import shutil
import subprocess
import tempfile
import time

SPEC = importlib.util.spec_from_file_location(
    'network_probe_helpers', Path(__file__).parents[1] / 'cas-network' / 'benchmark.py')
NETWORK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NETWORK)
PROFILE = NETWORK.PROFILE
VARIANTS = ('normal', 'dry_basis', 'dry_no_basis', 'empty', 'ssh_true')


def fixtures(root, mib, files):
    fixture_began = time.monotonic()
    baseline, tiny, empty = (root / name for name in ('baseline', 'tiny', 'empty'))
    details = PROFILE.fixture(baseline, mib, files)
    for path in baseline.rglob('*'):
        if path.is_file() and not path.is_symlink():
            path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
    began = time.monotonic()
    original_records = PROFILE.manifest(baseline)
    baseline_capture_seconds = time.monotonic() - began
    PROFILE.clone_links(baseline, tiny)
    path = tiny / 'large/075.bin'
    old = path.stat()
    replacement = path.with_name(path.name + '.replacement')
    shutil.copyfile(path, replacement)
    with replacement.open('r+b') as handle:
        handle.seek(old.st_size // 2)
        original = handle.read(16)
        if len(original) != 16:
            raise ValueError('tiny-edit fixture needs sixteen bytes')
        handle.seek(old.st_size // 2)
        handle.write(bytes(value ^ 0x55 for value in original))
    replacement.chmod(0o444)
    os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
    os.replace(replacement, path)
    began = time.monotonic()
    records = PROFILE.manifest(tiny)
    tiny_capture_seconds = time.monotonic() - began
    empty.mkdir()
    details.update(tiny_edit_bytes=16, tiny_edit_file_bytes=old.st_size,
                   baseline_capture_seconds=baseline_capture_seconds,
                   tiny_capture_seconds=tiny_capture_seconds,
                   source_preparation_seconds=time.monotonic() - fixture_began)
    return baseline, tiny, empty, original_records, records, details


def parse_stats(stdout):
    result = PROFILE.parse_stats(stdout)
    for key, label in (('file_list_generation_seconds', 'File list generation time'),
                       ('file_list_transfer_seconds', 'File list transfer time')):
        found = re.search(r'^' + label + r': ([0-9]+(?:\.[0-9]+)?) seconds\s*$', stdout, re.MULTILINE)
        result[key] = float(found.group(1)) if found else None
    return result


def rsync_argv(link, rsync, source, stage, sample, variant, *, debug=False):
    argv = [rsync, '-a', '--no-times', '--checksum', '--delete', '--stats',
            '--files-from=-', '--from0', '-e', link.rsh]
    if variant in ('normal', 'dry_basis'):
        argv += ['--link-dest=' + link.root + '/baseline']
    if variant in ('dry_basis', 'dry_no_basis'):
        argv.append('--dry-run')
    if debug:
        argv += ['--debug=NSTR2,CMD2', '--remote-option=--debug=NSTR2,CMD2']
    argv += ['--rsync-path=' + NETWORK.shlex.join(
        ['python3', link.root + '/code/experiments/cas-network/remote.py',
         link.root, link.token, 'receive', sample]),
        str(source) + '/', '%s:%s/' % (link.host, NETWORK.shlex.quote(stage))]
    return argv


def sample(link, rsync, source, records, variant, identifier, *, debug=False):
    outside = {}
    phase = time.monotonic()
    setup, _, _ = link.call('setup', {'sample': identifier, 'method': 'rsync', 'warm': True})
    outside['setup'] = time.monotonic() - phase
    phase = time.monotonic()
    plan, _, _ = link.call('plan', {'sample': identifier, 'method': 'rsync'})
    outside['plan'] = time.monotonic() - phase
    argv = rsync_argv(link, rsync, source, plan['stage'], identifier, variant, debug=debug)
    names = b'\0'.join(record['path'].encode() for record in records) + (b'\0' if records else b'')
    environment = {**os.environ, 'LC_ALL': 'C'}
    before, before_self = NETWORK.cpu(), NETWORK.cpu(resource.RUSAGE_SELF)
    began = time.monotonic()
    link.deadline = began + 600
    try:
        if variant == 'ssh_true':
            output, errors = link.command(['true']), b''
        else:
            proc = subprocess.run(argv,
                                  input=names, capture_output=True, timeout=link.timeout(1800),
                                  env=environment)
            if proc.returncode:
                raise RuntimeError('scratch rsync failed: ' + proc.stderr.decode('utf-8', 'replace'))
            output, errors = proc.stdout, proc.stderr
        wall = time.monotonic() - began
        after, after_self = NETWORK.cpu(), NETWORK.cpu(resource.RUSAGE_SELF)
    finally:
        link.deadline = None
    stdout, stderr = output.decode('utf-8', 'replace'), errors.decode('utf-8', 'replace')
    phase = time.monotonic()
    finalized, _, _ = link.call('finalize', {'sample': identifier})
    outside['finalize'] = time.monotonic() - phase
    phase = time.monotonic()
    expected = records if variant == 'normal' else []
    audit, _, _ = link.call('audit', {'sample': identifier, 'records': expected})
    outside['audit'] = time.monotonic() - phase
    return {'variant': variant, 'wall_seconds': wall,
            'sender_children': {key: after[key] - before[key] for key in before},
            'sender_self': {key: after_self[key] - before_self[key] for key in before_self},
            'receiver': finalized['receiver'], 'stats': parse_stats(stdout),
            'stdout': stdout, 'stderr': stderr, 'outside_timer_seconds': outside, 'audit': audit,
            'load_average': setup['load_average'], 'verified': audit['verified'],
            'verified_full_source': variant == 'normal', 'verified_empty_stage': variant != 'normal'}


def run(*, host, rounds=3, mib=375, files=5000):
    if rounds < 1:
        raise ValueError('rounds must be positive')
    rsync = shutil.which('rsync')
    if not rsync:
        raise RuntimeError('rsync required')
    with tempfile.TemporaryDirectory(prefix='rsync-probe-src-') as local, \
            tempfile.TemporaryDirectory(prefix='rsync-probe-', dir='/tmp') as control:
        baseline, tiny, empty, original, records, details = fixtures(Path(local), mib, files)
        link = NETWORK.Link(host, Path(control))
        try:
            began = time.monotonic()
            allocated = json.loads(link.command(['python3', '-c', NETWORK.ALLOCATE]))
            link.root, link.token = allocated['root'], allocated['token']
            startup_seconds = time.monotonic() - began
            link.command(['python3', '-c', NETWORK.UPLOAD, link.root, link.token], NETWORK.code_archive())
            began = time.monotonic()
            NETWORK.rsync_send(link, rsync, baseline, link.root + '/baseline', original)
            baseline_upload_seconds = time.monotonic() - began
            link.call('baseline_audit', {'records': original})  # Never initialize/seed CAS.
            version = link.command(['rsync', '--version']).decode()
            filesystem = link.command(['df', '-T', link.root]).decode()
            samples, priming = [], []
            for round_index in range(-1, rounds):
                offset = max(round_index, 0) % len(VARIANTS)
                order = [*VARIANTS[offset:], *VARIANTS[:offset]]
                for position, variant in enumerate(order):
                    source, chosen = (empty, []) if variant in ('empty', 'ssh_true') else (baseline, original)
                    result = sample(link, rsync, source, chosen, variant, 'probe_%d_%d' % (round_index + 1, position))
                    result.update(round=round_index, order=position)
                    (priming if round_index == -1 else samples).append(result)
                    print('%s round=%d wall=%.3fs audited' % (variant, round_index, result['wall_seconds']), flush=True)
            edit = sample(link, rsync, tiny, records, 'normal', 'edit_probe')
            debug = sample(link, rsync, baseline, original, 'normal', 'debug_probe', debug=True)
            immutable, _, _ = link.call('baseline_audit', {'records': original})
            PROFILE.verify(baseline, original)
            PROFILE.verify(tiny, records)
            cleanup, _, _ = link.call('cleanup', {})
            link.root = None
            return {'schema': 1, 'variants': list(VARIANTS), 'rounds': rounds, 'priming_rounds_excluded': 1,
                    'samples': samples, 'priming': priming, 'edit_probe': edit, 'debug_probe': debug, 'fixture': details,
                    'baseline_upload_seconds': baseline_upload_seconds, 'baseline_audit': immutable,
                    'source_variants_immutable': True, 'cleanup': cleanup,
                    'rsync': {'sender_path': rsync, 'sender_version': subprocess.check_output([rsync, '--version'], text=True),
                              'receiver_version': version}, 'receiver_filesystem': filesystem,
                    'transport': {'host': host, 'startup_seconds': startup_seconds, 'master_close': link.close(),
                                  'policy': 'one owned isolated warmed private SSH master; auto/persist10m'},
                    'limitations': ['Only rsync or ssh true invocation is timed; source generation/capture/upload, setup/plan/finalize/audit are excluded and reported separately.',
                                    'Dry-run and empty/control variants prove empty output only, not source transfer.',
                                    'Differences between dry-run variants are diagnostics, not isolated checksum CPU measurements.',
                                    'Waited-child CPU excludes persistent master and SSH server CPU; receiver helper imports/startup are not attributed.',
                                    'Raw filesystem blocks are not logical byte reads; filesystem caches and shared worker load are uncontrolled.',
                                    *(['%d measured rotations only partially balance five variant positions; this is exploratory evidence.' % rounds]
                                      if rounds % len(VARIANTS) else []),
                                    'NSTR/CMD debug is an additional untimed audited probe, never a measured sample.']}
        finally:
            link.deadline = None
            try:
                if link.root:
                    link.command(['python3', '-c', NETWORK.EMERGENCY_CLEANUP, link.root, link.token], timeout=120)
            finally:
                if not link.closed:
                    link.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='ubuntu@5.135.138.35')
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--mib', type=float, default=375)
    parser.add_argument('--files', type=int, default=5000)
    parser.add_argument('--out', type=Path, default=Path('artifacts/rsync-diagnostic.json'))
    args = parser.parse_args(argv)
    report = run(host=args.host, rounds=args.rounds, mib=args.mib, files=args.files)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')


if __name__ == '__main__':
    main()
