"""The version this tree carries, read from the `pyproject.toml` beside it.

`pandora --version` and a snapshot's META both come from here, so a checkout,
a version directory and a release tarball answer the same way. A tree without
the file reads None, not a guess.
"""
import tomllib
from pathlib import Path


def read(home=None):
    root = Path(home) if home else Path(__file__).resolve().parent.parent
    try:
        with open(root / 'pyproject.toml', 'rb') as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    found = (data.get('project') or {}).get('version')
    return found if isinstance(found, str) and found else None
