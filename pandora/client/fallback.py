"""The fallback policy, and the log line every passthrough leaves.

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
* a busy worker (``admission-refused``, ``queue-timeout``) is never a reason,
  at any size or declaration: the worker queues, and the queue is the answer;
* anything that writes back (``--update``) never falls back, at any size,
  because a local run would write files the worker should have written.

A `local` verdict is not permission to `exec`. It means "admit this job into the
local lane, with its size class, behind the same queue as every other local
job" -- which the daemon does. When the daemon itself is the thing that is gone
there is no lane to admit into, and the shim runs the command as it would on a
machine with no Pandora: a passthrough, not a fallback, with no budget to hold.
"""
import json
import os
from pathlib import Path

# The causes, in the order they occur along a submission. Every one of them is
# provably non-executing; that is what makes falling back *permissible*, and it
# is the last question this module treats as interesting.
CAUSES = ('daemon-unreachable', 'daemon-closed', 'handshake-timeout',
          # `worker-down` is `worker-unreachable` known in advance, from the
          # health poll, rather than discovered by paying an SSH timeout. Same
          # verdict, same reasons; a separate name so the receipts and
          # `pandora stats` can tell "we waited 12 s to find out" apart from "we
          # already knew", which is the whole point of polling.
          'worker-down',
          'worker-unreachable', 'snapshot-failed', 'transfer-failed',
          'queue-timeout', 'admission-refused', 'engine-error')
# Sizes small enough that one more of them on this Mac is a slowdown rather than
# a stall. The line is drawn here because `large` is what eichler calls a
# browser suite and a full `check`, and both of them are what killed the Mac.
LOCAL_SIZES = ('small', 'medium')
# Causes that name a *busy* worker rather than a broken path to it (ruled
# 2026-09-24). A full worker queues the run; a queue that did not admit it in
# time, or a worker with every slot taken, is not a reason to put the same job
# on this Mac, where it competes with the agents that are busy for the same
# reason. They stay in `CAUSES` so a `pandora.toml` naming them still loads,
# and they refuse whatever it declares.
NEVER_LOCAL = ('admission-refused', 'queue-timeout')


# What a refusal tells the caller to do next. On 2026-09-24 a refusal that said
# "run it with PANDORA_OFF=1" sent several agents to run `pnpm check` here at
# once, unmanaged, and the memory gate paused the lane with swap growing at
# 4 GiB/min. The local lane is the same command behind the same budget, so it is
# the next step whenever the job can run there; PANDORA_OFF only when it cannot.
QUEUE_STEP = 'Retry, or run it in the local queue with PANDORA_WHERE=local.'
LAST_RESORT = ('Retry. As a last resort, PANDORA_OFF=1 runs it here with no Pandora at '
               'all, outside the queue.')


def next_step(local_lane):
    """The sentence a refusal ends with. `local_lane`: the job may run in it
    (`placement.why_not_local(job)` is None)."""
    return QUEUE_STEP if local_lane else LAST_RESORT


def decide(*, cause, size='large', writeback=False, declared=None, notice=None,
           local_lane=True):
    """The one fallback decision. Returns {'action', 'reason'}.

    `declared` is the job's `fallback` table, or None when the job and the
    repository both said nothing. `size` is the job's declared class, and the
    conservative default is `large`: a caller that cannot say how big a job is
    has not earned the right to run it here. `local_lane` says whether the job
    could run in the local lane if asked; it changes only the refusal's next step.
    """
    step = next_step(local_lane)
    if cause not in CAUSES:
        return {'action': 'refuse', 'reason': 'unknown fallback cause %r. %s' % (cause, step)}
    if cause in NEVER_LOCAL:
        return {'action': 'refuse',
                'reason': 'the worker is busy, not unreachable, and a busy worker is waited '
                          'for rather than moved to this Mac. %s' % step}
    if writeback:
        return {'action': 'refuse',
                'reason': 'a write-back run is never moved to this Mac automatically, because '
                          'a local run would write files the worker should have written. %s'
                          % step}
    if declared is not None and cause not in declared['on']:
        return {'action': 'refuse',
                'reason': 'this job declares fallback only for %s, and this is %s. %s'
                          % (', '.join(declared['on']), cause, step)}
    if declared is not None:
        action = declared['action']
        why = 'the job declares fallback = "%s"' % action
    else:
        action = 'local' if size in LOCAL_SIZES else 'refuse'
        why = 'the job is size %s and declares no fallback' % size
    if action == 'local':
        return {'action': 'local', 'reason': notice or ('%s, so it runs in the local lane' % why)}
    return {'action': 'refuse',
            'reason': '%s, so it is not moved to this Mac automatically. %s' % (why, step)}


def record(state, entry):
    """Append one JSONL line.  O_APPEND on a short line is atomic enough."""
    path = Path(state) / 'passthrough.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, separators=(',', ':')) + '\n'
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), 'a') as handle:
        handle.write(line)
