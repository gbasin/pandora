#!/usr/bin/env python3
"""Real Docker workflow build/run/failure/crash proof, under exclusive capacity."""
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'warm'))
from docker_workflow import execute
from docker_cleanup import BUILDER_CONTAINER, cleanup
from docker_images import resolve, remove
from probe import BASE, docker


def child(attempt):
    with (attempt / 'attempt.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        return execute(attempt, json.loads((attempt / 'submission.json').read_text()), {})


def main(root, output, sentinel_image):
    output.mkdir(parents=True, exist_ok=False)
    key, tag = uuid.uuid4().hex + uuid.uuid4().hex, 'pandora-owner-probe'
    attempts = []
    process = None
    sentinel = 'pandora-docker-owner-sentinel-' + uuid.uuid4().hex
    evidence = {}
    with (root / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not docker('ps', '--format', '{{.Names}}').stdout.strip(), 'Worker must be idle'
        before = json.loads(docker('inspect', BUILDER_CONTAINER).stdout)[0]['Mounts']
        try:
            docker('run', '-d', '--name', sentinel, '--cpus=.1', '--memory=64m', '--memory-swap=64m',
                   '--network=none', sentinel_image, 'sleep', '300')
            previous = None
            for mode in ('build', 'run', 'failed', 'interrupted', 'warm'):
                attempt = root / 'runs' / uuid.uuid4().hex
                (attempt / 'source').mkdir(parents=True)
                attempts.append(attempt)
                if mode == 'run':
                    spec = {'worktree_key': key, 'image': previous, 'config': {'outputs': [], 'network': 'none'},
                            'request': {'kind': 'run', 'tag': tag, 'mount': None,
                                        'command': ['sh', '-c', 'test "$(cat /proof)" = docker-owner-proof']}}
                else:
                    command = ('echo ' + attempt.name + ' && sleep 300') if mode == 'interrupted' else 'exit 7' if mode == 'failed' else 'echo docker-owner-proof > /proof'
                    (attempt / 'source/Dockerfile').write_text('FROM ' + BASE + '\nRUN ' + command + '\n')
                    spec = {'worktree_key': key, 'request': {'kind': 'build', 'tag': tag, 'dockerfile': 'Dockerfile'}}
                (attempt / 'submission.json').write_text(json.dumps({'workflow': 'docker', 'source_digest': 'd' * 64, 'docker': spec}))
                logfile = output / (mode + '.log')
                with logfile.open('w') as log:
                    process = subprocess.Popen([sys.executable, '-B', __file__, '--child', str(attempt)], stdout=log, stderr=subprocess.STDOUT)
                    if mode == 'interrupted':
                        deadline = time.monotonic() + 90
                        while not any(line.rstrip().endswith(attempt.name) and 'RUN ' not in line for line in logfile.read_text().splitlines()):
                            if process.poll() is not None or time.monotonic() > deadline:
                                raise RuntimeError('Build did not reach interruption point')
                            time.sleep(.25)
                        assert cleanup(attempts[0])
                        assert docker('ps', '--filter', 'name=^/' + BUILDER_CONTAINER + '$', '--format', '{{.Names}}').stdout.strip()
                        process.kill()
                        process.wait(timeout=10)
                        subprocess.run([sys.executable, '-B', str(Path(__file__).resolve().parents[1] / 'warm/service_cleanup.py'), str(attempt)], check=True, timeout=90)
                        assert json.loads((attempt / 'admission-cleanup.json').read_text())['cleanup_verified'] is True
                        assert not (attempt / 'terminal.json').exists()
                        assert resolve(root, key, tag) == previous
                        evidence['crash_cleanup_preserved_previous_mapping'] = True
                        evidence['delayed_cleanup_left_successor_running'] = True
                    else:
                        status = process.wait(timeout=120)
                        assert status == (1 if mode == 'failed' else 0), logfile.read_text()
                        assert json.loads((attempt / 'docker-cleanup.json').read_text())['verified'] is True
                        if mode == 'failed':
                            assert resolve(root, key, tag) == previous
                            evidence['failed_rebuild_preserved_previous_mapping'] = True
                        elif mode == 'run':
                            evidence['built_image_ran_in_separate_call'] = True
                        else:
                            previous = resolve(root, key, tag)
                            if mode == 'warm':
                                assert 'CACHED' in logfile.read_text()
                                evidence['cache_reused_after_crash'] = True
                assert not (attempt / 'docker-cleanup.pending').exists()
                assert not docker('ps', '--filter', 'name=^/' + BUILDER_CONTAINER + '$', '--format', '{{.Names}}').stdout.strip()
                assert docker('ps', '--filter', 'name=^/' + sentinel + '$', '--format', '{{.Names}}').stdout.strip()
            assert before == json.loads(docker('inspect', BUILDER_CONTAINER).stdout)[0]['Mounts']
            evidence.update(attempts=[a.name for a in attempts], builder_volume_preserved=True, sentinel_survived=True)
            (output / 'result.json').write_text(json.dumps(evidence, indent=2) + '\n')
            print(json.dumps(evidence))
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            for attempt in attempts:
                assert cleanup(attempt), 'Probe cleanup unresolved: ' + attempt.name
            docker('rm', '-f', sentinel)
            remove(root, key, tag)
            for attempt in attempts:
                result = subprocess.run(['sudo', 'docker', 'image', 'rm', 'pandora-build:' + attempt.name], capture_output=True, text=True, timeout=30)
                if result.returncode and 'No such image' not in result.stderr:
                    raise RuntimeError(result.stderr)


if __name__ == '__main__':
    if sys.argv[1] == '--child':
        raise SystemExit(child(Path(sys.argv[2])))
    main(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
