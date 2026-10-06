"""What is baked into this worker, and which repository asked for it.

A golden's instance name carries its fingerprint and nothing else, so the
worker cannot tell from Incus alone which repository a golden belongs to or
when it was last used. Attempts explain each golden's repository and use history. Golden identity also
lives on the Incus instance, so retaining attempt directories cannot turn an
enrolled recipe's golden into an unexplained orphan. Old goldens are enriched
when next prepared or reused. The index joins those records rather than keeping
a second mutable inventory that can outlive the instances.
"""
import json
import math
import re
import sqlite3
from pathlib import Path

from ..engine.ledger import LIVE
from ..engine.runner import Paths, toolchain_of


def index(paths, driver):
    """[{fingerprint, name, repo, ...}] newest use first.

    Joins three sources: the instances Incus has, the attempt directories that
    say which toolchain produced which name, and the ledger rows that say which
    repository and when.
    """
    present = {item['name']: item for item in driver.instances()
               if item['name'].startswith('golden-')}
    sizes = driver.qgroups()
    seen = {}
    for run_id, row in attempts(paths).items():
        spec = row['toolchain']
        try:
            toolchain = toolchain_of(spec)
        except (KeyError, TypeError):
            continue
        name = 'golden-' + toolchain.fingerprint()
        entry = seen.setdefault(name, {'fingerprint': toolchain.fingerprint(), 'name': name,
                                       'recipe': recipe_of(spec),
                                       'repo': row['repo'], 'source_id': spec.get('source_id', ''),
                                       'pinned': bool(spec.get('pins')),
                                       'pins': dict(spec.get('pins') or {}),
                                       'base_image': spec.get('base_image', ''),
                                       'uses': 0, 'last_used': 0, 'last_run': ''})
        entry['uses'] += 1
        if row['at'] > entry['last_used']:
            entry['last_used'], entry['last_run'] = row['at'], run_id
            entry['repo'] = row['repo'] or entry['repo']
    rows = []
    for name, item in sorted(seen.items()):
        item = dict(item)
        item['present'] = name in present
        item['state'] = present.get(name, {}).get('state', 'absent')
        item['created'] = present.get(name, {}).get('created', '')
        referenced, exclusive = sizes.get('containers/%s_%s' % (driver.project, name), (0, 0))
        snap = sizes.get('containers-snapshots/%s_%s/warm' % (driver.project, name), (0, 0))
        item['referenced_bytes'] = referenced
        item['exclusive_bytes'] = exclusive + snap[1]
        rows.append(item)
    # A golden Incus has that no attempt explains is still real and still costs
    # disk, so it is listed with an unknown repository rather than hidden.
    read_metadata = getattr(driver, 'golden_metadata', None)
    for name, item in sorted(present.items()):
        if name in seen:
            continue
        referenced, exclusive = sizes.get('containers/%s_%s' % (driver.project, name), (0, 0))
        metadata = identity(name, read_metadata(name)) if callable(read_metadata) else {}
        rows.append({'fingerprint': name[len('golden-'):], 'name': name,
                     'recipe': metadata.get('recipe', ''), 'repo': None,
                     'source_id': metadata.get('source_id', ''),
                     'pinned': bool(metadata.get('pins')), 'pins': metadata.get('pins', {}),
                     'base_image': metadata.get('base_image', ''),
                     'uses': 0, 'last_used': metadata.get('last_used', 0),
                     'last_run': '', 'present': True,
                     'state': item['state'], 'created': item['created'],
                     'referenced_bytes': referenced, 'exclusive_bytes': exclusive})
    rows.sort(key=lambda item: (-item['last_used'], item['name']))
    return rows


def identity(name, value):
    """Validate persistent identity before it can affect destructive GC policy."""
    if value is None:
        return {}
    if (not isinstance(value, dict) or value.get('fingerprint') != name[len('golden-'):]
            or not isinstance(value.get('recipe'), str)
            or not re.fullmatch('[0-9a-f]{16}', value['recipe'])
            or not isinstance(value.get('pins'), dict)
            or not isinstance(value.get('source_id'), str)
            or not isinstance(value.get('base_image'), str)
            or isinstance(value.get('last_used'), bool)
            or not isinstance(value.get('last_used'), (int, float))
            or not math.isfinite(value['last_used']) or value['last_used'] < 0):
        raise ValueError('invalid golden identity on %s; refusing to guess its GC family' % name)
    return value


def recipe_of(spec):
    """The recipe's own fingerprint: the toolchain with its pins left out.

    What a client computes from a `[worker]` table, and what every golden
    pinned from that table has in common. For a toolchain resolved before
    pinning existed it is the golden's fingerprint itself.
    """
    return toolchain_of({key: value for key, value in spec.items()
                         if key != 'pins'}).fingerprint()


def toolchains(paths):
    """{run_id: (toolchain, mtime)} for every attempt directory with one."""
    found = {}
    for path in sorted(Path(paths.runs).glob('*/toolchain.json')):
        try:
            found[path.parent.name] = (json.loads(path.read_text()), path.stat().st_mtime)
        except (OSError, ValueError):
            continue
    return found


def ledger_rows(paths):
    """{run_id: ledger row} from a read-only connection, {} without a ledger."""
    rows = {}
    if Path(paths.ledger).is_file():
        connection = sqlite3.connect('file:%s?mode=ro' % paths.ledger, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            for row in connection.execute(
                    'select run_id, repo, job, state, created, instance from attempts'):
                rows[row['run_id']] = dict(row)
        except sqlite3.Error:
            pass
        connection.close()
    return rows


def attempts(paths):
    """{run_id: {repo, job, at, state, toolchain}} for every attempt on disk.

    The toolchain files are read before the ledger, deliberately. A submit
    claims the ledger row before it writes `toolchain.json`, so every
    toolchain read here has its row in the later ledger read, and a live
    attempt is never mistaken for one with no state. A submit that lands
    after the toolchain read is not seen at all, like one after the sweep,
    and gc's pre-delete re-check reads again.
    """
    files = toolchains(paths)
    ledger = ledger_rows(paths)
    rows = {}
    for run_id, (spec, mtime) in files.items():
        row = ledger.get(run_id, {})
        rows[run_id] = {'repo': row.get('repo'), 'job': row.get('job'),
                        'state': row.get('state'), 'instance': row.get('instance'),
                        'at': row.get('created') or mtime,
                        'toolchain': spec}
    return rows


def live_state(paths):
    """(golden names, recipes) the live attempts hold, from one read of each.

    The ledger's own `LIVE`, `queued` included: a queued attempt has not cloned
    yet, which is exactly when removing its golden hurts. A live attempt whose
    `toolchain.json` has `pins` holds its own golden; one without holds every
    golden of its recipe, because its supervisor has not named one yet.

    One pass, deliberately. Two walks, one for names and one for recipes,
    could read an attempt unpinned in the first and pinned in the second, and
    then hold neither its recipe nor the golden it just named.
    """
    names, recipes = set(), set()
    for run_id, row in attempts(paths).items():
        if row['state'] not in LIVE:
            continue
        spec = row['toolchain']
        try:
            names.add('golden-' + toolchain_of(spec).fingerprint())
            if 'pins' not in spec:
                recipes.add(recipe_of(spec))
        except (KeyError, TypeError, AttributeError):
            continue
    return names, recipes


def live_goldens(paths):
    """Golden names a live attempt is still using, which GC may never remove."""
    return live_state(paths)[0]


def newest_pinned(paths, recipe_spec, exists):
    """The newest attempt's resolved toolchain for this recipe whose golden
    still exists, or None. What the canary proves when it has no source to
    pin against: the golden routed runs used last."""
    recipe = recipe_of(recipe_spec)
    found = sorted(attempts(paths).values(), key=lambda row: -float(row['at'] or 0))
    for row in found:
        spec = row['toolchain']
        if not isinstance(spec, dict) or 'pins' not in spec:
            continue
        try:
            if recipe_of(spec) != recipe:
                continue
            name = 'golden-' + toolchain_of(spec).fingerprint()
        except (KeyError, TypeError):
            continue
        if exists(name):
            return dict(recipe_spec, pins=dict(spec['pins']))
    return None


def live_recipes(paths):
    """Recipes a live attempt has not resolved to a golden name yet."""
    return live_state(paths)[1]


def listing(root, driver):
    paths = Paths(root)
    return {'ok': True, 'project': driver.project, 'pool': driver.pool,
            'goldens': index(paths, driver)}
