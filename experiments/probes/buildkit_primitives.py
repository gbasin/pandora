#!/usr/bin/env python3
"""Bounded Docker primitive probe. Run on a disposable Linux Docker worker.

Requires Docker Buildx. Downloads a BuildKit image and the pinned Node base.
Creates its own builder, images, and temporary build context, then removes them.
This does not test Pandora routing or representative compilation performance.
"""
import argparse
import json
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    name = 'pandora-probe-' + uuid.uuid4().hex[:12]
    tags = [name + '-a:test', name + '-b:test']
    records = []

    def call(label, argv, expected=0, timeout=180):
        start = time.monotonic()
        result = subprocess.run(['docker', *argv], text=True, capture_output=True,
                                timeout=timeout)
        (output / (label + '.stdout')).write_text(result.stdout)
        (output / (label + '.stderr')).write_text(result.stderr)
        record = dict(label=label, seconds=round(time.monotonic()-start, 3),
                      exit_code=result.returncode)
        records.append(record)
        print(json.dumps(record), flush=True)
        if expected is not None and result.returncode != expected:
            raise RuntimeError(label + ': ' + result.stderr[-1500:])
        return result

    def run(label, tag, extra=(), expected=0):
        return call(label, ['run', '--rm', '--name', name + '-run',
                           '--network=none', '--memory=128m', '--memory-swap=128m',
                           '--cpus=.5', '--pids-limit=64', *extra, tag], expected)

    try:
        call('create-builder', ['buildx', 'create', '--name', name,
             '--driver=docker-container', '--driver-opt',
             'memory=1g,memory-swap=1g,cpu-period=100000,cpu-quota=100000'])
        call('bootstrap-builder', ['buildx', 'inspect', name, '--bootstrap'])
        call('builder-limits', ['inspect', 'buildx_buildkit_' + name + '0',
             '--format', '{{json .HostConfig}}'])
        with tempfile.TemporaryDirectory(prefix=name) as directory:
            root = Path(directory)
            (root / 'Dockerfile').write_text('''FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6
WORKDIR /app
COPY deps /app/deps
RUN --mount=type=cache,target=/probe-cache if test -f /probe-cache/marker; then echo PROBE_STORE_WARM; else echo PROBE_STORE_COLD; fi; touch /probe-cache/marker
COPY source.js /app/source.js
RUN node --check source.js && mkdir -p dist && cp source.js dist/result.js
CMD ["node", "/app/dist/result.js"]
''')
            (root / 'deps').write_text('dependency-v1')
            source = root / 'source.js'

            def build(label, tag, expected=0):
                return call(label, ['buildx', 'build', '--builder', name, '--load',
                            '--provenance=false', '--network=none',
                            '--progress=plain', '-t', tag, str(root)], expected)

            source.write_text('console.log("worktree-a")\n')
            build('cold-build', tags[0])
            assert run('run-a', tags[0]).stdout.strip() == 'worktree-a'
            build('identical-build', tags[0])
            source.write_text('console.log("worktree-b")\n')
            build('source-edit-build', tags[1])
            assert run('run-b', tags[1]).stdout.strip() == 'worktree-b'
            assert run('rerun-a', tags[0]).stdout.strip() == 'worktree-a'
            (root / 'deps').write_text('dependency-v2')
            build('dependency-edit-build', tags[1])
            source.write_text('this is invalid javascript !!!\n')
            assert build('failed-rebuild', tags[1], expected=None).returncode != 0
            assert run('old-tag-after-failure', tags[1]).stdout.strip() == 'worktree-b'
            mounted = run('mount-obscures-image', tags[1],
                          extra=['-v', str(root) + ':/app:ro'], expected=None)
            assert mounted.returncode != 0 and 'MODULE_NOT_FOUND' in mounted.stderr
        report = dict(records=records, assertions='passed',
                      scope='Docker primitives; no routing, package install, or real compilation benchmark')
        (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    finally:
        call('cleanup-run', ['rm', '-f', name + '-run'], expected=None, timeout=30)
        for i, tag in enumerate(tags):
            call('cleanup-image-' + str(i), ['image', 'rm', tag], expected=None, timeout=30)
        call('cleanup-builder', ['buildx', 'rm', name], expected=None, timeout=60)
        (output / 'steps.json').write_text(json.dumps(records, indent=2) + '\n')


if __name__ == '__main__':
    main()
