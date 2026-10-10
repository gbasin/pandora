#!/usr/bin/env python3
"""Write a bounded, fail-open plan before PR unit tests execute."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pandora.client.reuse import plan  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--collection', required=True)
parser.add_argument('--repo', required=True)
parser.add_argument('--default-branch', default='main')
parser.add_argument('--event', default='')
parser.add_argument('--run-number', type=int, default=0)
parser.add_argument('--output', required=True)
args = parser.parse_args()
started = time.monotonic()
result = plan(Path.cwd(), args.collection, repo=args.repo, default_branch=args.default_branch,
              event=args.event, run_number=args.run_number)
result['verification_elapsed_ms'] = round((time.monotonic() - started) * 1000)
output = Path(args.output)
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(result, indent=2) + '\n')
print('Pandora unit reuse: %s; %d whole files reused.' % (result['reason'], len(result['skip_files'])))
