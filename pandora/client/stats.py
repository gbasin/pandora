"""`pandora stats`: what ran, what waited, what fell back, and what did not route.

Built from what is already on disk. The client writes a `meta.json` and a
`result.json` per run and one JSONL line per passthrough; the worker's engine
answers one `health` call carrying its ledger counts, its scheduler picture, its
pool, its goldens and its ready state. Nothing here samples anything, and
nothing here is a counter that a restart would lose.

The requirement this answers in full is the one about what *did not* route.
Routing reports what Pandora took. A Mac that is on fire is usually on fire
because of the commands Pandora declined -- the unclaimed heavy ones that went
straight through the shim -- and until they are counted beside the routed ones
nobody can say whether routing helped. So the passthrough table is not a
footnote: it is the same table, with `why` instead of `outcome`.

Two honesty notes that the renderer says out loud rather than hiding:

* **Queue wait** is the wall clock from the request reaching the daemon to the
  `accepted` frame. For a local run that is the admission wait. For a remote one
  it is freeze plus ship plus submit -- the engine admits synchronously and
  refuses rather than queues -- so it is a *pre-accept* wait rather than time
  spent in a queue, and it is labelled that way.
* **Execute** is the command's own time: the engine's `durations.execute` for a
  remote run, the whole supervised wall for a local one. They are not the same
  measurement and they are not summed together.
"""
import json
import time
from pathlib import Path

WINDOWS = {'h': 3600, 'd': 86400, 'w': 604800, 'm': 60, 's': 1}


def parse_since(text, *, now=None):
    """`24h`, `7d`, `90m`, `3600` -> the epoch second the window opens at.

    None or empty means all of it. An unparseable window is an error the caller
    reports rather than a silently different window, because "stats for the last
    24 hours" and "stats for all time" are answers a reader acts on differently.
    """
    if text in (None, '', 'all'):
        return None
    raw = str(text).strip().lower()
    unit = WINDOWS.get(raw[-1:], None)
    digits = raw[:-1] if unit else raw
    if not digits.replace('.', '', 1).isdigit():
        raise ValueError('cannot read %r as a window; try 24h, 7d or 90m' % text)
    return (now if now is not None else time.time()) - float(digits) * (unit or 1)


def percentile(values, fraction):
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


def spread(values):
    return {'n': len(values), 'p50': round(percentile(values, 0.5), 2),
            'p95': round(percentile(values, 0.95), 2)}


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def read_runs(state, since=None):
    """Every run in the window, as (meta, result) pairs. Newest first.

    `result.json` is read only for runs inside the window, so a state directory
    with a year of history costs one `stat` per run outside it.
    """
    rows = []
    for meta_path in sorted((Path(state) / 'runs').glob('*/meta.json')):
        meta = read_json(meta_path)
        if meta is None:
            continue
        if since is not None and (meta.get('started') or 0) < since:
            continue
        rows.append((meta, read_json(meta_path.parent / 'result.json') or {}))
    rows.sort(key=lambda pair: pair[0].get('started', 0), reverse=True)
    return rows


def read_passthrough(state, since=None):
    rows = []
    path = Path(state) / 'passthrough.jsonl'
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if since is not None and (item.get('ts') or 0) < since:
            continue
        rows.append(item)
    return rows


def execute_seconds(meta, result):
    """The command's own time, from whichever lane measured it."""
    if not result:
        return None
    durations = result.get('durations') or {}
    if 'execute' in durations:
        return float(durations['execute'])
    if result.get('lane') == 'local' or (meta.get('lane') == 'local' and 'wall_seconds' in result):
        return float(result.get('wall_seconds') or 0)
    return None


def build(state, *, since=None, worker=None, pause=None, local=None, window=None):
    """The whole report, as one dictionary. `--json` prints exactly this."""
    runs = read_runs(state, since)
    passthrough = read_passthrough(state, since)
    by_job, waits, executes = {}, {'local': [], 'remote': []}, {}
    fallbacks, drift = {}, {'warned': 0, 'failed': 0}
    oom = 0
    retried = {'runs': 0, 'recovered': 0, 'causes': {}}
    flaky = {'pairs': 0, 'shard_pairs': 0}
    overrides = overrides_from(runs, passthrough)
    for meta, result in runs:
        lane = meta.get('lane') or 'remote'
        job = meta.get('job') or '(unclassified)'
        outcome = result.get('outcome') or meta.get('state') or 'unknown'
        key = (job, lane, outcome)
        by_job[key] = by_job.get(key, 0) + 1
        if meta.get('queue_ms') is not None:
            waits[lane if lane in waits else 'remote'].append(meta['queue_ms'] / 1000.0)
        seconds = execute_seconds(meta, result)
        if seconds is not None:
            executes.setdefault((job, lane), []).append(seconds)
        if meta.get('reason', '').startswith('fallback:'):
            cause = meta['reason'].split(':', 1)[1]
            fallbacks[cause] = fallbacks.get(cause, 0) + 1
        if outcome == 'oom':
            oom += 1
        if result.get('drifted'):
            drift['failed' if result.get('drift') == 'fail' else 'warned'] += 1
        attempts = result.get('attempts') or meta.get('attempts') or []
        if attempts:
            # One caller-visible run, several attempts; counted once, as a run
            # that was retried, and as recovered if its last attempt reached a
            # verdict. The cause is the first attempt's.
            retried['runs'] += 1
            if outcome in ('passed', 'command_failed'):
                retried['recovered'] += 1
            cause = attempts[0].get('cause') or 'unknown'
            retried['causes'][cause] = retried['causes'].get(cause, 0) + 1
        pair = result.get('flaky') or {}
        # A pair is recorded on its later attempt only, so counting results
        # counts pairs.
        if pair.get('order'):
            flaky['pairs'] += 1
        flaky['shard_pairs'] += len(pair.get('shards') or [])
    return {
        'window': window or ('all' if since is None else None),
        'since': since,
        'runs': len(runs),
        'by_job': [{'job': job, 'lane': lane, 'outcome': outcome, 'count': count}
                   for (job, lane, outcome), count in
                   sorted(by_job.items(), key=lambda item: (-item[1], item[0]))],
        'queue_wait_seconds': {lane: spread(values) for lane, values in waits.items()},
        'execute_seconds': [dict({'job': job, 'lane': lane}, **spread(values))
                            for (job, lane), values in
                            sorted(executes.items(), key=lambda item: -sum(item[1]))],
        'fallbacks': [{'reason': reason, 'count': count}
                      for reason, count in sorted(fallbacks.items(), key=lambda i: -i[1])],
        'oom': oom,
        'drift': drift,
        'retries': retried,
        'flaky': flaky,
        'pause': pause or {},
        'local': local or {},
        'overrides': overrides,
        'passthrough': passthrough_summary(
            [row for row in passthrough if row.get('reason') != 'override-ignored']),
        'worker': worker or {},
    }


def overrides_from(runs, passthrough):
    """`--local`/`--remote` by direction: asked, how many moved a run, how many hit nothing.

    `asked` counts every routed run that carried an override, `moved` the ones
    where it changed the lane -- `PANDORA_WHERE=remote pnpm check` on a remote job
    asks and moves nothing. `unclaimed` is an override on a command no job claims,
    which runs as if the shim were absent; a steady count there is an agent
    that believes Pandora owns a command it does not. A refused override (exit
    64) never becomes a run and is not counted here.
    """
    out = {where: {'asked': 0, 'moved': 0, 'unclaimed': 0} for where in ('local', 'remote')}
    for meta, _result in runs:
        record = meta.get('placement') or {}
        if record.get('override') in out:
            out[record['override']]['asked'] += 1
            if record.get('overridden'):
                out[record['override']]['moved'] += 1
    for row in passthrough:
        if row.get('override') in out:
            out[row['override']]['unclaimed'] += 1
    return out


def passthrough_summary(rows):
    """Unclaimed heavy-looking commands, grouped, worst total first.

    Grouped on the first two argv tokens because that is what a person recognises
    (`pnpm test:surface`, not the whole command line), and ordered by total
    duration because the question is "what is eating this Mac", not "what did I
    type most".
    """
    groups = {}
    for row in rows:
        key = ' '.join((row.get('argv') or [])[:2]) or '(unknown)'
        groups.setdefault(key, []).append(row)
    out = []
    for key, group in groups.items():
        durations = [row.get('duration_ms', 0) for row in group]
        out.append({'command': key, 'runs': len(group),
                    'p50_ms': percentile(durations, 0.5),
                    'p95_ms': percentile(durations, 0.95),
                    'total_seconds': round(sum(durations) / 1000.0, 1),
                    'why': ', '.join(sorted({row.get('reason') or row.get('kind') or ''
                                             for row in group}))})
    out.sort(key=lambda item: -item['total_seconds'])
    return out


# -- rendering ---------------------------------------------------------------

def render(report):
    """The text table. One screen for a quiet day, and no colour anywhere."""
    lines = ['window: %s, %d routed run(s)'
             % (report.get('window') or 'all', report['runs'])]
    if report['by_job']:
        lines.append('')
        lines.append('%-22s %-6s %-14s %6s' % ('job', 'lane', 'outcome', 'runs'))
        for row in report['by_job']:
            lines.append('%-22s %-6s %-14s %6d'
                         % (row['job'][:22], row['lane'][:6], row['outcome'][:14], row['count']))
    waits = report['queue_wait_seconds']
    if any(waits[lane]['n'] for lane in waits):
        lines.append('')
        lines.append('pre-accept wait (request to accepted; the engine admits, it does not queue)')
        for lane in sorted(waits):
            item = waits[lane]
            if item['n']:
                lines.append('  %-6s p50 %6.2fs  p95 %6.2fs  (%d)'
                             % (lane, item['p50'], item['p95'], item['n']))
    if report['execute_seconds']:
        lines.append('')
        lines.append('%-22s %-6s %8s %8s %6s' % ('execute', 'lane', 'p50 s', 'p95 s', 'runs'))
        for row in report['execute_seconds']:
            lines.append('%-22s %-6s %8.2f %8.2f %6d'
                         % (row['job'][:22], row['lane'][:6], row['p50'], row['p95'], row['n']))
    flags = []
    if report['fallbacks']:
        flags.append('fallbacks: ' + ', '.join('%s x%d' % (row['reason'], row['count'])
                                               for row in report['fallbacks']))
    if report['oom']:
        flags.append('oom kills: %d' % report['oom'])
    retried = report.get('retries') or {}
    if retried.get('runs'):
        flags.append('infra retries: %d run(s), %d recovered (%s)' % (
            retried['runs'], retried['recovered'],
            ', '.join('%s x%d' % item for item in sorted(retried['causes'].items()))))
    flaky = report.get('flaky') or {}
    if flaky.get('pairs') or flaky.get('shard_pairs'):
        flags.append('flaky: %d run pair(s), %d shard pair(s) failed and passed on one input'
                     % (flaky.get('pairs', 0), flaky.get('shard_pairs', 0)))
    if report['drift']['warned'] or report['drift']['failed']:
        flags.append('drift: %d warning(s), %d failure(s)'
                     % (report['drift']['warned'], report['drift']['failed']))
    overrides = report.get('overrides') or {}
    said = ['%s x%d (%d moved, %d on unclaimed)'
            % (where, row['asked'] + row['unclaimed'], row['moved'], row['unclaimed'])
            for where, row in sorted(overrides.items()) if row['asked'] or row['unclaimed']]
    if said:
        flags.append('overrides: ' + ', '.join(said))
    if flags:
        lines.append('')
        lines.extend(flags)
    pause = report.get('pause') or {}
    if pause:
        lines.append('pause gate: %s, %d episode(s), %gs paused, %d delayed, %d refused'
                     % ('PAUSED (%s)' % pause.get('evidence') if pause.get('paused')
                        else ('open' if pause.get('enabled') else 'disabled'),
                        pause.get('episodes', 0), pause.get('paused_seconds', 0),
                        pause.get('jobs_delayed', 0), pause.get('jobs_refused', 0)))
    lines.append('')
    lines.append('local, not routed: %d command(s), %.1fs in total'
                 % (sum(row['runs'] for row in report['passthrough']),
                    sum(row['total_seconds'] for row in report['passthrough'])))
    if report['passthrough']:
        lines.append('%-26s %5s %9s %9s %9s  %s'
                     % ('command', 'runs', 'p50 ms', 'p95 ms', 'total s', 'why'))
        for row in report['passthrough']:
            lines.append('%-26s %5d %9d %9d %9.1f  %s'
                         % (row['command'][:26], row['runs'], row['p50_ms'], row['p95_ms'],
                            row['total_seconds'], row['why'][:24]))
    lines.append('')
    lines.extend(render_worker(report.get('worker') or {}))
    return '\n'.join(lines)


def render_worker(worker):
    """What the one health call said. `unreachable` is a line, not a silence."""
    if not worker:
        return ['worker: not polled']
    if worker.get('error') or 'down' in (worker.get('state'), worker.get('worker')):
        return ['worker: unreachable (%s)' % (worker.get('error')
                                              or worker.get('reason') or 'no answer')]
    health = worker.get('health') or worker
    capacity = health.get('capacity') or {}
    scheduler = health.get('scheduler') or {}
    lines = ['worker: %s%s' % (worker.get('worker') or health.get('state') or '?',
                               '' if health.get('ok', True)
                               else ' -- ' + str(health.get('reason')))]
    if scheduler:
        lines.append('  scheduler: %s MiB held of %s, %s lane(s)'
                     % (scheduler.get('held_mib'), scheduler.get('budget_mib'),
                        scheduler.get('lanes')))
    lines.append('  disk: %s free%s (floor %s GiB)'
                 % ('%.2f GiB' % capacity['free_gib'] if 'free_gib' in capacity else 'unmeasured',
                    '' if capacity.get('ok', True) else ', BELOW FLOOR',
                    capacity.get('floor_gib', '?')))
    lines.append('  goldens: %s' % (', '.join(health.get('goldens') or []) or 'none'))
    canary = health.get('canary') or {}
    lines.append('  ready: %s%s; last canary %s'
                 % (health.get('state') or '?',
                    ' since ' + health['ready_since'] if health.get('ready_since') else '',
                    'pass' if canary.get('ok') else
                    ('FAIL (%s failure(s))' % canary.get('failures') if canary else 'never run')))
    if health.get('kernel_drift'):
        lines.append('  kernel drift: running %s, the canary passed on %s'
                     % (health.get('kernel'), health.get('canary_kernel')))
    for row in health.get('outcomes') or []:
        if row.get('outcome'):
            lines.append('  ledger: %-14s %-10s %d'
                         % (row['outcome'], row['state'], row['count']))
    return lines
