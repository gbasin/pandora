#!/usr/bin/env python3
"""Dry-run planner and CI-drift linter for a repo-owned Pandora configuration.

    python3 plan.py --config pandora.toml --shards 4 -- pnpm journeys --keep-going
    python3 plan.py lint --config pandora.toml

``plan`` prints the resolved plan as JSON, including a ``provenance`` table that
says, per field, whether the fact came from ``pandora.toml`` or from a job in
the repository's workflow.  ``lint`` compares jobs that restate a CI job against
that job.  No SSH, no Docker, no worktree access.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from classify import classify
from config import ConfigError, load, lint

EXIT_REJECTED = 64
EXIT_DRIFT = 65
EXIT_CONFIG = 78


def _common(parser):
    parser.add_argument('--config', required=True)
    parser.add_argument('--repo-root', default=None,
                        help='repository root that ci_workflow resolves against')


def _load(options):
    try:
        return load(options.config, root=options.repo_root)
    except ConfigError as error:
        print('[pandora] ' + str(error), file=sys.stderr)
        return None


def _report(reports):
    total = 0
    for report in reports:
        header = '%s <- %s:%s' % (report['job'], report['source'], report['ci_job'])
        if not report['findings']:
            print('%s: agrees%s' % (header, ' (except %s)' % ', '.join(report['exceptions'])
                                    if report['exceptions'] else ''))
            continue
        print(header + ':')
        for finding in report['findings']:
            total += 1
            print('  %-28s %-16s ci=%s pandora=%s'
                  % (finding['field'], finding['kind'],
                     json.dumps(finding['ci']), json.dumps(finding['pandora'])))
    return total


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog='plan.py', description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command')
    linter = sub.add_parser('lint', help='report drift against the repository workflow')
    _common(linter)
    linter.add_argument('--json', action='store_true')
    planner = sub.add_parser('plan', help='resolve one agent argv (the default)')
    _common(planner)
    planner.add_argument('--shards', type=int, default=None)
    planner.add_argument('--cwd', default='.', help='invocation directory relative to the root')
    planner.add_argument('--env', action='append', default=[], metavar='NAME=VALUE')
    planner.add_argument('command', nargs=argparse.REMAINDER)
    if argv[:1] != ['lint']:
        argv = ['plan'] + argv
    options = parser.parse_args(argv)

    config = _load(options)
    if config is None:
        return EXIT_CONFIG
    if options.command == 'lint':
        try:
            reports = lint(config, root=options.repo_root or '.')
        except ConfigError as error:
            print('[pandora] ' + str(error), file=sys.stderr)
            return EXIT_CONFIG
        if options.json:
            print(json.dumps(reports, indent=2))
            return EXIT_DRIFT if any(r['findings'] for r in reports) else 0
        return EXIT_DRIFT if _report(reports) else 0

    command = options.command[1:] if options.command[:1] == ['--'] else options.command
    if not command:
        parser.error('provide the agent command after --')
    environment = {}
    for item in options.env:
        name, _, value = item.partition('=')
        environment[name] = value
    result = classify(config, command, options.cwd, shards=options.shards, env=environment)
    print(json.dumps(result, indent=2, sort_keys=False))
    return EXIT_REJECTED if result['decision'] == 'reject' else 0


if __name__ == '__main__':
    raise SystemExit(main())
