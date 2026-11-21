#!/usr/bin/env python3
"""Refuse any e2e job whose code did not come from this repository's own branches.

The runner's job-started hook (ACTIONS_RUNNER_HOOK_JOB_STARTED) runs this after
GitHub assigns a job and before its first step. A workflow cannot remove it: the
hook comes from the runner's environment, not from the workflow file, and a
workflow cannot override GITHUB_* variables. So a fork pull request that deletes
e2e.yml's `if:` guard is still refused here.

A refusal kills every process of the runner's user, not just this hook. A hook
that only failed would still let `if: always()` steps run, and a graceful runner
stop cancels the job, which runs them as well. The runner user runs nothing but
the runner, so this stops the job before any step. Any failure to reach a
verdict is a refusal.
"""
import json
import os
import signal
import sys
import time

REPOSITORY = 'gbasin/pandora'

# Events that run the head repository's workflow file and code.
HEAD_CODE_EVENTS = frozenset({'pull_request', 'pull_request_review',
                              'pull_request_review_comment'})
# Events only a repository writer can cause, running this repository's own code.
# pull_request_target and workflow_run are refused: no e2e workflow uses them, and
# one that checked out a pull request head would give fork code this runner.
BASE_CODE_EVENTS = frozenset({'push', 'schedule', 'workflow_dispatch', 'merge_group'})
UNTRUSTED_ACTORS = frozenset({'dependabot[bot]'})
EVENT_LIMIT = 25 * 1024 * 1024


def _login(user):
    login = user.get('login') if isinstance(user, dict) else None
    return login.lower() if isinstance(login, str) else None


def _same_repository(base_name, base_id, head):
    """Repository IDs decide when both are known; otherwise case-insensitive names."""
    head = head if isinstance(head, dict) else {}
    head_id, head_name = head.get('id'), head.get('full_name')
    if base_id is not None and isinstance(head_id, int) and not isinstance(head_id, bool):
        return head_id == base_id
    return isinstance(head_name, str) and head_name.lower() == base_name.lower()


def verdict(environ, repository=REPOSITORY):
    """None for a trusted job, or why it is refused."""
    event = environ.get('GITHUB_EVENT_NAME')
    actual = environ.get('GITHUB_REPOSITORY')
    path = environ.get('GITHUB_EVENT_PATH')
    if not event or not actual or not path:
        return 'the job has no event name, repository or event payload'
    if actual.lower() != repository.lower():
        return 'the job belongs to %s, not %s' % (actual, repository)
    repository_id = environ.get('GITHUB_REPOSITORY_ID') or ''
    repository_id = int(repository_id) if repository_id.isdecimal() else None
    try:
        with open(path, 'rb') as file:
            data = file.read(EVENT_LIMIT + 1)
        if len(data) > EVENT_LIMIT:
            return 'the event payload is too large'
        payload = json.loads(data)
    except (OSError, ValueError):
        return 'the event payload is missing or malformed'
    if not isinstance(payload, dict):
        return 'the event payload is malformed'
    for name in ('GITHUB_ACTOR', 'GITHUB_TRIGGERING_ACTOR'):
        if (environ.get(name) or '').lower() in UNTRUSTED_ACTORS:
            return '%s is %s' % (name, environ[name])
    if event in HEAD_CODE_EVENTS:
        pull = payload.get('pull_request')
        if not isinstance(pull, dict):
            return 'the %s payload has no pull request' % event
        head = pull.get('head') if isinstance(pull.get('head'), dict) else {}
        repo = head.get('repo') if isinstance(head.get('repo'), dict) else None
        if not _same_repository(actual, repository_id, repo):
            name = repo.get('full_name') if repo else None
            return 'the pull request comes from head repository %s, not %s' % (name, actual)
        author = _login(pull.get('user'))
        if author is None or author in UNTRUSTED_ACTORS:
            return 'the pull request author is %s' % author
        return None
    if event in BASE_CODE_EVENTS:
        return None
    return 'event %s is not admitted to the e2e runner' % event


def refuse(reason, *, sleep=time.sleep, kill=os.kill, killpg=os.killpg,
           euid=os.geteuid, own_group=os.getpgrp):
    try:
        print('::error::The e2e runner runs only jobs from %s branches. This job was '
              'refused: %s.' % (REPOSITORY, reason), flush=True)
        sleep(2)  # Lets the runner forward the message before it stops.
    except Exception:
        pass
    if euid() != 0:
        # Every process of the runner user: the listener, the worker, this hook.
        kill(-1, signal.SIGKILL)
    # Reached only as root, or if the signal could not be sent.
    killpg(own_group(), signal.SIGKILL)
    return 1


def main(environ=os.environ, refusal=refuse):
    try:
        reason = verdict(environ)
    except BaseException as error:
        reason = 'the job could not be checked (%s)' % type(error).__name__
    if reason is None:
        return 0
    return refusal(reason)


if __name__ == '__main__':
    sys.exit(main())
