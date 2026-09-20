"""Bound immutable-attempt artifact delivery before transferring bulk data."""
from pathlib import PurePosixPath


DEFAULT_ARTIFACT_DELIVERY_LIMIT_BYTES = 2 * 1024 ** 3


class ArtifactDeliveryLimitExceeded(ValueError):
    """The remote attempt is complete, but its declared data exceeds delivery policy."""

    def __init__(self, total_bytes, limit_bytes):
        self.total_bytes = total_bytes
        self.limit_bytes = limit_bytes
        super().__init__(
            'Declared artifacts total ' + format_bytes(total_bytes) +
            ' and exceed the ' + format_bytes(limit_bytes) +
            ' delivery limit. The remote test result remains retained; increase '
            'artifact_delivery_limit_bytes and retry the same invocation to retrieve this attempt.'
        )


def format_bytes(value):
    if value < 1024 ** 3:
        return f'{value} bytes'
    return f'{value / 1024 ** 3:.2f} GiB'


def artifact_delivery_limit(value=None):
    if value is None:
        return DEFAULT_ARTIFACT_DELIVERY_LIMIT_BYTES
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError('Artifact delivery limit must be a positive integer number of bytes')
    return value


def artifact_paths(manifest):
    if not isinstance(manifest, dict) or any(not isinstance(name, str) or not isinstance(digest, str)
                                             for name, digest in manifest.items()):
        raise ValueError('Invalid artifact manifest')
    paths = []
    for name in manifest:
        path = PurePosixPath(name)
        if (not name or '\x00' in name or '\n' in name or '\r' in name or str(path) != name or
                path.is_absolute() or '.' in path.parts or '..' in path.parts):
            raise ValueError('Unsafe artifact path: ' + repr(name))
        paths.append(name)
    return tuple(paths)


def declared_artifact_total(manifest, remote_state):
    paths = artifact_paths(manifest)
    if not isinstance(remote_state, dict):
        raise ValueError('Invalid remote artifact statistics')
    sizes = remote_state.get('artifact_sizes')
    if not isinstance(sizes, dict) or set(sizes) != set(paths):
        raise ValueError('Remote artifact statistics do not match the declared manifest')
    if any(isinstance(size, bool) or not isinstance(size, int) or size < 0 for size in sizes.values()):
        raise ValueError('Remote artifact statistics contain an invalid size')
    total = sum(sizes.values())
    declared_total = remote_state.get('artifact_total_bytes')
    if isinstance(declared_total, bool) or not isinstance(declared_total, int) or declared_total != total:
        raise ValueError('Remote artifact total does not match declared artifact sizes')
    return total


def enforce_declared_artifact_limit(manifest, remote_state, limit_bytes=None):
    """Return the verified total or raise before a client transfers declared files."""
    limit = artifact_delivery_limit(limit_bytes)
    total = declared_artifact_total(manifest, remote_state)
    if total > limit:
        raise ArtifactDeliveryLimitExceeded(total, limit)
    return total
