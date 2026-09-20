#!/usr/bin/env python3
"""Exercise a durable image reservation across rebuild, removal and collection."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import uuid

p = argparse.ArgumentParser()
p.add_argument('--host', required=True)
p.add_argument('--root', type=Path, required=True)
a = p.parse_args()
a.root.mkdir(parents=True, exist_ok=False)
base = Path(__file__).resolve().parents[1]
repo = a.root / 'repo'
shutil.copytree(base / 'docker/fixture', repo)
initial = 'alpha-' + uuid.uuid4().hex
(repo / 'value.txt').write_text(initial + '\n')
subprocess.run(['git', 'init', '-q', str(repo)], check=True)
subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Trial', '-c', 'user.email=trial@example.com', 'commit', '-qm', 'fixture'], check=True)
ssh = ['ssh', '-o', 'BatchMode=yes', a.host]
key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()
profile = json.loads((base / 'docker/profile.json').read_text())
command = ['python3', str(base / 'routing/launch.py'), '--host', a.host, '--state', str(a.root / 'state'),
           '--docker-profile', str(base / 'docker/profile.json'), '--']

def route(name, *argv):
    with (a.root / (name + '.log')).open('w') as log:
        subprocess.run(command + list(argv), cwd=repo, stdout=log, stderr=subprocess.STDOUT, check=True)


def remote(script):
    return subprocess.check_output([*ssh, 'python3 -'], input=script, text=True)

route('build-old', 'docker', 'build', '-t', 'gc:test', '.')
attempt = uuid.uuid4().hex
reserved = json.loads(subprocess.check_output([*ssh, 'python3 - ' + key + ' gc:test ' + attempt],
                     input=(base / 'warm/docker_images.py').read_text(), text=True))
(repo / 'value.txt').write_text('beta\n')
route('build-new', 'docker', 'build', '-t', 'gc:test', '.')
route('remove', 'docker', 'image', 'rm', 'gc:test')
# Use current uploaded helpers, holding the same worker lease as admission.
helper = remote("from pathlib import Path\nr=Path.home()/'pandora-warm/runs'\nprint(max((p for p in r.glob('*/image_gc.py')),key=lambda p:p.stat().st_mtime).parent)\n").strip()
gc_script = f"""import sys, fcntl, json
from pathlib import Path
sys.path.insert(0, {helper!r})
from image_gc import collect
root=Path.home()/'pandora-warm'
with (root/'worker.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX)
 removed=collect(root)
 print(json.dumps(removed))
"""
first_gc = remote(gc_script)
subprocess.run([*ssh, 'sudo docker image inspect ' + shlex.quote(reserved['image_id']) + ' >/dev/null'], check=True)
# Resume the reserved request after its logical tag has disappeared.
spec = {'request': {'kind': 'run', 'tag': 'gc:test', 'mount': None, 'command': []},
        'config': profile, 'worktree_key': key}
with (a.root / 'reserved-run.log').open('w') as log:
    subprocess.run(['python3', str(base / 'warm/warm.py'), '--host', a.host, '--repo', str(repo),
                    '--output', str(a.root / 'reserved-run'), '--workflow', 'docker', '--attempt', attempt,
                    '--docker-request', json.dumps(spec)], stdout=log, stderr=subprocess.STDOUT, check=True)
result = json.loads((a.root / 'reserved-run/results/outputs/dist/result.json').read_text())
assert result['value'] == initial, result
terminal = json.loads((a.root / 'reserved-run/terminal.json').read_text())
assert terminal['exit_code'] == 0 and terminal['cleanup_verified']
subprocess.run([*ssh, 'touch ~/pandora-warm/runs/' + attempt + '/released'], check=True)
second_gc = remote(gc_script)
# Physical attempt tag must be gone. Shared layers or equivalent tags may remain.
status = subprocess.run([*ssh, 'sudo docker image inspect pandora-build:' + reserved['attempt'] + ' >/dev/null 2>&1']).returncode
assert status != 0
(a.root / 'summary.json').write_text(json.dumps({'reserved': reserved, 'attempt': attempt,
    'result': result, 'first_gc': first_gc, 'second_gc': second_gc, 'collected_after_ack': True}, indent=2) + '\n')
print(a.root / 'summary.json')
