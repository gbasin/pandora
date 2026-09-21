#!/usr/bin/env python3
"""Run Claude print mode without background tasks that die at end-of-turn."""
import os
import sys


def main():
    # Headless Monitor/Bash notifications cannot resume a process after -p exits.
    # Scope the supported workaround to this evaluator, never interactive Claude.
    environment = dict(os.environ)
    environment['CLAUDE_CODE_DISABLE_BACKGROUND_TASKS'] = '1'
    # Match the pilot's queue + execution allowance with retrieval headroom.
    # Operators with different deadlines can set these before this launcher.
    environment.setdefault('BASH_DEFAULT_TIMEOUT_MS', '2700000')
    environment.setdefault('BASH_MAX_TIMEOUT_MS', '2700000')
    os.execvpe('claude', ['claude', '-p', *sys.argv[1:]], environment)


if __name__ == '__main__':
    main()
