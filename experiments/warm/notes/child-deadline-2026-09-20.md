---
status: log
date: 2026-09-20
---

# Child deadline stop

Suite children run as subprocesses of the parent worker. They do not have a
`pandora-worker-<child>` systemd unit. The deadline timer now writes a durable
deadline marker and signals only the registered child PID when its Linux start
ticks still match. The worker records this interruption as exit 124. The suite
parent records a planning-child exit 124 as a deadline stop, and does not call
it a planning failure.
