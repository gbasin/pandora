"""Immutable requests for the bounded remote validation workflow."""

SUITES = frozenset({
    'unit', 'tools', 'full', 'agent-web', 'employee-browser',
    'browser-integration', 'mockup-browser', 'postgres',
})


def validate_request(value):
    """Return a validated, normalized validation request.

    The request deliberately carries only the suite planner's public inputs.
    It is captured with the source snapshot and is never inferred from shell
    argv on the worker.
    """
    if not isinstance(value, dict) or set(value) != {'version', 'suite', 'args'}:
        raise ValueError('Validation request requires version, suite and args')
    if type(value['version']) is not int or value['version'] != 1:
        raise ValueError('Validation request version must be 1')
    if not isinstance(value['suite'], str) or value['suite'] not in SUITES:
        raise ValueError('Validation request has an unsupported suite')
    if not isinstance(value['args'], list) or any(not isinstance(arg, str) for arg in value['args']):
        raise ValueError('Validation request args must be a list of strings')
    if value['suite'] != 'postgres' and value['args']:
        raise ValueError('Validation suite takes no arguments')
    if value['suite'] == 'postgres' and value['args'] not in (
        ['api'], ['scenarios'], ['api', '--foundation-only'],
    ):
        raise ValueError('PostgreSQL validation requires api or scenarios, with --foundation-only only for api')
    return {'version': 1, 'suite': value['suite'], 'args': list(value['args'])}
