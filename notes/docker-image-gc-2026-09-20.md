---
status: log
---
# Image reservations and collection, 2026-09-20

Physical image retention now follows explicit references. The image registry lock
covers logical mapping changes, resolution plus reservation, and collection. The
worker lease excludes builds during collection. Only acknowledged builds enter a
collectible ledger, which survives deletion of their attempt directories.

The state trace used for validation:

- Worktree A maps `gc:test` to image X. Request B reserves X under the registry lock.
- A rebuild replaces A's mapping with Y. Removing the tag removes the mapping.
- Collection sees B's reservation and retains X. It can collect acknowledged Y.
- B resumes by its existing attempt identity, runs X, and returns the original
  fixture value. It does not resolve the now-absent logical tag again.
- Once B's successful result and cleanup are acknowledged, collection removes X.

The VM reproduced this sequence. The first probe used identical fixture contents
to an existing worktree image. Its assertion that the image should disappear was
wrong: that worktree's mapping still protected it. A second probe used unique
contents and verified collection after acknowledgement. Both probes verified that
the reserved image remained executable after the mapping disappeared.

Local tests cover unresolved and current images, reservation retry, mapping
replacement and removal, eligibility after attempt retention, and corrupt pins.
Corrupt metadata stops collection. No force deletion or global prune is used.

Limits: reservations lost before submission remain pinned for operator review.
Historical images without completion evidence remain outside collection. Shared
layers can remain in Docker or BuildKit after an attempt tag disappears. Older
clients and workers must drain before rollout because they do not take these locks.
