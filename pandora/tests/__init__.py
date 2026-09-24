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
for _name in ('PANDORA_SESSION', 'CLAUDE_SESSION_ID', 'CODEX_COMPANION_SESSION_ID'):
    os.environ.pop(_name, None)
