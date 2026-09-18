# Pandora vs crabbox

[crabbox](https://github.com/openclaw/crabbox) is an open-source "remote testbox"
for maintainers and AI agents: sync a dirty worktree to a fast remote Linux
machine, run a command there, stream the output back. Pandora targets the same
itch, and crabbox is a reasonable comparison point — but the two designs diverge
in fundamentals, not details.

## What crabbox is

Per its docs and code:

- **Lease-per-run ephemeral VMs.** Each `crabbox run` creates a fresh lease on a
  vanilla Ubuntu instance (Hetzner Cloud or AWS EC2 Spot) unless you reuse a
  `crabbox warmup` lease by `--id`. Leases have TTL + idle-timeout reclamation.
- **Cloudflare Worker coordinator.** Auth (GitHub OAuth), lease state in Durable
  Objects, cost guardrails (`CRABBOX_MAX_*` limits, live pricing estimates),
  provider credentials held centrally. A debugging "direct path" talks to
  provider APIs from the CLI.
- **rsync of the dirty tree.** Manifest from `git ls-files --cached --others
  --exclude-standard`; `.git`, ignored dirs, and dependency folders excluded.
  Fingerprint-skip when remote state matches; mass-deletion guard; remote
  worktree seeded from `origin` before the first sync.
- **Streamed output, no write-back.** Commands run over SSH; stdout/stderr
  stream to the local terminal. There is no mechanism for remote source changes
  (formatter output) to flow back to the local tree.
- **CLI in Go, coordinator in TypeScript.** v0.2.0 (2026-05-01), CI with an 85%
  coverage gate on the Go core.
- **Explicitly out of scope (MVP):** Kubernetes, central secret storage,
  autoscaling, untrusted multi-tenant execution, Windows/macOS workers, hiding
  SSH.

## Where Pandora diverges

Pandora is currently `DESIGN.md` only — no code. The differences are
architectural:

### Persistence model

crabbox's unit is the **machine**: a lease is a whole VM, machine lifecycle
(TTL, heartbeats, cost caps) is part of the product. Pandora's unit is the
**run** on a persistent dedicated host: per-worktree Btrfs CoW workspaces
(`/work/ws/<ws>/v<N>` sealed generations), per-run writable snapshots, no
machine lifecycle in the contract at all.

### Concurrency and isolation

crabbox gives you parallelism by leasing *N machines*. Pandora is built for the
case crabbox doesn't serve: many agents, many worktrees, *concurrent runs from
one worktree* on one shared box — each run in its own snapshot and container,
cgroup-admitted and metered, Docker/Compose available inside the run through a
per-run Docker API proxy (path rewriting, cgroup parenting, label-scoped
cleanup).

### Source provenance

Pandora's core invariant is **sealed inputs**: a run executes against an
immutable generation identified by `input_id` (tree hash), and results carry
provenance. crabbox rsyncs and runs; there is no sealed-input or generation
model. Remote git in crabbox is a clone seeded from `origin` — a run's
`git rev-parse HEAD` doesn't see your local unpushed commits' history. Pandora
pushes the real HEAD and constructs a real gitdir (shared object store +
alternates), so `git status`, `git diff HEAD`, `git log`, and branch name all
match the local worktree exactly. Eichler's `fingerprint()` requires this.

### Write-back

Pandora's agent-UX contract includes automatic propagation of run-produced
source changes (per-file guards against concurrent local edits), artifacts
returning to their worktree-relative paths, and a barrier so changes are on disk
before `pandora run` exits. crabbox's sync is one-way; remote edits stay remote.

### Warmth

crabbox gets warm by keeping a VM alive. Pandora gets warm from **prepared
generations**: `node_modules` installed once per (lockfile, toolchain)
fingerprint and cloned into runs in seconds, plus shared pnpm store / Turbo
cache / browser mounts. A `pnpm check` starts in ~seconds, not after a VM boot
and install.

### Control plane

crabbox's coordinator brokers leases across cloud providers for a team, with
OAuth and monthly cost caps. Pandora is single-tenant: one `pandorad` on one
box, SSH-only transport, no fleet management, no per-run metering cost model
(the box is a fixed monthly cost).

### Run semantics

Pandora specifies observed-exit-code vs CLI-exit-code, outcomes (`oom`,
`infra_failed`, `prepare_failed`, `timed_out`), idempotent `request_id`
submission, pending-record recovery (`pandora ps`/`wait`), daemon restart
re-adoption of running containers, and `hint` diagnostics. crabbox streams
output and exits.

## Honest summary

- **Same problem space**: remote execution for coding agents over rsync of the
  dirty tree, agent-driven, Hetzner-friendly.
- **Not a clone**: crabbox is a lease broker for ephemeral VMs; Pandora is a
  persistent-host execution substrate with sealed inputs, per-run snapshots,
  shared caches, resource governance, and two-way source semantics.
- If the goal were "occasionally run a command on a beefy remote box," crabbox
  already does it. Pandora's substance is the parts crabbox doesn't have
  because its unit is the machine: continuous mirroring, sealed generations,
  per-run isolation on shared capacity, write-back, and admission.
- Closer relatives of Pandora's model: Bazel remote execution (sealed inputs,
  cache-addressed actions) and CI runner pools — not ephemeral VM leasing.
- Also worth noting: crabbox's MVP explicitly excludes macOS workers. Pandora's
  executor interface keeps a macOS executor open (iOS/Xcode suites) as a
  follow-on.

## What to steal anyway

- **Fingerprint-skip rsync**: skip transfer when remote state provably matches
  (Pandora's Mutagen flush plays this role, but the idea transfers).
- **Mass-deletion guard**: refuse syncs that would delete >N tracked files.
- **Machine classes as profiles**: `standard/fast/beast` ↔ Pandora's run
  profiles (`--memory`, `--cpus`, expected-usage admission).
- **Cost visibility**: even on a fixed box, `pandora usage`-style reporting of
  run-hours per workspace is cheap and useful.
