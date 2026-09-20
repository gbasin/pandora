"""Collect only acknowledged Pandora build tags under the worker lease."""
import json
from pathlib import Path
import re
import subprocess
from docker_images import locked


def acknowledged(attempt):
    terminal = attempt / 'terminal.json'
    return ((attempt / 'released').is_file() and terminal.is_file()
            and json.loads(terminal.read_text()).get('cleanup_verified') is True)


def eligibility(root):
    ledger = root / 'docker-collectible.json'
    tags = set(json.loads(ledger.read_text())) if ledger.exists() else set()
    for attempt in (root / 'runs').glob('*'):
        if not re.fullmatch('[a-f0-9]{32}', attempt.name) or attempt.is_symlink():
            continue
        submission = attempt / 'submission.json'
        if submission.is_file() and acknowledged(attempt):
            submitted = json.loads(submission.read_text())
            if submitted.get('docker', {}).get('request', {}).get('kind') == 'build':
                tags.add('pandora-build:' + attempt.name)
    if any(not re.fullmatch('pandora-build:[a-f0-9]{32}', tag) for tag in tags):
        raise ValueError('Invalid collectible image ledger')
    return tags


def protected_images(root):
    protected = set()
    for mapping in (root / 'docker-images').glob('*/*.json'):
        protected.add(json.loads(mapping.read_text())['image_id'])
    for pin in (root / 'docker-pins').glob('*.json'):
        if not acknowledged(root / 'runs' / pin.stem):
            protected.add(json.loads(pin.read_text())['image']['image_id'])
        else:
            pin.unlink()
    # Also cover accepted requests submitted before durable pins were introduced.
    for submission in (root / 'runs').glob('*/submission.json'):
        if not acknowledged(submission.parent):
            image = json.loads(submission.read_text()).get('docker', {}).get('image')
            if image:
                protected.add(image['image_id'])
    return protected


def collect(root):
    # Lock order: worker lease, then image registry. Clients take registry only.
    with locked(root):
        tags = eligibility(root)
        protected = protected_images(root)
        retained = []
        removed = []
        listed = subprocess.run(['sudo', 'docker', 'image', 'ls', '--no-trunc',
                                 '--format', '{{json .}}'], check=True, capture_output=True, text=True)
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
            result = subprocess.run(['sudo', 'docker', 'image', 'rm', tag], capture_output=True)
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
