"""Keep the pool from filling up, and say exactly what was removed.

Three sweeps, in the order of how much they are trusted:

1. **leaked run instances** -- a `run-*` instance whose attempt is not live.
   Every one of these is a supervisor that died between `clone` and `destroy`,
   so removing it is pure recovery and needs no policy.
2. **leaked storage volumes** -- a volume in the pool with no instance. The
   destroy receipt already checks this per run; this catches the case where
   the receipt itself never ran.
3. **old goldens** -- keep the `keep` most recently used per repository, plus
   every golden a live attempt still needs. This is the only sweep with a
   policy in it, and it is the only one `--dry-run` exists for.

The receipt is the point. A sweep that prints "cleaned up" and nothing else is
indistinguishable from a sweep that deleted a golden somebody was about to use.
"""
import json
import time
from pathlib import Path

from ..engine.ledger import LIVE, Ledger
from ..engine.runner import Paths
from ..executor.interface import DestroyIncomplete, Instance
from . import goldens as golden_index


def live_run_instances(paths):
    """{instance_name: run_id} for attempts the ledger still calls live."""
    live = {}
    if not Path(paths.ledger).is_file():
        return live
    ledger = Ledger(paths.ledger)
    try:
        for row in ledger.live():
            if row['state'] in LIVE:
                live[row['instance'] or ('run-' + row['run_id'])] = row['run_id']
    finally:
        ledger.close()
    return live


def sweep(root, driver, *, keep=2, dry_run=False):
    paths = Paths(root)
    started = time.monotonic()
    live = live_run_instances(paths)
    protected = golden_index.live_goldens(paths)
    instances = driver.instances()
    removed, kept, failed = [], [], []

    # 1. leaked run instances
    for item in instances:
        name = item['name']
        if not name.startswith('run-') or name in live:
            continue
        entry = {'kind': 'run-instance', 'name': name, 'state': item['state'],
                 'why': 'no live attempt owns it'}
        if dry_run:
            entry['removed'] = False
            removed.append(entry)
            continue
        entry.update(destroy(driver, name))
        (removed if entry['removed'] else failed).append(entry)

    # 2. leaked storage volumes
    names = {item['name'] for item in driver.instances()}
    rc, out, _ = driver.incus('storage', 'volume', 'list', driver.pool,
                              '--format', 'csv', check=False, timeout=180)
    for line in out.splitlines() if rc == 0 else []:
        parts = line.split(',')
        if len(parts) < 2 or parts[0] != 'container' or parts[1] in names:
            continue
        entry = {'kind': 'volume', 'name': parts[1], 'why': 'no instance of that name'}
        if dry_run:
            entry['removed'] = False
        else:
            code, _, err = driver.incus('storage', 'volume', 'delete', driver.pool,
                                        'container/' + parts[1], check=False, timeout=300)
            entry['removed'] = code == 0
            if code != 0:
                entry['error'] = err.strip()[:200]
        (removed if entry['removed'] or dry_run else failed).append(entry)

    # 3. goldens, newest use first per repository
    by_repo = {}
    for item in golden_index.index(paths, driver):
        if not item['present']:
            continue
        by_repo.setdefault(item['repo'] or '(unknown)', []).append(item)
    for repo, items in sorted(by_repo.items()):
        for rank, item in enumerate(items):
            reason = None
            if item['name'] in protected:
                reason = 'a live attempt needs it'
            elif rank < keep:
                reason = 'one of the %d most recently used for %s' % (keep, repo)
            if reason:
                kept.append({'kind': 'golden', 'name': item['name'], 'repo': repo,
                             'referenced_bytes': item['referenced_bytes'], 'why': reason})
                continue
            entry = {'kind': 'golden', 'name': item['name'], 'repo': repo,
                     'referenced_bytes': item['referenced_bytes'],
                     'why': 'rank %d for %s, keep %d' % (rank + 1, repo, keep)}
            if dry_run:
                entry['removed'] = False
            else:
                entry.update(destroy(driver, item['name']))
            (removed if entry['removed'] or dry_run else failed).append(entry)

    after = driver.pool_usage()
    receipt = {'ok': not failed, 'dry_run': bool(dry_run), 'keep': keep,
               'at': time.time(), 'seconds': round(time.monotonic() - started, 2),
               'removed': removed, 'kept': kept, 'failed': failed,
               'freed_bytes': sum(item.get('referenced_bytes', 0) for item in removed
                                  if item.get('removed')),
               'pool': after}
    return receipt


def destroy(driver, name):
    """Delete one instance and check it left nothing, reusing the run receipt."""
    try:
        got = driver.destroy(Instance(name=name, run_id=name, golden=''))
        return {'removed': True, 'receipt_clean': got.clean,
                'seconds': round(got.seconds, 2)}
    except DestroyIncomplete as error:
        return {'removed': True, 'receipt_clean': False, 'leftovers': error.receipt}
    except Exception as error:                                       # noqa: BLE001
        return {'removed': False, 'error': str(error)[:200]}


def write_receipt(root_worker, receipt):
    """Put the receipt where `worker status` and a person can both find it."""
    directory = Path(root_worker) / 'receipts'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ('gc-%s.json' % time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))
    path.write_text(json.dumps(receipt, indent=1, sort_keys=True) + '\n')
    receipt['receipt'] = str(path)
    return receipt
