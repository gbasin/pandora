"""Inspect an exact attempt-owned Docker resource before removing its ID."""
import json
import re
import subprocess


_CONTAINER_STATES = frozenset({
    'created', 'running', 'paused', 'restarting', 'removing', 'exited', 'dead',
})


def _missing(kind, name, error):
    """Recognize only Docker's resource-specific absence messages."""
    if kind == 'container':
        return any(text in error for text in (
            'No such container: ' + name,
            'No such object: ' + name,
        ))
    return any(text in error for text in (
        'No such network: ' + name,
        'network ' + name + ' not found',
    ))


def _inspect(kind, name):
    result = subprocess.run(['sudo', 'docker', kind, 'inspect', name,
                             '--format', '{{json .}}'], capture_output=True,
                            text=True, timeout=30)
    if result.returncode:
        if _missing(kind, name, result.stderr):
            return None, None
        return None, result.stderr
    try:
        value = json.loads(result.stdout)
    except ValueError:
        return None, 'Docker returned malformed ' + kind + ' inspection for ' + name
    if not isinstance(value, dict):
        return None, 'Docker returned invalid ' + kind + ' inspection for ' + name
    return value, None


def _matches(value, name, labels, *, container):
    expected_name = '/' + name if container else name
    config = value.get('Config')
    actual_labels = config.get('Labels') if container and isinstance(config, dict) else value.get('Labels')
    managed_labels = ({key: label for key, label in actual_labels.items()
                       if isinstance(key, str) and key.startswith('pandora.')}
                      if isinstance(actual_labels, dict) else None)
    if (value.get('Name') != expected_name or not isinstance(value.get('Id'), str)
            or not re.fullmatch('[a-f0-9]{64}', value['Id']) or managed_labels != labels):
        return False
    if container:
        state = value.get('State')
        return (isinstance(state, dict) and state.get('Status') in _CONTAINER_STATES
                and isinstance(state.get('Running'), bool))
    return True


def remove_container(name, labels):
    """Remove only the inspected exact container ID; preserve replacements."""
    value, error = _inspect('container', name)
    if error:
        return False, error
    if value is None:
        return True, None
    if not _matches(value, name, labels, container=True):
        return False, 'Container identity is not owned by this cleanup: ' + name
    result = subprocess.run(['sudo', 'docker', 'rm', '-f', '-v', value['Id']],
                            capture_output=True, text=True, timeout=30)
    if result.returncode and not _missing('container', value['Id'], result.stderr):
        return False, result.stderr
    return True, None


def remove_network(name, labels):
    """Remove only the inspected exact network ID; preserve replacements."""
    value, error = _inspect('network', name)
    if error:
        return False, error
    if value is None:
        return True, None
    if not _matches(value, name, labels, container=False):
        return False, 'Network identity is not owned by this cleanup: ' + name
    result = subprocess.run(['sudo', 'docker', 'network', 'rm', value['Id']],
                            capture_output=True, text=True, timeout=30)
    if result.returncode and not _missing('network', value['Id'], result.stderr):
        return False, result.stderr
    return True, None


def absent_container(name):
    """Return False if a name replacement appears after its cleanup attempt."""
    value, error = _inspect('container', name)
    return value is None, error


def absent_network(name):
    """Return False if a name replacement appears after its cleanup attempt."""
    value, error = _inspect('network', name)
    return value is None, error
