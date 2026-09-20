"""Scripted Docker semantics probes. Uses disposable local fixture worktrees."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--host', required=True)
p.add_argument('--root', required=True, type=Path)
a = p.parse_args()
base = Path(__file__).resolve().parent
routing = base.parent / 'routing'
a.root.mkdir(parents=True, exist_ok=False)
seed = a.root / 'seed'
shutil.copytree(base / 'fixture', seed)
def git(*args):
    return subprocess.run(['git', *args], check=True, capture_output=True, text=True)
git('init', '-b', 'fixture-seed', str(seed))
git('-C', str(seed), 'add', '.')
git('-C', str(seed), 'commit', '-m', 'test: Docker semantics fixture')
for lane in ('alpha', 'beta'):
    git('-C', str(seed), 'worktree', 'add', str(a.root / lane), '-b', 'test/' + lane)
    (a.root / lane / 'value.txt').write_text(lane + '\n')
state = a.root / 'state'
records = []
command = [sys.executable, str(routing / 'launch.py'), '--host', a.host,
           '--state', str(state), '--docker-profile', str(base / 'profile.json'), '--', 'docker']


def record(lane):
    key = hashlib.sha256(str((a.root / lane).resolve()).encode()).hexdigest()
    path = state / key / 'active.json'
    return json.loads(path.read_text()) if path.exists() else None


def call(label, lane, args, expected=0):
    started = time.monotonic()
    result = subprocess.run([*command, *args], cwd=a.root / lane, capture_output=True, text=True, timeout=180)
    (a.root / (label + '.log')).write_text(result.stdout + result.stderr)
    current = record(lane)
    report = Path(current['output']) / 'results/docker.json' if current else None
    entry = {'case': label, 'lane': lane, 'argv': args, 'seconds': round(time.monotonic() - started, 2),
             'exit_code': result.returncode, 'attempt': current['attempt'] if current else None,
             'report': json.loads(report.read_text()) if report and report.exists() else None}
    records.append(entry)
    (a.root / 'results.json').write_text(json.dumps(records, indent=2) + '\n')
    print(label + ': exit=' + str(result.returncode), flush=True)
    if result.returncode != expected:
        raise AssertionError(label + ': expected ' + str(expected) + '\n' + (result.stdout + result.stderr)[-2500:])
    return entry


def output(lane, value):
    assert json.loads((a.root / lane / 'dist/result.json').read_text())['value'] == value


for lane in ('alpha', 'beta'):
    call('build-' + lane, lane, ['build', '-t', 'app:test', '.'])
for lane in ('alpha', 'beta'):
    call('run-' + lane, lane, ['run', '--rm', 'app:test'])
    output(lane, lane)
assert records[0]['report']['image_id'] != records[1]['report']['image_id']
(a.root / 'alpha/value.txt').write_text('alpha-edit\n')
call('image-source-unchanged', 'alpha', ['run', '--rm', 'app:test'])
output('alpha', 'alpha')
call('mount-current-source', 'alpha', ['run', '--rm', '-v', str(a.root / 'alpha') + ':/workspace', 'app:test'])
output('alpha', 'alpha-edit')
call('readonly-mount', 'alpha', ['run', '--rm', '-v', str(a.root / 'alpha') + ':/workspace:ro', 'app:test'], 1)
output('alpha', 'alpha-edit')
call('failed-output-retained', 'alpha', ['run', '--rm', 'app:test', 'node', '-e',
     "require('fs').mkdirSync('dist');require('fs').writeFileSync('dist/result.json',JSON.stringify({value:'failed-output'}));process.exit(7)"], 7)
output('alpha', 'alpha-edit')
dockerfile = a.root / 'alpha/Dockerfile'
original = dockerfile.read_text()
dockerfile.write_text(original + '\nRUN exit 3\n')
call('failed-rebuild', 'alpha', ['build', '-t', 'app:test', '.'], 1)
call('old-tag-survives', 'alpha', ['run', '--rm', 'app:test'])
output('alpha', 'alpha')
dockerfile.write_text(original)
call('rebuild-edited-source', 'alpha', ['build', '-t', 'app:test', '.'])
call('identical-build', 'alpha', ['build', '-t', 'app:test', '.'])
call('new-tag-source', 'alpha', ['run', '--rm', 'app:test'])
output('alpha', 'alpha-edit')
old = record('alpha')['attempt']
call('unsupported-detached', 'alpha', ['run', '-d', 'app:test'], 64)
assert record('alpha')['attempt'] == old
call('remove-mapping', 'alpha', ['image', 'rm', 'app:test'])
call('removed-image-feedback', 'alpha', ['run', '--rm', 'app:test'], 64)
assert record('alpha')['state'] == 'terminal'
call('other-worktree-survives', 'beta', ['run', '--rm', 'app:test'])
output('beta', 'beta')
print('All Docker command semantics verified.', flush=True)
