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
import hashlib
import json
import os
import re
import signal
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


def environment(extra=None):
    """The environment for one git command: never a prompt, never a terminal.

    `GIT_TERMINAL_PROMPT=0` stops git's own credential prompt and
    `SSH_ASKPASS_REQUIRE=never` stops ssh from asking a helper program. Unless
    the user already chose an ssh command (`GIT_SSH_COMMAND` or `GIT_SSH`;
    `publish` also checks `core.sshCommand`), ssh runs with `BatchMode=yes`, so
    a key that needs a passphrase or a host that needs a password fails at once.
    """
    full = dict(os.environ, GIT_TERMINAL_PROMPT='0', SSH_ASKPASS_REQUIRE='never')
    full.update(extra or {})
    return full


def git(worktree, *args, data=None, env=None, deadline=None, config=()):
    """stdout of one git command in the worktree, or raise Failed.

    Git starts in a session of its own, so neither it nor ssh has a controlling
    terminal to prompt on, and a timeout kills the whole process group, ssh
    included, not only git.
    """
    if deadline is None:
        deadline = time.monotonic() + TIMEOUT
    timeout = deadline - time.monotonic()
    name = args[0]
    if timeout <= 0:
        raise Failed('git %s not started: the %ds deadline is used up' % (name, TIMEOUT))
    argv = ['git', '-C', str(worktree)]
    for item in config:
        argv += ['-c', item]
    try:
        proc = subprocess.Popen(argv + list(args), env=environment(env),
                                stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
    except OSError as error:
        raise Failed('git could not start: %s' % error) from None
    started = time.monotonic()
    try:
        out, err = proc.communicate(data, timeout=timeout)
    except subprocess.TimeoutExpired:
        for kill in (lambda: os.killpg(proc.pid, signal.SIGKILL), proc.kill):
            try:
                kill()
            except OSError:
                pass
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # A child that left the group still holds the pipes; git itself is gone.
            proc.wait()
        now = time.monotonic()
        used = min(max(TIMEOUT - (deadline - now), 0), TIMEOUT)
        raise Failed('git %s timed out after %.0fs (%.0fs of the %ds deadline used)'
                     % (name, now - started, used, TIMEOUT)) from None
    if proc.returncode != 0:
        why = err.decode('utf-8', 'replace').strip().splitlines()
        raise Failed('git %s exited %d%s' % (name, proc.returncode,
                                             ': ' + why[-1][:300] if why else ''))
    return out.decode('utf-8', 'replace').strip()


def has_remote(worktree, remote, *, deadline=None):
    try:
        git(worktree, 'remote', 'get-url', '--', remote, deadline=deadline)
    except Failed:
        return False
    return True


def ssh_environment(worktree, *, deadline=None):
    """`GIT_SSH_COMMAND` for ls-remote and push, unless the user chose an ssh command."""
    if os.environ.get('GIT_SSH_COMMAND') or os.environ.get('GIT_SSH'):
        return {}
    try:
        if git(worktree, 'config', '--get', 'core.sshCommand', deadline=deadline):
            return {}
    except Failed:
        pass                                    # unset: `git config --get` exits 1
    return {'GIT_SSH_COMMAND': 'ssh -o BatchMode=yes'}


def build(worktree, tree, job, payload, signature, signer, *, deadline=None, report=None):
    """The verdict commit's id, written into the worktree's object store."""
    blobs = {}
    files = [('payload.json', payload), ('signer', signer.rstrip('\n') + '\n'),
             ('verdict.sig', signature)]
    if report is not None:
        files.append(('report.json', report))
    for name, text in files:
        blobs[name] = git(worktree, 'hash-object', '-w', '--stdin', data=text.encode(),
                          deadline=deadline)
    listing = ''.join('100644 blob %s\t%s\n' % (blobs[name], name) for name in sorted(blobs))
    root = git(worktree, 'mktree', data=listing.encode(), deadline=deadline)
    # A configured i18n.commitEncoding other than UTF-8 would add an
    # `encoding` header and change the id.
    return git(worktree, 'commit-tree', '--no-gpg-sign', '-m',
               'pandora verdict %s %s' % (tree, job), root, env=IDENTITY, deadline=deadline,
               config=('i18n.commitEncoding=UTF-8',))


def present(ref, listed):
    """The record for a ref the remote already has; `commit` is the remote's."""
    return {'state': 'present', 'ref': ref, 'commit': listed.split()[0], 'reason': None}


def publish(worktree, remote, result):
    """Push the result's verdict, unless the remote already has one for the tree.

    None when there is nothing to do and nothing to say: no verdict, or no such
    remote in this worktree. Otherwise `{'state': 'published' | 'present' |
    'failed', 'ref', 'reason'}`.
    """
    if not isinstance(result, dict) or not result.get('verdict'):
        return None
    # One deadline for every subprocess, from the first `remote get-url` on.
    deadline = time.monotonic() + TIMEOUT
    if not has_remote(worktree, remote, deadline=deadline):
        return None
    ref = None
    try:
        tree, job, payload, signature, signer = parts(result)
        ref = ref_for(tree, job)
        commit = build(worktree, tree, job, payload, signature, signer, deadline=deadline)
        ssh = ssh_environment(worktree, deadline=deadline)
        listed = git(worktree, 'ls-remote', '--', remote, ref, env=ssh, deadline=deadline)
        if listed:
            return present(ref, listed)
        try:
            git(worktree, 'push', '--quiet', '--', remote, '%s:%s' % (commit, ref),
                env=ssh, deadline=deadline)
        except Failed as pushed:
            # Another publisher may have pushed a verdict for the same tree and
            # job between our ls-remote and our push. It serves as well as ours.
            try:
                listed = git(worktree, 'ls-remote', '--', remote, ref, env=ssh,
                             deadline=deadline)
            except Failed:
                listed = ''
            if listed:
                return present(ref, listed)
            raise pushed from None
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


EVIDENCE_PREFIX = 'refs/pandora/test-evidence'
EVIDENCE_RECORD = 'test-evidence-publish.json'
RUN_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z')


def publish_evidence(worktree, remote, result):
    """One immutable ref per report/run; existing verdict refs stay unchanged."""
    if not isinstance(result, dict) or not result.get('test_evidence') or not result.get('verdict'):
        return None
    deadline = time.monotonic() + TIMEOUT
    ref = None
    try:
        tree, job, payload, signature, signer = parts(result)
        body = json.loads(payload)
        run_id = body.get('run_id')
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise Failed('invalid evidence run id')
        report = result['test_evidence'].get('report')
        binding = body.get('test_evidence') or {}
        if not isinstance(report, str) or len(report.encode()) > 4 * 1024 * 1024:
            raise Failed('missing or oversized test report')
        if (hashlib.sha256(report.encode()).hexdigest() != binding.get('sha256') or
                len(report.encode()) != binding.get('bytes')):
            raise Failed('test report does not match signed digest')
        if not has_remote(worktree, remote, deadline=deadline):
            return None
        ref = '%s/%s/%s/%s' % (EVIDENCE_PREFIX, tree, job, run_id)
        commit = build(worktree, tree, job, payload, signature, signer, deadline=deadline,
                       report=report)
        ssh = ssh_environment(worktree, deadline=deadline)
        listed = git(worktree, 'ls-remote', '--', remote, ref, env=ssh, deadline=deadline)
        if listed:
            if listed.split()[0] != commit:
                raise Failed('evidence ref already names a different report')
            return {'state': 'present', 'ref': ref, 'commit': commit, 'reason': None}
        try:
            git(worktree, 'push', '--quiet', '--', remote, '%s:%s' % (commit, ref),
                env=ssh, deadline=deadline)
        except Failed:
            listed = git(worktree, 'ls-remote', '--', remote, ref, env=ssh, deadline=deadline)
            if not listed or listed.split()[0] != commit:
                raise
        return {'state': 'published', 'ref': ref, 'commit': commit, 'reason': None}
    except (Failed, ValueError, TypeError, AttributeError) as error:
        return {'state': 'failed', 'ref': ref, 'reason': str(error)}
