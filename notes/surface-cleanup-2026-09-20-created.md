---
status: log
---
# Surface cleanup after container creation

After [PR #52](https://github.com/gbasin/pandora/pull/52), a VM probe exercised
the surface cleanup state after Docker acknowledged container creation and before
the worker started the container or wrote a terminal result.

The probe ran under the worker's exclusive lock after the twelve-session trial
drained. It created attempt `0bc13c95c3b23299f46cc3050a09bbac`, copied the integrated
cleanup helpers, recorded `surface-cleanup.pending`, and created one container
with the exact attempt, workflow, and experiment labels. It then invoked the
actual `service_cleanup.py` stop-hook entrypoint.

Cleanup removed the created container and pending marker. It wrote
`admission-cleanup.json` with the matching attempt and `cleanup_verified: true`.
It did not create `terminal.json`. The probe never started the container and did
not send a kill signal to a running worker. It establishes recovery from the
post-create state, rather than a full running-process crash experiment.

The local result is retained at
`~/.local/state/pandora/v01-agent-eval/surface-crash-proof.json`. The remote
attempt retains the cleanup evidence. No unrelated container was removed.
