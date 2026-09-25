"""Keep the pool from filling up, and say exactly what was removed.

Three sweeps, in the order of how much they are trusted:

1. **leaked run instances** -- a `run-*` instance whose attempt is not live.
   Every one of these is a supervisor that died between `clone` and `destroy`,
   so removing it is pure recovery and needs no policy.
2. **leaked storage volumes** -- a volume in the pool with no instance. The
   destroy receipt already checks this per run; this catches the case where
   the receipt itself never ran.
3. **old goldens** -- keep the `keep` most recently used per toolchain
   family, plus every golden a live attempt still needs (queued ones too),
   every golden a currently enrolled repository's `[worker]` table names
   (`protect`), and every pinned golden. When the caller supplies its
   enrolled families (`--family`, or `--families-known` for an empty
   enrollment) and the repositories its enrollment covers (`--repos`), a
   family no enrolled config names is orphaned: a member whose own age is
   known and past `orphan_grace` (a day by default) is collectable, because
   nothing that could still want it names it (#116). The rule reaches only
   the caller's repositories -- one worker serves many Macs, and one Mac's
   enrollment is no evidence about another's -- plus the `(unknown)` family,
   which no repository owns. A member whose age cannot be read is kept. When
   no enrollment data reaches the sweep at all -- a bare `gc` typed on the
   worker itself -- no family is orphaned and `keep` alone ranks every
   family: the sweep cannot tell "nothing is enrolled" from "nobody could
   say", so it does not guess. `--drop-family` removes a family on sight,
   enrollment or not. This is the only sweep with a policy in it, and it is
   the only one `--dry-run` exists for.

A toolchain family is `(repo, source_id)`: the `[worker]` table's own name for
the tree it bakes in, which survives a node bump or a new package where the
fingerprint does not. Ranking per repository instead (issue #81) let
`--keep 1` delete eichler's only surfaces golden because its journeys golden
had been used more recently. A toolchain with no `source_id` is its own
family, so `keep` never prunes it against a different toolchain -- only
against nothing, which means it is kept while enrollment names it, and
orphaned only when enrollment data says nothing does. Goldens no recorded
attempt explains (built by a canary, or by hand) have neither a repository
nor a `source_id` and share one `(unknown)` bucket; no enrollment ever names
it, so with enrollment data it ages out like any orphan, clocked by the
instance creation time Incus reports.

`protect` and `families` exist because the worker cannot know which goldens
are *named*: the repositories' `pandora.toml` files live on the client. Last
use is a proxy for "still wanted", and the proxy is wrong exactly when a
toolchain is used rarely -- the surfaces golden on 2026-09-23 -- so the client
passes the fingerprints and `(repo, source_id)` families its enrolled
configurations name, and the sweep treats them as a floor that no `keep` or
grace can go below. A caller that cannot supply them -- a `gc` typed on the
worker itself ships neither `--family` nor `--families-known` -- leaves the
sweep with `keep` as its only evidence, and the sweep collects no orphans it
cannot prove.

The receipt is the point. A sweep that prints "cleaned up" and nothing else is
indistinguishable from a sweep that deleted a golden somebody was about to use.
"""
import calendar
from datetime import datetime, timezone
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


def family_key(text):
    """One `REPO=SOURCE_ID` wire value -> its family key, or ValueError.

    A malformed value is refused, never dropped: "--family oops" that parsed
    to nothing would read as "the caller names nothing", which is the
    authoritative claim (#116).
    """
    repo, _, source_id = str(text).partition('=')
    repo, source_id = repo.strip(), source_id.strip()
    if not repo or not source_id:
        raise ValueError('expected REPO=SOURCE_ID, not %r' % (text,))
    return repo, 'source:' + source_id


def parse_families(values):
    """`REPO=SOURCE_ID` values -> the family keys an enrolled config still names."""
    return {family_key(value) for value in values or ()}


def created_ts(text):
    """Epoch seconds of an `incus list -c D` value, or 0 when it cannot be read.

    Incus prints `2006/01/02 15:04 MST` in table and CSV output and RFC3339 in
    JSON. 0 means "no usable clock", and the sweep treats that as unknown age
    rather than as ancient -- the same don't-guess rule as an aborted listing.
    """
    text = (text or '').strip()
    if not text:
        return 0
    try:
        moment = datetime.fromisoformat(text.replace(' UTC', '+00:00')
                                      .replace(' GMT', '+00:00').replace('Z', '+00:00'))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    except ValueError:
        pass
    if text[-4:] in (' UTC', ' GMT'):
        text = text[:-4]
    for fmt in ('%Y/%m/%d %H:%M:%S', '%Y/%m/%d %H:%M'):
        try:
            return calendar.timegm(time.strptime(text, fmt))
        except ValueError:
            pass
    return 0


def sweep(root, driver, *, keep=2, dry_run=False, protect=None, enrolled=None,
          repos=None, drop=(), orphan_grace=86400):
    """`protect` is {fingerprint: repo-or-None}: goldens named by a config.

    `enrolled` is the set of family keys a config still names
    (`parse_families`), or None when no enrollment data reached the caller at
    all. Those are different claims: an explicitly empty set means "the
    client's configurations name nothing", and a member of a family no key
    names is collectable once its own age passes `orphan_grace` -- and kept
    when its age cannot be read. None means "nobody could say" -- a bare `gc`
    on the worker -- and then every family gets the `keep` ranking and
    nothing is orphaned. A family whose label is in `drop` is collectable on
    sight either way. A live attempt's golden and a pinned golden are never
    touched, drop or not.

    `repos` is the set of repository names the caller's enrollment covers, or
    None when the caller did not say. One worker serves several Macs, and a
    family in a repository this caller never enrolled is wanted -- the orphan
    rule does not reach it, exactly as if `enrolled` were None for that
    family. The `(unknown)` family belongs to no repository, so any
    enrollment data covers it; `repos=None` covers everything.
    """
    paths = Paths(root)
    protect = dict(protect or {})
    enrolled = None if enrolled is None else set(enrolled)
    repos = None if repos is None else set(repos)
    drop = set(drop or ())
    started = time.monotonic()
    live = live_run_instances(paths)
    protected = golden_index.live_goldens(paths)
    removed, kept, failed = [], [], []
    try:
        instances = driver.instances(check=True)
    except Exception as error:                                       # noqa: BLE001
        # Every sweep below deletes what this listing does *not* name. A
        # listing that failed names nothing, so without this every volume in
        # the pool would read as leaked (#88). Abort before anything is touched.
        return aborted(driver, started, keep, dry_run, protect, enrolled, repos,
                       drop, orphan_grace,
                       'incus list failed, so nothing was swept: %s' % error)

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

    # 2. leaked storage volumes. The volume list comes before the instance
    # re-list so the gap between "no instance of that name" and the delete is
    # the delete loop, not both listings; and each candidate is re-checked the
    # moment before its delete, which is what the gap is for (#88).
    try:
        rc, out, err = driver.incus('storage', 'volume', 'list', driver.pool,
                                    '--format', 'csv', check=False, timeout=180)
        if rc != 0:
            # A nonzero listing is unusable, exactly like an exception: skip
            # the sweep and say so, or the receipt reads ok while a leak sits.
            raise RuntimeError('incus storage volume list exited %d%s'
                               % (rc, ': ' + err.strip()[:160] if err.strip() else ''))
        names = {item['name'] for item in driver.instances(check=True)}
    except Exception as error:                                       # noqa: BLE001
        failed.append({'kind': 'listing', 'name': 'incus list', 'removed': False,
                       'why': 'volume sweep skipped: %s' % error})
        out = ''
    for line in out.splitlines():
        parts = line.split(',')
        if len(parts) < 2 or parts[0] != 'container' or parts[1] in names:
            continue
        entry = {'kind': 'volume', 'name': parts[1], 'why': 'no instance of that name'}
        if dry_run:
            entry['removed'] = False
            removed.append(entry)
            continue
        try:
            leaked = parts[1] not in {item['name']
                                      for item in driver.instances(check=True)}
        except Exception as error:                                  # noqa: BLE001
            entry['removed'] = False
            entry['why'] = 'kept: the instance re-check failed: %s' % str(error)[:160]
            kept.append(entry)
            continue
        if not leaked:
            entry['removed'] = False
            entry['why'] = 'kept: an instance of that name appeared mid-sweep'
            kept.append(entry)
            continue
        code, _, err = driver.incus('storage', 'volume', 'delete', driver.pool,
                                    'container/' + parts[1], check=False, timeout=300)
        entry['removed'] = code == 0
        if code != 0:
            entry['error'] = err.strip()[:200]
        (removed if entry['removed'] else failed).append(entry)

    # 3. goldens, newest use first per toolchain family
    families = {}
    for item in golden_index.index(paths, driver):
        if not item['present']:
            continue
        key, label = family_of(item)
        families.setdefault(key, (label, []))[1].append(item)
    dropped = set()
    now = time.time()
    for key, (label, items) in sorted(families.items()):
        # The orphan rule reaches only the repositories the caller's own
        # enrollment covers: one worker serves many Macs, and this Mac's
        # config is no evidence about another's. `(unknown)` belongs to no
        # repository, so any enrollment data covers it; a caller that sent no
        # `--repos` at all is treated as covering everything.
        covered = repos is None or key[0] == '(unknown)' or key[0] in repos
        wanted = (enrolled is None or not covered or key in enrolled
                  or any(item['fingerprint'] in protect for item in items))
        if label in drop:
            dropped.add(label)
        for rank, item in enumerate(items):
            reason = why = None
            if item['name'] in protected:
                reason = 'a live attempt needs it'
            elif item.get('pinned'):
                # A pinned golden is one somebody resolved to digests on
                # purpose; its bytes cannot be rebuilt from the description
                # alone once a tag moves, so a last-use policy does not get to
                # decide it. Remove one by hand with `incus delete`.
                reason = 'pinned; gc never removes a pinned golden'
            elif label in drop:
                why = 'family %s removed by --drop-family' % label
            elif item['fingerprint'] in protect:
                reason = 'named by %s pandora.toml' % (protect[item['fingerprint']]
                                                       or 'an enrolled')
            elif wanted:
                if rank < keep:
                    reason = 'one of the %d most recently used for %s' % (keep, label)
                else:
                    why = 'rank %d for %s, keep %d' % (rank + 1, label, keep)
            else:
                # An orphan is judged on its own clock: last use, or the
                # creation time Incus reports for a golden no attempt
                # explains. An age that cannot be read keeps the item -- a
                # sibling's old age must not drag it into the same delete.
                age = max(item['last_used'], created_ts(item['created']))
                if not age:
                    reason = ('family %s is named by no enrolled config, but its '
                              'age is unknown' % label)
                elif now - age > orphan_grace:
                    why = ('family %s is named by no enrolled config and was last '
                           'used %.1f h ago' % (label, (now - age) / 3600))
                else:
                    reason = ('family %s is named by no enrolled config, '
                              'collectable in %.1f h'
                              % (label, (age + orphan_grace - now) / 3600))
            row = {'kind': 'golden', 'name': item['name'], 'repo': key[0],
                   'family': label, 'referenced_bytes': item['referenced_bytes']}
            if reason:
                kept.append(dict(row, why=reason))
                continue
            entry = dict(row, why=why)
            if dry_run:
                entry['removed'] = False
            else:
                # `protected` is a snapshot from sweep start; a run submitted
                # since would lose its golden here. Re-ask immediately before
                # the delete, the same pattern as the volume re-check, and
                # keep the golden when the answer cannot be had.
                try:
                    claimed = item['name'] in golden_index.live_goldens(paths)
                except Exception as error:                        # noqa: BLE001
                    entry['removed'] = False
                    entry['why'] = ('kept: the live-attempt re-check failed: %s'
                                    % str(error)[:160])
                    kept.append(entry)
                    continue
                if claimed:
                    entry['removed'] = False
                    entry['why'] = 'kept: a live attempt claimed it mid-sweep'
                    kept.append(entry)
                    continue
                entry.update(destroy(driver, item['name']))
            (removed if entry['removed'] or dry_run else failed).append(entry)
    for label in sorted(drop - dropped):
        failed.append({'kind': 'family', 'name': label, 'removed': False,
                       'why': 'no such golden family'})

    if any(item['kind'] == 'golden' and item.get('removed') for item in removed):
        # Deleting a golden-sized subvolume makes the kernel mark qgroups
        # inconsistent rather than trace the tree; until a rescan, no clone's
        # quota is enforced. Ask for the rescan here, once, rather than leave it
        # to the next run. Best-effort: `incus delete` returns before btrfs has
        # freed the subvolume, so a rescan this soon can still count some of it.
        try:
            driver.settle_qgroups()
        except Exception as error:                                   # noqa: BLE001
            failed.append({'kind': 'qgroups', 'name': driver.pool, 'removed': False,
                           'why': 'rescan after golden removal: %s' % error})
    after = driver.pool_usage()
    receipt = {'ok': not failed, 'dry_run': bool(dry_run), 'keep': keep,
               'protect': {fp: repo for fp, repo in sorted(protect.items())},
               # null vs [] is the receipt's own record of the two modes:
               # null when no enrollment data arrived, [] when the caller
               # said its configurations name nothing.
               'enrolled': (sorted('%s=%s' % (repo, key.split(':', 1)[1])
                                   for repo, key in enrolled)
                            if enrolled is not None else None),
               'repos': sorted(repos) if repos is not None else None,
               'drop': sorted(drop), 'orphan_grace_hours': orphan_grace / 3600,
               'at': time.time(), 'seconds': round(time.monotonic() - started, 2),
               'removed': removed, 'kept': kept, 'failed': failed,
               'freed_bytes': sum(item.get('referenced_bytes', 0) for item in removed
                                  if item.get('removed')),
               'pool': after}
    return receipt


def aborted(driver, started, keep, dry_run, protect, enrolled, repos, drop,
            orphan_grace, reason):
    """The receipt of a sweep that removed nothing because it could not look."""
    try:
        pool = driver.pool_usage()
    except Exception as error:                                       # noqa: BLE001
        pool = {'ok': False, 'error': str(error)[:200]}
    return {'ok': False, 'reason': reason, 'dry_run': bool(dry_run), 'keep': keep,
            'protect': {fp: repo for fp, repo in sorted(protect.items())},
            'enrolled': (sorted('%s=%s' % (repo, key.split(':', 1)[1])
                                for repo, key in enrolled)
                         if enrolled is not None else None),
            'repos': sorted(repos) if repos is not None else None,
            'drop': sorted(drop), 'orphan_grace_hours': orphan_grace / 3600,
            'at': time.time(), 'seconds': round(time.monotonic() - started, 2),
            'removed': [], 'kept': [],
            'failed': [{'kind': 'listing', 'name': 'incus list', 'removed': False,
                        'why': reason}],
            'freed_bytes': 0, 'pool': pool}


def destroy(driver, name):
    """Delete one instance and check it left nothing, reusing the run receipt."""
    try:
        got = driver.destroy(Instance(name=name, run_id=name, golden=''))
        return {'removed': True, 'receipt_clean': got.clean,
                'seconds': round(got.seconds, 2)}
    except DestroyIncomplete as error:
        # Not removed: the receipt says something is still there, and counting
        # it as freed bytes would make the receipt claim space the pool never
        # got back. It lands in `failed`, where a person looks.
        return {'removed': False, 'receipt_clean': False, 'leftovers': error.receipt,
                'error': str(error)[:200]}
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
