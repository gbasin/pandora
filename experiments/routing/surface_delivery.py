"""Assemble validated surface planner and shard outputs for local publication."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys

from delivery import deliver
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'warm'))
from workflow_options import surface_outputs
from suite_parent_cleanup import validate_registry


def _safe(relative):
    path = PurePosixPath(relative)
    if not relative or path.is_absolute() or '..' in path.parts or '.' in path.parts:
        raise ValueError('Unsafe surface output artifact path')
    return path


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def deliver_surface(repo, output, submitted):
    """Publish compiled planner outputs plus retained shard-generated outputs."""
    output = Path(output)
    request = submitted['surface_suite']
    outputs = surface_outputs(request['app'])
    registry = json.loads((output / 'children.json').read_text())
    children = validate_registry(Path(submitted['attempt']), registry)
    if not children:
        raise ValueError('Surface run has no reserved planner attempt')
    manifest = json.loads((output / 'artifacts.json').read_text())
    if not isinstance(manifest, dict):
        raise ValueError('Invalid parent artifact manifest')
    logical, sources = {}, {}
    planner = children[0]
    for index, identity in enumerate(children):
        kind = 'results/outputs' if index == 0 else 'results/generated'
        prefix = f'results/attempts/{identity}/{kind}/'
        for artifact, digest in manifest.items():
            if not isinstance(artifact, str) or not isinstance(digest, str) or not artifact.startswith(prefix):
                continue
            relative = artifact[len(prefix):]
            _safe(relative)
            if not any(relative == root or relative.startswith(root + '/') for root in outputs):
                continue
            if relative in logical and logical[relative] != digest:
                raise ValueError('Surface outputs disagree on ' + relative)
            logical.setdefault(relative, digest)
            sources.setdefault(relative, output / artifact)
    if not logical:
        raise ValueError('Surface run returned no declared outputs')
    stage = output / '.surface-delivery'
    stage.mkdir(exist_ok=True)
    for relative, digest in logical.items():
        source, destination = sources[relative], stage / relative
        if source.is_symlink() or not source.is_file() or _digest(source) != digest:
            raise ValueError('Invalid retained surface output: ' + relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_file() or _digest(destination) != digest:
                raise ValueError('Surface delivery stage differs on retry: ' + relative)
            continue
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)
        if _digest(destination) != digest:
            raise ValueError('Surface delivery stage digest mismatch: ' + relative)
    deliver(repo, output, outputs=outputs, source_outputs=stage, manifest_mapping=logical)
