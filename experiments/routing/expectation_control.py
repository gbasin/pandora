#!/usr/bin/env python3
"""Resolve one local, conflicted journey expectation publication."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'warm'))
import journey_updates
import catalog_updates
import route
from tracked_outputs import resolve_with_local_contents
from transport import validate_evidence


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument('action', choices=('resolve-expectations',))
    command.add_argument('attempt')
    command.add_argument('--keep-local', action='store_true')
    return command


def main():
    args = parser().parse_args()
    if not args.keep_local:
        raise ValueError('Resolve requires --keep-local to accept the current local expectation contents')
    if len(args.attempt) != 32 or any(character not in '0123456789abcdef' for character in args.attempt):
        raise ValueError('Invalid attempt ID')
    repo = Path(subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], text=True).strip()).resolve()
    if Path.cwd().resolve() != repo:
        raise ValueError('Run pandora resolve-expectations from the repository root')
    state = Path(os.environ['PANDORA_STATE']) / hashlib.sha256(str(repo).encode()).hexdigest()
    active = state / 'active.json'
    lock = (state / 'request.lock').open('a')
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('The active Pandora request is still running') from error
        if not active.exists():
            raise ValueError('No active Pandora request exists for this worktree')
        record = json.loads(active.read_text())
        if record.get('state') != 'active' or record.get('attempt') != args.attempt:
            raise ValueError('Attempt does not match the active Pandora request')
        output = Path(record.get('output', ''))
        if output.parent != state or output.name != args.attempt:
            raise ValueError('Active Pandora request has an invalid evidence path')
        terminal = validate_evidence(output, args.attempt)
        if terminal['exit_code'] != 0:
            raise ValueError('Only a successful journey update can resolve expectations')
        submitted = json.loads((output / 'submission.json').read_text())
        if submitted.get('attempt') != args.attempt:
            raise ValueError('Submission evidence does not match the active attempt')
        updates = catalog_updates if catalog_updates.is_update(submitted) else journey_updates
        if not updates.is_update(submitted):
            raise ValueError('Attempt is not a journey expectation update')
        changes = updates.declarations(output)
        resolved = resolve_with_local_contents(repo, output, changes)
        # This only acknowledges already verified terminal evidence. It never
        # contacts the worker to execute or retrieve another journey.
        route.complete(active, record, output, terminal)
        print('[pandora] Accepted current local contents for: ' + ', '.join(str(repo / name) for name in resolved))
        print('[pandora] These manually merged contents were not remotely validated. Review git diff, then run ordinary validation without --update.')
        return 0
    finally:
        lock.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        print('[pandora] ' + str(error), file=sys.stderr)
        raise SystemExit(64)
