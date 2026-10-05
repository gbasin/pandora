"""Publish a worker's signed run verdict as a git ref.

A passing remote run on a ready worker comes home with `verdict`: a canonical
JSON payload naming the git tree it ran over, the job and the argv, signed by a
key only the worker holds. A repository that opts in (`[verdicts] publish =
true` in its `pandora.toml`) gets that verdict pushed to
`refs/pandora/verdicts/<tree>/<job>` on its remote, where a CI job for the same
tree can verify it and skip the work.

The ref points at a parentless commit whose tree holds exactly `payload.json`,
`verdict.sig` and `signer`. Everything about the commit is fixed (author,
committer, date, message), so the same verdict always makes the same commit id,
whoever builds it. It is built with plumbing only, in the worktree's own object
store: no checkout, no index, no working-tree change. In a linked worktree the
objects land in the common directory, which is where `git push` reads them.

Never raises. `publish` answers a record, `{'state', 'ref', 'reason'}`, that the
daemon turns into one log line; a failure here is a sentence, never a verdict
on the run.
"""
import json
import os
import re
import subprocess
import time

TIMEOUT = 60.0
PREFIX = 'refs/pandora/verdicts'
IDENTITY = {'GIT_AUTHOR_NAME': 'pandora', 'GIT_AUTHOR_EMAIL': 'pandora@localhost',
            'GIT_COMMITTER_NAME': 'pandora', 'GIT_COMMITTER_EMAIL': 'pandora@localhost',
            # `@` marks a raw epoch; the commit records `1 +0000`.
            'GIT_AUTHOR_DATE': '@1 +0000', 'GIT_COMMITTER_DATE': '@1 +0000'}
TREE = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
JOB = re.compile(r'[a-z][a-z0-9-]*\Z')
# Where the daemon records what happened, beside result.json, so `pandora
# result` can say "published" without asking the remote.
RECORD = 'verdict-publish.json'


class Failed(Exception):
    """One step could not finish; the message is the log line's reason."""


def ref_for(tree, job):
    return '%s/%s/%s' % (PREFIX, tree, job)


def parts(result):
    """(tree, job, payload, signature, signer) of a result's verdict, or raise Failed.

    The tree and the job are read from the signed payload, because that is
    what a verifier checks against; a `tree` beside it that disagrees means the
    record is not what was signed.
    """
    verdict = result.get('verdict')
    if not isinstance(verdict, dict):
        raise Failed('the result has no verdict')
    payload, signature, signer = (verdict.get(key) for key in ('payload', 'signature', 'signer'))
    if not all(isinstance(item, str) and item for item in (payload, signature, signer)):
        raise Failed('the verdict is missing its payload, signature or signer')
    try:
        body = json.loads(payload)
    except ValueError:
        raise Failed('the verdict payload is not JSON') from None
    if not isinstance(body, dict):
        raise Failed('the verdict payload is not a JSON object')
    tree, job = body.get('tree'), body.get('job')
    if not isinstance(tree, str) or not TREE.fullmatch(tree):
        raise Failed('the verdict payload names no git tree')
    if not isinstance(job, str) or not JOB.fullmatch(job):
        raise Failed('the verdict payload names no valid job')
    if result.get('tree') not in (None, tree):
        raise Failed('the result tree %s is not the signed tree %s'
                     % (result.get('tree'), tree))
    return tree, job, payload, signature, signer


def git(worktree, *args, data=None, env=None, deadline=None):
    """stdout of one git command in the worktree, or raise Failed."""
    timeout = TIMEOUT if deadline is None else deadline - time.monotonic()
    if timeout <= 0:
        raise Failed('timed out after %ds' % TIMEOUT)
    full = dict(os.environ, GIT_TERMINAL_PROMPT='0', **(env or {}))
    try:
        proc = subprocess.run(['git', '-C', str(worktree), *args], input=data,
                              capture_output=True, env=full, timeout=timeout,
                              stdin=None if data is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise Failed('git %s timed out after %ds' % (args[0], TIMEOUT)) from None
    except OSError as error:
        raise Failed('git could not start: %s' % error) from None
    if proc.returncode != 0:
        why = proc.stderr.decode('utf-8', 'replace').strip().splitlines()
        raise Failed('git %s exited %d%s' % (args[0], proc.returncode,
                                             ': ' + why[-1][:300] if why else ''))
    return proc.stdout.decode('utf-8', 'replace').strip()


def has_remote(worktree, remote):
    try:
        git(worktree, 'remote', 'get-url', '--', remote)
    except Failed:
        return False
    return True


def build(worktree, tree, job, payload, signature, signer, *, deadline=None):
    """The verdict commit's id, written into the worktree's object store."""
    blobs = {}
    for name, text in (('payload.json', payload), ('signer', signer.rstrip('\n') + '\n'),
                       ('verdict.sig', signature)):
        blobs[name] = git(worktree, 'hash-object', '-w', '--stdin', data=text.encode(),
                          deadline=deadline)
    listing = ''.join('100644 blob %s\t%s\n' % (blobs[name], name) for name in sorted(blobs))
    root = git(worktree, 'mktree', data=listing.encode(), deadline=deadline)
    return git(worktree, 'commit-tree', '--no-gpg-sign', '-m',
               'pandora verdict %s %s' % (tree, job), root, env=IDENTITY, deadline=deadline)


def publish(worktree, remote, result):
    """Push the result's verdict, unless the remote already has one for the tree.

    None when there is nothing to do and nothing to say: no verdict, or no such
    remote in this worktree. Otherwise `{'state': 'published' | 'present' |
    'failed', 'ref', 'reason'}`.
    """
    if not isinstance(result, dict) or not result.get('verdict'):
        return None
    if not has_remote(worktree, remote):
        return None
    ref = None
    try:
        tree, job, payload, signature, signer = parts(result)
        ref = ref_for(tree, job)
        deadline = time.monotonic() + TIMEOUT
        commit = build(worktree, tree, job, payload, signature, signer, deadline=deadline)
        if git(worktree, 'ls-remote', '--', remote, ref, deadline=deadline):
            return {'state': 'present', 'ref': ref, 'commit': commit, 'reason': None}
        git(worktree, 'push', '--quiet', '--', remote, '%s:%s' % (commit, ref),
            deadline=deadline)
        return {'state': 'published', 'ref': ref, 'commit': commit, 'reason': None}
    except Failed as error:
        return {'state': 'failed', 'ref': ref, 'reason': str(error)}


def line(record):
    """The run log's sentence for a publish record, without the `pandora: ` prefix."""
    if record['state'] == 'failed':
        return 'verdict not published: ' + record['reason']
    if record['state'] == 'present':
        return 'verdict published %s (already on the remote)' % record['ref']
    return 'verdict published ' + record['ref']
