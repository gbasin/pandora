#!/usr/bin/env python3
"""Dry-run planner: resolve one agent argv against a repo configuration.

    python3 plan.py --config pandora.toml --shards 4 -- pnpm journeys --keep-going

Prints the resolved plan as JSON.  No SSH, no Docker, no worktree access.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from classify import classify
from config import ConfigError, load

EXIT_REJECTED = 64
EXIT_CONFIG = 78


def main(argv=None):
    parser = argparse.ArgumentParser(prog='plan.py', description=__doc__.splitlines()[0])
    parser.add_argument('--config', required=True)
    parser.add_argument('--shards', type=int, default=None)
    parser.add_argument('--cwd', default='.', help='invocation directory relative to the repository root')
    parser.add_argument('--env', action='append', default=[], metavar='NAME=VALUE')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    command = options.command[1:] if options.command[:1] == ['--'] else options.command
    if not command:
        parser.error('provide the agent command after --')
    environment = {}
    for item in options.env:
        name, _, value = item.partition('=')
        environment[name] = value
    try:
        config = load(options.config)
    except ConfigError as error:
        print('[pandora] ' + str(error), file=sys.stderr)
        return EXIT_CONFIG
    result = classify(config, command, options.cwd, shards=options.shards, env=environment)
    print(json.dumps(result, indent=2, sort_keys=False))
    return EXIT_REJECTED if result['decision'] == 'reject' else 0


if __name__ == '__main__':
    raise SystemExit(main())
