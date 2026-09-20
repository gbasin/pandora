---
status: log
---

# Cleanup identity verification, 2026-09-20

Docker-run and journey-service cleanup now inspect each exact resource before
removing its immutable Docker ID. Names, 64-character IDs, container state, and
the complete Pandora label namespace must match. Inherited image labels outside
that namespace remain valid. Journey networks retain their existing attempt label.

Cleanup checks the name again after removal. A replacement resource remains
untouched and keeps cleanup pending. Docker inspection errors cannot establish
absence. Missing-resource recognition is restricted to resource-specific messages.
Stopped-surface retention also permits inherited non-Pandora image labels.

All 168 worker tests passed, including identity mismatches, inherited labels,
replacement races, malformed IDs, and daemon-context errors. A serialized VM probe
removed an owned running container with an inherited OCI label, preserved a
same-name container carrying another attempt's label, and removed an owned
network. The probe removed only its own resources afterward. Its result is
`/home/ubuntu/pandora-cleanup-identity/result.json` on the trial worker.

This strengthens deletion authority. It does not grant resource admission or
turn cleanup evidence into a passing test result.
