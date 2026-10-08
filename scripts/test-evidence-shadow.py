#!/usr/bin/env python3
"""Nonblocking CI measurement; all CI commands continue to run normally."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pandora.client.observations import measure  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--ci-report', required=True)
parser.add_argument('--repo', required=True)
parser.add_argument('--job', default='unit')
parser.add_argument('--head-sha', default='')
parser.add_argument('--default-branch', default='main')
parser.add_argument('--output', required=True)
args = parser.parse_args()
result = measure(Path.cwd(), args.ci_report, repo=args.repo, job=args.job,
                 head_sha=args.head_sha, default_branch=args.default_branch)
output = Path(args.output)
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(result, indent=2) + '\n')
print('Pandora test observations: %s; %d complete files overlap; skipping disabled.' %
      (result['reason'], result.get('observed_complete_files', 0)))
