# Contention and recovery evaluation

This is an agent UX trial against the existing SSH backend. It does not install a
new router or change target-repository configuration.

The fixture uses the pinned Node image and waits 30 seconds before returning its
validation result. The delay creates sustained queue pressure without placing
heavy work on the Mac. It is not a compiled-build throughput or memory benchmark.

Four agent-fanout lanes use separate worktrees and the same logical image tag:
two native Codex lanes through `routing/fanout-adapter.py`, and two command lanes
running Claude Opus through `routing/launch.py` and `output-ux/claude.py`. Each lane
starts with `broken-<phase>` in `value.txt`, observes failure, repairs that file,
rebuilds, runs, and checks the automatically returned `dist/result.json`.

An external Docker profile permits `Dockerfile`, no mounts, one generated output
mapping from `/workspace/dist` to `dist`, and network mode `none`. The fixture's
`.dockerignore` admits only `Dockerfile`, `check.mjs`, and `value.txt`. Its local
`.gitignore` excludes `dist/`.

`recovery.py` is a separate scripted command lane. Give it a fixture worktree with
value `recovery`, the same external profile, a fresh output directory, a shared
routing state directory, and the worker host. Start it while other lanes are
contending. It verifies duplicate-command rejection, changed-command rejection,
and same-attempt recovery after terminating its owned transport client while queued
and while executing. The queued case fails if contention was not actually observed.
It never sends signals to coding agents or their controller.

`resources.py` samples Mac memory-pressure output and remote container names every
few seconds, with a 15-minute upper bound and an explicit stop file. It does not
manage agent liveness. Use agent-fanout status, logs, wait, collect, and cleanup for
that purpose.

`collect.py` checks completed controller status, expected per-agent output, four
attempts per agent, verified cleanup receipts, one intended failure per agent,
and both recovery cases. It retains task-relevant transcript events, rather than
unrelated agent initialization metadata. The dated note records measured results
and limitations. Dirty fixture worktrees remain available after controller cleanup.
