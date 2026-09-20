"""Publish the surface profile's generated directories after verified success."""
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'output-ux'))
from publish import publish, verify

OUTPUTS = ('apps/borrower-web/dist', 'apps/borrower-web/e2e/dist')


def deliver(repo, output, fault=lambda point: None):
    manifest = json.loads((output / 'artifacts.json').read_text())
    plans = []
    # Validate every root before publishing any. Multiple roots are individually
    # atomic, not a transaction. Their receipts make partial delivery recoverable.
    for name in OUTPUTS:
        destination = repo / name
        for part in [destination, *destination.parents]:
            if part == repo:
                break
            if part.is_symlink():
                raise ValueError('Output path contains a symlink: ' + name)
        tracked = subprocess.check_output(['git', '-C', str(repo), 'ls-files', '--', name])
        ignored = subprocess.run(['git', '-C', str(repo), 'check-ignore', '-q', name + '/'],
                                 check=False).returncode == 0
        if tracked or not ignored:
            raise ValueError('Output must be ignored and contain no tracked files: ' + name)
        prefix = 'results/outputs/' + name + '/'
        files = {p[len(prefix):]: sha for p, sha in manifest.items() if p.startswith(prefix)}
        if not files:
            raise ValueError('Missing declared output: ' + name)
        source = output / 'results/outputs' / name
        verify(source, files)
        plans.append((name, destination, source, files))
    for index, (name, destination, source, files) in enumerate(plans):
        artifacts = output / 'publication' / str(index)
        artifacts.mkdir(parents=True, exist_ok=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        receipt = publish(repo, artifacts, files, fault, source=source, destination=destination)
        print(f'[pandora] output ready: {destination}', flush=True)
        if receipt['retained_previous']:
            print(f'[pandora] previous output retained: {receipt["retained_previous"]}', flush=True)
