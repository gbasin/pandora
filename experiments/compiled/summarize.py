#!/usr/bin/env python3
"""Extract a compact evidence record from named route logs and local attempts."""
import argparse
import hashlib
import json
from pathlib import Path
import re

p = argparse.ArgumentParser()
p.add_argument('--state', type=Path, required=True)
p.add_argument('--logs', type=Path, required=True)
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
records = []
for label in ['cold', 'cold-output', 'identical', 'identical-output', 'source-edit',
              'source-output', 'dependency-edit', 'dependency-output', 'toolchain-edit', 'toolchain-output']:
    text = (a.logs / ('pandora-compiled-' + label + '.log')).read_text()
    identity = re.search(r'attempt=([0-9a-f]{32})', text)[1]
    matches = list(a.state.glob('*/' + identity))
    assert len(matches) == 1, identity
    attempt = matches[0]
    submission = json.loads((attempt / 'submission.json').read_text())
    terminal = json.loads((attempt / 'terminal.json').read_text())
    assert terminal['exit_code'] == 0 and terminal['cleanup_verified'], terminal
    metrics = json.loads((attempt / 'metrics.json').read_text())
    docker = json.loads((attempt / 'results/docker.json').read_text())
    record = {'label': label, 'attempt': identity, 'source_digest': submission['source_digest'],
              'transfer_seconds': submission['transfer_seconds'], 'request_seconds': submission['total_seconds'],
              'metrics': metrics, 'docker': docker, 'cleanup_verified': True}
    output = attempt / 'results/outputs/apps/borrower-web/dist/index.html'
    if output.exists():
        html = output.read_bytes()
        record['html_sha256'] = hashlib.sha256(html).hexdigest()
        record['source_marker_present'] = b'source-edit-20260920' in html
    records.append(record)
a.output.parent.mkdir(parents=True, exist_ok=True)
a.output.write_text(json.dumps(records, indent=2) + '\n')
for record in records:
    print(record['label'], round(record['metrics']['execution_seconds'], 1), round(record['request_seconds'], 1))
