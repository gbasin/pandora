#!/usr/bin/env python3
"""Invoke subscribed Claude Opus under the normal trial session environment."""
import os
from pathlib import Path
import sys

# Interactive permissions cannot be answered by the headless evaluator. Allow
# only shell/read inspection for this validation-only brief; no editing tools.
prompt = Path(sys.argv[1]).read_text()
os.execvp('claude', ['claude', '-p', '--model', 'opus', '--verbose',
                    '--output-format', 'stream-json', '--permission-mode', 'dontAsk',
                    '--allowedTools', 'Bash,Read,Glob,Grep', '--', prompt])
