"""Write-back on the worker: which declared files a run changed, and one proposal.

A `--update` run's job is to rewrite files the repository tracks -- acme's
journey ledgers and its route manifest. Those files must come home, but not the
way artifacts do. An artifact directory is Pandora's to replace; a tracked
fixture is also the agent's, and the agent may have edited it while the run was
on the worker. So the engine never hands back "the files"; it hands back a
*proposal*: the declared files whose bytes differ from the frozen source the run
started from, and nothing else. The client decides whether it may be written.

What a declared path means, in one place, because the client must agree:

* a literal path names one file, or every file below it when it is a directory;
* a glob may appear only in the last component (`fixtures/*.ledger.jsonl`), and
  matches regular files directly inside that one directory. The loader refuses
  anything wider, because a pattern that can match across the tree cannot be
  pulled out of an instance without pulling the tree.

Three rules this file holds:

**Write-back adds and replaces; it never deletes.** A declared file that was in
the frozen source and is absent after the run is indistinguishable, from here,
from a pull that failed. The proposal is marked incomplete and nothing is
published, which is the v0.1.1 rule ("an update omitted an existing ledger")
kept.

**A fan-out publishes all of its shards or none of them.** The parent merges
proposals only when every shard passed. Two shards proposing the same bytes for
one path agree. Two proposing different bytes collide, unless the file is a JSON
object and the shards changed disjoint top-level keys -- which is exactly what a
per-journey route manifest written by several shards looks like, and what
v0.1.1's catalog update merged by hand.

**Evidence stays on the worker.** A merged file is written beside the attempt,
and every proposal is a sha256 the client verifies after transfer.
"""
import fnmatch
import hashlib
import json
import shutil
from pathlib import Path, PurePosixPath

GLOB = set('*?[')
PROPOSAL = 'writeback'          # beside the attempt: the changed files only
PULLED = 'writeback-pulled'     # scratch: what the instance had, before filtering
# The exit a caller gets for a proposal that could not be made, when the command
# itself passed. A collision is the tree disagreeing with itself, as for
# artifacts; everything else is Pandora failing to bring the files home.
COLLISION_EXIT = 75
INFRA_EXIT = 70


# --- what a declared path matches -----------------------------------------------

def is_glob(pattern):
    return any(char in GLOB for char in pattern)


def pull_root(pattern):
    """The one worktree-relative path to pull out of an instance for a pattern."""
    if is_glob(pattern):
        parent = str(PurePosixPath(pattern).parent)
        return '' if parent == '.' else parent
    return pattern


def matches(path, patterns):
    """Whether a worktree-relative file path is declared by any pattern."""
    for pattern in patterns:
        if is_glob(pattern):
            head, tail = PurePosixPath(pattern).parent, PurePosixPath(pattern).name
            if PurePosixPath(path).parent == head and fnmatch.fnmatchcase(
                    PurePosixPath(path).name, tail):
                return True
        elif path == pattern or path.startswith(pattern.rstrip('/') + '/'):
            return True
    return False


def expand(root, patterns):
    """Every regular file under `root` that a pattern declares, sorted.

    Symlinks are not followed and not returned: a declared path that is a link
    is not a file Pandora may write through.
    """
    root = Path(root)
    found = set()
    for pattern in patterns:
        if is_glob(pattern):
            directory = root / pull_root(pattern)
            name = PurePosixPath(pattern).name
            if directory.is_dir() and not directory.is_symlink():
                for item in directory.iterdir():
                    if (fnmatch.fnmatchcase(item.name, name) and item.is_file()
                            and not item.is_symlink()):
                        found.add(item.relative_to(root).as_posix())
            continue
        target = root / pattern
        if target.is_symlink():
            continue
        if target.is_file():
            found.add(pattern)
        elif target.is_dir():
            for item in target.rglob('*'):
                if item.is_file() and not item.is_symlink():
                    found.add(item.relative_to(root).as_posix())
    return sorted(found)


def digest(path):
    hasher = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            hasher.update(block)
    return hasher.hexdigest()


def patterns_of(outputs):
    return [path for output in outputs or [] if output.get('kind') == 'writeback'
            for path in output['paths']]


# --- one run --------------------------------------------------------------------

def propose(source, pulled, into, patterns):
    """Compare what the run left against the frozen source; keep only changes.

    Returns the proposal record the result carries:
    `{complete, why, exit, changes: {path: sha256}, removed: [path]}`.
    """
    source, pulled, into = Path(source), Path(pulled), Path(into)
    if not source.is_dir():
        return incomplete('the frozen source %s is gone, so nothing can be compared '
                          'against it' % source, INFRA_EXIT)
    before = {path: digest(source / path) for path in expand(source, patterns)}
    after = {path: digest(pulled / path) for path in expand(pulled, patterns)}
    removed = sorted(set(before) - set(after))
    changes = {path: sha for path, sha in sorted(after.items()) if before.get(path) != sha}
    for path in changes:
        target = into / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pulled / path, target)
    record = {'complete': True, 'why': None, 'exit': None, 'changes': changes,
              'removed': removed}
    if removed:
        record.update(complete=False, exit=INFRA_EXIT,
                      why='%d declared file(s) were in the frozen source and did not come '
                          'back: %s. Write-back never deletes, so nothing was proposed'
                          % (len(removed), ', '.join(removed[:5])))
    return record


def incomplete(why, code):
    return {'complete': False, 'why': why, 'exit': code, 'changes': {}, 'removed': []}


def collect(pull, source, attempt, outputs):
    """Pull every declared root out of an instance and make the proposal.

    `pull(relative, into_parent)` copies `/work/<relative>` into `into_parent`
    the way `incus file pull -r` does and returns False when nothing was there.
    An absent root is not an error: a run may create a fixture that did not
    exist, and a root nothing wrote is simply one with no changes.
    """
    patterns = patterns_of(outputs)
    if not patterns:
        return None
    attempt = Path(attempt)
    pulled, into = attempt / PULLED, attempt / PROPOSAL
    for directory in (pulled, into):
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
    for root in sorted({pull_root(pattern) for pattern in patterns}):
        parent = pulled / str(PurePosixPath(root).parent) if root else pulled
        parent.mkdir(parents=True, exist_ok=True)
        pull(root, parent)
    try:
        return propose(source, pulled, into, patterns)
    finally:
        shutil.rmtree(pulled, ignore_errors=True)


# --- a fan-out ------------------------------------------------------------------

def merge(source, shards, into):
    """One proposal from N shard proposals, or an incomplete record saying why.

    `shards` maps a shard index to `(proposal record, proposal directory)`. The
    caller has already decided every shard passed; a shard whose own proposal is
    incomplete makes the merge incomplete, because a catalog update missing one
    shard's fixtures is a partial suite presented as a whole one.
    """
    into = Path(into)
    shutil.rmtree(into, ignore_errors=True)
    into.mkdir(parents=True)
    by_path = {}
    for index in sorted(shards):
        record, directory = shards[index]
        if not record or not record.get('complete'):
            return incomplete('shard %d made no complete proposal (%s); no shard\'s files '
                              'were published' % (index, (record or {}).get('why')
                                                  or 'nothing collected'),
                              (record or {}).get('exit') or INFRA_EXIT)
        for path, sha in record['changes'].items():
            by_path.setdefault(path, {})[index] = (sha, Path(directory) / path)
    changes, collided = {}, []
    for path in sorted(by_path):
        offers = by_path[path]
        shas = {sha for sha, _ in offers.values()}
        target = into / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if len(shas) == 1:
            shutil.copyfile(next(iter(offers.values()))[1], target)
            changes[path] = shas.pop()
            continue
        base = Path(source) / path
        merged = merge_json(base.read_bytes() if base.is_file() else None,
                            [offers[index][1].read_bytes() for index in sorted(offers)])
        if merged is None:
            collided.append({'path': path, 'shards': sorted(offers)})
            continue
        target.write_bytes(merged)
        changes[path] = hashlib.sha256(merged).hexdigest()
    if collided:
        return dict(incomplete(
            '%d declared file(s) were changed differently by more than one shard: %s. '
            'Nothing was published' % (len(collided), ', '.join(item['path'] for item in collided)),
            COLLISION_EXIT), collisions=collided)
    return {'complete': True, 'why': None, 'exit': None, 'changes': changes, 'removed': []}


MISSING = object()


def merge_json(base, targets):
    """Merge JSON objects whose shards changed disjoint top-level keys, or None.

    The serialization must be one this function can reproduce byte for byte --
    detected from the base, and confirmed against every shard's own output --
    because a merged fixture that differs from what the repository's own writer
    would have produced is a diff nobody asked for. Anything else is a collision.
    """
    try:
        parsed = [json.loads(item) for item in targets]
        original = json.loads(base) if base is not None else {}
    except ValueError:
        return None
    if not isinstance(original, dict) or not all(isinstance(item, dict) for item in parsed):
        return None
    style = next((style for style in STYLES
                  if all(render(value, style) == raw for value, raw in zip(parsed, targets))
                  and (base is None or render(original, style) == base)), None)
    if style is None:
        return None
    merged, owner = dict(original), {}
    for index, value in enumerate(parsed):
        # Base order, then this shard's new keys in its own order: deterministic,
        # and the order an unsorted writer would most plausibly have produced.
        for key in list(original) + [key for key in value if key not in original]:
            new = value.get(key, MISSING)
            if new == original.get(key, MISSING):
                continue
            if key in owner and owner[key][1] != new:
                return None
            owner[key] = (index, new)
    for key, (_, new) in owner.items():
        if new is MISSING:
            merged.pop(key, None)
        else:
            merged[key] = new
    return render(merged, style)


# The shapes a JSON file is commonly written in. `sort_keys` first: it is the
# only one whose key order a merge can reproduce exactly.
STYLES = tuple((indent, sort, newline) for sort in (True, False)
               for indent in (2, 4, None) for newline in (True, False))


def render(value, style):
    indent, sort, newline = style
    text = json.dumps(value, indent=indent, sort_keys=sort, ensure_ascii=False)
    return (text + ('\n' if newline else '')).encode('utf-8')
