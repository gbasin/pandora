"""Bounded local fallback, and the passthrough record it leaves behind.

The owner's rule is that a claimed command may run locally only while its
non-execution on the worker is still provable.  That makes fallback correct but
not free: if the worker is down, every agent on the machine falls back at once
and the Mac is exactly as overloaded as it was before Pandora existed.

So a claimed command that falls back must take one of ``fallback_slots``
file locks first.  The locks live in the state directory rather than in the
daemon, because the commonest reason to fall back is that the daemon is gone.
"""
import errno
import fcntl
import json
import os
from pathlib import Path
import time


class Slot:
    def __init__(self, handle, index):
        self.handle = handle
        self.index = index

    def release(self):
        try:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
        finally:
            self.handle.close()


class Unbounded(Exception):
    """The budget's own storage is unreachable, so it cannot be enforced.

    This is not hypothetical.  A shim running inside a Codex `workspace-write`
    sandbox cannot write to the daemon's state directory unless it was passed
    with `--add-dir`, and that is exactly the case where the daemon is also
    unreachable and fallback is most likely.  Refusing to run the command would
    be worse than running it unbounded, so the caller warns and proceeds.
    """


def acquire(state, count, wait_seconds=0.0, poll=0.02):
    """One of ``count`` slots, or None once ``wait_seconds`` has elapsed."""
    directory = Path(state) / 'fallback'
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise Unbounded(str(error)) from None
    deadline = time.monotonic() + max(wait_seconds, 0.0)
    while True:
        for index in range(count):
            try:
                handle = (directory / ('slot-%d.lock' % index)).open('a+')
            except OSError as error:
                raise Unbounded(str(error)) from None
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                handle.close()
                if error.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                continue
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()) + '\n')
            handle.flush()
            return Slot(handle, index)
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


def record(state, entry):
    """Append one JSONL line.  O_APPEND on a short line is atomic enough."""
    path = Path(state) / 'passthrough.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, separators=(',', ':')) + '\n'
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), 'a') as handle:
        handle.write(line)


def config_for(state):
    path = Path(state) / 'config.json'
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
