"""Collect only acknowledged Pandora build tags under the worker lease."""
import json
import re
import subprocess
from docker_images import locked


class CollectionDeferred(RuntimeError):
    """Metadata is changing or corrupt, so no image may be deleted."""


def _object(path, description):
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise CollectionDeferred(description + ' is incomplete or unreadable') from error
    if not isinstance(value, dict):
        raise CollectionDeferred(description + ' is not an object')
    return value


def acknowledged(attempt):
    terminal = attempt / 'terminal.json'
    return ((attempt / 'released').is_file() and terminal.is_file()
            and _object(terminal, 'terminal receipt').get('cleanup_verified') is True)


def eligibility(root):
    ledger = root / 'docker-collectible.json'
    tags = set(json.loads(ledger.read_text())) if ledger.exists() else set()
    for attempt in (root / 'runs').glob('*'):
        if not re.fullmatch('[a-f0-9]{32}', attempt.name) or attempt.is_symlink():
            continue
        submission = attempt / 'submission.json'
        if submission.is_file() and acknowledged(attempt):
            submitted = _object(submission, 'submission metadata')
            docker = submitted.get('docker')
            if docker is not None and not isinstance(docker, dict):
                raise CollectionDeferred('submission docker metadata is malformed')
            request = docker.get('request') if docker else None
            if request is not None and not isinstance(request, dict):
                raise CollectionDeferred('submission docker request metadata is malformed')
            if request and request.get('kind') == 'build':
                tags.add('pandora-build:' + attempt.name)
    if any(not re.fullmatch('pandora-build:[a-f0-9]{32}', tag) for tag in tags):
        raise ValueError('Invalid collectible image ledger')
    return tags


def protected_images(root):
    protected = set()
    for mapping in (root / 'docker-images').glob('*/*.json'):
        image_id = _object(mapping, 'image mapping').get('image_id')
        if not isinstance(image_id, str) or not image_id:
            raise CollectionDeferred('image mapping has no image identity')
        protected.add(image_id)
    stale_pins = []
    for pin in (root / 'docker-pins').glob('*.json'):
        if not acknowledged(root / 'runs' / pin.stem):
            image = _object(pin, 'image pin').get('image')
            if not isinstance(image, dict) or not isinstance(image.get('image_id'), str) or not image['image_id']:
                raise CollectionDeferred('image pin has no image identity')
            protected.add(image['image_id'])
        else:
            stale_pins.append(pin)
    # Also cover accepted requests submitted before durable pins were introduced.
    for submission in (root / 'runs').glob('*/submission.json'):
        if not acknowledged(submission.parent):
            submitted = _object(submission, 'submission metadata')
            docker = submitted.get('docker')
            if docker is not None and not isinstance(docker, dict):
                raise CollectionDeferred('submission docker metadata is malformed')
            image = docker.get('image') if docker else None
            if image:
                if not isinstance(image, dict) or not isinstance(image.get('image_id'), str) or not image['image_id']:
                    raise CollectionDeferred('submission image metadata is malformed')
                protected.add(image['image_id'])
    # Do not discard acknowledged pins until every protection source is valid.
    for pin in stale_pins:
        pin.unlink()
    return protected


def collect(root):
    # Lock order: worker lease, then image registry. Clients take registry only.
    with locked(root):
        try:
            tags = eligibility(root)
            protected = protected_images(root)
        except CollectionDeferred as error:
            print('[pandora] deferred image collection: ' + str(error), flush=True)
            return []
        retained = []
        removed = []
        listed = subprocess.run(['sudo', 'docker', 'image', 'ls', '--no-trunc',
                                 '--format', '{{json .}}'], check=True, capture_output=True, text=True, timeout=60)
        inventory = {}
        for line in listed.stdout.splitlines():
            item = json.loads(line)
            inventory[item['Repository'] + ':' + item['Tag']] = item['ID']
        for tag in sorted(tags):
            if tag not in inventory:
                continue
            if inventory[tag] in protected:
                retained.append(tag)
                continue
            result = subprocess.run(['sudo', 'docker', 'image', 'rm', tag], capture_output=True, timeout=60)
            if result.returncode:
                retained.append(tag)
            else:
                removed.append(tag)
        ledger = root / 'docker-collectible.json'
        temporary = ledger.with_suffix('.tmp')
        temporary.write_text(json.dumps(retained) + '\n')
        temporary.replace(ledger)
        if removed:
            print(f'[pandora] collected {len(removed)} unused acknowledged build tags', flush=True)
        return removed
