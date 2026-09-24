"""The client's half of the progress lines: estimates from this Mac's own runs.

The engine says what happens on the worker -- `instance ready in 3.1 s`,
`running (typical 4m10s for check; ...)` -- from its ledger. The client says the
rest: how long the local lane's queue is likely to hold a job, and where a run
is when `pandora wait` attaches to it. Both come from what the daemon already
wrote per run, `meta.json` and `result.json`, and both follow the engine's
rule: the median of the last few runs of the same job that reached a verdict,
and no estimate at all when there are too few of them.

Every line here is one line at one moment. Nothing re-prints on a timer except
the queue's `still queued`, which is bounded to once a minute by its caller.
"""
import json
import statistics
import time
from pathlib import Path

from ..engine.history import MIN_SAMPLES, RECENT, VERDICTS, fmt_seconds

STILL_EVERY = 60.0


def _read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def seconds_of(result, lane):
    """The command's own time: the engine's `execute`, or a local run's wall."""
    durations = result.get('durations') or {}
    if lane != 'local' and isinstance(durations.get('execute'), (int, float)):
        return float(durations['execute'])
    if lane == 'local' and isinstance(result.get('wall_seconds'), (int, float)):
        return float(result['wall_seconds'])
    return None


def typical(state, job, lane, *, exclude=None):
    """Median command time of the last `RECENT` verdicts of `job` in `lane`.

    Walks the run directories newest first by modification time and stops as
    soon as it has enough, so a year of history costs one `stat` per run rather
    than one parse per run.
    """
    if not job:
        return None
    try:
        metas = sorted((Path(state) / 'runs').glob('*/meta.json'),
                       key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return None
    values = []
    for path in metas:
        meta = _read(path)
        if not meta or meta.get('id') == exclude or meta.get('job') != job:
            continue
        if (meta.get('lane') or 'remote') != lane or meta.get('state') not in VERDICTS:
            continue
        result = _read(path.parent / 'result.json') or {}
        value = seconds_of(result, lane)
        if value:
            values.append(value)
        if len(values) >= RECENT:
            break
    if len(values) < MIN_SAMPLES:
        return None
    return statistics.median(values)


def queue_eta(state, runs, *, now=None):
    """Seconds until the first of the running local `runs` should finish, or None."""
    now = time.time() if now is None else now
    best = None
    for run in runs:
        expected = typical(state, run.request.get('job'), 'local', exclude=run.id)
        if expected is None or run.accepted is None:
            continue
        remaining = max(0.0, expected - (now - run.accepted))
        best = remaining if best is None else min(best, remaining)
    return best


def queue_line(count, eta, *, first):
    """`queued behind 1 run, ~40 s`, or `still queued behind 1 run`."""
    runs = '%d run%s' % (count, '' if count == 1 else 's')
    if not first:
        return 'still queued behind ' + runs
    return 'queued behind %s%s' % (runs, ', ~' + fmt_seconds(eta) if eta is not None else '')


# What the engine's row state means to someone attaching.
PHASES = {'queued': 'waiting for admission on the worker',
          'admitted': 'starting an instance on the worker',
          'running': 'running on the worker',
          'collecting': 'collecting outputs on the worker'}


# The daemon's own steps before `accepted` (`Worker.submit`'s `phase`).
PRE_ACCEPT = {'freeze': ', freezing the worktree', 'ship': ', shipping its source to the worker',
              'submit': ', submitting to the worker'}


def attach_line(state, run, *, now=None):
    """The one line `pandora wait` prints on attach: where the run is right now."""
    now = time.time() if now is None else now
    if run.done.is_set() or run.state not in ('queued', 'running'):
        return 'run %s finished: %s, exit %s' % (run.id, run.state, run.exit_code)
    if run.accepted is None:
        return 'run %s is queued%s' % (run.id, PRE_ACCEPT.get(run.phase, ''))
    lane = run.lane or 'remote'
    job = run.request.get('job')
    if lane == 'local':
        where = 'running locally'
    else:
        where = PHASES.get(run.phase or 'running', 'running on the worker')
        if run.remote:
            where += ' as ' + run.remote
    expected = typical(state, job, lane, exclude=run.id)
    extra = ('; typical %s for %s' % (fmt_seconds(expected), job)
             if expected is not None else '')
    retried = ', second attempt' if run.attempts else ''
    return 'run %s is %s%s (%s since accepted%s)' % (
        run.id, where, retried, fmt_seconds(now - run.accepted), extra)
