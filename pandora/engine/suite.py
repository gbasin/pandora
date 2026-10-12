"""Typed, signed suite observations. They never authorize execution skips."""
import hashlib
import json
import re
from pathlib import Path

from . import shards, verdict

KIND = 'pandora-suite-observation'
VERDICT_KIND = 'pandora-suite-verdict'
MAX_BYTES = 4 * 1024 * 1024
TREE = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')


def read(path, root):
    """Bound the exact bytes whose digest the aggregate will bind."""
    path, root = Path(path), Path(root).resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('unsafe_suite_report')
    with path.open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('suite_report_too_large')
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def assemble(parent, plan_result, plan_report, children, *, plan_digest):
    """Static planned fanout only. `children` pairs engine results and reports.

    The caller reads ledger rows and collected outputs, not client-supplied
    claims. Build outputs are separate from the source tree measured before
    grafting. Incomplete runtime provenance remains explicit in observations.
    """
    tree = plan_result.get('tree')
    if (parent.get('role') != 'parent' or not isinstance(tree, str) or
            not TREE.fullmatch(tree) or plan_result.get('role') != 'plan' or
            plan_result.get('parent') != parent['run_id'] or
            plan_result.get('outcome') != 'passed' or plan_result.get('cli_exit') != 0):
        raise ValueError('unproven_suite_plan')
    if (plan_report.get('kind') != 'pandora-playwright-plan' or
            type(plan_report.get('version')) is not int or plan_report['version'] != 1 or
            not SHA256.fullmatch(plan_digest)):
        raise ValueError('unsupported_suite_plan')
    build = plan_report.get('build')
    if (not isinstance(build, dict) or not isinstance(build.get('sha256'), str) or
            not SHA256.fullmatch(build['sha256'])):
        raise ValueError('missing_suite_build')
    partition = plan_report.get('inventory')
    if not isinstance(partition, list) or not partition:
        raise ValueError('missing_suite_partition')
    planned = []
    for index, row in enumerate(partition, 1):
        if not isinstance(row, dict) or type(row.get('shard')) is not int or row['shard'] != index:
            raise ValueError('invalid_suite_partition')
        planned.append(identifiers(row.get('testIds')))
    required = shards.flatten(planned)
    if not required:
        raise ValueError('empty_suite_inventory')
    full_inventory = identifiers(plan_report.get('tests'), objects=True)
    if sorted(full_inventory) != sorted(required) or len(set(required)) != len(required):
        raise ValueError('suite_partition_not_full_inventory')
    selection = {key: plan_report[key] for key in ('app', 'selectors', 'grep')}
    reports, tests, provenance = {}, [], []
    profile = None
    for result, report, report_digest, golden_pins in children:
        index = result.get('shard')
        if not isinstance(index, str) or not re.fullmatch(r'[1-9][0-9]*/[1-9][0-9]*', index):
            raise ValueError('invalid_suite_shard')
        index, total = map(int, index.split('/'))
        if total != len(planned) or not 1 <= index <= total or index in reports:
            raise ValueError('invalid_suite_shard')
        if (result.get('role') != 'shard' or result.get('parent') != parent['run_id'] or
                result.get('outcome') != 'passed' or result.get('cli_exit') != 0 or
                result.get('tree') != tree or result.get('repo') != parent['repo'] or
                result.get('job') != parent['job'] or
                result.get('input_id') != parent['input_id']):
            raise ValueError('suite_child_context_mismatch')
        if (report.get('kind') != 'pandora-playwright-report' or
                type(report.get('version')) is not int or report['version'] != 1 or
                report.get('status') != 'passed' or report.get('exitCode') != 0 or
                type(report.get('errors')) is not int or report['errors'] != 0 or
                report.get('build') != build or
                report.get('shard') != '%d/%d' % (index, total) or
                {key: report.get(key) for key in selection} != selection):
            raise ValueError('suite_child_report_mismatch')
        current_profile = {'execution': report.get('profile'), 'golden_pins': golden_pins}
        if not isinstance(current_profile['execution'], dict) or not golden_pins:
            raise ValueError('missing_suite_profile')
        if profile is None:
            profile = current_profile
        elif current_profile != profile:
            raise ValueError('suite_profile_mismatch')
        observed = identifiers(report.get('observed'), objects=True)
        reports[index] = observed
        outcomes = report.get('outcomes')
        if not isinstance(outcomes, list):
            raise ValueError('missing_suite_outcomes')
        identities = []
        for case in outcomes:
            identity, attempts = case.get('id'), case.get('attempts')
            if (not isinstance(identity, str) or not isinstance(attempts, list) or not attempts):
                raise ValueError('missing_suite_attempts')
            for retry, attempt in enumerate(attempts):
                if (type(attempt.get('retry')) is not int or attempt['retry'] != retry or
                        attempt.get('status') not in ('passed', 'failed', 'timedOut',
                                                     'skipped', 'interrupted') or
                        attempt.get('expected_status') not in ('passed', 'failed', 'skipped')):
                    raise ValueError('invalid_suite_attempts')
            last = attempts[-1]
            status = last['status']
            if last['expected_status'] == 'failed':
                status = {'failed': 'expected', 'passed': 'unexpected'}.get(status, status)
            if case.get('status') != status or status not in ('passed', 'skipped', 'expected'):
                raise ValueError('suite_outcome_mismatch')
            identities.append(identity)
            tests.append(case)
        if sorted(identities) != sorted(observed) or len(set(identities)) != len(identities):
            raise ValueError('suite_outcomes_incomplete')
        if not isinstance(report_digest, str) or not SHA256.fullmatch(report_digest):
            raise ValueError('invalid_suite_report_digest')
        provenance.append({'run_id': result['run_id'], 'shard': index,
                           'source_tree': tree, 'report_sha256': report_digest})
    if not shards.verify(planned, reports)['verified']:
        raise ValueError('suite_coverage_incomplete')
    if (plan_result.get('repo') != parent['repo'] or plan_result.get('job') != parent['job'] or
            plan_result.get('input_id') != parent['input_id']):
        raise ValueError('suite_plan_context_mismatch')
    return {'kind': KIND, 'v': 1, 'runner': 'playwright', 'worker_run': parent['run_id'],
            'source_tree': tree, 'input_id': parent['input_id'], 'selection': selection,
            'profile': profile, 'plan_sha256': plan_digest, 'build': build,
            'planned': required, 'tests': sorted(tests, key=lambda test: test['id']),
            'children': sorted(provenance, key=lambda child: child['shard']),
            'complete': True, 'outcome': 'passed', 'authorizes_reuse': False}


def identifiers(rows, *, objects=False):
    """Typed evidence never silently discards an unrecognized identity row."""
    if not isinstance(rows, list):
        raise ValueError('invalid_suite_inventory')
    ids = []
    for row in rows:
        identity = row.get('id') if objects and isinstance(row, dict) else row
        if not isinstance(identity, str) or not identity:
            raise ValueError('invalid_suite_identity')
        if objects and not isinstance(row, dict):
            raise ValueError('invalid_suite_identity')
        ids.append(identity)
    if len(set(ids)) != len(ids):
        raise ValueError('duplicate_suite_identity')
    return ids


def binding(report):
    raw = verdict.canonical(report)
    if len(raw) > MAX_BYTES:
        raise ValueError('suite_report_too_large')
    return {'report': raw.decode(), 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}


def sign(root, parent, report, *, finished, golden, golden_pins):
    """A distinct envelope cannot be consumed as a whole-run skip verdict."""
    if (parent.get('role') != 'parent' or report.get('kind') != KIND or
            report.get('worker_run') != parent['run_id'] or
            report.get('input_id') != parent['input_id'] or
            report.get('complete') is not True or report.get('outcome') != 'passed'):
        raise ValueError('suite_signing_context_mismatch')
    if verdict.ready_state() != 'ready' or verdict.worker_drift(root):
        raise ValueError('suite_worker_unproven')
    if not golden:
        raise ValueError('suite_golden_unknown')
    artifact = binding(report)
    payload = json.loads(verdict.payload(argv=parent['argv'], cwd=parent.get('cwd') or '',
        engine=verdict.engine_id(), env_digest=verdict.env_digest(parent.get('env')),
        finished=finished, golden=golden, golden_pins=golden_pins,
        input_id=parent['input_id'], job=parent['job'], outcome='passed',
        repo=parent['repo'], run_id=parent['run_id'], tree=report['source_tree']))
    payload.update(kind=VERDICT_KIND,
                   test_evidence={key: artifact[key] for key in ('sha256', 'bytes')})
    return verdict.sign(root, verdict.canonical(payload)), artifact
