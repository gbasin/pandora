#!/usr/bin/env python3
"""Measure rsync transfer processing using disposable worker-local source trees.

Run through the repository's Pandora benchmark job. No production source cache,
SSH transport, daemon state, or configuration is used. Local transport timings
cannot establish WAN compression savings or CAS performance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import re
import shutil
import subprocess
import tempfile
import time

CHUNK = 1 << 20
CASES = ('cold', 'warm_unchanged', 'warm_delta', 'divergent', 'four_bases',
         'cold_compressed')
STAT_LABELS = {
    'transferred_file_bytes': ('Total transferred file size',),
    'literal_bytes': ('Literal data',),
    'matched_bytes': ('Matched data',),
    'file_list_bytes': ('File list size',),
    'sent_bytes': ('Total bytes sent',),
    'received_bytes': ('Total bytes received',),
    'regular_files_transferred': ('Number of regular files transferred',
                                  'Number of files transferred'),
}


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b''):
            value.update(chunk)
    return value.hexdigest()


def manifest(root):
    records = []
    for path in sorted(root.rglob('*')):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            records.append({'path': name, 'link': os.readlink(path)})
        elif path.is_file():
            records.append({'path': name, 'sha256': digest(path),
                            'mode': path.stat().st_mode & 0o777})
    return records


def verify(root, expected):
    if manifest(root) != expected:
        raise RuntimeError('materialized source differs in paths, SHA256, modes, or symlinks')


def write_payload(path, size, seed, compressible):
    """Stream deterministic data without holding a whole large file in memory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    pattern = ('synthetic source fixture %d\n' % seed).encode()
    left = size
    with path.open('wb') as handle:
        while left:
            width = min(CHUNK, left)
            if compressible:
                block = (pattern * ((width + len(pattern) - 1) // len(pattern)))[:width]
            else:
                block = rng.randbytes(width)
            handle.write(block)
            left -= width
    path.chmod(0o644)


def fixture(root, mib, files):
    if mib <= 0 or files < 103:
        raise ValueError('mib must be positive and files must be at least 103')
    total = int(mib * (1 << 20))
    small_count = files - 100
    small_size = min(128, total // (files * 2))
    if small_size < 1:
        raise ValueError('fixture byte budget is too small for the requested file count')
    large_total = total - small_count * small_size
    large_size, extra = divmod(large_total, 100)
    root.mkdir()
    for index in range(100):
        write_payload(root / ('large/%03d.bin' % index), large_size + (index < extra),
                      index, index < 50)
    for index in range(small_count):
        name = ('small/%05d.txt' % index)
        if index == small_count - 2:
            name = 'small/path with spaces.txt'
        elif index == small_count - 1:
            name = 'small/path with\nnewline.txt'
        write_payload(root / name, small_size, 1000 + index, True)
    (root / 'small/00000.txt').chmod(0o755)
    (root / 'internal-link').symlink_to('small/00000.txt')
    return {'regular_files': files, 'manifest_paths': files + 1,
            'regular_bytes': total, 'large_files': 100,
            'compressible_large_files': 50, 'incompressible_large_files': 50,
            'small_files': small_count, 'small_file_bytes': small_size,
            'symlinks': 1, 'executable_path': 'small/00000.txt',
            'odd_paths': ['small/path with spaces.txt', 'small/path with\nnewline.txt']}


def clone_links(source, target):
    shutil.copytree(source, target, copy_function=os.link, symlinks=True)


def replace_payload(path, seed, compressible, *, mode=None, mtime=None):
    """Never truncate, chmod, or otherwise write a hardlinked source inode."""
    old = path.stat()
    temporary = path.with_name(path.name + '.replacement')
    try:
        write_payload(temporary, old.st_size, seed, compressible)
        temporary.chmod(old.st_mode & 0o777 if mode is None else mode)
        if mtime is not None:
            os.utime(temporary, ns=(old.st_atime_ns, mtime))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def parse_stats(stdout):
    result = {name: None for name in STAT_LABELS}
    for name, labels in STAT_LABELS.items():
        for label in labels:
            found = re.search(r'^' + re.escape(label) + r':\s*([0-9][0-9, ]*)',
                              stdout, flags=re.MULTILINE)
            if found:
                result[name] = int(re.sub(r'[^0-9]', '', found.group(1)))
                break
    return result


def shared_inodes(target, bases, records):
    source_inodes = set()
    for base in bases:
        for record in records:
            if 'sha256' not in record:
                continue
            path = base / record['path']
            if path.is_file() and not path.is_symlink():
                value = path.stat()
                source_inodes.add((value.st_dev, value.st_ino))
    count = byte_count = 0
    for record in records:
        if 'sha256' not in record:
            continue
        value = (target / record['path']).stat()
        if (value.st_dev, value.st_ino) in source_inodes:
            count += 1
            byte_count += value.st_size
    return {'regular_files': count, 'regular_bytes': byte_count}


def transfer(rsync, source, target, bases, records, compressed=False):
    target.mkdir()
    names = b'\0'.join(record['path'].encode() for record in records) + b'\0'
    argv = [rsync, '-a', '--no-times', '--checksum', '--delete', '--files-from=-',
            '--from0', '--stats', '--no-whole-file']
    argv.extend('--link-dest=' + str(base.resolve()) for base in bases)
    if compressed:
        argv.append('-z')
    argv.extend((str(source) + '/', str(target) + '/'))
    started = time.monotonic()
    proc = subprocess.run(argv, input=names, capture_output=True, timeout=1800,
                          env={**os.environ, 'LC_ALL': 'C'})
    wall = time.monotonic() - started
    stdout = proc.stdout.decode('utf-8', 'replace')
    stderr = proc.stderr.decode('utf-8', 'replace')
    if proc.returncode:
        raise RuntimeError('scratch rsync failed (%d): %s' % (proc.returncode, stderr))
    verify_started = time.monotonic()
    verify(target, records)
    verified_seconds = time.monotonic() - verify_started
    return {'wall_seconds': wall, 'verification_seconds': verified_seconds,
            'stats': parse_stats(stdout), 'stdout': stdout, 'stderr': stderr,
            'shared_with_bases': shared_inodes(target, bases, records),
            'verified': True}


def sanity(rsync, scratch, baseline, original):
    probes = {}
    same_size = scratch / 'probe-same-size'
    clone_links(baseline, same_size)
    edited = same_size / 'large/000.bin'
    original_mtime = edited.stat().st_mtime_ns
    replace_payload(edited, 900001, True, mtime=original_mtime)
    records = manifest(same_size)
    if edited.stat().st_mtime_ns != original_mtime:
        raise RuntimeError('same-size probe failed to preserve mtime')
    probes['same_size_edit_original_mtime'] = transfer(
        rsync, same_size, scratch / 'probe-same-size-target', [baseline], records)
    if digest(edited) == digest(baseline / 'large/000.bin'):
        raise RuntimeError('same-size probe did not change content')

    # Copies have independent inodes: changing mtimes must not mutate the base.
    new_mtime = scratch / 'probe-new-mtime'
    shutil.copytree(baseline, new_mtime, symlinks=True)
    for record in original:
        if 'sha256' in record:
            path = new_mtime / record['path']
            value = path.stat()
            os.utime(path, ns=(value.st_atime_ns, value.st_mtime_ns + 10_000_000_000))
    probes['unchanged_content_new_mtime'] = transfer(
        rsync, new_mtime, scratch / 'probe-new-mtime-target', [baseline], original)
    expected_regular = sum('sha256' in record for record in original)
    if probes['unchanged_content_new_mtime']['shared_with_bases']['regular_files'] != expected_regular:
        raise RuntimeError('unchanged bytes with new mtimes did not reuse every regular file')

    mode = scratch / 'probe-mode'
    clone_links(baseline, mode)
    path = mode / 'small/00000.txt'
    temporary = path.with_name(path.name + '.replacement')
    shutil.copyfile(path, temporary)
    temporary.chmod(0o644)
    os.replace(temporary, path)
    probes['same_bytes_changed_mode'] = transfer(
        rsync, mode, scratch / 'probe-mode-target', [baseline], manifest(mode))
    verify(baseline, original)
    probes['baseline_immutable'] = True
    return probes


def run_benchmark(*, mib=375, files=5000, rounds=10, rsync=None):
    if rounds < 1:
        raise ValueError('rounds must be at least one')
    rsync = rsync or shutil.which('rsync')
    if not rsync:
        raise RuntimeError('rsync is required')
    version = subprocess.run([rsync, '--version'], capture_output=True, text=True,
                             check=True, env={**os.environ, 'LC_ALL': 'C'}).stdout
    with tempfile.TemporaryDirectory(prefix='pandora-transfer-poc-') as temporary:
        scratch = Path(temporary)
        baseline = scratch / 'baseline'
        details = fixture(baseline, mib, files)
        original = manifest(baseline)
        details['manifest_sha256'] = hashlib.sha256(
            json.dumps(original, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        delta = scratch / 'delta'
        divergent = scratch / 'divergent'
        clone_links(baseline, delta)
        replace_payload(delta / 'large/000.bin', 100000, True)
        clone_links(baseline, divergent)
        for index in range(90):
            replace_payload(divergent / ('large/%03d.bin' % index), 200000 + index, index < 50)
        delta_records, divergent_records = manifest(delta), manifest(divergent)
        original_by_path = {record['path']: record for record in original}
        for label, source, records in (('delta', delta, delta_records),
                                       ('divergent', divergent, divergent_records)):
            changed = [record['path'] for record in records if 'sha256' in record
                       and record['sha256'] != original_by_path[record['path']]['sha256']]
            details[label + '_changed_regular_files'] = len(changed)
            details[label + '_changed_bytes'] = sum((source / name).stat().st_size for name in changed)
        poor = []
        for index in range(3):
            base = scratch / ('poor-%d' % index)
            clone_links(divergent, base)
            poor.append(base)
        setup = {
            'cold': (baseline, [], original, False),
            'warm_unchanged': (baseline, [baseline], original, False),
            'warm_delta': (delta, [baseline], delta_records, False),
            'divergent': (baseline, [poor[0]], original, False),
            'four_bases': (delta, poor + [baseline], delta_records, False),
            'cold_compressed': (baseline, [], original, True),
        }
        samples = []
        priming = []
        for round_index in range(-1, rounds):
            order = list(CASES)
            rotation = max(round_index, 0) % len(order)
            order = order[rotation:] + order[:rotation]
            for position, case in enumerate(order):
                source, bases, records, compressed = setup[case]
                target = scratch / ('target-%s-%d' % (case, round_index))
                sample = transfer(rsync, source, target, bases, records, compressed)
                sample.update(case=case, round=round_index, order=position)
                (priming if round_index == -1 else samples).append(sample)
                shutil.rmtree(target)
        probes = sanity(rsync, scratch, baseline, original)
        verify(baseline, original)
        verify(delta, delta_records)
        verify(divergent, divergent_records)
        for base in poor:
            verify(base, divergent_records)
        return {
            'schema': 1, 'rsync': {'path': rsync, 'version': version},
            'machine': {'platform': platform.platform(), 'machine': platform.machine(),
                        'python': platform.python_version(), 'logical_cpus': os.cpu_count()},
            'fixture': details, 'rounds': rounds, 'priming_rounds_excluded': 1,
            'samples': samples, 'priming': priming, 'sanity_probes': probes,
            'scope': 'worker-local scratch rsync; warm filesystem caches; no SSH or live cache',
            'limitations': [
                'Local transport cannot establish WAN compression savings or CAS performance.',
                'No system caches were dropped; later cases can benefit from prior reads.',
                'Three poor bases are synthetic equivalent hardlinked copies of one divergent tree.',
                'Fixture preparation, verification, and inode accounting are excluded from transfer wall time.',
                'Production SSH helper calls, freeze, submit, and container injection are not measured.',
            ],
            'scratch_cleanup': 'TemporaryDirectory removes all source and target trees on exit',
            'source_variants_immutable': True,
        }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mib', type=float, default=375)
    parser.add_argument('--files', type=int, default=5000)
    parser.add_argument('--rounds', type=int, default=10)
    parser.add_argument('--output', type=Path, default=Path('artifacts/transfer-profile.json'))
    args = parser.parse_args(argv)
    result = run_benchmark(mib=args.mib, files=args.files, rounds=args.rounds)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print('Transfer profile written to %s' % args.output)


if __name__ == '__main__':
    main()
