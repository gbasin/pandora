#!/usr/bin/env python3
"""Configure agent-fanout's native Codex launcher without editing the skill."""
import argparse
from pathlib import Path
import shlex

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--skill', type=Path, required=True)
p.add_argument('--directory', type=Path, required=True)
p.add_argument('--host', required=True)
p.add_argument('--state', type=Path, required=True)
p.add_argument('--session', required=True)
p.add_argument('--treatment', choices=['normal', 'block', 'redirect'], default='normal')
a = p.parse_args()
routing = Path(__file__).resolve().parent
scripts = a.directory.resolve() / 'scripts'
scripts.mkdir(parents=True, exist_ok=False)
for name in ['agent-fanout', 'launch-command-lane']:
    (scripts / name).symlink_to(a.skill.resolve() / 'scripts' / name)
command = ['python3', str(routing / 'launch.py'), '--host', a.host,
           '--state', str(a.state.resolve()), '--session', a.session, '--treatment', a.treatment, '--',
           str(a.skill.resolve() / 'scripts/launch-codex-lane'),
           '--codex-bin', str(routing / 'bin/codex')]
launcher = scripts / 'launch-codex-lane'
launcher.write_text('#!/bin/sh\nexec ' + shlex.join(command) + ' "$@"\n')
launcher.chmod(0o755)
print(scripts / 'agent-fanout')
