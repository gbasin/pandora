#!/usr/bin/env python3
"""Launch one trial session with private pnpm routing. No global installation."""
import argparse
import os
from pathlib import Path
import shutil
import sys
import uuid

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--host', required=True)
p.add_argument('--state', required=True, type=Path)
p.add_argument('--session', default=None)
p.add_argument('command', nargs=argparse.REMAINDER)
a = p.parse_args()
command = a.command[1:] if a.command[:1] == ['--'] else a.command
if not command:
    p.error('Specify a command after --')
real = shutil.which('pnpm')
if not real or os.environ.get('PANDORA_REAL_PNPM'):
    p.error('Start from an ordinary shell with pnpm available, not a nested routed session')
env = dict(os.environ)
env['PANDORA_REAL_CODEX'] = shutil.which('codex') or ''
env.update(PANDORA_HOST=a.host, PANDORA_STATE=str(a.state.resolve()),
           PANDORA_SESSION=a.session or uuid.uuid4().hex, PANDORA_REAL_PNPM=real)
env['PATH'] = str(Path(__file__).resolve().parent / 'bin') + os.pathsep + env['PATH']
os.execvpe(command[0], command, env)
