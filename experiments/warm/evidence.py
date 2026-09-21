"""Verify immutable worker receipts without transport side effects."""
import json
from pathlib import Path
from snapshot import digest


def validate_evidence(stage, attempt, submitted=None):
    if submitted is None and (stage / "submission.json").exists():
        submitted = json.loads((stage / "submission.json").read_text())
    terminal = json.loads((stage / 'terminal.json').read_text())
    manifest = json.loads((stage / 'artifacts.json').read_text())
    if terminal.get('attempt') != attempt or not terminal.get('cleanup_verified'):
        raise ValueError('Unverified terminal identity or cleanup')
    if submitted is not None and terminal.get('workflow', 'surface') != submitted.get('workflow', 'surface'):
        raise ValueError('Terminal workflow disagrees with submission')
    for name, expected in manifest.items():
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or not path.parts:
            raise ValueError('Unsafe artifact path')
        target = stage / path
        if target.is_symlink() or not target.is_file():
            raise ValueError('Missing or nonregular artifact: ' + name)
        if digest(target) != expected:
            raise ValueError('Artifact checksum mismatch: ' + name)
    if terminal.get('workflow') == 'surface-run':
        from surface_parent import validate_result
        if submitted is None:
            raise ValueError('Surface invocation requires its captured submission')
        validate_result(stage, submitted, terminal, manifest)
    if terminal.get('workflow') == 'surface' and submitted and 'surface_suite' in submitted:
        from surface_child import validate_result
        validate_result(stage, submitted, terminal, manifest)
    if terminal.get('workflow') == 'suite-run':
        from suite_parent import validate_result
        if submitted is None:
            raise ValueError('Suite invocation requires its captured submission')
        validate_result(stage, submitted, terminal, manifest)
    if terminal.get('workflow') == 'suite':
        from suite import validate_result
        if submitted is None:
            raise ValueError('Suite evidence requires its captured submission')
        validate_result(stage, submitted, terminal, manifest)
    if terminal['exit_code'] == 0:
        report = {'journey': 'results/journey.json', 'docker': 'results/docker.json'}.get(terminal.get('workflow'), 'results/junit.xml')
        if terminal.get('workflow') == 'surface-run':
            report = 'results/surface-run.json'
        if terminal.get('workflow') == 'surface' and submitted and 'surface_suite' in submitted:
            report = 'results/surface-' + submitted['surface_suite']['action'] + '.json'
        if terminal.get('workflow') == 'suite-run':
            report = 'results/suite-run.json'
        if terminal.get('workflow') == 'suite':
            report = 'results/suite-' + submitted['suite']['action'] + '.json'
        if not {'results/exit-code', report} <= set(manifest):
            raise ValueError('Successful run lacks test evidence')
        if (stage / 'results/exit-code').read_text().strip() != '0':
            raise ValueError('Test evidence disagrees with successful terminal')
        if terminal.get('workflow') == 'docker':
            result = json.loads((stage / report).read_text())
            if result.get('exit_code') != 0 or result.get('kind') not in ('build', 'run', 'remove'):
                raise ValueError('Docker evidence disagrees with successful terminal')
        if terminal.get('workflow') == 'journey':
            result = json.loads((stage / report).read_text())
            from journey import journey_config
            expected = journey_config(submitted) if submitted is not None else None
            if (result.get('status') != 'pass' or
                    (expected is not None and any(result.get(k, False if k == 'update' else None) != expected.get(k)
                     for k in ('update', 'fault'))) or
                    (expected is not None and result.get('journey') != expected['id'])):
                raise ValueError('Journey evidence disagrees with successful terminal')
        if terminal.get('workflow', 'surface') == 'surface' and submitted and 'surface_app' in submitted:
            if 'results/surface.json' not in manifest:
                raise ValueError('Successful surface lacks app identity')
            result = json.loads((stage / 'results/surface.json').read_text())
            if result.get('app') != submitted['surface_app']:
                raise ValueError('Surface evidence disagrees with requested app')
    return terminal

