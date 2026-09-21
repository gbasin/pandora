#!/usr/bin/env python3
"""Attach to one active Pandora attempt without replacing it."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'warm'))
from transport import follow, validate_evidence
import route


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('attempt')
    args = parser.parse_args()
    if not re.fullmatch('[0-9a-f]{32}', args.attempt):
        raise ValueError('Invalid attempt ID')
    repo = Path(subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], text=True).strip()).resolve()
    if Path.cwd().resolve() != repo:
        raise ValueError('Run pandora wait from the repository root')
    state = Path(os.environ['PANDORA_STATE']) / hashlib.sha256(str(repo).encode()).hexdigest()
    active = state / 'active.json'
    if not active.exists():
        raise ValueError('No active Pandora request exists for this worktree')
    started = time.monotonic()
    last_feedback = None
    while True:
        with route.locked(state / 'state.lock'):
            record = json.loads(active.read_text())
            if record.get('protocol') != 2:
                raise ValueError('This active request predates explicit wait recovery. Keep waiting on its original client.')
            if record.get('attempt') != args.attempt:
                print('[pandora] The worktree now has a different request. No replacement submitted.', file=sys.stderr)
                return 75
            output = route.evidence_path(state, record)
            if record.get('state') in ('terminal', 'infrastructurefailure'):
                return route.completed_result(repo, output, record)
            if record.get('state') != 'active':
                raise ValueError('Attempt is not active')
            submitted = (output / 'submission.json').exists()
            busy = route.owner_is_busy(output)
        if submitted:
            break
        if not busy:
            print('[pandora] Initial capture ended before submission. Retry the original command to recover it; no replacement submitted.', flush=True)
            return 75
        now = time.monotonic()
        if now - started > record.get('queue_timeout_seconds', 900):
            print(f'[pandora] Capture is still owned by the original client. Reattach with `pandora wait {args.attempt}`; no replacement submitted.', flush=True)
            return 75
        if last_feedback is None or now - last_feedback >= 10:
            print(f'[pandora] Waiting for input capture for {args.attempt}; the original client still owns submission.', flush=True)
            last_feedback = now
        time.sleep(1)
    json.loads((output / 'submission.json').read_text())
    if not (output / 'terminal.json').exists():
        try:
            config = None
            if record.get('tool') == 'docker' and 'PANDORA_DOCKER_PROFILE_JSON' in os.environ:
                from docker_commands import profile
                config = profile(os.environ['PANDORA_DOCKER_PROFILE_JSON'])
            limit = route.effective_artifact_delivery_limit(int(os.environ.get(
                'PANDORA_ARTIFACT_DELIVERY_LIMIT_BYTES', '2147483648')), record.get('tool', 'pnpm'), config)
        except (TypeError, ValueError) as error:
            raise ValueError(str(error)) from None
        status = follow(record['host'], output,
                        artifact_delivery_limit_bytes=limit,
                        registration_pending=lambda: route.owner_is_busy(output))
        if not (output / 'terminal.json').exists() and not (output / 'operator-result.json').exists():
            return status if status else 75
    # Reuse route finalization, with the attempt identity as an explicit fence.
    saved = sys.argv
    try:
        sys.argv = ['route.py', *record['command']]
        return route.main(record.get('tool', 'pnpm'), expected_attempt=args.attempt, observer=True)
    finally:
        sys.argv = saved


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('[pandora] Detached from this observer. The original request continues unchanged.', file=sys.stderr)
        raise SystemExit(130)
    except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        print('[pandora] ' + str(error), file=sys.stderr)
        raise SystemExit(64)
