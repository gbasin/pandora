---
status: log
---

# Orphan CPU loops found during Pandora verification

Gary reported many `zsh` processes and CPU saturation during surface-sharding
work. Process inspection found six orphaned shells, each consuming roughly one
CPU core. Their command lines contained six explicit `while :; do :; done`
loops from the separate `papercut-1253-webkit` experiment. Each had parent PID 1
and approximately 36 hours of elapsed time.

The orchestrator terminated exactly those six processes and verified that they
were gone. Gary explicitly authorized killing them. Two broad file searches
started by a Pandora reviewer were also terminated. A preparation worker's
recursive disk-size query was stopped. No coding-agent CLI or unrelated service
was terminated.

The orphaned loops predated Pandora's recent agent trials. Earlier Mac
responsiveness samples therefore included unrelated CPU contention. They remain
observations of that host state, not measurements of Pandora's isolated overhead.
The next twelve-agent evaluation requires a fresh baseline after this cleanup.

Pandora's private command routing does not impose OS limits on arbitrary local
shell commands. Moving configured tests to the worker cannot prevent an unrelated
agent from launching a local busy loop. This event does not change that boundary.

The scoped process-cleanup receipt is
`~/.local/state/pandora/surface-shards-proof/local-process-cleanup.json`.
