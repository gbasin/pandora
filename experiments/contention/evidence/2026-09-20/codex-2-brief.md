You are working in /Users/you/Code/pandora/.worktrees/_int-pandora-contention-20260920/.worktrees/pandora-contention-20260920-codex-2. Run pwd first; stop if it differs.

Repair and validate this fixture using its Docker build/run workflow. First build the current Dockerfile as app:test and run it to observe the failure. Then change value.txt to exactly codex-2, rebuild the same tag, and run successfully. Read the resulting local dist/result.json and verify its value.

SCOPE: edit only value.txt. Treat Dockerfile and check.mjs as fixed validation inputs. Work from this repository root. Use ordinary docker build and docker run commands. Do not commit or open a PR. Do not modify infrastructure, configuration, or unrelated files. Do not run repository-wide tests. If blocked, report the command and evidence instead of changing infrastructure.

Report the commands, observed failure, repair, successful result, and any friction. This is a validation lane; the orchestrator owns review and landing.
