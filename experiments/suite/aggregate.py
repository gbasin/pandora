#!/usr/bin/env python3
"""Verify downloaded attempts, then aggregate an exact frozen shard set."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'warm'))
from transport import validate_evidence
from suite_evidence import aggregate


def load_attempt(path, action):
    submitted = json.loads((path / 'submission.json').read_text())
    if submitted.get('workflow') != 'suite' or submitted['suite']['action'] != action:
        raise ValueError('Expected a suite ' + action + ' attempt')
    terminal = validate_evidence(path, submitted['attempt'], submitted)
    if action == 'plan' and terminal['exit_code'] != 0:
        raise ValueError('Cannot use a failed plan attempt')
    result = json.loads((path / ('results/suite-' + action + '.json')).read_text())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan-attempt', type=Path, required=True)
    parser.add_argument('shard_attempts', type=Path, nargs='+')
    args = parser.parse_args()
    try:
        plan = load_attempt(args.plan_attempt, 'plan')
        reports = [load_attempt(path, 'shard') for path in args.shard_attempts]
        result = aggregate(plan, reports)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print('[pandora] Suite evidence incomplete or invalid: ' + str(error), file=sys.stderr)
        return 75
    print(json.dumps(result, indent=2))
    return result['exit_code']


if __name__ == '__main__':
    raise SystemExit(main())
