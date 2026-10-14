"""Digest-bound adapter reports. Trusted CI policy establishes their authority."""
import hashlib
import json
from pathlib import Path

PATH = 'tmp/pandora-execution-report.json'
MAX_BYTES = 4 * 1024 * 1024


def enabled(outputs):
    return any(output.get('kind') == 'artifacts' and PATH in output.get('paths', [])
               for output in outputs or [])


def load(root, outputs, run_id):
    if not enabled(outputs):
        return None, None
    try:
        root = Path(root).resolve()
        path = root / PATH
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('unsafe_execution_report')
        with path.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('execution_report_too_large')
        report = json.loads(raw)
        execution = report.get('execution')
        if (report.get('kind') != 'pandora-execution-report' or type(report.get('schema')) is not int
                or report['schema'] != 1 or report.get('worker_run') != run_id
                or not isinstance(execution, dict) or execution.get('complete') is not True
                or execution.get('outcome') != 'passed'):
            raise ValueError('invalid_execution_report')
        return {'report': raw.decode('utf-8'), 'sha256': hashlib.sha256(raw).hexdigest(),
                'bytes': len(raw)}, None
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return None, 'execution_report_unavailable'


def prepare_environment(env):
    """Preparation cannot emit an execution report for the current run."""
    return {key: value for key, value in env.items() if key not in {
        'PANDORA_EXECUTION_REPORT', 'PANDORA_EXECUTION_RUN',
        'PANDORA_TEST_EVIDENCE', 'PANDORA_TEST_EVIDENCE_RUN'}}


def fresh_output(driver, instance):
    clear = getattr(driver, 'clear_execution_report', None)
    try:
        return callable(clear) and clear(instance.name, '/work') is True
    except Exception:  # noqa: BLE001 - unavailable evidence must not fail tests
        return False


def source_integrity(driver, instance, inventory):
    check = getattr(driver, 'source_integrity', None)
    try:
        return isinstance(inventory, dict) and callable(check) and check(instance.name, '/work', inventory) is True
    except Exception:  # noqa: BLE001 - unavailable evidence must not fail tests
        return False
