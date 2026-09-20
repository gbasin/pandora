"""Exclusive ownership of a persistent builder, including crash cleanup."""
import fcntl
import json
import re
import subprocess

from admission import alive

BUILDERS = {'pandora-surface-deps-v3', 'pandora-docker-builds-v1'}


def paths(attempt, builder):
    if builder not in BUILDERS or not re.fullmatch('[a-f0-9]{32}', attempt.name):
        raise ValueError('Invalid builder ownership identity')
    directory = attempt.parent.parent / 'builder-owners'
    directory.mkdir(parents=True, exist_ok=True)
    return directory / (builder + '.lock'), directory / (builder + '.json')


def owner(path):
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if (not isinstance(data, dict) or set(data) != {'attempt'} or
            not isinstance(data['attempt'], str) or not re.fullmatch('[a-f0-9]{32}', data['attempt'])):
        raise ValueError('Invalid builder ownership record')
    return data['attempt']


def stopped(builder):
    result = subprocess.run(['sudo', 'docker', 'ps', '--filter',
                             'name=^/buildx_buildkit_' + builder + '0$', '--format', '{{.Names}}'],
                            capture_output=True, text=True, timeout=30)
    return result.returncode == 0 and not result.stdout.strip()


def finish(attempt, builder, record, pending):
    current = owner(record)
    if current is None and pending.exists():
        # Interrupted after owner removal: no side effect may begin without
        # ownership under this lock. Verify absence before clearing the barrier.
        if not stopped(builder):
            return False
        pending.unlink()
        return True
    if current != attempt.name:
        # A delayed cleanup must never stop a successor's builder. An unresolved
        # marker without matching ownership requires operator reconciliation.
        return not pending.exists()
    exists = subprocess.run(['sudo', 'docker', 'buildx', 'inspect', builder], capture_output=True, timeout=30)
    if exists.returncode == 0:
        result = subprocess.run(['sudo', 'docker', 'buildx', 'stop', builder], capture_output=True, timeout=30)
        if result.returncode:
            return False
    if not stopped(builder):
        return False
    record.unlink()
    pending.unlink(missing_ok=True)
    return True


class Lease:
    def __init__(self, attempt, builder, handle, record, pending):
        self.attempt, self.builder = attempt, builder
        self.handle, self.record, self.pending = handle, record, pending

    def close(self):
        try:
            return finish(self.attempt, self.builder, self.record, self.pending)
        finally:
            self.handle.close()


def acquire(attempt, builder, marker):
    if not alive(attempt):
        raise RuntimeError('Builder ownership requires the held attempt lock')
    lock, record = paths(attempt, builder)
    handle = lock.open('a')
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Builder is owned by another run; no build started') from None
        if owner(record) is not None:
            raise RuntimeError('Builder cleanup is unresolved; no build started')
        if not stopped(builder):
            raise RuntimeError('Builder has unowned activity; operator cleanup required')
        temporary = record.with_suffix('.tmp')
        temporary.write_text(json.dumps({'attempt': attempt.name}) + '\n')
        temporary.replace(record)
        pending = attempt / marker
        pending.touch()
        return Lease(attempt, builder, handle, record, pending)
    except BaseException:
        handle.close()
        raise


def cleanup(attempt, builder, marker):
    lock, record = paths(attempt, builder)
    with lock.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Unrelated attempts with no marker own nothing to clean. The active
            # builder owner cannot be cleaned while its process holds this lock.
            return owner(record) != attempt.name and not (attempt / marker).exists()
        return finish(attempt, builder, record, attempt / marker)
