"""Cutting one job into N, and proving afterward that the cut was honest.

Nothing here talks to Incus, to SQLite or to a socket, because every decision
worth arguing about is arithmetic on lists: how many shards to cut, what each
shard's command line is, and whether the test ids the shards *observed* are
exactly the ones the plan said they would.

The two tiers of the direction note live here as one code path with one branch:

  tier 1  a shard index reaches the job (an argv flag, named variables, or the
          `PANDORA_SHARD_*` pair) and nothing comes back. `verify` is not run
          and the result says `unverified`, which is the honest word: a green
          `--shard=2/4` proves that something ran, never that four shards
          between them covered the suite.
  tier 2  a `plan` command emits an inventory, each shard emits a report, and
          `verify` holds them against each other. A shard that ran the wrong
          tests, a shard whose report never arrived, and a suite that lost a
          test between planning and running are three different failures and
          all three are named.

The JSON contract with the repository is deliberately small, because a
repository Pandora has never seen has to be able to meet it:

  plan    {"inventory": [{"shard": 1, "testIds": [...]}, ...]}   1-based, in order
  report  {"observed": [{"id": ...}, ...]}                        ids, or bare strings

Anything else in either file is the repository's business and is carried
through untouched.
"""
import json
from pathlib import Path

# Handed to every sharded run whatever the strategy, so a runner that likes
# neither the argv template nor the named variables still has the pair.
INDEX_VAR = 'PANDORA_SHARD_INDEX'
TOTAL_VAR = 'PANDORA_SHARD_TOTAL'
# Where a tier-2 plan's inventory lands inside the instance, worktree-relative.
PLAN_PATH = '.pandora/plan.json'


def render(text, *, index=None, total=None, plan=None):
    """Substitute the shard tokens. Unknown braces were refused at load time."""
    out = text
    if index is not None:
        out = out.replace('{i}', str(index))
    if total is not None:
        out = out.replace('{n}', str(total))
    if plan is not None:
        out = out.replace('{plan}', plan)
    return out


def requested(env, config):
    """How many shards the caller asked for.

    `--shards` in the command line is the *repository's* flag and Pandora does
    not parse the repository's command line, so the override is an environment
    variable the shim sets. An unreadable value is ignored rather than refused:
    the configured default is always a safe answer, and failing a run over a
    typo in an optimization hint would be worse than running it.
    """
    raw = (env or {}).get('PANDORA_SHARDS', '')
    if isinstance(raw, str) and raw.strip().isdigit() and int(raw) >= 1:
        return int(raw)
    return config['default']


def count(config, *, want=None, free_slots=None, inventory=None):
    """The shard count for one fan-out, and why it is that number.

    Three clamps, in order, each of which can only lower the number:

      `max`        the repository's own ceiling. It owns its suite's shape.
      free slots   what the worker can admit *now*. For the POC this is the
                   whole concurrency story: every shard of a fan-out is
                   admitted at dispatch, so a box with two free lanes cuts two
                   shards rather than cutting four and queuing two. It makes
                   the partition depend on momentary load, which is a real
                   cost, and it is what keeps a fan-out from dead-locking
                   against its own siblings.
      inventory    tier 2 only, and only after planning: a selection Playwright
                   puts entirely in shard 1 is a one-shard job, and running the
                   other three to observe nothing is pure cost.

    Returns (n, reasons) where reasons names every clamp that bit.
    """
    want = config['default'] if want is None else want
    n, reasons = max(1, int(want)), []
    if n > config['max']:
        n, _ = config['max'], reasons.append('max=%d' % config['max'])
    if free_slots is not None and n > max(1, free_slots):
        reasons.append('free-slots=%d' % free_slots)
        n = max(1, free_slots)
    if inventory is not None:
        filled = sum(1 for shard in inventory if shard)
        if 1 <= filled < n:
            reasons.append('inventory-fills=%d' % filled)
            n = filled
    return n, reasons


def child_argv(argv, config, *, index, total, plan_path=None):
    """One shard's command line: the job's own, plus what says which shard."""
    out = list(argv)
    if config['strategy'] == 'argv':
        out.append(render(config['template'], index=index, total=total))
    if plan_path and config['expect_flag']:
        out += [config['expect_flag'], plan_path]
    return out


def child_env(env, config, *, index, total):
    out = dict(env)
    for key, value in config['env'].items():
        out[key] = render(value, index=index, total=total)
    out[INDEX_VAR] = str(index)
    out[TOTAL_VAR] = str(total)
    return out


def plan_argv(config, args, *, total, plan_path=PLAN_PATH):
    """The build-once command, with the forwarded arguments spliced back in."""
    argv = list(config['plan'])
    if config['plan_args_at'] is not None:
        argv[config['plan_args_at']:config['plan_args_at'] + 1] = list(args)
    return [render(item, total=total, plan=plan_path) for item in argv]


def report_path(config, *, index, total):
    return render(config['report'], index=index, total=total)


# --- reading what the repository wrote --------------------------------------

def ids(rows):
    """Test ids from an inventory or an observation, in the order written."""
    out = []
    for row in rows or []:
        if isinstance(row, str):
            out.append(row)
        elif isinstance(row, dict) and isinstance(row.get('id'), str):
            out.append(row['id'])
        elif isinstance(row, dict) and isinstance(row.get('testId'), str):
            out.append(row['testId'])
    return out


def inventory(document):
    """[[ids of shard 1], [ids of shard 2], ...] from a plan document."""
    rows = (document or {}).get('inventory')
    if not isinstance(rows, list) or not rows:
        raise ValueError('the plan has no inventory')
    ordered = []
    for position, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError('inventory entry %d is not an object' % position)
        if row.get('shard') not in (None, position):
            raise ValueError('inventory entry %d calls itself shard %r' % (position, row['shard']))
        ordered.append(ids(row.get('testIds') or row.get('tests')))
    return ordered


def observed(document):
    rows = (document or {}).get('observed')
    if rows is None:
        raise ValueError('the report has no observed list')
    return ids(rows)


def read(path):
    return json.loads(Path(path).read_text())


# --- the receipt ------------------------------------------------------------

def verify(planned, reports):
    """Did the shards, between them, run exactly the planned partition?

    `planned` is the inventory; `reports` maps a 1-based shard index to that
    shard's observed ids, with a missing key meaning no report arrived. Four
    things can be wrong and each is reported separately, because "shard 3 never
    filed a report" and "shard 3 ran somebody else's tests" want different
    answers from a human.

    A missing report is never treated as an empty shard. That is the whole
    reason this function exists: an aggregator that scores a silent shard as
    zero failures is an aggregator that fabricates a pass.
    """
    result = {'verified': False, 'shards': len(planned),
              'planned_tests': sum(len(rows) for rows in planned),
              'observed_tests': 0, 'missing_reports': [], 'per_shard': [],
              'missing': [], 'unexpected': [], 'duplicated': []}
    seen = []
    for index, want in enumerate(planned, 1):
        got = reports.get(index)
        if got is None:
            # A shard the plan gave nothing to is never dispatched, so it files
            # no report and owes none. Every other silence is a missing report.
            empty = not want
            if not empty:
                result['missing_reports'].append(index)
            result['per_shard'].append({'shard': index, 'planned': len(want),
                                        'observed': 0 if empty else None,
                                        'matches': empty,
                                        'dispatched': False})
            continue
        seen.extend(got)
        result['observed_tests'] += len(got)
        matches = sorted(got) == sorted(want)
        result['per_shard'].append({'shard': index, 'planned': len(want),
                                    'observed': len(got), 'matches': matches,
                                    'dispatched': True})
        if not matches:
            result['missing'] += sorted(set(want) - set(got))
            result['unexpected'] += sorted(set(got) - set(want))
    whole = [test for rows in planned for test in rows]
    counts = {}
    for test in seen:
        counts[test] = counts.get(test, 0) + 1
    result['duplicated'] = sorted(test for test, n in counts.items() if n > 1)
    result['missing'] = sorted(set(result['missing']) | (set(whole) - set(seen)))
    result['unexpected'] = sorted(set(result['unexpected']) | (set(seen) - set(whole)))
    result['verified'] = (not result['missing_reports'] and not result['missing']
                          and not result['unexpected'] and not result['duplicated']
                          and all(item['matches'] for item in result['per_shard']))
    result['reason'] = _why(result)
    return result


def _why(result):
    if result['verified']:
        return 'observed test ids are exactly the planned partition'
    if result['missing_reports']:
        return 'no report from shard %s' % ', '.join(str(i) for i in result['missing_reports'])
    if result['duplicated']:
        return '%d test(s) ran in more than one shard' % len(result['duplicated'])
    if result['missing']:
        return '%d planned test(s) were never observed' % len(result['missing'])
    if result['unexpected']:
        return '%d observed test(s) were not in the plan' % len(result['unexpected'])
    return 'a shard observed its tests in an order the plan did not describe'


# --- artifacts --------------------------------------------------------------

def collisions(trees):
    """Paths more than one shard wrote with different bytes.

    `trees` maps a shard index to {relative path: sha256}. Identical bytes at
    one path are not a collision -- two shards writing the same build manifest
    is expected -- but differing bytes are, because whichever is merged last
    silently replaces evidence the other shard produced.
    """
    byte_paths = {}
    for index in sorted(trees):
        for path, digest in trees[index].items():
            byte_paths.setdefault(path, {})[index] = digest
    found = []
    for path in sorted(byte_paths):
        digests = byte_paths[path]
        if len(digests) > 1 and len(set(digests.values())) > 1:
            found.append({'path': path, 'shards': sorted(digests)})
    return found
