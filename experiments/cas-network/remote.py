"""Private scratch receiver helper. Uploaded code; no installed service or cache."""
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location('scratch_cas', ROOT / 'cas-poc' / 'cas.py')
CAS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CAS)
PROFILE = CAS._BENCHMARK


def checked_root(root, token):
    root = Path(root)
    if (root.is_symlink() or not root.name.startswith('pandora-cas-network-')
            or root.parent.resolve() != Path.home().resolve()
            or (root / '.owner').read_text() != token):
        raise ValueError('not an owned private network scratch root')
    return root.resolve()


def sample_dir(root, sample):
    if not isinstance(sample, str) or not re.fullmatch('[a-zA-Z0-9_-]+', sample):
        raise ValueError('invalid sample identifier')
    return root / 'samples' / sample


def usage(who):
    value = resource.getrusage(who)
    return {'user_seconds': value.ru_utime, 'system_seconds': value.ru_stime,
            'filesystem_input_blocks': value.ru_inblock, 'filesystem_output_blocks': value.ru_oublock}


def io():
    try:
        return {line.split(':')[0]: int(line.split(':')[1])
                for line in Path('/proc/self/io').read_text().splitlines()}
    except OSError:
        return None


def subtract(after, before):
    if after is None or before is None:
        return None
    return {key: after[key] - before[key] for key in before}


def receiver(root, sample, argv):
    directory = sample_dir(root, sample)
    expected = directory / 'stage'
    if not argv or argv[0] != '--server' or '--sender' in argv:
        raise ValueError('expected scratch rsync receiver')
    if Path(argv[-1]).resolve() != expected.resolve():
        raise ValueError('receiver destination is not this sample stage')
    if expected.is_symlink():
        raise ValueError('receiver stage must not be a symlink')
    for argument in argv:
        if argument.startswith('--link-dest=') and Path(argument.split('=', 1)[1]).resolve() != root / 'baseline':
            raise ValueError('receiver link-dest must be the retained scratch baseline')
        if argument in ('--copy-links', '--copy-dirlinks', '--keep-dirlinks'):
            raise ValueError('receiver must not follow source symlinks')
    before = usage(resource.RUSAGE_CHILDREN)
    before_self = usage(resource.RUSAGE_SELF)
    began = time.monotonic()
    # Preserve protocol stdin/stdout. Diagnostics go only to the sidecar.
    result = subprocess.run(['rsync', *argv], check=False)
    metrics = {'wall_seconds': time.monotonic() - began,
               'children': subtract(usage(resource.RUSAGE_CHILDREN), before),
               'self': subtract(usage(resource.RUSAGE_SELF), before_self),
               'child_logical_read_bytes': None, 'child_logical_write_bytes': None,
               'io_scope': 'waited rsync child/descendants; raw filesystem blocks, not bytes'}
    (directory / 'receiver.json').write_text(json.dumps(metrics))
    return result.returncode


def action(root, name, request):
    if name == 'initialize':
        records = request['records']
        PROFILE.verify(root / 'baseline', records)
        store = CAS.CasStore(root / 'template')
        store.seed(root / 'baseline', records)
        return {'baseline_verified': True, 'platform': sys.version,
                'rsync_path': shutil.which('rsync'),
                'rsync_version': subprocess.check_output(['rsync', '--version'], text=True),
                'filesystem': subprocess.check_output(['df', '-T', str(root)], text=True)}
    if name == 'cleanup':
        shutil.rmtree(root)
        return {'cleaned': True}
    if name == 'baseline_audit':
        PROFILE.verify(root / 'baseline', request['records'])
        return {'baseline_immutable': True}
    directory = sample_dir(root, request['sample'])
    if name == 'setup':
        if request['method'] not in ('rsync', 'cas_trusted', 'cas_rehash'):
            raise ValueError('unknown transfer method')
        directory.mkdir(parents=True)
        if request['method'] != 'rsync':
            blobs = directory / 'store' / 'blobs'
            if request['warm']:
                shutil.copytree(root / 'template' / 'blobs', blobs, copy_function=os.link)
            else:
                blobs.mkdir(parents=True)
        return {'ready': True, 'load_average': os.getloadavg()}
    if name == 'plan':
        if request['method'] not in ('rsync', 'cas_trusted', 'cas_rehash'):
            raise ValueError('unknown transfer method')
        stage = directory / 'stage'
        stage.mkdir()
        records = request.get('records')
        state = {'method': request['method'], 'records': records}
        missing = []
        if request['method'] != 'rsync':
            CAS.validate(records)
            store = CAS.CasStore(directory / 'store')
            cached = {}
            for record in records:
                path = stage / record['path']
                path.parent.mkdir(parents=True, exist_ok=True)
                if 'link' in record:
                    path.symlink_to(record['link'])
                elif store.known(record):
                    os.link(store.blob(record), path)
                    cached[(record['sha256'], record['mode'])] = record
                else:
                    missing.append(record)
            state.update(missing=missing, cached=list(cached.values()))
            for record in records:
                if 'link' in record and not (stage / record['path']).resolve().is_relative_to(stage):
                    raise ValueError('symlink chain escapes stage')
        (directory / 'state.json').write_text(json.dumps(state))
        return {'stage': str(stage), 'missing_paths': [record['path'] for record in missing]}
    if name == 'finalize':
        state = json.loads((directory / 'state.json').read_text())
        stage = directory / 'stage'
        steps = {}
        if state['method'] != 'rsync':
            store = CAS.CasStore(directory / 'store')
            began = time.monotonic()
            for record in state['missing']:
                path = stage / record['path']
                store.install(path, record)
                path.unlink()
                os.link(store.blob(record), path)
            steps['verify_install_missing'] = time.monotonic() - began
            if state['method'] == 'cas_rehash':
                began = time.monotonic()
                for record in state['cached']:
                    if not store.known(record) or PROFILE.digest(store.blob(record)) != record['sha256']:
                        raise ValueError('cached bytes differ from manifest')
                steps['verify_cached'] = time.monotonic() - began
            actual = {path.relative_to(stage).as_posix() for path in stage.rglob('*')
                      if path.is_file() or path.is_symlink()}
            if actual != {record['path'] for record in state['records']}:
                raise ValueError('unexpected materialized paths')
        began = time.monotonic()
        os.rename(stage, directory / 'output')
        steps['publish'] = time.monotonic() - began
        metrics = directory / 'receiver.json'
        return {'steps': steps, 'receiver': json.loads(metrics.read_text()) if metrics.exists() else None}
    if name == 'audit':
        records = request['records']
        CAS.validate(records)
        began = time.monotonic()
        PROFILE.verify(directory / 'output', records)
        output_audit_seconds = time.monotonic() - began
        began = time.monotonic()
        CAS.writable_copy(directory / 'output', directory / 'execution')
        copy_seconds = time.monotonic() - began
        expected = [dict(record, mode=0o755 if record['mode'] & 0o111 else 0o644)
                    if 'mode' in record else dict(record) for record in records]
        began = time.monotonic()
        PROFILE.verify(directory / 'execution', expected)
        for record in records:
            if 'sha256' in record:
                source = (directory / 'output' / record['path']).stat()
                copied = (directory / 'execution' / record['path']).stat()
                if (source.st_dev, source.st_ino) == (copied.st_dev, copied.st_ino):
                    raise ValueError('writable execution copy shares source inode')
        execution_audit_seconds = time.monotonic() - began
        shutil.rmtree(directory)
        return {'verified': True, 'output_audit_seconds': output_audit_seconds,
                'execution_copy_seconds': copy_seconds, 'execution_audit_seconds': execution_audit_seconds}
    raise ValueError('unknown scratch action')


def main():
    root, token, name = sys.argv[1:4]
    root = checked_root(root, token)
    if name == 'receive':
        raise SystemExit(receiver(root, sys.argv[4], sys.argv[5:]))
    before, io_before, began = usage(resource.RUSAGE_SELF), io(), time.monotonic()
    request = json.load(sys.stdin)
    result = action(root, name, request)
    result['helper_metrics'] = {
        'wall_seconds': time.monotonic() - began,
        'self': subtract(usage(resource.RUSAGE_SELF), before),
        'proc_self_io_delta': subtract(io(), io_before),
        'proc_io_scope': 'helper process only; excludes rsync child',
    }
    print(json.dumps(result))


if __name__ == '__main__':
    main()
