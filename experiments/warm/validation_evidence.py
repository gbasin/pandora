"""Bind broad-validation reports to the accepted invocation and test evidence."""
import json
from pathlib import PurePosixPath

from validation_request import validate_request


def validate_result(stage, submitted, terminal, manifest):
    if not isinstance(submitted, dict):
        raise ValueError('Validation evidence requires its captured submission')
    request = validate_request(submitted.get('validation'))
    name = 'results/validation.json'
    if name not in manifest:
        if terminal['exit_code'] == 0:
            raise ValueError('Successful validation lacks its report')
        return  # Preparation, cancellation, or infrastructure failure before a report.
    report = json.loads((stage / name).read_text())
    if (type(report.get('version')) is not int or report['version'] != 1 or
            report.get('attempt') != submitted.get('attempt') or
            report.get('suite') != request['suite'] or report.get('args') != request['args']):
        raise ValueError('Validation report disagrees with the accepted request')
    if type(report.get('exit_code')) is not int or not 0 <= report['exit_code'] <= 255:
        raise ValueError('Invalid validation exit status')
    steps = report.get('steps')
    if not isinstance(steps, list):
        raise ValueError('Validation report lacks command evidence')
    count = 0
    for step in steps:
        if (not isinstance(step, dict) or not isinstance(step.get('argv'), list) or
                not step['argv'] or any(not isinstance(arg, str) for arg in step['argv']) or
                type(step.get('exit_code')) is not int):
            raise ValueError('Invalid validation command evidence')
        tests = step.get('test_count')
        if tests is not None:
            if type(tests) is not int or tests < 0:
                raise ValueError('Invalid validation test count')
            count += tests
            if terminal['exit_code'] == 0 and tests == 0:
                raise ValueError('Successful validation contains an empty test step')
        path = step.get('report')
        if path is not None:
            if not isinstance(path, str):
                raise ValueError('Invalid test report path')
            relative = PurePosixPath(path)
            if relative.is_absolute() or '..' in relative.parts or not relative.parts:
                raise ValueError('Unsafe test report path')
            if 'results/' + path not in manifest:
                raise ValueError('Referenced test report is missing')
        if terminal['exit_code'] == 0 and step['exit_code'] != 0:
            raise ValueError('Successful validation contains a failed command')
    if terminal['exit_code'] == 0 and (report['exit_code'] != 0 or not steps or count == 0):
        raise ValueError('Successful validation lacks executed passing tests')
