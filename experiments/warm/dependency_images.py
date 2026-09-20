"""Pin dependency tags before lookup; collect only outside every accepted use."""
from contextlib import contextmanager
import fcntl
import json
import re
import subprocess

from admission import alive, receipt


def identity(image):
    if not isinstance(image, str) or not re.fullmatch(r'pandora-deps:[a-f0-9]{64}', image):
        raise ValueError('Unexpected managed dependency image identity')
    return image


@contextmanager
def locked(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'dependency-images.lock').open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def write(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value) + '\n')
    temporary.replace(path)


def pin(attempt, image):
    """Must precede image inspection/build and outlive execution/container cleanup."""
    identity(image)
    if not re.fullmatch('[a-f0-9]{32}', attempt.name) or not alive(attempt):
        raise ValueError('Dependency reservation requires a valid held attempt lock')
    root = attempt.parent.parent
    with locked(root):
        target = root / 'dependency-pins' / (attempt.name + '.json')
        record = {'attempt': attempt.name, 'image': image}
        if target.exists():
            if json.loads(target.read_text()) != record:
                raise ValueError('Attempt already reserved a different dependency image')
            return
        target.parent.mkdir(exist_ok=True)
        write(target, record)


def cleaned(attempt):
    return (receipt(attempt / 'terminal.json', attempt.name) or
            (not alive(attempt) and receipt(attempt / 'admission-cleanup.json', attempt.name)))


def release(attempt):
    """A dead process alone cannot release its image reservation."""
    root = attempt.parent.parent
    with locked(root):
        if not cleaned(attempt):
            return False
        (root / 'dependency-pins' / (attempt.name + '.json')).unlink(missing_ok=True)
        return True


def protected(root):
    images = set()
    # Validate all records before allowing any Docker deletion. Missing attempts
    # remain protected: retention or operator action is not cleanup evidence.
    records = []
    for path in (root / 'dependency-pins').glob('*.json'):
        data = json.loads(path.read_text())
        if (path.is_symlink() or not re.fullmatch('[a-f0-9]{32}', path.stem) or
                not isinstance(data, dict) or set(data) != {'attempt', 'image'} or
                data['attempt'] != path.stem):
            raise ValueError('Invalid dependency image reservation')
        records.append((path, identity(data['image'])))
    for path, image in records:
        if cleaned(root / 'runs' / path.stem):
            path.unlink()
        else:
            images.add(image)
    return images


def remember(root, image):
    identity(image)
    with locked(root):
        ledger = root / 'integrated-images.json'
        images = json.loads(ledger.read_text()) if ledger.exists() else []
        if not isinstance(images, list) or len(set(map(identity, images))) != len(images):
            raise ValueError('Invalid dependency image retention ledger')
        images = [item for item in images if item != image] + [image]
        pins = protected(root)
        retained = []
        for item in images[:-3]:
            if item in pins:
                retained.append(item)
                continue
            # No force: retained diagnostic containers also protect their image.
            result = subprocess.run(['sudo', 'docker', 'image', 'rm', item],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            if result.returncode:
                retained.append(item)
        write(ledger, retained + images[-3:])
