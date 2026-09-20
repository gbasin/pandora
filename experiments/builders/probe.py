#!/usr/bin/env python3
"""Exclusive VM proof against the existing cached dependency builder."""
import argparse
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'warm'))
from dependencies import BUILDER, CONTAINER, prepare, cleanup

BASE = 'node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6'


def docker(*args):
    return subprocess.run(['sudo', 'docker', *args], check=True, capture_output=True, text=True, timeout=60)


def child(attempt, image):
    with (attempt / 'attempt.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        prepare(attempt / 'deps-context', image)


def main(root, output, sentinel_image):
    output.mkdir(parents=True, exist_ok=False)
    with (root / 'worker.lock').open('a') as worker:
        fcntl.flock(worker, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not docker('ps', '--format', '{{.Names}}').stdout.strip(), 'Worker must be idle'
        before = json.loads(docker('inspect', CONTAINER).stdout)[0]['Mounts']
        attempts, images = [], []
        process = None
        sentinel = 'pandora-builder-sentinel-' + uuid.uuid4().hex
        evidence = {}
        try:
            docker('run', '-d', '--name', sentinel, '--cpus=.1', '--memory=64m',
                   '--memory-swap=64m', '--network=none', sentinel_image, 'sleep', '300')
            for mode in ('cold', 'interrupted', 'warm'):
                attempt = root / 'runs' / uuid.uuid4().hex
                context = attempt / 'deps-context'
                context.mkdir(parents=True)
                attempts.append(attempt)
                image = 'pandora-deps:' + uuid.uuid4().hex + uuid.uuid4().hex
                images.append(image)
                command = ('echo ' + attempt.name + ' && sleep 300') if mode == 'interrupted' else 'echo builder-ownership-proof > /proof'
                (context / 'Dockerfile').write_text('FROM ' + BASE + '\nRUN ' + command + '\n')
                (context / 'buildkitd.toml').write_text('[worker.oci]\n  gc = true\n')
                logfile = output / (mode + '.log')
                with logfile.open('w') as log:
                    process = subprocess.Popen([sys.executable, '-B', __file__, '--child', str(attempt), image], stdout=log, stderr=subprocess.STDOUT)
                    if mode == 'interrupted':
                        deadline = time.monotonic() + 90
                        while not any(line.rstrip().endswith(attempt.name) and 'RUN ' not in line for line in logfile.read_text().splitlines()):
                            if process.poll() is not None or time.monotonic() > deadline:
                                raise RuntimeError('Build did not reach the interruption point')
                            time.sleep(.25)
                        # A delayed cleanup from the previous attempt must not
                        # stop this active owner, even though the name is shared.
                        assert cleanup(attempts[0])
                        assert docker('ps', '--filter', 'name=^/' + CONTAINER + '$', '--format', '{{.Names}}').stdout.strip()
                        process.kill()
                        process.wait(timeout=10)
                        assert (attempt / 'dependency-cleanup.pending').exists()
                        subprocess.run([sys.executable, '-B', str(Path(__file__).resolve().parents[1] / 'warm/service_cleanup.py'), str(attempt)], check=True, timeout=90)
                        assert json.loads((attempt / 'admission-cleanup.json').read_text())['cleanup_verified'] is True
                        assert not (attempt / 'dependency-cleanup.pending').exists()
                        assert not (attempt / 'terminal.json').exists()
                        evidence['dead_owner_cleaned_without_test_terminal'] = True
                        evidence['systemd_cleanup_entrypoint_verified'] = True
                        evidence['delayed_cleanup_did_not_stop_successor'] = True
                    else:
                        assert process.wait(timeout=120) == 0, logfile.read_text()
                        assert not (attempt / 'dependency-cleanup.pending').exists()
                        if mode == 'warm':
                            assert 'CACHED' in logfile.read_text()
                            evidence['cache_reused_after_crash'] = True
                assert not docker('ps', '--filter', 'name=^/' + CONTAINER + '$', '--format', '{{.Names}}').stdout.strip()
                assert docker('ps', '--filter', 'name=^/' + sentinel + '$', '--format', '{{.Names}}').stdout.strip()
            after = json.loads(docker('inspect', CONTAINER).stdout)[0]['Mounts']
            assert before == after
            evidence.update(attempts=[a.name for a in attempts], builder_volume_preserved=True, sentinel_survived=True)
            (output / 'result.json').write_text(json.dumps(evidence, indent=2) + '\n')
            print(json.dumps(evidence))
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            for attempt in attempts:
                assert cleanup(attempt), 'Probe cleanup unresolved for ' + attempt.name
            docker('rm', '-f', sentinel)
            for image in images:
                result = subprocess.run(['sudo', 'docker', 'image', 'rm', image], capture_output=True, text=True, timeout=30)
                if result.returncode and 'No such image' not in result.stderr:
                    raise RuntimeError(result.stderr)


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--child':
        child(Path(sys.argv[2]), sys.argv[3])
    else:
        parser = argparse.ArgumentParser()
        parser.add_argument('--root', type=Path, required=True)
        parser.add_argument('--output', type=Path, required=True)
        parser.add_argument('--sentinel-image', required=True)
        args = parser.parse_args()
        main(args.root, args.output, args.sentinel_image)
