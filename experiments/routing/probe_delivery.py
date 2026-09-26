"""Exercise a recoverable local delivery refusal against the actual SSH route."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--repo', type=Path, required=True)
p.add_argument('--state', type=Path, required=True)
p.add_argument('--output', type=Path, required=True)
p.add_argument('--host', required=True)
a = p.parse_args()
a.repo = a.repo.resolve()
a.output.mkdir(parents=True, exist_ok=False)
saved_mode = (a.repo / 'apps/web').stat().st_mode & 0o777
destination = a.repo / 'apps/web/dist'
if destination.is_symlink() or not destination.is_dir():
    raise ValueError('Probe requires an existing ordinary generated build')
key = hashlib.sha256(str(a.repo).encode()).hexdigest()
active = a.state / key / 'active.json'
command = ['python3', str(Path(__file__).with_name('launch.py')), '--host', a.host,
           '--state', str(a.state), '--session', 'delivery-fault', '--',
           'pnpm', 'test:surface', 'web', 'pandora-iteration.spec.ts']
records = []


def invoke(label):
    start = time.monotonic()
    with (a.output / (label + '.log')).open('w') as log:
        result = subprocess.run(command, cwd=a.repo, stdout=log, stderr=subprocess.STDOUT, timeout=300)
    record = json.loads(active.read_text())
    records.append({'step': label, 'exit_code': result.returncode,
                    'seconds': time.monotonic() - start, 'attempt': record['attempt'], 'state': record['state']})
    print(json.dumps(records[-1]), flush=True)
    return result.returncode, record


destination.parent.chmod(0o555)
try:
    status, first = invoke('delivery-refused')
    assert status == 75 and first['state'] == 'active'
finally:
    destination.parent.chmod(saved_mode)
status, last = invoke('delivery-recovered')
assert status == 0 and last['state'] == 'terminal'
assert first['attempt'] == last['attempt']
log = (a.output / 'delivery-recovered.log').read_text()
assert 'Recovering existing request' in log and 'freezing current' not in log
for name in ['apps/web/dist', 'apps/web/e2e/dist']:
    assert '<title>App</title>' in (a.repo / name / 'index.html').read_text()
(a.output / 'result.json').write_text(json.dumps({'records': records, 'assertions': 'passed'}, indent=2) + '\n')
