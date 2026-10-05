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
rebuild per toolchain per week. A golden that has to be built anyway takes the
newest image (`settle`). Pure standard library.
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
LOCKFILE_PREFIX = 'lockfile:'


class PinFailed(RuntimeError):
    """A pin could not be resolved."""


def lockfiles(source):
    """{name: sha256 hex} of every lockfile at the tree's root.

    Root only, deliberately: a workspace has one lockfile that governs the
    install, and walking the tree would hash a fixture's lockfile and mint a
    golden for a change that installs nothing.
    """
    found = {}
    if not source:
        return found
    root = Path(source)
    if not root.is_dir():
        return found
    names = set(LOCKFILES)
    for pattern in LOCKFILE_PATTERNS:
        names.update(path.name for path in root.glob(pattern))
    for name in sorted(names):
        path = root / name
        if path.is_file():
            found[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def cache_file(root):
    return Path(root) / 'pins' / 'images.json'


def read_cache(root):
    try:
        data = json.loads(cache_file(root).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def remember(root, alias, fingerprint, now):
    """Record one alias resolution. Atomic, so a reader never sees half a file."""
    path = cache_file(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = read_cache(root)
        data[alias] = {'fingerprint': fingerprint, 'at': now}
        handle, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.images.')
        with os.fdopen(handle, 'w') as out:
            json.dump(data, out, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass                    # a cache that cannot be written only costs a lookup


def image(root, alias, lookup, *, fresh=False, now=None, ttl=IMAGE_TTL):
    """(fingerprint or None, how) for one base-image alias.

    `how` is `cached`, `resolved`, `stale: <why>` (the lookup failed and an
    older answer was used) or `unresolved: <why>` (no answer at all).
    """
    now = time.time() if now is None else now
    entry = read_cache(root).get(alias) or {}
    known = entry.get('fingerprint')
    if known and not fresh and now - float(entry.get('at') or 0) < ttl:
        return known, 'cached'
    try:
        if lookup is None:
            raise PinFailed('this driver cannot resolve images')
        found = lookup(alias)
        if not found:
            raise PinFailed('no fingerprint for %s' % alias)
    except Exception as error:                      # noqa: BLE001 - never fatal to a run
        why = str(error)[:200] or type(error).__name__
        if known:
            return known, 'stale: %s' % why
        return None, 'unresolved: %s' % why
    remember(root, alias, found, now)
    return found, 'resolved'


def resolve(spec, root, lookup, *, source=None, digests=None, fresh=False, now=None):
    """(pins, notes, how) for one recipe; `how` is the image answer's kind.

    `digests` stands in for `source` when the caller already hashed the
    lockfiles (the selftest, from the Mac)."""
    pins, notes = {}, []
    found, how = image(root, spec['base_image'], lookup, fresh=fresh, now=now)
    notes.append('image %s: %s' % (spec['base_image'], how))
    if found:
        pins['base_image'] = found
    try:
        hashed = dict(digests) if digests is not None else lockfiles(source)
    except OSError as error:
        hashed = {}
        notes.append('lockfiles unreadable: %s' % error)
    for name, digest in sorted(hashed.items()):
        pins[LOCKFILE_PREFIX + name] = digest
    return pins, notes, how


def settle(spec, root, driver, name_of, *, source=None, digests=None, now=None):
    """The recipe with `pins` resolved, plus `pin_notes`. Idempotent.

    A spec that already has `pins` is returned unchanged. When the image came
    from the cache and the golden it names is not built, the build is cold
    anyway, so the alias is looked up again and the build takes the newest
    image rather than one up to `IMAGE_TTL` old.
    """
    if 'pins' in spec:
        return dict(spec)
    lookup = getattr(driver, 'image_fingerprint', None)
    pins, notes, how = resolve(spec, root, lookup, source=source, digests=digests, now=now)
    exists = getattr(driver, 'exists', None)
    if how == 'cached' and callable(exists) and not exists(name_of(dict(spec, pins=pins))):
        pins, notes, how = resolve(spec, root, lookup, source=source, digests=digests,
                                   fresh=True, now=now)
        notes.append('the cached image named an unbuilt golden, so the alias was '
                     'looked up again')
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
