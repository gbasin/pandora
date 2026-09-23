"""Keep the pool from filling up, and say exactly what was removed.

Three sweeps, in the order of how much they are trusted:

1. **leaked run instances** -- a `run-*` instance whose attempt is not live.
   Every one of these is a supervisor that died between `clone` and `destroy`,
   so removing it is pure recovery and needs no policy.
2. **leaked storage volumes** -- a volume in the pool with no instance. The
   destroy receipt already checks this per run; this catches the case where
   the receipt itself never ran.
3. **old goldens** -- keep the `keep` most recently used per toolchain
   family, plus every golden a live attempt still needs, every golden a
   currently enrolled repository's `[worker]` table names (`protect`), and
   every pinned golden. This is the only sweep with a policy in it, and it is
   the only one `--dry-run` exists for.

A toolchain family is `(repo, source_id)`: the `[worker]` table's own name for
the tree it bakes in, which survives a node bump or a new package where the
fingerprint does not. Ranking per repository instead (issue #81) let
`--keep 1` delete eichler's only surfaces golden because its journeys golden
had been used more recently. A toolchain with no `source_id` is its own
family, so `keep` never prunes it against a different toolchain -- only
against nothing, which means it is kept. Goldens no recorded attempt explains
(built by a canary, or by hand) have neither a repository nor a `source_id`
and share one `(unknown)` bucket, as before; `protect` is what keeps an
enrolled repository's golden in there.

`protect` exists because the worker cannot know which goldens are *named*: the
repositories' `pandora.toml` files live on the client. Last use is a proxy for
"still wanted", and the proxy is wrong exactly when a toolchain is used rarely
-- the surfaces golden on 2026-09-23 -- so the client passes the fingerprints
its enrolled configurations name, and the sweep treats them as a floor that no
`keep` can go below.

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


def family_of(item):
    """(repo, toolchain family) for one index row, and the words for it."""
    repo = item['repo'] or '(unknown)'
    if item['repo'] is None:
        return (repo, ''), repo
    if item.get('source_id'):
        return (repo, 'source:' + item['source_id']), '%s %s' % (repo, item['source_id'])
    return (repo, 'fingerprint:' + item['fingerprint']), '%s %s' % (repo, item['name'])


def parse_protect(values):
    """`FINGERPRINT` or `FINGERPRINT=REPO` -> {fingerprint: repo or None}.

    A `golden-` prefix is tolerated, because that is how every listing prints
    the name a person would copy from.
    """
    out = {}
    for value in values or ():
        fingerprint, _, repo = str(value).partition('=')
        fingerprint = fingerprint.strip()
        if fingerprint.startswith('golden-'):
            fingerprint = fingerprint[len('golden-'):]
        if fingerprint:
            out[fingerprint] = repo.strip() or out.get(fingerprint)
    return out


def sweep(root, driver, *, keep=2, dry_run=False, protect=None):
    """`protect` is {fingerprint: repo-or-None}: goldens named by a config."""
    paths = Paths(root)
    protect = dict(protect or {})
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

    # 3. goldens, newest use first per toolchain family
    families = {}
    for item in golden_index.index(paths, driver):
        if not item['present']:
            continue
        key, label = family_of(item)
        families.setdefault(key, (label, []))[1].append(item)
    for key, (label, items) in sorted(families.items()):
        for rank, item in enumerate(items):
            reason = None
            if item['name'] in protected:
                reason = 'a live attempt needs it'
            elif item['fingerprint'] in protect:
                reason = 'named by %s pandora.toml' % (protect[item['fingerprint']]
                                                       or 'an enrolled')
            elif item.get('pinned'):
                # A pinned golden is one somebody resolved to digests on
                # purpose; its bytes cannot be rebuilt from the description
                # alone once a tag moves, so a last-use policy does not get to
                # decide it. Remove one by hand with `incus delete`.
                reason = 'pinned; gc never removes a pinned golden'
            elif rank < keep:
                reason = 'one of the %d most recently used for %s' % (keep, label)
            row = {'kind': 'golden', 'name': item['name'], 'repo': key[0],
                   'family': label, 'referenced_bytes': item['referenced_bytes']}
            if reason:
                kept.append(dict(row, why=reason))
                continue
            entry = dict(row, why='rank %d for %s, keep %d' % (rank + 1, label, keep))
            if dry_run:
                entry['removed'] = False
            else:
                entry.update(destroy(driver, item['name']))
            (removed if entry['removed'] or dry_run else failed).append(entry)

    if any(item['kind'] == 'golden' and item.get('removed') for item in removed):
        # Deleting a golden-sized subvolume makes the kernel mark qgroups
        # inconsistent rather than trace the tree; until a rescan, no clone's
        # quota is enforced. Pay for the rescan here, once, not on the next run.
        try:
            driver.settle_qgroups()
        except Exception as error:                                   # noqa: BLE001
            failed.append({'kind': 'qgroups', 'name': driver.pool, 'removed': False,
                           'why': 'rescan after golden removal: %s' % error})
    after = driver.pool_usage()
    receipt = {'ok': not failed, 'dry_run': bool(dry_run), 'keep': keep,
               'protect': {fp: repo for fp, repo in sorted(protect.items())},
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
