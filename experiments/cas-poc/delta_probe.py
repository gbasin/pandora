#!/usr/bin/env python3
"""Measure the full-blob penalty when a large source file changes by 16 bytes."""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile

import benchmark
from cas import CasStore


def run_probe(*, mib=375, files=5000, rounds=5):
    if rounds < 1:
        raise ValueError('rounds must be positive')
    rsync = shutil.which('rsync')
    if not rsync:
        raise RuntimeError('rsync is required')
    version = subprocess.run([rsync, '--version'], capture_output=True, text=True,
                             check=True, env={**os.environ, 'LC_ALL': 'C'}).stdout
    with tempfile.TemporaryDirectory(prefix='pandora-cas-delta-') as temporary:
        root = Path(temporary)
        baseline, changed = root / 'baseline', root / 'changed'
        details = benchmark.profile.fixture(baseline, mib, files)
        benchmark.canonicalize(baseline)
        original = benchmark.profile.manifest(baseline)
        benchmark.profile.clone_links(baseline, changed)
        path = changed / 'large/000.bin'
        replacement = path.with_name(path.name + '.replacement')
        old = path.stat()
        shutil.copyfile(path, replacement)
        offset = old.st_size // 2
        with replacement.open('r+b') as stream:
            stream.seek(offset)
            previous = stream.read(16)
            if len(previous) != 16:
                raise ValueError('fixture large files must hold at least 32 bytes')
            stream.seek(offset)
            stream.write(bytes(byte ^ 0xFF for byte in previous))
        replacement.chmod(0o444)
        os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
        os.replace(replacement, path)
        records = benchmark.profile.manifest(changed)
        details.update(edited_path='large/000.bin', changed_bytes=16,
                       edited_file_bytes=old.st_size, preserved_original_mtime=True,
                       readonly_modes=['0444', '0555'],
                       case_manifest_bytes={'partial_large_edit': len(
                           json.dumps(records, separators=(',', ':')).encode())})
        samples, priming = [], []
        for round_index in range(-1, rounds):
            methods = list(benchmark.METHODS)
            rotation = max(round_index, 0) % len(methods)
            methods = methods[rotation:] + methods[:rotation]
            for index, method in enumerate(methods):
                store = CasStore(root / ('store-%s-%d' % (method, round_index)))
                if method != 'rsync':
                    store.seed(baseline, original)
                before = benchmark.store_inventory(store)
                row = benchmark.measure(method, changed, [baseline], records, store,
                                        root / ('target-%s-%d' % (method, round_index)), rsync)
                row.update(case='partial_large_edit', method=method, round=round_index,
                           method_order=index, initial_store=before,
                           final_store=benchmark.store_inventory(store), candidate_bases=1)
                (priming if round_index == -1 else samples).append(row)
                shutil.rmtree(store.root)
        benchmark.profile.verify(baseline, original)
        benchmark.profile.verify(changed, records)
        return {'schema': 1, 'scope': 'worker-local 16-byte edit in a large file; no WAN or production cache',
                'rounds': rounds, 'priming_rounds_excluded': 1,
                'fixture': details, 'samples': samples, 'priming': priming,
                'source_variants_immutable': True,
                'machine': {'platform': platform.platform(), 'machine': platform.machine(),
                            'python': platform.python_version(), 'logical_cpus': os.cpu_count()},
                'rsync': {'path': rsync, 'version': version},
                'limitations': [
                    'Local transport timings do not measure the WAN cost of CAS whole-blob upload versus rsync block deltas.',
                    'Missing CAS blobs are sent in full into a fresh stage; new bytes are SHA256-verified before insertion.',
                    'Manifest capture, verified seeding and fixture preparation are excluded; full audits are separate for both paths.',
                    'Canonical readonly/execution modes and both integrity policies are identical to the main comparison.',
                    'No system caches are dropped; method rotation does not eliminate all warming or contention.',
                    'CAS control-message transport, GC, concurrency and crash durability remain unimplemented.',
                ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mib', type=float, default=375)
    parser.add_argument('--files', type=int, default=5000)
    parser.add_argument('--rounds', type=int, default=5)
    parser.add_argument('--output', type=Path, default=Path('artifacts/cas-delta-probe.json'))
    args = parser.parse_args()
    report = run_probe(mib=args.mib, files=args.files, rounds=args.rounds)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    print('CAS delta probe written to %s' % args.output)


if __name__ == '__main__':
    main()
