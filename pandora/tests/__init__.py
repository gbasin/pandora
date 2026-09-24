"""Test package.

Strip git's per-invocation environment before any test runs. `git rebase --exec`
and hooks export GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE and friends, and with
them set the tests' own `git init`/`git config`/`git commit` calls in temp
directories land in the real repository instead (2026-09-24: a suite run inside
a rebase set core.bare=true on the shared checkout the live daemon runs from).
"""
import os

for _name in [key for key in os.environ if key.startswith('GIT_')
              and key not in ('GIT_CONFIG_SYSTEM', 'GIT_CONFIG_GLOBAL', 'GIT_SSH_COMMAND')]:
    del os.environ[_name]

# An agent running the suite exports its own session id, which every run the
# tests submit would then record as its submitter. The tests that want one set it.
for _name in ('PANDORA_SESSION', 'CLAUDE_CODE_SESSION_ID', 'CODEX_COMPANION_SESSION_ID'):
    os.environ.pop(_name, None)

# Nothing in the suite reads the person's own client configuration: a test that
# wants one passes `--config` or sets this to a file it wrote.
os.environ['PANDORA_CONFIG'] = os.path.join(os.sep, 'nonexistent', 'pandora-test-config.toml')

# The same belt for Pandora's own data directory: `current` and its versions
# live under XDG_DATA_HOME, and no test may read the real one or write there.
import atexit as _atexit
import shutil as _shutil
import tempfile as _tempfile

_data = _tempfile.mkdtemp(prefix='pandora-test-data-')
os.environ['XDG_DATA_HOME'] = _data
_atexit.register(_shutil.rmtree, _data, True)
