#!/usr/bin/env python3
"""Launch one trial session with private pnpm routing. No global installation."""
import argparse
import hashlib
import shlex
import os
from pathlib import Path
import shutil
import sys
import uuid

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--host', required=True)
p.add_argument('--state', required=True, type=Path)
p.add_argument('--docker-profile', type=Path)
p.add_argument('--queue-timeout-seconds', type=int, default=900)
p.add_argument('--session', default=None)
p.add_argument('--treatment', choices=['normal', 'block', 'redirect'], default='normal')
p.add_argument('command', nargs=argparse.REMAINDER)
a = p.parse_args()
command = a.command[1:] if a.command[:1] == ['--'] else a.command
if not command:
    p.error('Specify a command after --')
from docker_commands import queue_timeout_seconds
try:
    queue_timeout_seconds(a.queue_timeout_seconds)
except ValueError as error:
    p.error(str(error))
real = os.environ.get('PANDORA_REAL_PNPM') or shutil.which('pnpm')
if not real:
    p.error('pnpm is not available')
env = dict(os.environ)
env['PANDORA_REAL_CODEX'] = os.environ.get('PANDORA_REAL_CODEX') or shutil.which('codex') or ''
env.update(PANDORA_TREATMENT=a.treatment, PANDORA_HOST=a.host, PANDORA_STATE=str(a.state.resolve()),
           PANDORA_SESSION=a.session or uuid.uuid4().hex, PANDORA_REAL_PNPM=real)
if a.docker_profile:
    from docker_commands import profile
    import json
    docker_profile = profile(a.docker_profile.read_text())
    env['PANDORA_DOCKER_PROFILE_JSON'] = json.dumps(docker_profile)
env['PANDORA_QUEUE_TIMEOUT_SECONDS'] = str(a.queue_timeout_seconds)
prefix = str(Path(__file__).resolve().parent / 'bin')
env['PATH'] = prefix + os.pathsep + env['PATH']
original = env.get('PANDORA_ORIGINAL_ZDOTDIR') or env.get('ZDOTDIR') or str(Path.home())
identity = hashlib.sha256((prefix + '\0' + original).encode()).hexdigest()[:16]
shell_dir = a.state.resolve() / '.shell' / identity
shell_dir.mkdir(parents=True, exist_ok=True)
for name in ['.zshenv', '.zprofile', '.zshrc', '.zlogin', '.zlogout']:
    source = shlex.quote(str(Path(original) / name))
    contents = f'if [ -r {source} ]; then . {source}; fi\nexport PATH={shlex.quote(prefix)}:\"$PATH\"\n'
    temporary = shell_dir / (name + '.' + uuid.uuid4().hex)
    temporary.write_text(contents)
    temporary.replace(shell_dir / name)
env['PANDORA_ORIGINAL_ZDOTDIR'] = original
env['ZDOTDIR'] = str(shell_dir)
os.execvpe(command[0], command, env)
