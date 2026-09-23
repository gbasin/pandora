"""What is baked into this worker, and which repository asked for it.

A golden's instance name carries its fingerprint and nothing else, so the
worker cannot tell from Incus alone which repository a golden belongs to or
when it was last used. Both facts are in the engine's own records -- every
attempt writes the toolchain it asked for beside its row -- so the index is
*derived* rather than maintained: a file that has to be kept in step with the
truth eventually is not.
"""
import json
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
    for name, item in sorted(present.items()):
        if name in seen:
            continue
        referenced, exclusive = sizes.get('containers/%s_%s' % (driver.project, name), (0, 0))
        rows.append({'fingerprint': name[len('golden-'):], 'name': name, 'repo': None,
                     'source_id': '', 'pinned': False, 'pins': {}, 'base_image': '',
                     'uses': 0, 'last_used': 0, 'last_run': '', 'present': True,
                     'state': item['state'], 'created': item['created'],
                     'referenced_bytes': referenced, 'exclusive_bytes': exclusive})
    rows.sort(key=lambda item: (-item['last_used'], item['name']))
    return rows


def attempts(paths):
    """{run_id: {repo, job, at, state, toolchain}} for every attempt on disk."""
    rows = {}
    ledger = {}
    if Path(paths.ledger).is_file():
        connection = sqlite3.connect('file:%s?mode=ro' % paths.ledger, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            for row in connection.execute(
                    'select run_id, repo, job, state, created, instance from attempts'):
                ledger[row['run_id']] = dict(row)
        except sqlite3.Error:
            pass
        connection.close()
    for path in sorted(Path(paths.runs).glob('*/toolchain.json')):
        run_id = path.parent.name
        try:
            spec = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        row = ledger.get(run_id, {})
        rows[run_id] = {'repo': row.get('repo'), 'job': row.get('job'),
                        'state': row.get('state'), 'instance': row.get('instance'),
                        'at': row.get('created') or path.stat().st_mtime,
                        'toolchain': spec}
    return rows


def live_goldens(paths):
    """Golden names a live attempt is still using, which GC may never remove.

    The ledger's own `LIVE`, `queued` included: a queued attempt has not cloned
    yet, which is exactly when removing its golden hurts.
    """
    names = set()
    for run_id, row in attempts(paths).items():
        if row['state'] not in LIVE:
            continue
        try:
            names.add('golden-' + toolchain_of(row['toolchain']).fingerprint())
        except (KeyError, TypeError):
            continue
    return names


def listing(root, driver):
    paths = Paths(root)
    return {'ok': True, 'project': driver.project, 'pool': driver.pool,
            'goldens': index(paths, driver)}
