"""Preparation-only POC. Never publishes a cache or starts a worker."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'experiments/warm'))
from snapshot import names, excluded, entry, encode, freeze, verify
from source_cache import repository_key
from transport import SSH_OPTIONS

HOST = 'ubuntu@WORKER'
SSH = ['ssh', *SSH_OPTIONS, HOST]


def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, **kwargs)


def remote(code):
    return run([*SSH, 'python3 -'], input=code.encode()).stdout.decode()


def capture(repo):
    inventory = names(repo)
    manifest = [record for name in inventory if not excluded(name)
                if (record := entry(repo, name)) is not None]
    if names(repo) != inventory:
        raise RuntimeError('Membership changed during capture')
    return inventory, manifest


def transfer(repo, dest, manifest, seed=None):
    # No -r: a file that becomes a directory must not recursively admit new files.
    options = ['--link-dest=' + seed] if seed else []
    return run(['rsync', '-lpcd', '--from0', '--files-from=-', '--stats',
                '-e', 'ssh ' + ' '.join(SSH_OPTIONS), *options,
                str(repo) + '/', HOST + ':' + dest + '/'],
               input=b''.join(r['path'].encode() + b'\0' for r in manifest))


def verify_remote(dest, manifest):
    helper = (ROOT / 'experiments/warm/snapshot.py').read_text()
    remote(helper + '\nverify(Path(' + repr(dest) + '), json.loads(' + repr(encode(manifest).decode()) + '))\n')


def benchmark(repo, mode, output):
    started = time.monotonic()
    if mode == 'manifest':
        inventory, manifest = capture(repo)
        source = repo
    else:
        manifest, _ = freeze(repo, output / 'source')
        source = output / 'source'
    captured = time.monotonic()
    token = uuid.uuid4().hex
    dest = '/home/ubuntu/pandora-warm/.poc-stress-test/' + token
    key = repository_key(repo)
    helper = (ROOT / 'experiments/warm/source_cache.py').read_text().split("if __name__ == '__main__':")[0]
    script = helper + f'\na=Path({dest!r}); a.mkdir(parents=True); (a/"source").mkdir(); print(prepare(Path.home()/"pandora-warm", {key!r}, a))\n'
    seed = remote(script).strip()
    prepared = time.monotonic()
    try:
        if mode == 'manifest':
            result = transfer(source, dest + '/source', manifest, seed)
        else:
            result = run(['rsync', '-rlpc', '--delete', '--stats', '-e', 'ssh ' + ' '.join(SSH_OPTIONS),
                          *(['--link-dest=' + seed] if seed else []),
                          str(source) + '/', HOST + ':' + dest + '/source/'])
        transferred = time.monotonic()
        (output / 'transfer.log').write_bytes(result.stdout + result.stderr)
        verify_remote(dest + '/source', manifest)
        verified = time.monotonic()
        if mode == 'manifest' and capture(repo) != (inventory, manifest):
            raise RuntimeError('Source changed during transfer; retry explicitly')
        finished = time.monotonic()
        record = dict(mode=mode, files=len(manifest), seeded=bool(seed),
                      capture_seconds=captured-started, setup_seconds=prepared-captured,
                      transfer_seconds=transferred-prepared, remote_verify_seconds=verified-transferred,
                      local_recheck_seconds=finished-verified, ready_seconds=finished-started)
        (output / 'result.json').write_text(json.dumps(record, indent=2)+'\n')
        print(json.dumps(record), flush=True)
    finally:
        remote('import shutil\nshutil.rmtree(' + repr(dest) + ')\n')
        if mode == 'copy':
            shutil.rmtree(output / 'source')


def fixtures(output):
    repo = output / 'repo'
    repo.mkdir()
    run(['git', 'init', '-q', str(repo)])
    (repo / '.gitignore').write_text('ignored\n')
    (repo / 'file').write_text('before')
    (repo / 'other').write_text('target')
    (repo / 'space and\nnewline').write_text('included')
    (repo / 'link').symlink_to('file')
    (repo / 'run').write_text('executable')
    (repo / 'run').chmod(0o755)
    (repo / '.env').write_text('secret-marker')
    (repo / 'ignored').write_text('ignored-marker')
    run(['git', '-C', str(repo), 'add', 'file'])
    dest = '/home/ubuntu/pandora-warm/.poc-stress-test/' + uuid.uuid4().hex
    results = []
    def case(label, mutate=None, after=False, tamper=False):
        inventory, manifest = capture(repo)
        path = dest + '/' + label
        remote('from pathlib import Path\nPath(' + repr(path) + ').mkdir(parents=True)\n')
        if mutate and not after:
            mutate()
        rejection = None
        try:
            transfer(repo, path, manifest)
            if mutate and after:
                mutate()
            if tamper:
                remote('from pathlib import Path\n(Path(' + repr(path) + ')/"unexpected").write_text("bad")\n')
            verify_remote(path, manifest)
            if capture(repo) != (inventory, manifest):
                raise RuntimeError('Local source changed')
        except (subprocess.CalledProcessError, RuntimeError, ValueError) as error:
            rejection = type(error).__name__
        expected_rejection = bool(mutate or tamper)
        assert bool(rejection) == expected_rejection, (label, rejection)
        # These paths must never transfer, including when an admitted file becomes a directory.
        forbidden = remote('from pathlib import Path\np=Path(' + repr(path) + ')\nprint([str(x.relative_to(p)) for x in p.rglob("*") if x.name in {".env", "ignored", "injected"}])\n').strip()
        assert forbidden == '[]', (label, forbidden)
        results.append(dict(case=label, rejected=bool(rejection), rejection=rejection, excluded_absent=True))
        print(results[-1], flush=True)
    try:
        case('unchanged-special-paths')
        old_stat = (repo/'file').stat()
        def same_size():
            (repo/'file').write_text('after!')
            os.utime(repo/'file', ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
        case('same-size-mtime', same_size)
        case('edit-after-transfer', lambda: (repo/'file').write_text('later!'), after=True)
        case('new-file', lambda: (repo/'added').write_text('added'), after=True)
        case('deletion', lambda: (repo/'file').unlink())
        (repo/'file').write_text('restore')
        def link_change():
            (repo/'link').unlink(); (repo/'link').symlink_to('other')
        case('symlink-change', link_change)
        case('mode-change', lambda: (repo/'run').chmod(0o644))
        case('ignore-change', lambda: (repo/'.gitignore').write_text('ignored\nadded\n'), after=True)
        case('unexpected-remote-file', tamper=True)
        def directory_change():
            (repo/'file').unlink(); (repo/'file').mkdir(); (repo/'file'/'injected').write_text('must not transfer')
        case('file-becomes-directory', directory_change)
        # Snapshot stays unchanged after a later local edit once acceptance checks finish.
        frozen = dest + '/unchanged-special-paths'
        text = remote('from pathlib import Path\nprint((Path(' + repr(frozen) + ')/"file").read_text())\n').strip()
        assert text == 'before'
        results.append(dict(case='accepted-snapshot-independent', passed=True))
    finally:
        remote('import shutil\nshutil.rmtree(' + repr(dest) + ')\n')
    (output/'results.json').write_text(json.dumps(results, indent=2)+'\n')


if __name__ == '__main__':
    p=argparse.ArgumentParser()
    p.add_argument('mode', choices=['copy','manifest','fixtures'])
    p.add_argument('--repo', type=Path)
    p.add_argument('--output', type=Path, required=True)
    args=p.parse_args(); args.output.mkdir(parents=True)
    if args.mode == 'fixtures': fixtures(args.output)
    else: benchmark(args.repo.resolve(), args.mode, args.output)
