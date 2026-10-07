#!/usr/bin/env python3
"""Compare scratch rsync and CAS policies on identical worker-local fixtures."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import time

from cas import CasStore, writable_copy

SPEC = importlib.util.spec_from_file_location(
    'rsync_profile', Path(__file__).parents[1] / 'transfer-profile' / 'benchmark.py')
profile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profile)
CASES = ('cold', 'warm_unchanged', 'warm_small_delta', 'warm_large_delta',
         'divergent', 'retained_history', 'mode_change')
METHODS = ('rsync', 'cas_trusted', 'cas_rehash')


def canonicalize(source):
    for path in source.rglob('*'):
        if path.is_file() and not path.is_symlink():
            path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)


def change_mode_copy(path, mode):
    replacement = path.with_name(path.name + '.replacement')
    shutil.copyfile(path, replacement)
    replacement.chmod(mode)
    os.replace(replacement, path)


def expected_execution(records):
    return [dict(record, mode=0o755 if record['mode'] & 0o111 else 0o644)
            if 'mode' in record else dict(record) for record in records]


def store_inventory(store):
    paths = list(store.blobs.iterdir())
    return {'blob_count': len(paths), 'blob_bytes': sum(path.stat().st_size for path in paths)}


def measure(method, source, bases, records, store, target, rsync):
    if method == 'rsync':
        result = profile.transfer(rsync, source, target, bases, records)
        result['steps'] = {'rsync': result['wall_seconds']}
    else:
        result = store.transfer(source, target, records, rsync=rsync,
                                integrity='trusted' if method == 'cas_trusted' else 'rehash')
        began = time.monotonic()
        profile.verify(target, records)
        result['verification_seconds'] = time.monotonic() - began
    # Both algorithms get the same independent execution-copy operation.
    execution = target.with_name(target.name + '.execution')
    began = time.monotonic()
    writable_copy(target, execution)
    result['execution_copy_seconds'] = time.monotonic() - began
    began = time.monotonic()
    profile.verify(execution, expected_execution(records))
    for record in records:
        if 'sha256' in record:
            cached, copied = (target / record['path']).stat(), (execution / record['path']).stat()
            if (cached.st_dev, cached.st_ino) == (copied.st_dev, copied.st_ino):
                raise RuntimeError('execution copy shares a writable cached inode')
    result['execution_audit_seconds'] = time.monotonic() - began
    result['verified_transfer_and_copy_seconds'] = (result['wall_seconds']
        + result['verification_seconds'] + result['execution_copy_seconds']
        + result['execution_audit_seconds'])
    result['transfer_and_copy_seconds'] = result['wall_seconds'] + result['execution_copy_seconds']
    result['verified'] = True
    shutil.rmtree(execution)
    shutil.rmtree(target)
    return result


def run_benchmark(*, mib=375, files=5000, rounds=5, rsync=None):
    if rounds < 1:
        raise ValueError('rounds must be positive')
    rsync = rsync or shutil.which('rsync')
    if not rsync:
        raise RuntimeError('rsync is required')
    version = subprocess.run([rsync, '--version'], capture_output=True, text=True,
                             check=True, env={**os.environ, 'LC_ALL': 'C'}).stdout
    with tempfile.TemporaryDirectory(prefix='pandora-cas-comparison-') as temporary:
        root = Path(temporary)
        baseline = root / 'baseline'
        details = profile.fixture(baseline, mib, files)
        canonicalize(baseline)  # Before any sharing; never chmod shared inodes.
        original = profile.manifest(baseline)
        small, large, divergent, mode = (root / name for name in ('small', 'large', 'divergent', 'mode'))
        for source in (small, large, divergent, mode):
            profile.clone_links(baseline, source)
        profile.replace_payload(small / details['odd_paths'][0], 400001, True)
        profile.replace_payload(large / 'large/000.bin', 400002, True)
        for index in range(90):
            profile.replace_payload(divergent / ('large/%03d.bin' % index), 500000 + index, index < 50)
        change_mode_copy(mode / 'small/00000.txt', 0o444)
        small_records, large_records, divergent_records, mode_records = (
            profile.manifest(source) for source in (small, large, divergent, mode))
        poor = [root / ('poor-%d' % index) for index in range(4)]
        for base in poor:
            profile.clone_links(divergent, base)
        # retained_history deliberately gives CAS a wider retained content set.
        # Its extra storage is counted, and it is not a same-retention comparison.
        setup = {
            'cold': (baseline, [], original, []),
            'warm_unchanged': (baseline, [baseline], original, [(baseline, original)]),
            'warm_small_delta': (small, [baseline], small_records, [(baseline, original)]),
            'warm_large_delta': (large, [baseline], large_records, [(baseline, original)]),
            'divergent': (baseline, poor, original, [(divergent, divergent_records)]),
            'retained_history': (baseline, poor, original,
                                 [(baseline, original), (divergent, divergent_records)]),
            'mode_change': (mode, [baseline], mode_records, [(baseline, original)]),
        }
        details['readonly_modes'] = ['0444', '0555']
        details['manifest_sha256'] = hashlib.sha256(
            json.dumps(original, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        details['case_manifest_bytes'] = {case: len(json.dumps(value[2], separators=(',', ':')).encode())
                                          for case, value in setup.items()}
        samples, priming = [], []
        for round_index in range(-1, rounds):
            cases = list(CASES)
            rotation = max(round_index, 0) % len(cases)
            cases = cases[rotation:] + cases[:rotation]
            for case_index, case in enumerate(cases):
                source, bases, records, seeds = setup[case]
                methods = list(METHODS)
                rotation = (max(round_index, 0) + case_index) % len(methods)
                methods = methods[rotation:] + methods[:rotation]
                for method_index, method in enumerate(methods):
                    # Fresh stores prevent a preceding policy/case from lending
                    # new blobs. Seed verification is excluded, like rsync bases.
                    store = CasStore(root / ('store-%s-%s-%d' % (case, method, round_index)))
                    if method != 'rsync':
                        for seeded_source, seeded_records in seeds:
                            store.seed(seeded_source, seeded_records)
                    inventory = store_inventory(store)
                    target = root / ('target-%s-%s-%d' % (case, method, round_index))
                    result = measure(method, source, bases, records, store, target, rsync)
                    result.update(case=case, method=method, round=round_index,
                                  case_order=case_index, method_order=method_index,
                                  initial_store=inventory, final_store=store_inventory(store),
                                  candidate_bases=len(bases))
                    (priming if round_index == -1 else samples).append(result)
                    shutil.rmtree(store.root)
        # Every source/base is checked after all shared-inode operations.
        for source, records in ((baseline, original), (small, small_records),
                                (large, large_records), (divergent, divergent_records),
                                (mode, mode_records), *((base, divergent_records) for base in poor)):
            profile.verify(source, records)
        return {
            'schema': 1, 'rsync': {'path': rsync, 'version': version},
            'machine': {'platform': platform.platform(), 'python': platform.python_version(),
                        'machine': platform.machine(), 'logical_cpus': os.cpu_count()},
            'fixture': details, 'rounds': rounds, 'priming_rounds_excluded': 1,
            'samples': samples, 'priming': priming,
            'source_variants_immutable': True,
            'scope': 'isolated worker-local comparison; no production caches, SSH or WAN',
            'limitations': [
                'Canonical readonly modes model executable identity, not arbitrary original permission bits.',
                'Manifest capture, store/base preparation, external output audits, and source construction are outside transfer timing.',
                'Every new CAS blob is SHA256-verified; trusted reuse assumes verified protected storage, while rehash verifies reused blobs inside its timer.',
                'External full SHA256/mode/symlink audits are timed separately and included in verified_transfer_and_copy_seconds for both paths.',
                'CAS manifest/control-message serialization and transport are not implemented; serialized manifest bytes are reported, not treated as zero wire cost.',
                'Both rsync processes use local transport with --no-whole-file; network RTT, Mac reads, SSH helpers and whole-request costs are unmeasured.',
                'retained_history gives CAS additional retained content; its seeded storage is counted, and equal-GC-budget comparisons remain open.',
                'Filesystem caches are not dropped; method/case order rotates but shared prior reads and seed preparation can still warm caches.',
                'No production concurrency, gateway protocol, source GC, crash durability, or rollout is implemented.',
            ],
            'scratch_cleanup': 'TemporaryDirectory removes all sources, stores and targets on exit',
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mib', type=float, default=375)
    parser.add_argument('--files', type=int, default=5000)
    parser.add_argument('--rounds', type=int, default=5)
    parser.add_argument('--output', type=Path, default=Path('artifacts/cas-poc.json'))
    args = parser.parse_args()
    result = run_benchmark(mib=args.mib, files=args.files, rounds=args.rounds)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    print('CAS comparison written to %s' % args.output)


if __name__ == '__main__':
    main()
