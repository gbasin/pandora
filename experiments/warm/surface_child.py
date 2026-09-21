"""Materialize immutable surface plans and compiled inputs into one container."""
import json
from pathlib import Path
import tarfile

from evidence import validate_evidence
from surface_suite import outputs_manifest, surface_request, validate_plan
from suite_parent_cleanup import validate_registry


def request(submitted):
    value = surface_request(submitted['surface_suite'])
    if value['action'] == 'run':
        raise ValueError('A surface child cannot execute a parent request')
    app = value['app'] if value['action'] == 'plan' else value['plan']['app']
    selectors = value['selectors'] if value['action'] == 'plan' else value['plan']['selectors']
    if app != submitted.get('surface_app') or selectors != submitted.get('selectors'):
        raise ValueError('Surface child differs from accepted app or selectors')
    if value['action'] == 'shard':
        plan = value['plan']
        if plan['parent_attempt'] != submitted.get('parent_attempt') or plan['source_digest'] != submitted['source_digest']:
            raise ValueError('Surface shard differs from accepted source or parent')
    return value


def build_source(attempt, submitted):
    """Resolve a planner only through this parent's reserved identity registry."""
    value = request(submitted)
    if value['action'] != 'shard':
        raise ValueError('Only surface shards reuse a compiled build')
    parent = attempt.parent / value['plan']['parent_attempt']
    identities = validate_registry(parent, json.loads((parent / 'children.json').read_text()))
    if len(identities) != value['plan']['shard_count'] + 1 or identities[value['shard']] != attempt.name:
        raise ValueError('Surface shard is not reserved by this parent')
    planner = parent / 'results/attempts' / identities[0]
    metadata = json.loads((planner / 'submission.json').read_text())
    expected = {key: value['plan'][key] for key in ('app', 'selectors', 'shard_count', 'keep_going')} | {'action': 'plan'}
    if (metadata.get('attempt') != identities[0] or metadata.get('parent_attempt') != parent.name
            or metadata.get('surface_suite') != expected or metadata.get('workflow') != 'surface'
            or metadata.get('worker_config') != submitted.get('worker_config')):
        raise ValueError('Surface build belongs to a different planning request')
    terminal = validate_evidence(planner, identities[0], metadata)
    plan = validate_plan(json.loads((planner / 'results/surface-plan.json').read_text()), metadata)
    if terminal['exit_code'] != 0 or plan != value['plan']:
        raise ValueError('Surface build lacks a successful matching planner receipt')
    source = planner / 'results/outputs'
    if outputs_manifest(source, plan['app']) != plan['build']:
        raise ValueError('Compiled surface build differs from the frozen plan')
    return source


def materialize(attempt, submitted, container, docker):
    value = request(submitted)
    path = attempt / 'surface-request.json'
    path.write_text(json.dumps(value) + '\n')
    for name, target in ((path.name, path.name), ('surface-runner.mjs', 'surface-runner.mjs'),
                         ('surface-reporter.cjs', 'surface-reporter.cjs')):
        docker('cp', str(attempt / name), container + ':/tmp/' + target)
    if value['action'] == 'shard':
        source = build_source(attempt, submitted)
        archive_path = attempt / 'surface-build.tar'
        try:
            with tarfile.open(archive_path, 'w') as archive:
                from workflow_options import surface_outputs
                for output in surface_outputs(value['plan']['app']):
                    archive.add(source / output, arcname=output)
            with archive_path.open('rb') as archive:
                docker('cp', '-a', '-', container + ':/workspace/source', stdin=archive)
        finally:
            archive_path.unlink(missing_ok=True)


def validate_result(stage, submitted, terminal, manifest):
    value = request(submitted)
    report_name = 'results/surface-' + value['action'] + '.json'
    if report_name not in manifest:
        if terminal['exit_code'] == 0:
            raise ValueError('Successful surface child lacks its report')
        return
    report = json.loads((stage / report_name).read_text())
    if value['action'] == 'plan':
        plan = validate_plan(report, submitted)
        if terminal['exit_code'] == 0:
            names = {'results/outputs/' + row['path'] for row in plan['build']['files']}
            if not names <= set(manifest) or outputs_manifest(stage / 'results/outputs', plan['app']) != plan['build']:
                raise ValueError('Surface build artifact set differs from its plan')
    else:
        from surface_suite import validate_shard
        report = validate_shard(report, value['plan'], value['shard'])
        if report['exit_code'] != terminal['exit_code']:
            raise ValueError('Surface shard result differs from terminal exit')
