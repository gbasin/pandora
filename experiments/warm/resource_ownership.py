"""Read-only ownership checks. Admission, not this inventory, authorizes work."""
import fcntl
import json
import re
import subprocess

from admission import alive
from builder_owner import BUILDERS, owner

PREFIX = 'pandora-warm-'
IDENTITY = re.compile('[a-f0-9]{32}')


class OwnershipUnresolved(RuntimeError):
    pass


def labels(value):
    result = {}
    for item in value.split(',') if value else []:
        key, separator, content = item.partition('=')
        if not separator or key in result:
            raise OwnershipUnresolved('Malformed Docker ownership labels')
        result[key] = content
    return result


def held(path):
    try:
        handle = path.open('r+')
    except FileNotFoundError:
        return False
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True


def active(root, identity, admitted):
    attempt = root / 'runs' / identity
    if identity not in admitted or attempt.is_symlink() or not alive(attempt):
        raise OwnershipUnresolved('Resource owner ' + identity + ' is dead or not admitted')
    return attempt


def workflow(attempt):
    value = json.loads((attempt / 'submission.json').read_text())
    return value.get('workflow', 'surface')


def container(root, item, admitted):
    name, state = item['Names'], item['State']
    builders = {'buildx_buildkit_' + builder + '0': builder for builder in BUILDERS}
    if name in builders:
        if state == 'exited':
            return  # Persistent stopped cache daemon, not an executing task.
        builder = builders[name]
        directory = root / 'builder-owners'
        identity = owner(directory / (builder + '.json'))
        if identity is None:
            raise OwnershipUnresolved('Active builder has no recorded owner: ' + name)
        attempt = active(root, identity, admitted)
        submitted = json.loads((attempt / 'submission.json').read_text())
        valid_workflow = (submitted.get('workflow', 'surface') in ('surface', 'journey', 'suite')
                          if builder == 'pandora-surface-deps-v3' else
                          submitted.get('workflow') == 'docker' and submitted.get('docker', {}).get('request', {}).get('kind') == 'build')
        if not valid_workflow:
            raise OwnershipUnresolved('Builder owner has an incompatible workflow: ' + name)
        marker = 'dependency-cleanup.pending' if builder == 'pandora-surface-deps-v3' else 'docker-cleanup.pending'
        if not held(directory / (builder + '.lock')) or not (attempt / marker).is_file():
            raise OwnershipUnresolved('Builder lease or cleanup intent is missing: ' + name)
        return
    raw_labels = item.get('Labels', '')
    if not name.startswith(PREFIX) and 'pandora.' not in raw_labels:
        return
    metadata = labels(raw_labels)
    if any(key.startswith('pandora.') and key not in ('pandora.workflow', 'pandora.attempt', 'pandora.experiment') for key in metadata):
        raise OwnershipUnresolved('Unrecognized managed container labels: ' + name)
    kind = metadata.get('pandora.workflow')
    surface = metadata.get('pandora.experiment') == 'warm-surface'
    managed = name.startswith(PREFIX) or any(key.startswith('pandora.') for key in metadata)
    if not managed:
        return
    match = re.fullmatch(PREFIX + '([a-f0-9]{32})(-db|-pool|-proxy)?', name)
    if match is None:
        raise OwnershipUnresolved('Unexpected managed container name: ' + name)
    identity, suffix = match.groups()
    if surface and kind is None:
        kind = 'surface'  # Pre-label surface containers use the same exact name.
        label_identity = metadata.get('pandora.attempt', identity)
    else:
        label_identity = metadata.get('pandora.attempt')
    if label_identity != identity or kind not in ('journey', 'docker', 'surface') or (suffix and kind != 'journey'):
        raise OwnershipUnresolved('Container name and ownership labels disagree: ' + name)
    if kind == 'surface' and state == 'exited':
        return  # Existing failed-surface diagnostics are deliberately retained.
    attempt = active(root, identity, admitted)
    submitted = workflow(attempt)
    expected = {'journey': ('journey', 'suite'), 'docker': ('docker',), 'surface': ('surface',)}[kind]
    if submitted not in expected:
        raise OwnershipUnresolved('Container workflow does not match its submission: ' + name)
    if kind != 'surface':
        marker = 'service-cleanup.pending' if kind == 'journey' else 'docker-cleanup.pending'
        if not (attempt / marker).is_file():
            raise OwnershipUnresolved('Container cleanup intent is missing: ' + name)


def network(root, item, admitted):
    name = item['Name']
    raw_labels = item.get('Labels', '')
    if not name.startswith(PREFIX) and 'pandora.' not in raw_labels:
        return
    metadata = labels(raw_labels)
    if any(key.startswith('pandora.') and key != 'pandora.attempt' for key in metadata):
        raise OwnershipUnresolved('Unrecognized managed network labels: ' + name)
    identity = name.removeprefix(PREFIX)
    if not name.startswith(PREFIX) or not IDENTITY.fullmatch(identity) or metadata.get('pandora.attempt') != identity:
        raise OwnershipUnresolved('Network name and ownership labels disagree: ' + name)
    attempt = active(root, identity, admitted)
    if workflow(attempt) not in ('journey', 'suite') or not (attempt / 'service-cleanup.pending').is_file():
        raise OwnershipUnresolved('Network has no matching workflow cleanup intent: ' + name)


def inventory(args):
    result = subprocess.run(['sudo', 'docker', *args, '--format', '{{json .}}'],
                            check=True, capture_output=True, text=True, timeout=30)
    return [json.loads(line) for line in result.stdout.splitlines() if line]


def check(root, admitted):
    """Caller supplies its authoritative admission snapshot, never just live PIDs.

    Production passes only its current exclusive owner. A concurrent scheduler
    must supply its admitted attempts when integrated. This function neither
    changes capacity nor grants admission, and never stops/deletes resources.
    """
    if not isinstance(admitted, (set, frozenset)) or any(not isinstance(i, str) or not IDENTITY.fullmatch(i) for i in admitted):
        raise ValueError('Expected admitted attempt identities')
    for retry in range(2):
        try:
            containers = inventory(['ps', '-a'])
            networks = inventory(['network', 'ls'])
            for item in containers:
                container(root, item, admitted)
            for item in networks:
                network(root, item, admitted)
            return
        except (OwnershipUnresolved, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            # A peer can remove resources after the inventory snapshot. Refresh
            # once so that ordinary completion does not look like an orphan.
            if retry:
                raise OwnershipUnresolved(str(error) + '; no validation started. Ownership remains unresolved; operator reconciliation required.') from error
