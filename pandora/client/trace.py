"""A run's timeline as a Perfetto trace: one JSON file beside `result.json`.

Durations in a result are lengths, not spans -- `result.json` is written with
sorted keys, so the order the engine ran them in lives here as a list instead.
A phase that ran inside another (a fan-out's `plan` inside `queue` time, say)
is still drawn sequentially: the lengths are honest, the offsets approximate.
The client half (freeze, ship, submit, the pre-accept wait) is placed against
real wall clock, because `meta` carries `started` and `accepted`.

Open a `trace.json` at <https://ui.perfetto.dev>; an agent can read it as JSON.
"""
import json
from pathlib import Path

# Engine phases in run order. Anything a result adds later lands at the end.
PHASES = ('queue', 'plan', 'clone', 'start', 'inject', 'git', 'prepare',
          'prepare_command', 'boot', 'harden', 'execute', 'collect', 'destroy')


def events(meta, result):
    """Complete-event rows for one run, or [] when there is nothing to time."""
    started = meta.get('started')
    if not isinstance(started, (int, float)):
        return []
    at = float(started) * 1e6
    out = []

    def span(name, seconds, tid):
        nonlocal at
        if not isinstance(seconds, (int, float)) or seconds <= 0:
            return
        out.append({'name': name, 'ph': 'X', 'ts': round(at, 1),
                    'dur': round(float(seconds) * 1e6, 1), 'pid': 1, 'tid': tid})
        at += float(seconds) * 1e6

    # The client half, inside the request-to-accepted window.
    base = at
    for step in ('freeze', 'ship', 'submit'):
        span(step, (meta.get('pre_accept') or {}).get(step), 1)
    accepted = meta.get('accepted')
    if isinstance(accepted, (int, float)):
        at = max(at, float(accepted) * 1e6)
        out.append({'name': 'request to accepted', 'ph': 'X', 'ts': round(base, 1),
                    'dur': round(float(accepted - started) * 1e6, 1),
                    'pid': 1, 'tid': 1})

    # Every attempt on the worker, in order -- a retried run is two rows deep.
    # Earlier attempts record only a wall clock; the last carries the phases.
    for depth, attempt in enumerate(list(result.get('attempts') or []) + [result]):
        durations = attempt.get('durations') or {}
        row = [name for name in PHASES if name in durations]
        row += [name for name in durations
                if name not in PHASES and name not in ('total', 'wall')]
        mark = at
        if not row:
            span('%d. %s' % (depth + 1, attempt.get('outcome') or 'attempt'),
                 attempt.get('wall_seconds'), 2 + depth)
        else:
            for name in row:
                span(name, durations[name], 2 + depth)
        at = max(at, mark)
    return out


def write(run_dir, meta, result):
    """`trace.json` beside `result.json`, or None. A trace is a courtesy."""
    try:
        rendered = events(meta, result or {})
        if not rendered:
            return None
        path = Path(run_dir) / 'trace.json'
        path.write_text(json.dumps({'traceEvents': rendered}))
        return path
    except (OSError, ValueError, TypeError):
        return None
