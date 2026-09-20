#!/usr/bin/env python3
"""Add untracked Docker evaluation inputs to a disposable Eichler worktree."""
import argparse
import json
from pathlib import Path
import subprocess

p = argparse.ArgumentParser()
p.add_argument('repo', type=Path)
a = p.parse_args()
repo = a.repo.resolve()
files = subprocess.check_output(['git', '-C', str(repo), 'ls-files', '-z'], text=True).split('\0')
inputs = sorted(f for f in files if f and (Path(f).name == 'package.json' or
                f in {'pnpm-lock.yaml', 'pnpm-workspace.yaml', '.npmrc', '.pnpmfile.cjs', 'pnpmfile.cjs'} or f.startswith('patches/')))
recipe = '''FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS build
RUN npm install --global pnpm@12.3.4
WORKDIR /workspace
ENV CI=true
'''
for item in inputs:
    recipe += 'COPY ' + json.dumps([item, './' + item]) + '\n'
recipe += '''RUN --mount=type=cache,id=pandora-compiled-pnpm,target=/pnpm/store pnpm install --frozen-lockfile --store-dir=/pnpm/store
# Preserve dependency input timestamps while overlaying current application source.
RUN --mount=type=bind,source=.,target=/inputs node /inputs/pandora-copy-source.cjs && pnpm --filter @eichler/borrower-web build
FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6
WORKDIR /workspace
COPY --from=build /workspace/apps/borrower-web/dist /workspace/dist
COPY pandora-check-build.cjs /workspace/check.cjs
CMD ["node", "check.cjs"]
'''
for name in ['Pandora.Dockerfile', 'pandora-copy-source.cjs', 'pandora-check-build.cjs']:
    if (repo / name).exists():
        raise SystemExit('Refusing to overwrite existing evaluation input: ' + name)
(repo / 'Pandora.Dockerfile').write_text(recipe)
(repo / 'pandora-copy-source.cjs').write_text('''const fs = require('node:fs');
const path = require('node:path');
const inputs = new Set(''' + json.dumps(inputs) + ''');
function copy(dir = '') {
  for (const e of fs.readdirSync(path.join('/inputs', dir), {withFileTypes:true})) {
    const rel = path.join(dir,e.name);
    if (inputs.has(rel)) continue;
    const src=path.join('/inputs',rel), dst=path.join('/workspace',rel);
    if (e.isDirectory()) { fs.mkdirSync(dst,{recursive:true}); copy(rel); }
    else if (e.isSymbolicLink()) fs.symlinkSync(fs.readlinkSync(src),dst);
    else fs.copyFileSync(src,dst);
  }
}
copy();
''')
(repo / 'pandora-check-build.cjs').write_text('''const fs = require('node:fs');
const html = fs.readFileSync('dist/index.html','utf8');
if (!html.includes('<html') || !fs.readdirSync('dist/assets').some(f=>f.endsWith('.js'))) throw Error('Missing compiled application');
const expected = process.argv[2];
if (expected && !html.includes(expected)) throw Error('Stale build: missing '+expected);
console.log('Compiled application verified'+(expected?' '+expected:''));
''')
print(json.dumps({'dockerfiles':['Pandora.Dockerfile'], 'mounts':[],
                  'outputs':[{'container':'/workspace/dist','workspace':'apps/borrower-web/dist'}], 'network':'none'},indent=2))
