---
status: log
---

# Remote surface experiment: first baseline, 2026-09-19

This note records the first worker setup and baseline attempt. It is evidence
from an experiment, not a claim that Pandora's earlier architecture exists.
The scoped design is proposed in [PR #1](https://github.com/gbasin/pandora/pull/1).
The implementation in this branch is under `experiments/surface/`.

## Why this experiment

Gary runs many coding agents on a dedicated sidecar Mac. Heavy validation can
exhaust shared resources, and agents sometimes respond to slow or silent work
by retrying, cancelling, or interfering with other processes. Moving every agent
into a remote environment is not the desired workflow. Agents and edits should
stay local; selected heavy commands should execute remotely.

The primary criterion is agent UX: familiar commands, useful progress, correct
results, and recovery without human coordination. The resource priorities are
Mac responsiveness, then infrastructure cost, then completion speed. The target
repo should need no tracked changes, so other engineers retain normal behavior.

We chose borrower-web surface validation as the first workload. It builds static
fixtures and runs Playwright without a database or journey service stack. A
working remote baseline separates execution problems from later routing,
queueing, source synchronization, and agent-behavior problems.

## Scope agreed with Gary

- Keep agents and worktrees local in the existing subscribed harnesses.
- Trial selected remote validation on owned Hetzner or OVHcloud capacity.
- Keep source, dependencies, browser binaries, and appropriate caches warm on
  the worker. Measure warm iteration separately from cold startup.
- Give each accepted run frozen source and its own writable execution state.
  Later local edits must not change a queued or running check.
- Queue when capacity is full. Do not silently fall back to heavy local work.
- Report an existing active request instead of automatically replacing it.
- Put the experiment and integration in Pandora, with no tracked Eichler edits.
- Start scripted checks before trial agents, then use two agents before larger
  fanout. Agent trials have not started.

The full queue/backend and foreground cancellation contract are not implemented
by this baseline. The provisional GitHub Actions control-plane option in PR #1
has not been deployed or selected by measured comparison. This first script uses
SSH directly to establish that the workload and resource limits work.

## Worker and cost

Gary provisioned an OVHcloud b3-16 and supplied SSH access. Read-only inspection
found Ubuntu 26.04, four logical CPUs, approximately 16 GiB RAM, no swap, and a
96 GiB root filesystem. Docker was installed on this disposable worker.

Gary reported a price of approximately US$0.13/hour and agreed to delete the VM
after the trial, within 12 hours unless extended. The experiment budget remains
US$20. Provider API access and automatic billable-resource deletion are not
configured. The container deadline described below does not stop VM billing.

## Implemented baseline

The Mac harness creates a compressed archive of a named committed revision,
records SHA256, uploads it, and verifies that hash on the worker. The first
baseline intentionally uses a clean commit because the existing local checkout
contains unrelated untracked files. Dirty-source submission is not implemented.
The target checkout, HEAD, and index are not changed.

A Docker image pins the Node base image digest and pnpm version. The browser
version must match the target lockfile. Each run records the built image ID;
apt package resolution is still build-time input, so this is not a fully
reproducible image build.

Each container has a two-CPU limit, 6 GiB memory limit, no swap, a 512-process
limit, and one Playwright worker. There is no Docker socket, host credential
mount, or published port. This is trusted-code isolation on a disposable worker,
not a claim of hostile multi-tenant security.

The worker sets a 20-minute systemd deadline for the named container. Normal
completion removes it and stops the timer. Logs, JUnit, available Playwright
artifacts, cgroup counters, and final Docker state return to the Mac. Workspace
files remain remotely for diagnosis. Interrupted-client behavior and deadline
failure paths have not yet been exercised.

## First attempt: setup failure, not contention

- Eichler revision: `5fddeb6b081a72af4d690a171c9e00cc007c3c01`.
- Selector: `smoke.spec.ts`, 45 tests.
- Attempt: `7c6cfeedb08f4195909bed517cceb496`.
- Source SHA256: `d57b99687fee8b6cea102c259ae3ce3b21dd71d8f7c1d902c7c566517622a399`.
- Local evidence: `/tmp/pandora-surface-smoke-01/`.
- Container runtime: approximately 63 seconds, including install and builds.
- Frozen dependency install reported 8.1 seconds.
- JUnit reported 45 errors. None reached a usable browser session.
- Cgroup memory peak: 3,465,068,544 bytes, approximately 3.23 GiB.
- Memory events recorded zero OOMs and zero OOM kills.
- Docker recorded exit code 1, `OOMKilled=false`; the container was removed.

The image installed Playwright 1.61.1 based on the package manifest's lower
bound. The lockfile actually resolved 1.62.1. Tests therefore requested Chromium
headless shell revision 1234 while the image held revision 1228. The image pin
was corrected to 1.62.1 and its rebuild was launched. A successful rerun is not
yet evidence in this note.

This is an infrastructure setup failure. It does not establish a product test
failure, contention, a passing baseline, or sufficient capacity for two jobs.
The result illustrates why dependency identity must come from resolved inputs.

## Warm-state implication

The source archive was about 380 MiB before compression and 291 MiB compressed.
Uploading it took minutes. Much of the tracked tree consists of images and
other assets unrelated to this surface. Repeating that upload for every edit
would give poor agent UX even if execution is fast.

The older Pandora spec's warm-state idea remains appropriate: retain a source
mirror and dependencies remotely, transfer changed source, and derive isolated
run snapshots. This baseline has not yet implemented that path. Reusing the
already uploaded archive for the immediate retry avoids another full upload but
is not itself a general incremental-sync implementation.

Before agent evaluation, the experiment still needs demonstrated warm source and
dependency reuse, frozen dirty inputs, bounded admission, session-local command
routing, duplicate-request feedback, and cancellation/recovery evidence. Those
are stated gaps, not delivered capabilities. See PR #1 for the evaluation and
adoption criteria.
