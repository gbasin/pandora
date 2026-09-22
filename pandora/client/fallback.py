"""The fallback policy, and the bounded exec that is its last resort.

The owner's rule is that a claimed command may run locally only while its
non-execution on the worker is still provable. That makes fallback correct but
not free, and on 2026-09-22 it cost 3.3 minutes of a 302-test browser suite on
this Mac: a submission the worker's ledger refused came back as one more reason
to `exec` pnpm here, because "the command has not run" was the only question
anyone asked.

The second question this module exists to ask is *should it run here*, and it
has one answer for every cause:

* a job may declare ``fallback = "local"`` or ``"refuse"``;
* a job that declares nothing is decided by its size class -- ``small`` and
  ``medium`` fall back, ``large`` and ``xlarge`` do not;
* anything that writes back (``--update``) never falls back, at any size,
  because a local run would write files the worker should have written.

A `local` verdict is not permission to `exec`. It means "admit this job into the
local lane, with its size class, behind the same queue as every other local
job" -- which the daemon does. The slot budget below is what is left when the
daemon itself is the thing that is gone: there is no local lane to admit into,
so the bound is ``fallback_slots`` file locks, held in the state directory
rather than in a process that is not running.
"""
import errno
import fcntl
import json
import os
from pathlib import Path
import time

# The causes, in the order they occur along a submission. Every one of them is
# provably non-executing; that is what makes falling back *permissible*, and it
# is the last question this module treats as interesting.
CAUSES = ('daemon-unreachable', 'daemon-closed', 'handshake-timeout',
          'worker-unreachable', 'snapshot-failed', 'transfer-failed',
          'queue-timeout', 'admission-refused', 'engine-error')
# Sizes small enough that one more of them on this Mac is a slowdown rather than
# a stall. The line is drawn here because `large` is what eichler calls a
# browser suite and a full `check`, and both of them are what killed the Mac.
LOCAL_SIZES = ('small', 'medium')


def decide(*, cause, size='large', writeback=False, declared=None, notice=None):
    """The one fallback decision. Returns {'action', 'reason'}.

    `declared` is the job's `fallback` table, or None when the job and the
    repository both said nothing. `size` is the job's declared class, and the
    conservative default is `large`: a caller that cannot say how big a job is
    has not earned the right to run it here.
    """
    if cause not in CAUSES:
        return {'action': 'refuse', 'reason': 'unknown fallback cause %r' % cause}
    if writeback:
        return {'action': 'refuse',
                'reason': 'a write-back run is never run locally, because a local run would '
                          'write files the worker should have written'}
    if declared is not None and cause not in declared['on']:
        return {'action': 'refuse',
                'reason': 'this job declares fallback only for %s, and this is %s'
                          % (', '.join(declared['on']), cause)}
    if declared is not None:
        action = declared['action']
        why = 'the job declares fallback = "%s"' % action
    else:
        action = 'local' if size in LOCAL_SIZES else 'refuse'
        why = 'the job is size %s and declares no fallback' % size
    if action == 'local':
        return {'action': 'local', 'reason': notice or ('%s, so it runs in the local lane' % why)}
    return {'action': 'refuse',
            'reason': '%s, so it is not run on this Mac. Run it with PANDORA_OFF=1 if you '
                      'mean to.' % why}


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
