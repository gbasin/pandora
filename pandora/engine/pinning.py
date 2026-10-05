"""Name a golden by what it is built from, not only by how it is described.

The `[worker]` table is a recipe: `images:ubuntu/26.04`, a package list, a node
version, an install command. The alias resolves to a different image over time
and the install reads whatever lockfile the source carries, so two goldens
built weeks apart from one recipe are different machines. A fingerprint over
the recipe alone names neither of them.

So the worker resolves two inputs before it names the golden, and folds both
into `Toolchain.pins`, and therefore into `golden-<fingerprint>`:

    base_image          the Incus image fingerprint the alias points at
    lockfile:<name>     sha256 hex of each lockfile at the source's root

Apt package versions stay unpinned: the image pin fixes the archive snapshot
the golden starts from, not what `apt-get install` fetches on top of it.
Service images (`service_images`) stay unpinned too: they are pulled inside
the instance after launch, and `pandora worker pins` reports their digests
without folding them in.

Resolution happens on the worker, because only the worker sees the image
server, and it happens once per attempt, when the supervisor first reads the
attempt's `toolchain.json`. The resolved table is written back to that file
with a `pins` key, so every later reader -- the verdict, `worker goldens`, gc's
live-attempt guard, a fan-out's children -- reads the same name. A `pins` key
already present means "resolved"; an empty one means "resolved to nothing",
which fingerprints exactly as the recipe did before pinning existed.

The alias lookup is cached under `<engine_root>/pins/images.json` for
`IMAGE_TTL` seconds. Upstream publishes a new image about daily, and a new
image means a cold golden build, so the cache is what bounds that churn to one
rebuild per recipe per week. The cache is keyed by alias and shared by every
recipe that names that alias, so only the ordinary expired-entry lookup moves
it. A golden that has to be built anyway takes the newest image (`settle`),
but that answer is used for that attempt only: writing it back would move the
alias under every other recipe and cold-build each of them. Read-only callers
(`golden`, `pandora worker pins`) never write the cache. A failed lookup is
remembered for `FAILED_TTL` seconds, so an image-server outage costs one
lookup timeout per alias per window rather than one per run. Pure standard
library.
"""
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

LOCKFILES = ('pnpm-lock.yaml', 'package-lock.json', 'yarn.lock', 'bun.lock', 'bun.lockb',
             'npm-shrinkwrap.json', 'Cargo.lock', 'poetry.lock', 'uv.lock', 'Pipfile.lock',
             'go.sum', 'Gemfile.lock', 'composer.lock')
# Root-level patterns, for the files a project names by convention rather
# than by one fixed name.
LOCKFILE_PATTERNS = ('requirements*.txt',)
IMAGE_TTL = 7 * 86400
# How long a failed alias lookup stands before the next one is tried.
FAILED_TTL = 5 * 60
# Distinct aliases the cache holds; the least recently answered is dropped.
MAX_ALIASES = 32
LOCKFILE_PREFIX = 'lockfile:'
CHUNK = 1 << 20
# `share` modes for the image cache: write every answer, write only when the
# shared entry is missing or past `IMAGE_TTL`, or never write.
SHARE, SHARE_EXPIRED, SHARE_NEVER = 'always', 'expired', 'never'


class PinFailed(RuntimeError):
    """A pin could not be resolved."""


def lockfiles(source, notes=None):
    """{name: sha256 hex} of every lockfile at the tree's root.

    Root only, deliberately: a workspace has one lockfile that governs the
    install, and walking the tree would hash a fixture's lockfile and mint a
    golden for a change that installs nothing. A lockfile that is a symlink
    resolving outside the tree is refused, and said so in `notes`: the name
    must describe the source, not whatever the link reaches on the host.
    Hashed in chunks, so a large lockfile is never read whole.
    """
    found = {}
    if not source:
        return found
    root = Path(source)
    if not root.is_dir():
        return found
    inside = root.resolve()
    names = set(LOCKFILES)
    for pattern in LOCKFILE_PATTERNS:
        names.update(path.name for path in root.glob(pattern))
    for name in sorted(names):
        path = root / name
        if not path.is_file():
            continue
        real = path.resolve()
        if real != inside and inside not in real.parents:
            if notes is not None:
                notes.append('lockfile %s refused: it resolves outside the source tree'
                             % name)
            continue
        digest = hashlib.sha256()
        with open(real, 'rb') as handle:
            for chunk in iter(lambda: handle.read(CHUNK), b''):
                digest.update(chunk)
        found[name] = digest.hexdigest()
    return found


def cache_file(root):
    return Path(root) / 'pins' / 'images.json'


def read_cache(root):
    try:
        data = json.loads(cache_file(root).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def entry_age(entry):
    """When an entry last learned anything, answer or failure."""
    try:
        return max(float(entry.get('at') or 0), float(entry.get('failed_at') or 0))
    except (TypeError, ValueError, AttributeError):
        return 0.0


def write_entry(root, alias, entry):
    """Store one alias's entry, dropping the oldest past `MAX_ALIASES`.
    Atomic, so a reader never sees half a file."""
    path = cache_file(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = read_cache(root)
        data[alias] = entry
        while len(data) > MAX_ALIASES:
            del data[min((key for key in data if key != alias),
                         key=lambda key: entry_age(data[key]))]
        handle, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.images.')
        with os.fdopen(handle, 'w') as out:
            json.dump(data, out, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass                    # a cache that cannot be written only costs a lookup


def remember(root, alias, fingerprint, now):
    """Record one alias resolution."""
    write_entry(root, alias, {'fingerprint': fingerprint, 'at': now})


def image(root, alias, lookup, *, fresh=False, now=None, ttl=IMAGE_TTL, share=SHARE):
    """(fingerprint or None, how) for one base-image alias.

    `how` is `cached`, `resolved`, `stale: <why>` (the lookup failed and an
    older answer was used) or `unresolved: <why>` (no answer at all).

    `share` says whether an answer goes back into the shared cache: always,
    only when the entry there is missing or expired (`settle`'s refresh), or
    never (read-only callers). A failure is recorded unless `share` is never,
    because recording it does not move the alias.
    """
    now = time.time() if now is None else now
    entry = read_cache(root).get(alias)
    entry = entry if isinstance(entry, dict) else {}
    known = entry.get('fingerprint') or None
    try:
        at = float(entry.get('at') or 0)
        failed_at = float(entry.get('failed_at') or 0)
    except (TypeError, ValueError):
        at = failed_at = 0.0
    if known and not fresh and now - at < ttl:
        return known, 'cached'
    if not isinstance(alias, str) or not alias or alias.startswith('-'):
        return None, 'unresolved: %r is not an image alias' % (alias,)
    if failed_at and now - failed_at < FAILED_TTL:
        why = 'the lookup failed %ds ago: %s' % (now - failed_at, entry.get('failed') or '?')
        return (known, 'stale: ' + why) if known else (None, 'unresolved: ' + why)
    try:
        if lookup is None:
            raise PinFailed('this driver cannot resolve images')
        found = lookup(alias)
        if not found:
            raise PinFailed('no fingerprint for %s' % alias)
    except Exception as error:                      # noqa: BLE001 - never fatal to a run
        why = str(error)[:200] or type(error).__name__
        if share != SHARE_NEVER and lookup is not None:
            write_entry(root, alias, dict(entry, failed_at=now, failed=why))
        if known:
            return known, 'stale: %s' % why
        return None, 'unresolved: %s' % why
    if share == SHARE or (share == SHARE_EXPIRED and (not known or now - at >= ttl)):
        remember(root, alias, found, now)
    return found, 'resolved'


def resolve(spec, root, lookup, *, source=None, digests=None, fresh=False, now=None,
            share=SHARE):
    """(pins, notes, how) for one recipe; `how` is the image answer's kind.

    `digests` stands in for `source` when the caller already hashed the
    lockfiles (the selftest, from the Mac)."""
    pins, notes = {}, []
    found, how = image(root, spec['base_image'], lookup, fresh=fresh, now=now, share=share)
    notes.append('image %s: %s' % (spec['base_image'], how))
    if found:
        pins['base_image'] = found
    try:
        hashed = dict(digests) if digests is not None else lockfiles(source, notes)
    except OSError as error:
        hashed = {}
        notes.append('lockfiles unreadable: %s' % error)
    for name, digest in sorted(hashed.items()):
        pins[LOCKFILE_PREFIX + name] = digest
    return pins, notes, how


def settle(spec, root, driver, name_of, *, source=None, digests=None, now=None,
           write=True):
    """The recipe with `pins` resolved, plus `pin_notes`. Idempotent.

    A spec that already has `pins` is returned unchanged. When the image came
    from the cache and the golden it names is not warm, the build is cold
    anyway, so the alias is looked up again and the build takes the newest
    image rather than one up to `IMAGE_TTL` old. That fresh answer names this
    attempt's golden only: the shared cache entry is left alone unless it has
    expired, because other recipes name the same alias and moving it would
    cold-build every one of them. `write=False` never writes the cache.
    """
    if 'pins' in spec:
        return dict(spec)
    lookup = getattr(driver, 'image_fingerprint', None)
    share = SHARE if write else SHARE_NEVER
    pins, notes, how = resolve(spec, root, lookup, source=source, digests=digests, now=now,
                               share=share)
    # `warm` is what `prepare` reuses; a golden that exists without its warm
    # snapshot is deleted and rebuilt, so it counts as unbuilt here.
    probe = getattr(driver, 'warm', None)
    if not callable(probe):
        probe = getattr(driver, 'exists', None)
    if how == 'cached' and callable(probe) and not probe(name_of(dict(spec, pins=pins))):
        pins, notes, how = resolve(spec, root, lookup, source=source, digests=digests,
                                   fresh=True, now=now,
                                   share=SHARE_EXPIRED if write else SHARE_NEVER)
        notes.append('the cached image named an unbuilt golden, so the alias was '
                     'looked up again for this attempt only')
    return dict(spec, pins=pins, pin_notes=notes)


def golden_pins(spec):
    """`{image, lockfiles}` for the verdict payload, or None for a toolchain
    that was never resolved (an attempt written before pinning)."""
    if not isinstance(spec, dict) or 'pins' not in spec:
        return None
    pins = spec.get('pins') or {}
    return {'image': pins.get('base_image'),
            'lockfiles': {key[len(LOCKFILE_PREFIX):]: value
                          for key, value in sorted(pins.items())
                          if key.startswith(LOCKFILE_PREFIX)}}


def describe(spec):
    """One log line: what the golden's name was pinned to."""
    found = golden_pins(spec)
    if found is None:
        return 'unpinned'
    files = ', '.join('%s %s' % (name, digest[:12])
                      for name, digest in found['lockfiles'].items()) or 'no lockfiles'
    return 'image %s, %s' % ((found['image'] or 'unresolved')[:12], files)
