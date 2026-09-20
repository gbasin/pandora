#!/usr/bin/env python3
"""Exercise the free-space guard on a bounded tmpfs, never the worker's disk."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'warm'))
from execution_guard import Guard


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise ValueError('Evidence destination already exists')
    with tempfile.TemporaryDirectory(prefix='pandora-disk-guard-') as temp:
        attempt = Path(temp)
        options = f'size=128m,uid={os.getuid()},gid={os.getgid()},mode=0700'
        subprocess.run(['sudo', 'mount', '-t', 'tmpfs', '-o', options, 'tmpfs', temp], check=True)
        guard = None
        try:
            interrupted = threading.Event()
            config = {'execution_seconds': 30, 'scheduler': {'disk_floor_mib': 100}}
            guard = Guard(attempt, config, interval=.1, interrupt=interrupted.set).start()
            with (attempt / 'bounded-payload').open('wb') as payload:
                for _ in range(64):
                    payload.write(b'x' * 1024**2)
                payload.flush()
                os.fsync(payload.fileno())
            assert interrupted.wait(5), 'Guard did not report low free space'
            guard.close(); guard = None
            result = json.loads((attempt / 'execution-stop.json').read_text())
            assert result['reason'] == 'disk-floor'
            assert (attempt / 'disk-stop.request').is_file()
            assert not (attempt / 'deadline.request').exists()
            args.output.write_text(json.dumps({'assertions': 'passed', 'stop': result,
                'filesystem_capacity_mib': 128, 'payload_mib': 64}, indent=2) + '\n')
        finally:
            if guard is not None:
                guard.close()
            subprocess.run(['sudo', 'umount', temp], check=True)


if __name__ == '__main__':
    main()
