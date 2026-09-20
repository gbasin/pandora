---
status: log
---

# Retention ownership verification, 2026-09-20

Retention now requires a matching terminal attempt ID and verified cleanup before
considering an acknowledged attempt for removal. A remaining surface container
must have the exact name, expected ownership labels, and an exited state.
Removal uses the inspected immutable container ID without force. Unknown ownership,
live state, inspection failure, and removal failure preserve the attempt.

The legacy surface label remains accepted for exited diagnostics from older pilot
runs. New containers require the complete matching attempt and workflow labels.

All 148 worker tests passed. A serialized live Docker probe on the trial VM
preserved a live container despite an inconsistent cleanup receipt, removed the
correctly owned exited container, and preserved an exited exact-name container
whose attempt label belonged to another owner. The probe also verified retention
of the last three managed image tags. Probe resources were cleaned up afterward.
The remote result is `/home/ubuntu/pandora-retention-owner/evidence/result.json`.

This verifies retention ownership. It does not establish parallel workflow
throughput or twelve-agent readiness.
