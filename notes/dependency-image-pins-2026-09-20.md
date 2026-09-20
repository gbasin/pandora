---
status: log
---

# Dependency image reservations, 2026-09-20

The worker now reserves its content-keyed dependency tag before image inspection
or preparation. Retention takes the same filesystem lock when it reads pins,
updates its ledger, and removes tags. This closes the gap between choosing an
image and creating a container that references it.

A reservation survives worker death. Verified terminal cleanup releases it.
For a dead owner, a matching admission-cleanup receipt also permits release,
without creating a test terminal. Missing attempts, malformed records, and unknown
cleanup do not authorize image removal. No Docker removal uses force.

The production worker bundle includes this helper. Admission remains single-slot.
Pins do not serialize cache misses or make shared BuildKit daemons safe to stop
while another build is using them. Builder ownership, disk accounting, and parallel
parent dispatch remain prerequisites for overlapping production workflows.

## Evidence

All 116 worker tests passed, including nine new reservation/retention tests.
The cases cover the pre-container gap, owner death, matching cleanup identity,
live-owner protection, missing attempts, invalid ledgers/pins, and failed removal.

An isolated real-Docker probe created five unique alias tags against an existing
warm image. Retention removed an unused older tag while preserving the pinned
oldest tag. Releasing the process lock did not permit collection. An explicit
cleanup receipt did permit collection, without a test result. The probe removed
all its alias tags and verified that the original warm image was unchanged.

The integrated S0-01 journey attempt `51658b2d5cb34410957d6399eba611d6` passed
with exit 0 and verified cleanup. Its dependency reservation was observed while
all four journey containers were running and was absent after completion.
The dependency image was warm: lookup/preparation took 0.045 seconds and execution
took 48.84 seconds. No containers remained running afterward.

Local receipts are retained at
`~/.local/state/pandora/dependency-pins-20260920/` (the `journey` directory and
`probe-result.json`). This is a single-journey regression plus an isolated retention
probe, not proof of concurrent journey execution or twelve-agent readiness.
