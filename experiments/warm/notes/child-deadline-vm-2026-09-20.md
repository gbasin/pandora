---
status: log
date: 2026-09-20
---

# Suite-child deadline VM proof

A two-shard suite selected `S0-01` and `S0-02` from the clean
`pandora-suite-eval` worktree. A separate one-shot systemd timer invoked the
first shard's `deadline_stop.py` after the journey containers started. The
production 20-minute timer was unchanged.

The child `8234d05768eb4634b27fe41ef331ef93` recorded terminal exit 124. Its
parent `fd3d422861e84534a348011fa1b93ad2` recorded terminal exit 75. The suite
receipt has `stop_reason: deadline`, no completed shards, and both planned
shards unrun. The timer did not stop an unrelated sentinel process. After the
sentinel and one-shot timer were stopped, `docker ps` was empty.

The pre-deadline Docker snapshot recorded 89.08% CPU and 1.85 GiB memory for
the main journey container, 21.05% and 73.66 MiB for Postgres, 5.70% and 2.336
MiB for PgBouncer, and 11.24% and 7.484 MiB for the proxy. This one sample does
not establish a safe lower resource profile.

The retained local artifact directory is
`/tmp/pandora-child-deadline-vm-20260920-1`.
