"""Verify evidence from two isolated real surface repair sessions."""
import argparse
import hashlib
import json
from pathlib import Path
import re

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--state', type=Path, required=True)
p.add_argument('--repo', type=Path, action='append', required=True)
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
assert len(a.repo) == 2
rows = []
for repo in a.repo:
    repo = repo.resolve()
    fixture = json.loads((repo / 'pandora-dependency-fixture.json').read_text())
    state = a.state / hashlib.sha256(str(repo).encode()).hexdigest()
    active = json.loads((state / 'active.json').read_text())
    assert active['state'] == 'terminal', active
    submissions = sorted(state.glob('*/submission.json'), key=lambda path: path.stat().st_mtime)
    assert len(submissions) == 2, submissions
    attempts = []
    manifests = []
    for submission_path in submissions:
        output = submission_path.parent
        submitted = json.loads(submission_path.read_text())
        terminal = json.loads((output / 'terminal.json').read_text())
        metrics = json.loads((output / 'metrics.json').read_text())
        assert terminal['cleanup_verified']
        manifest = json.loads((output / 'manifest.json').read_text())
        manifests.append({entry['path']: entry for entry in manifest})
        assert not any('.worktrees' in Path(entry['path']).parts for entry in manifest)
        stdout = (output / 'stdout.log').read_text()
        assert json.dumps({'marker': fixture['marker'], 'installed': fixture['version']}, separators=(',', ':')) in stdout
        memory = {key: int(value) for key, value in
                  [line.split() for line in (output / 'results/memory-events').read_text().splitlines()]}
        assert memory['oom_kill'] == 0
        progress = re.findall(r'Progress: resolved (\d+), reused (\d+), downloaded (\d+)',
                              stdout + (output / 'stderr.log').read_text())
        attempts.append({'attempt': terminal['attempt'], 'source_digest': submitted['source_digest'],
                         'exit_code': terminal['exit_code'], 'total_seconds': submitted['total_seconds'],
                         'metrics': metrics, 'memory_peak_bytes': int((output / 'results/memory-peak-bytes').read_text()),
                         'install_progress': progress[-1] if progress else None})
    changed = {name for name in manifests[0].keys() | manifests[1].keys()
               if manifests[0].get(name) != manifests[1].get(name)}
    assert changed == {'apps/borrower-web/index.html'}, changed
    assert [run['exit_code'] for run in attempts] == [1, 0]
    assert [run['metrics']['dependency_cache_hit'] for run in attempts] == [False, True]
    assert attempts[0]['metrics']['image_id'] == attempts[1]['metrics']['image_id']
    assert attempts[0]['source_digest'] != attempts[1]['source_digest']
    for index, name in enumerate(['apps/borrower-web/dist', 'apps/borrower-web/e2e/dist']):
        html = (repo / name / 'index.html').read_text()
        assert '<title>Ike</title>' in html and fixture['marker'] in html
        assert not (repo / name / 'obsolete.txt').exists()
        receipt = json.loads((submissions[1].parent / 'publication' / str(index) / 'publication.json').read_text())
        previous = Path(receipt['retained_previous'])
        assert (previous / 'index.html').read_text() == '<title>Stale output</title>'
        assert (previous / 'obsolete.txt').read_text() == fixture['marker']
    rows.append({'fixture': fixture, 'attempts': attempts, 'output_and_prior_generation_verified': True,
                 'nested_worktrees_excluded': True})
assert rows[0]['attempts'][0]['metrics']['image_id'] != rows[1]['attempts'][0]['metrics']['image_id']
assert rows[0]['fixture']['version'] != rows[1]['fixture']['version']
a.output.parent.mkdir(parents=True, exist_ok=True)
a.output.write_text(json.dumps(rows, indent=2) + '\n')
print('Verified distinct dependency images, warm reruns, source and output isolation.')
