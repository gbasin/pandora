"""What earlier attempts say about this one: typical durations and flaky pairs.

Both are read from the ledger and neither is a model. A typical duration is the
median of the last few attempts of the same job that reached a verdict, and a
job with too few of them has no typical duration -- the caller prints no
estimate rather than one invented from nothing. A flaky pair is two attempts
with the same input, the same command line and the same environment, one of
which failed and one of which passed. That is evidence of non-determinism and
nothing more: nothing here re-runs anything because of it.
"""
import json
import statistics
import time

# The last N attempts that reached a verdict, and how many of them an estimate
# needs. Three is the admission rule's cold-start threshold for the same reason:
# one or two runs of a job are the ones most likely to be unrepresentative.
RECENT = 5
MIN_SAMPLES = 3
VERDICTS = ('passed', 'command_failed')


def fmt_seconds(seconds):
    """`40 s`, `4m10s`, `1h02m`: short enough for one stderr line."""
    seconds = int(round(max(0.0, float(seconds))))
    if seconds < 60:
        return '%d s' % seconds
    if seconds < 3600:
        return '%dm%02ds' % (seconds // 60, seconds % 60)
    return '%dh%02dm' % (seconds // 3600, (seconds % 3600) // 60)


def _durations(row):
    try:
        return json.loads(row['durations'] or '{}')
    except (TypeError, ValueError):
        return {}


def samples(ledger, repo, job, *, role='single', key='execute', limit=RECENT):
    """Up to `limit` recent values of one recorded duration, newest first.

    `key='wall'` is the attempt's whole life, created to finished, which is
    what a queue estimate needs: the lane frees when the attempt ends, not when
    its command does.
    """
    rows = ledger.db.execute(
        "SELECT durations, created, finished FROM attempts WHERE repo=? AND job=? "
        "AND COALESCE(role, 'single')=? AND state='finished' AND outcome IN (?, ?) "
        "ORDER BY finished DESC LIMIT ?",
        (repo, job, role, *VERDICTS, limit * 2)).fetchall()
    out = []
    for row in rows:
        if key == 'wall':
            value = (row['finished'] or 0) - (row['created'] or 0)
        else:
            value = _durations(row).get(key)
        if isinstance(value, (int, float)) and value > 0:
            out.append(float(value))
        if len(out) >= limit:
            break
    return out


def typical(ledger, repo, job, *, role='single', key='execute'):
    """The median of the recent samples, or None when there are too few."""
    values = samples(ledger, repo, job, role=role, key=key)
    if len(values) < MIN_SAMPLES:
        return None
    return statistics.median(values)


def queue_eta(ledger, rows, *, now=None):
    """Seconds until the first of `rows` is expected to free its lane, or None.

    The soonest, not the sum: admission re-checks every time a lane frees, and
    a waiting run needs one lane. A row whose job has no history contributes
    nothing, and if none has any the answer is None rather than a guess.
    """
    now = time.time() if now is None else now
    best = None
    for row in rows:
        wall = typical(ledger, row['repo'], row['job'],
                       role=row['role'] or 'single', key='wall')
        if wall is None:
            continue
        remaining = max(0.0, wall - (now - (row['created'] or now)))
        best = remaining if best is None else min(best, remaining)
    return best


# -- flaky pairs ----------------------------------------------------------------

def caller_id(row):
    """The run id the caller was given, from the request id the daemon chose.

    The daemon submits `<caller run id>:<job>` (and `...:retry` for a retry),
    and that first segment is what `pandora result` takes. A request that did
    not come from a daemon has no such segment and is named by its own id.
    """
    request = row['request_id'] or ''
    return request.split(':', 1)[0] if ':' in request else row['run_id']


def _same_command(a, b):
    def decoded(value):
        try:
            return json.loads(value) if isinstance(value, str) else value
        except ValueError:
            return value
    return (decoded(a['argv']) == decoded(b['argv']) and decoded(a['env']) == decoded(b['env'])
            and a['cwd'] == b['cwd'])


def previous_verdict(ledger, row):
    """The most recent earlier attempt of the same input that reached a verdict.

    Same repository, job and input digest -- the `same_input_as` key -- and
    additionally the same argv, environment and cwd, because `same_input_as`
    does not compare them and `journey S0-01` and `journey S0-02` share every
    byte of their input while testing different things.
    """
    candidates = ledger.db.execute(
        "SELECT * FROM attempts WHERE repo=? AND job=? AND input_id=? AND run_id<>? "
        "AND COALESCE(role, 'single')=? AND state='finished' AND outcome IN (?, ?) "
        "AND created<=? ORDER BY created DESC LIMIT 20",
        (row['repo'], row['job'], row['input_id'], row['run_id'], row['role'] or 'single',
         *VERDICTS, row['created'])).fetchall()
    for candidate in candidates:
        if _same_command(candidate, row):
            return candidate
    return None


def _pair(earlier, later, outcome_earlier, outcome_later):
    failed, passed = (earlier, later) if outcome_earlier == 'command_failed' else (later, earlier)
    return {'order': ('failed-then-passed' if outcome_earlier == 'command_failed'
                      else 'passed-then-failed'),
            'failed': caller_id(failed), 'failed_run': failed['run_id'],
            'passed': caller_id(passed), 'passed_run': passed['run_id']}


def latest_shards(ledger, parent):
    """{index: row} for a parent's shards, the last attempt at each index."""
    out = {}
    for row in ledger.children(parent):
        if (row['role'] or '') != 'shard' or row['shard_index'] is None:
            continue
        current = out.get(row['shard_index'])
        if current is None or (row['created'] or 0) >= (current['created'] or 0):
            out[row['shard_index']] = row
    return out


def flaky(ledger, run_id, outcome):
    """The flaky pair this attempt completes, or None.

    Whole attempts compare with whole attempts. A fan-out parent additionally
    compares shard by shard with the earlier parent when both were cut into the
    same number of shards, because "shard 2 of 4 failed then passed" is the
    finer and more useful fact, and it can be true while both parents failed on
    account of a different shard.
    """
    row = ledger.get(run_id)
    if row is None or (row['role'] or 'single') not in ('single', 'parent'):
        return None
    earlier = previous_verdict(ledger, row)
    if earlier is None:
        return None
    answer = {'with': earlier['run_id']}
    if outcome in VERDICTS and earlier['outcome'] != outcome:
        answer.update(_pair(earlier, row, earlier['outcome'], outcome))
    if (row['role'] or 'single') == 'parent':
        mine, theirs = latest_shards(ledger, run_id), latest_shards(ledger, earlier['run_id'])
        pairs = []
        for index in sorted(set(mine) & set(theirs)):
            now_row, then_row = mine[index], theirs[index]
            if now_row['shard_total'] != then_row['shard_total']:
                continue
            if (now_row['outcome'] in VERDICTS and then_row['outcome'] in VERDICTS
                    and now_row['outcome'] != then_row['outcome']):
                pair = _pair(earlier, row, then_row['outcome'], now_row['outcome'])
                pair['shard'] = '%d/%d' % (index, now_row['shard_total'])
                pairs.append(pair)
        if pairs:
            answer['shards'] = pairs
    if 'order' not in answer and not answer.get('shards'):
        return None
    return answer
