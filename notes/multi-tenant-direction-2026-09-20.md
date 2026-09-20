---
status: log
---

# Multi-tenant direction, 2026-09-20

A design review of the v0.1 scope against one question: what changes if Pandora
serves mutually untrusted tenants with mixed workloads? This note records the
decisions Gary made in that review and the reasoning behind them. It changes no
v0.1 scope, contract, or code. Every utilization and storage number below is an
estimate from typical published figures and queueing arithmetic; none was
measured on Pandora.

## Decisions

| Question | Decision |
| --- | --- |
| Tenants | Untrusted. Workloads vary; journeys are one job type among many. |
| v0.1 | Finish as scoped and keep it frozen. No multi-tenant changes are pulled in. |
| Job unit | An arbitrary command in a VM with Docker inside, plus optional hints. |
| Warm state | Keyed immutable prepare images, plus opt-in sticky cache paths. |
| Substrate | Rented bare metal (Hetzner or OVH dedicated). RAM is the hard bottleneck. |
| Tiers | Three: priority, killable, batch. |
| Default tier | Killable, with egress off. |
| Cross-host cache tier | Deferred until measured. |
| First step after the v0.1 gate | Control-plane skeleton against the current Docker executor. |

## What v0.1 contributes

v0.1 serves one trusted operator, one SSH user, and one 16 GiB worker. It makes
no security claim. Untrusted tenants replace most of its backend: rsync over SSH,
a shared dockerd, systemd units, and a single SSH user do not contain hostile
code.

The client contract and its evidence survive:

- frozen snapshots with a remotely verified manifest;
- attempt identity, same-attempt recovery, and exit-75 semantics;
- generated-output and `--update` publication rules;
- suite planning, frozen shard membership, and aggregation;
- the admission core, extended to tenant → invocation → shard.

This matches the pilot's own position that Pandora is "not a commitment to a
custom execution platform". Gary chose to keep v0.1 frozen rather than pull in
owner-scoped identities, per-run usage curves, or a narrower slot hold. Those
remain candidates for the multi-tenant work, not v0.1.

## What multi-tenant breaks in the current backend

- **Identity.** The source cache key is a hash of the client's Git common-directory
  path. Two tenants with the same local path collide. Source caches, tag
  mappings, worktree identities, and attempt namespaces all need a tenant prefix.
- **Global ownership.** The worker lease, image-registry lock, fixed shared
  BuildKit daemons, and the rule that a foreign journey container is an orphan
  all assume one owner. The resource-admission audit already lists these as
  blockers for overlapping runs.
- **Isolation.** Hostile code needs a VM per run (Firecracker or Cloud
  Hypervisor), Docker inside the guest, and caches attached as block devices.
  Host-side tree inspection is lost.
- **Cache trust.** Shared writable caches (dependency images, the pnpm BuildKit
  mount, image layers) allow poisoning and side channels across tenants. Only
  content-addressed, integrity-verified, read-only data is safe to share.
- **Scheduling.** Fair turns are per invocation. They need a tenant level above
  that. Retention targets must become per-tenant disk quotas.
- **Missing entirely.** Real authentication and an API (SSH does not scale past
  trusted users), secrets, egress policy, parsing of tenant-supplied hints,
  metering, client version skew (the "drain older clients" rule assumes clients
  the operator controls), and multi-host placement.

## Job contract

The scheduler does not know what a journey is. A job is a resource envelope, a
set of cache keys, a deadline, and a tier. The default is an arbitrary command in
a VM with Docker inside. Optional hints unlock the behavior v0.1 gets from
operator-written profiles: cache paths, lockfiles for prepare keys, declared
outputs, `retryable`, and `tier`.

Job length changes placement. A 20-minute build absorbs a 2-minute cold start; a
15-second check does not. The rule "prefer a warm host; spill when expected queue
wait exceeds the cold penalty" applies per job, using predicted duration.

## Warm state

Two cache kinds, because tools behave in two ways:

- **Keyed prepare images.** A hinted prepare job (lockfile → install) builds an
  immutable image. Ordinary runs get a throwaway copy-on-write clone and never
  promote their side effects. Same-key preparation deduplicates. This keeps
  v0.1's rule that execution never modifies shared state.
- **Sticky cache paths.** Declared paths (turbo, cargo target, Docker layers) are
  per-tenant volumes. Each run gets a copy-on-write clone. On success, one clone
  is committed as the new base; last writer wins. Poisoning and nondeterminism
  stay inside the tenant.

Package installs go through Pandora-run registry mirrors, because jobs run with
egress off by default. The mirrors are an integrity-verified read-through cache,
which makes them the safe place for cross-tenant deduplication.

## Tiers and memory pressure

| Tier | Admission | Under memory pressure | Promise |
| --- | --- | --- | --- |
| priority | Reserved at its ceiling | Never killed because of another job | Fast start, never evicted |
| killable | Predicted usage plus a margin | Evicted after batch, newest first; retried once, and the retry cannot be evicted | Fast start, may occasionally run twice |
| batch | Only into spare capacity | Evicted first, at any time | No start-time promise; cheapest |

Safety rule per host: the sum of priority ceilings is at most physical RAM minus
a reserve. Everything packed above that line is evictable, so a wrong prediction
can only hurt a job that accepted the risk. Evictable RAM is both the
oversubscription headroom and the shock absorber.

Pressure escalates in order: throttle (PSI and `memory.high`), zswap and NVMe
swap, kill batch jobs, kill killable jobs. The per-job hard limit stays, so a
runaway job still dies alone.

Agents need a new outcome, `preempted`, with `layer: host`, distinct from
`infra_failed`. The retry keeps the same attempt identity. This does not conflict
with the v0.1 rule against automatically replacing ambiguous executions: an
eviction is a kill the scheduler issued itself, with verified cleanup.

Rerunning is only safe for idempotent jobs, and tenants submit arbitrary
commands. Egress-off is what makes the killable default safe: a job with no
external network cannot double-apply an external side effect. A job that
requests open egress is forced to priority unless it declares `retryable`.

## Utilization

RAM sets the slot count and is never blindly oversubscribed. Queueing is one
lever among five.

1. **Pack real usage, not reservations.** Admit on a predicted peak keyed by
   tenant, repo, command shape, and shard; unknown shapes get a full reservation
   until enough samples exist. Reserve a curve over time rather than a flat peak,
   because install, build, and test phases peak at different moments. Size each
   VM from history and reclaim guest memory with a balloon, free-page reporting,
   or virtio-mem; without reclaim, guest page cache holds host RAM. zswap in
   front of NVMe swap turns idle pages (a waiting Postgres, a parked browser)
   into a soft limit. Deduplicate identical pages only among one tenant's VMs;
   across tenants it is a side channel. The gain needs roughly ten or more jobs
   per host, so it appears on 128 GB-class hosts and is zero on a 16 GiB worker.
   Free RAM is also the page cache, so memory oversubscription and storage speed
   trade against each other.
2. **Cut RAM-seconds per job.** A cold job holds its reservation through a
   hydrate and prepare phase that is mostly I/O. Run hydration, prepare, and VM
   restore with a small footprint, admit the full reservation only when the job
   is ready to execute, and release it at command exit rather than after artifact
   return. A cache miss is therefore also a RAM-occupancy cost.
3. **Let the scheduler choose shard width.** The v0.1 contract already gives
   shard concurrency to the operator. Run suites wide when the fleet is idle and
   narrow when it is busy. The agent sees a slower suite instead of a queue.
4. **Fill the trough.** On fixed monthly hardware this matters most. Developer
   load in one time zone peaks at roughly 3–5× its trough, so a 90% peak averages
   about 25–35% over a day regardless of packing. Only delay-tolerant work changes
   that: a cheap preemptible batch tier, tenants in other time zones, and
   Pandora's own prepare and cache-warming work. This requires backfill past a
   blocked request for the batch class, which v0.1's fair-turn policy refuses.
5. **Pool.** Queueing hurts today because there is one slot. For roughly
   60-second jobs with a p95 wait under a minute, one slot sustains about 30–40%
   utilization, about eight slots sustain 65–70%, and about 32 sustain 85%. At
   around 100 slots, 85–90% peak utilization produces waits of seconds.

Estimated ceiling: 80–90% of RAM reserved at peak, 60–70% actually used, and a
24-hour average of 50–60% with a real batch tier or about 30% without one.

Utilization of fixed hardware is not the business measure. A bare-metal host with
128 GB RAM and NVMe rents for about $0.25/h. At GitHub-Actions-class pricing of
about $0.48/h per 2-vCPU slot, the host has margin at 25–30% utilization.

## Cross-host caches: deferred until measured

Affinity partitions the fleet: a tenant can only use slots on hosts where it is
warm. v0.1 measured a cold source upload at 64–69 s against 2.7 s warm, and a new
dependency image at about 80 s, against a 50 s warm journey. A cold placement
costs two to four times the job. Whether the fleet behaves as one pool or as
small per-tenant groups depends on how cheap a cold host can become.

Options considered:

- **Local NVMe plus an object store, with affinity as a hint.** Hosts keep an LRU
  of cache images. Images are chunked and content-addressed in S3-compatible
  storage, and a cold host lazily pulls only the chunks a job touches (Nydus,
  SOCI, eStargz, or overlaybd style). Sticky volumes commit a delta on success.
  Warmth becomes gradual rather than all-or-nothing. Hash-verified chunks are safe
  to deduplicate across tenants against poisoning; timing still reveals whether
  another tenant holds a chunk, which is tolerable for public packages and not
  for private layers.
- **Affinity only, with two or three warm homes per tenant.** No shared tier.
  Simplest. The usable slot pool per tenant stays small, and losing a host means
  a cold rebuild.
- **Ceph RBD or NVMe-oF block cluster.** Fleet-wide O(1) copy-on-write clones,
  roughly 0.2–0.5 ms latency on 25GbE. Best utilization, largest operational
  burden; a storage outage is a fleet outage.

Source snapshots need none of this. v0.1 already verifies a manifest, so file
blobs keyed by content hash in an object store make source host-independent.

Expected storage differences, to be replaced by measurement:

| Tier | Latency | IOPS | Throughput |
| --- | --- | --- | --- |
| Local NVMe | ~0.02–0.1 ms | 500k+ | 3–7 GB/s |
| Network SSD block (gp3 class) | ~0.5–2 ms | 3k–16k | 125–1000 MB/s |
| Premium network block (Ceph on NVMe class) | ~0.2–0.5 ms | 100k+ | multi-GB/s |

Small-file, latency-bound phases (pnpm linking, layer extraction, checkout,
`node_modules` traversal) are likely 3–10× slower cold on gp3-class storage, and
5–20× slower on NFS-style filesystems. Files already in page cache show no
difference. The cache interface stays abstract until the same real job has run on
local NVMe, on a network volume, and with lazy object-store hydration, recording
RAM-seconds held as well as wall time.

## First step and its limit

After the v0.1 twelve-agent gate, the first multi-tenant step is a control-plane
skeleton: HTTPS API, tenant authentication, tenant → invocation → shard
admission, the three tiers, and metering, built against the current Docker
executor.

The Docker executor does not contain hostile code. Until a VM executor exists,
this skeleton can serve only Gary and other trusted users. Untrusted tenants must
not run on it.

## Open questions

- Whether anyone wants this. The workload model rests on one repository.
- A latency objective and price for each tier.
- The VM memory-reclaim mechanism: balloon, free-page reporting, or virtio-mem.
- Safe parsing of hints from untrusted tenants.
- Client version skew once the operator no longer controls the clients.
- The cross-host cache tier, pending the measurement above. It decides whether
  pooling, and therefore most of the utilization estimate, is achievable.
