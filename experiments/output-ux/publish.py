"""POC publication of one exclusively managed generated-output directory.

Uses atomic directory exchange, preserving the previous directory and open
handles. Assumes cooperating publishers and trusted local state. It is not a
filesystem sandbox or a guarantee against arbitrary concurrent local writers.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    os.replace(temporary, path)


def identity(path):
    info = path.stat(follow_symlinks=False)
    return [info.st_dev, info.st_ino]


def rename(source, destination, exchange):
    libc = ctypes.CDLL(None, use_errno=True)
    if platform.system() == 'Darwin':
        result = libc.renamex_np(os.fsencode(source), os.fsencode(destination),
                                2 if exchange else 4)
    elif platform.system() == 'Linux':
        result = libc.renameat2(-100, os.fsencode(source), -100,
                               os.fsencode(destination), 2 if exchange else 1)
    else:
        raise RuntimeError('Atomic publication is unsupported on this platform')
    if result:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))


def verify(directory, manifest):
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('Expected a regular artifact directory')
    actual = {}
    for path in directory.rglob('*'):
        if path.is_symlink():
            raise ValueError('Symlinks are outside this output profile')
        if path.is_file():
            actual[path.relative_to(directory).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif not path.is_dir():
            raise ValueError('Unsupported artifact file type')
    if actual != manifest:
        raise ValueError('Artifact set or digest mismatch')


def publish(root, artifacts, manifest, fault=lambda point: None, *, source=None, destination=None):
    """Caller holds the worktree lock and validates any explicit output paths."""
    root, artifacts = Path(root), Path(artifacts)
    source = Path(source) if source is not None else artifacts / 'dist'
    stage = artifacts / 'generation'
    journal = artifacts / 'publication.json'
    destination = Path(destination) if destination is not None else root / 'dist'
    verify(source, manifest)
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise ValueError('Output destination must be a regular directory')
    if not journal.exists():
        if stage.exists():
            # A partial copy before the prepared receipt is not a published generation.
            shutil.rmtree(stage)
        shutil.copytree(source, stage)
        verify(stage, manifest)
        state = {'phase': 'prepared', 'incoming_inode': identity(stage),
                 'previous_inode': identity(destination) if destination.exists() else None,
                 'manifest': manifest}
        atomic_json(journal, state)
        fault('after_prepare')
    else:
        state = json.loads(journal.read_text())
        if state['manifest'] != manifest:
            raise ValueError('Pending publication belongs to different output')
    if destination.exists() and identity(destination) == state['incoming_inode']:
        # An exchange can complete before its receipt. Never exchange a second time.
        verify(destination, manifest)
    else:
        if state['phase'] != 'prepared':
            raise ValueError('Published output was replaced outside this publisher')
        if not stage.exists() or identity(stage) != state['incoming_inode']:
            raise ValueError('Prepared generation identity is unresolved')
        current = identity(destination) if destination.exists() else None
        if current != state['previous_inode']:
            raise ValueError('Destination identity changed during publication')
        verify(stage, manifest)
        rename(stage, destination, exchange=current is not None)
        fault('after_exchange')
        verify(destination, manifest)
    state['phase'] = 'published'
    state['retained_previous'] = str(stage) if state['previous_inode'] is not None else None
    atomic_json(journal, state)
    return state
