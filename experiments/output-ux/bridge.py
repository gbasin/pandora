"""Evaluator-only remote output UX fixture with generation publication.
No general routing or remote execution recovery; local completed delivery retries
reuse their result. root/dist is the profile's exclusively managed output root.
"""
import difflib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from publish import atomic_json, publish

root = Path.cwd()
action = sys.argv[1]
assert action in ['build', 'generate', 'test']
state = root / '.pandora-artifacts'
state.mkdir(exist_ok=True)
lock = (state / 'request.lock').open('a')
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print('[pandora] A command is already active for this worktree.', file=sys.stderr)
    sys.exit(75)
schema = json.loads((root / 'src/schema.json').read_text())
fixture_text = (root / 'fixtures/receipt.json').read_text()
inputs = {'action': action, 'schema': schema, 'fixture': json.loads(fixture_text)}
key = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
active = state / 'delivery.json'
started = time.monotonic()
recovered = active.exists()
if recovered:
    pending = json.loads(active.read_text())
    if pending['key'] != key:
        print('[pandora] Delivery remains pending for different inputs. Existing results preserved.', file=sys.stderr)
        sys.exit(75)
    run = pending['id']
    directory = state / run
    result = json.loads((directory / 'result.json').read_text())
    print('[pandora] Recovering completed build delivery; no remote build submitted.', flush=True)
else:
    run = uuid.uuid4().hex
    directory = state / run
    directory.mkdir()
    request = {'id': run, **inputs}
    print('[pandora] Running remotely; run ' + run, flush=True)
    r = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                        'ubuntu@40.160.93.34', 'python3 /home/ubuntu/pandora-output-ux/remote.py'],
                       input=json.dumps(request), capture_output=True, text=True, timeout=90)
    (directory / 'transport.stderr').write_text(r.stderr)
    if r.returncode:
        print('[pandora] Transport failed: ' + r.stderr, file=sys.stderr)
        sys.exit(70)
    result = json.loads(r.stdout)
    for path, contents in result['files'].items():
        assert path in {'dist/report.json', 'fixtures/receipt.json'}
        dest = directory / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(contents)
    atomic_json(directory / 'result.json', result)
    with (state / 'remote-events.jsonl').open('a') as log:
        log.write(json.dumps({'id': run, 'action': action, 'key': key, 'remote_exit': result['exit']}) + '\n')
    if action == 'build' and result['exit'] == 0:
        atomic_json(active, {'id': run, 'key': key})
status = result['exit']
if action == 'build' and not status:
    manifest = {'report.json': hashlib.sha256(result['files']['dist/report.json'].encode()).hexdigest()}
    def fault(point):
        if os.environ.get('PANDORA_PUBLISH_CRASH') == point:
            os._exit(86)
    try:
        receipt = publish(root, directory, manifest, fault)
    except (OSError, ValueError) as error:
        print('[pandora] Build complete, delivery incomplete: ' + str(error), file=sys.stderr)
        print('[pandora] Retry the same command to recover delivery without rebuilding.', file=sys.stderr)
        sys.exit(75)
    active.unlink()
    print('[pandora] Build succeeded. Local output: ' + str(root / 'dist/report.json'))
    if receipt['retained_previous']:
        print('[pandora] Previous output retained: ' + receipt['retained_previous'])
elif action == 'generate' and not status:
    contents = result['files']['fixtures/receipt.json']
    patch = ''.join(difflib.unified_diff(fixture_text.splitlines(True), contents.splitlines(True),
                                      fromfile='a/fixtures/receipt.json', tofile='b/fixtures/receipt.json'))
    (directory / 'changes.patch').write_text(patch)
    print('[pandora] Remote generation succeeded. Workspace source was NOT changed.')
    print('[pandora] Action required: review and apply the returned changes, then run pnpm test.')
    print('[pandora] Diff: ' + str(directory / 'changes.patch'))
    print('[pandora] Generated file: ' + str(directory / 'fixtures/receipt.json'))
    status = 75
else:
    print('[pandora] ' + result['message'])
event = {'id': run, 'action': action, 'input_sha256': key,
         'seconds': round(time.monotonic() - started, 3), 'remote_exit': result['exit'],
         'local_exit': status, 'recovered_delivery': recovered}
with (state / 'events.jsonl').open('a') as log:
    log.write(json.dumps(event) + '\n')
sys.exit(status)
