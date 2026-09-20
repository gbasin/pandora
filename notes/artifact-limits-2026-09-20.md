---
status: log
---

# Artifact delivery limits, 2026-09-20

The client checks declared remote artifact sizes before bulk transfer. The default
is 2 GiB per invocation, configurable through the private launcher or Docker
profile. The current delivery policy applies on retry; it is not an immutable
execution setting. An over-limit result stays remote without automatic output
publication or retention acknowledgement.

Remote statistics require a matching terminal receipt with verified cleanup.
Manifest paths must be canonical, regular files beneath an ordinary attempt
directory. Symlink parents and unsafe paths are rejected. The client verifies the
exact manifest membership and integer size total before transfer, then retains
the existing artifact hash checks before local publication.

All 153 worker tests and 44 routing tests passed. Tests cover the default and
boundary, malformed statistics, traversal and symlink parents, and a failed
delivery followed by a higher-limit retry. The failed delivery performs only the
metadata transfer and does not promote terminal evidence or release the attempt.

A VM recovery trial retrieved completed journey
`60029acb42404726ad54828367e74a21`. A one-byte limit refused its 4,238 declared
artifact bytes with exit 75 and left no local terminal receipt. Raising the limit
to 2 GiB retrieved the same successful terminal and artifacts with exit 0.
No new worker submission was made. Local evidence is under
`~/.local/state/pandora/artifact-limit-20260920/recovery/`.

The small overridden limit exercises the recovery path without transferring a
multi-gigabyte fixture. This is delivery evidence, not twelve-agent capacity
evidence or a hard remote filesystem quota.
