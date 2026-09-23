"""Write-back on this Mac: publish a `--update` run's proposal, or say why not.

The engine hands back a proposal -- the declared files a passing run changed,
each with its sha256 (`pandora.engine.writeback`). This module decides whether
the proposal may land in the worktree, and it is the only code that writes a
tracked file on Pandora's behalf. Four questions, in this order, each of which
can stop it:

1. **Did the run pass, and is the proposal whole?** A failed run proposes
   nothing. A fan-out proposes only when every shard passed and their proposals
   merged. A proposal with a missing file is not a smaller proposal.
2. **Did the bytes arrive?** Every fetched file is hashed and compared with the
   engine's record before anything else looks at it.
3. **Is the run still about this tree?** The worktree is frozen again and
   compared with the manifest the run was submitted from, *outside* the
   declared files and the declared artifacts. A difference means the fixtures
   were computed for source that no longer exists, so the result is stale: exit
   75 and nothing written. This is v0.1.1's `source_is_current`.
4. **Are the declared files as they were frozen?** Each file the proposal would
   write is compared with its frozen hash. An agent that edited a fixture while
   the run was away gets its edit kept and the worker's version beside the run,
   never a silent overwrite: exit 75, the paths, and `pandora resolve`.

A write-back is one publication: every file, or none. Publishing the ledger and
not the route manifest leaves a pair that no run ever produced. Each file is
written atomically (a temporary file in the same directory, then a rename); the
set is not a transaction, and a crash between two renames leaves the published
ones published and the record saying which.

Nothing here ever deletes a file. See the engine half for why.
"""
import json
import os
import time
from pathlib import Path

from ..engine import writeback as proposals
from ..errors import SnapshotError

CONTEXT = 'writeback.json'      # in the run directory: what was frozen
PROPOSED = 'writeback'          # in the run directory: the worker's versions
STALE_CAP = 20


# --- what was frozen ---------------------------------------------------------------

def context(manifest, plan, *, worktree, input_id):
    """What the post-run checks compare against, taken from the submitted manifest.

    `base` is the frozen hash of every declared file that was in the snapshot.
    A declared path that is not in it was absent at freeze time, which is a
    recorded fact too: a local file there now is a local edit. `rest` is the
    manifest minus the declared files and the declared artifacts -- the part of
    the tree whose change makes the run stale.
    """
    patterns = proposals.patterns_of(plan['outputs'])
    artifacts = [path for output in plan['outputs'] if output['kind'] == 'artifacts'
                 for path in output['paths']]
    base, rest = {}, {}
    for record in manifest:
        path = record['path']
        if proposals.matches(path, patterns):
            base[path] = record.get('sha256') or 'link:' + str(record.get('link'))
        elif not proposals.matches(path, artifacts):
            rest[path] = record
    return {'version': 1, 'worktree': str(worktree), 'input_id': input_id,
            'patterns': patterns, 'artifacts': artifacts,
            'exclude_globs': list(plan.get('secrets_exclude_globs') or ()),
            'base': base, 'rest': rest}


def save(run_dir, value):
    path = Path(run_dir) / CONTEXT
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, separators=(',', ':')))
    temporary.replace(path)


def load(run_dir):
    try:
        return json.loads((Path(run_dir) / CONTEXT).read_text())
    except (OSError, ValueError):
        return None


# --- the local tree ----------------------------------------------------------------

def local_state(worktree, path):
    """This file as the frozen hash would describe it: sha256, None, or a refusal.

    A path whose parent is a symlink, or that resolves outside the worktree, is
    reported as something no frozen hash can equal, so it lands as a conflict
    rather than as a write through a link.
    """
    root = Path(worktree)
    target = root / path
    try:
        if not target.resolve().is_relative_to(root.resolve()):
            return 'outside-worktree'
        for parent in target.relative_to(root).parents:
            if str(parent) != '.' and (root / parent).is_symlink():
                return 'symlinked-parent'
        if target.is_symlink():
            return 'link:' + os.readlink(target)
        if not target.exists():
            return None
        if not target.is_file():
            return 'not-a-file'
        return proposals.digest(target)
    except OSError as error:
        return 'unreadable: %s' % error


def stale_paths(worktree, frozen, freeze):
    """Files outside the declared ones that differ from the submitted manifest.

    `freeze(worktree, exclude_globs)` returns a manifest. A tree that cannot be
    frozen cannot be shown to be current, so it is reported as stale with the
    reason in place of a path.
    """
    try:
        manifest = freeze(worktree, frozen['exclude_globs'])
    except (SnapshotError, OSError) as error:
        return ['(the worktree could not be frozen again: %s)' % error]
    covered = frozen['patterns'] + frozen['artifacts']
    now = {record['path']: record for record in manifest
           if not proposals.matches(record['path'], covered)}
    before = frozen['rest']
    return [path for path in sorted(set(before) | set(now))
            if before.get(path) != now.get(path)][:STALE_CAP]


def default_freeze(cache):
    def freeze(worktree, exclude_globs):
        from ..snapshot import freeze as snapshot
        manifest, _dropped, _input = snapshot.freeze(worktree, exclude_globs=exclude_globs,
                                                     cache=cache)
        return manifest
    return freeze


def write_atomically(worktree, path, source):
    """Replace one worktree file with `source`'s bytes: temp file, fsync, rename.

    The mode of the file being replaced is kept, so an executable fixture stays
    executable; a new file gets the umask's default.
    """
    target = Path(worktree) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name('.%s.pandora-%d.tmp' % (target.name, os.getpid()))
    data = Path(source).read_bytes()
    with temporary.open('wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    if target.is_file():
        os.chmod(temporary, target.stat().st_mode & 0o7777)
    os.replace(temporary, target)


# --- the decision --------------------------------------------------------------------

def settle(run_dir, result, *, run_id, fetch, freeze):
    """Publish the proposal, or record why not. Returns the record for the result.

    `fetch(into)` brings the worker's proposal directory to `into`. The record
    says what happened in `state`, and `exit` is the code the caller must get
    instead of the command's own 0 -- None when the command's code stands.
    """
    frozen = load(run_dir)
    if frozen is None:
        return None
    proposal = dict(result.get('writeback') or {})
    record = dict(proposal, state=None, exit=None, written=[], conflicts=[], stale=[],
                  proposed=str(Path(run_dir) / PROPOSED),
                  resolve='pandora resolve %s --keep-local' % run_id)
    if result.get('outcome') != 'passed' or result.get('cli_exit') != 0:
        return dict(record, state='not-run', why='the run did not pass')
    if not proposal:
        return dict(record, state='incomplete', exit=proposals.INFRA_EXIT,
                    why='the engine returned no proposal for a write-back run')
    if not proposal.get('complete'):
        return dict(record, state='incomplete',
                    exit=proposal.get('exit') or proposals.INFRA_EXIT)
    changes = proposal.get('changes') or {}
    into = Path(run_dir) / PROPOSED
    if changes:
        fetch(into)
        wrong = [path for path in changes if not (into / path).is_file()
                 or proposals.digest(into / path) != changes[path]]
        if wrong:
            return dict(record, state='incomplete', exit=proposals.INFRA_EXIT,
                        why='%d proposed file(s) did not arrive intact: %s'
                            % (len(wrong), ', '.join(wrong[:5])))
    stale = stale_paths(frozen['worktree'], frozen, freeze)
    if stale:
        return dict(record, state='stale', exit=proposals.COLLISION_EXIT, stale=stale,
                    why='the worktree changed outside the declared files during the run')
    conflicts = []
    for path in sorted(changes):
        local = local_state(frozen['worktree'], path)
        if local != frozen['base'].get(path) and local != changes[path]:
            conflicts.append({'path': path, 'frozen': frozen['base'].get(path),
                              'local': local, 'proposed': changes[path]})
    if conflicts:
        return dict(record, state='conflicted', exit=proposals.COLLISION_EXIT,
                    conflicts=conflicts,
                    why='%d declared file(s) changed here during the run' % len(conflicts))
    written = publish(frozen['worktree'], into, changes)
    return dict(record, state='published' if written else 'unchanged', written=written,
                why=None, published_at=time.time())


def publish(worktree, into, changes):
    """Write every proposed file whose local bytes are not already the proposal."""
    written = []
    for path in sorted(changes):
        if local_state(worktree, path) == changes[path]:
            continue
        write_atomically(worktree, path, Path(into) / path)
        written.append(path)
    return written


# --- after a conflict ---------------------------------------------------------------

def resolve(run_dir, result, *, keep_local):
    """Settle a conflicted write-back by hand. Returns (exit, lines to print).

    `keep_local` records that the declared files as they are now are the
    answer -- the agent merged the worker's versions in by hand, or chose its
    own -- and writes nothing. Otherwise the worker's versions are published,
    but only over files still exactly as the conflict report saw them: an edit
    made after the report is newer than anything the resolve verb knows about.
    """
    record = result.get('writeback') or {}
    if record.get('state') != 'conflicted':
        return 64, ['run has no conflicted write-back (state: %s)' % record.get('state')]
    frozen = load(run_dir)
    if frozen is None:
        return 70, ['the run directory has lost its write-back record']
    worktree = frozen['worktree']
    if keep_local:
        record.update(state='resolved', resolution='keep-local', resolved_at=time.time())
        paths = [item['path'] for item in record['conflicts']]
        return 0, ['kept the local contents of %s' % ', '.join(paths),
                   'they were not validated remotely: review `git diff`, then validate '
                   'without --update']
    seen = {item['path']: item['local'] for item in record['conflicts']}
    moved = [path for path in sorted(record['changes'])
             if local_state(worktree, path) not in (
                 seen.get(path, frozen['base'].get(path)), record['changes'][path])]
    if moved:
        return 75, ['changed again since the conflict was reported, so nothing was '
                    'written: %s' % ', '.join(moved)]
    written = publish(worktree, record['proposed'], record['changes'])
    record.update(state='resolved', resolution='take-worker', written=written,
                  resolved_at=time.time())
    return 0, ['wrote the worker\'s version of %s' % (', '.join(written) or 'nothing'),
               'review `git diff`, then validate without --update']



def describe(record):
    """The `pandora:` lines a caller sees about its write-back, in order."""
    state, names = record.get('state'), _names
    if state == 'published':
        return ['wrote back %d file(s): %s' % (len(record['written']), names(record['written']))]
    if state == 'unchanged':
        return ['the run changed none of the declared files; nothing was written back']
    if state == 'conflicted':
        lines = ['not written back: %s. Your versions are kept:' % record['why']]
        lines += ['  %s  (the worker\'s: %s/%s)' % (item['path'], record['proposed'], item['path'])
                  for item in record['conflicts']]
        lines.append('merge the worker\'s versions in by hand, then `%s`; or `%s` to '
                     'replace your edits with them'
                     % (record['resolve'], record['resolve'].replace('--keep-local',
                                                                      '--take-worker')))
        return lines
    if state == 'stale':
        return ['not written back: %s: %s. The proposal is kept in %s'
                % (record['why'], names(record['stale']), record['proposed'])]
    if state in ('incomplete', 'not-run'):
        return ['not written back: %s' % record.get('why')]
    return []


def _names(paths, cap=8):
    paths = list(paths)
    text = ', '.join(paths[:cap])
    return text + (' and %d more' % (len(paths) - cap) if len(paths) > cap else '')
