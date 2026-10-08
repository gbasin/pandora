"""Opt-in test observations. These are measurement artifacts, never skip verdicts."""
import hashlib
import json
import math
from pathlib import Path

PATH = 'tmp/pandora-test-evidence.json'
KIND = 'pandora-test-report'
MAX_BYTES = 4 * 1024 * 1024


def enabled(outputs):
    return any(output.get('kind') == 'artifacts' and PATH in output.get('paths', [])
               for output in outputs or [])


def identity(test):
    values = [test.get(key) for key in ('project', 'file', 'name')]
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError('missing_identity')
    path = Path(values[1])
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('unsafe_identity')
    location = test.get('location')
    if not isinstance(location, dict) or not all(type(location.get(key)) is int and
            location[key] > 0 for key in ('line', 'column')):
        raise ValueError('missing_location')
    position = test.get('collection_index')
    if type(position) is not int or position < 0:
        raise ValueError('missing_collection_index')
    return tuple(values + [location['line'], location['column'], position])


def validate(report):
    if not isinstance(report, dict) or report.get('kind') != KIND or type(report.get('v')) is not int or report['v'] != 1:
        raise ValueError('unsupported_report')
    if report.get('runner') != 'vitest' or not isinstance(report.get('runner_version'), str):
        raise ValueError('unsupported_runner')
    if not isinstance(report.get('profile'), dict) or not isinstance(report.get('selection'), dict):
        raise ValueError('missing_context')
    if not isinstance(report.get('errors'), list) or not isinstance(report.get('modules'), list):
        raise ValueError('missing_context')
    tests = report.get('tests')
    if not isinstance(tests, list) or not tests:
        raise ValueError('empty_inventory')
    seen = set()
    for test in tests:
        key = identity(test)
        if key in seen:
            raise ValueError('ambiguous_identity')
        seen.add(key)
        if test.get('status') not in ('passed', 'failed', 'skipped', 'pending'):
            raise ValueError('invalid_status')
        if test.get('mode') not in ('run', 'only', 'skip', 'todo'):
            raise ValueError('invalid_mode')
        duration = test.get('duration_ms')
        if duration is not None and (type(duration) not in (int, float) or
                not math.isfinite(duration) or duration < 0):
            raise ValueError('invalid_duration')
    for module in report['modules']:
        if not isinstance(module, dict) or not all(isinstance(module.get(key), str)
                for key in ('project', 'file', 'state')):
            raise ValueError('invalid_module')
    return report


def load(outputs_root, outputs, run_id):
    """Bounded, declared output only; an absent/bad report cannot fail the run."""
    if not enabled(outputs):
        return None, None
    try:
        root = Path(outputs_root).resolve()
        path = root / PATH
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            return None, 'unsafe_report_path'
        with path.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return None, 'report_too_large'
        text = raw.decode('utf-8')
        report = validate(json.loads(text))
        if report.get('worker_run') != run_id:
            return None, 'report_run_mismatch'
        if (report.get('complete') is not True or report.get('outcome') != 'passed' or
                report['errors'] or any(test['status'] in ('pending', 'failed') or
                    test['mode'] == 'only' for test in report['tests'])):
            return None, 'report_not_passed'
        if not any(test['status'] == 'passed' for test in report['tests']):
            return None, 'no_executed_tests'
        return {'report': text, 'sha256': hashlib.sha256(raw).hexdigest(),
                'bytes': len(raw)}, None
    except FileNotFoundError:
        return None, 'report_missing'
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
        return None, 'invalid_report'
