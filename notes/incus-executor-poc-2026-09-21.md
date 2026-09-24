---
status: log
---
# An executor driver over Incus, 2026-09-21

A six-operation executor seam and an Incus implementation of it, measured on
the same OVHcloud b3-16 the Firecracker spike used (Ubuntu 26.04, kernel
7.0.0-14-generic, 4 vCPU, 15 GiB RAM, ext4 root, no swap). Eichler's own
`journey-runner.mjs` runs `S0-01` to a pass inside a clone with its own
dockerd, in **56 s wall from nothing to a destroyed instance**, of which the
driver's own work — clone, inject, destroy — is **1.5 s**.

The spike's one bad result was that a hard memory cap livelocked instead of
killing the run. §4 reproduces it, explains it, shows that **no cgroup setting
fixes it**, and gives the watchdog that does.

Everything is under `experiments/executor/`. Python stdlib only.

---

## 1. The seam

`interface.py`. Six operations, no container vocabulary anywhere in it, so the
Firecracker work can implement the same interface later:

| Operation | Signature |
| --- | --- |
| `prepare` | `(toolchain) -> Golden` — build or reuse the warm instance for a fingerprint |
| `clone` | `(golden, run_id, limits) -> Instance` — copy-on-write, started, limited |
| `execute` | `(instance, argv, env, cwd, limits, on_log) -> Result` — streamed log, exit code |
| `usage` | `(instance) -> Usage` — read from the instance cgroup |
| `collect` | `(instance, paths, into) -> {path: local}` |
| `destroy` | `(instance) -> Receipt` — and the receipt is machine-checkable |

Typed errors: `BackendUnavailable`, `PrepareFailed`, `CloneFailed`,
`ExecutionFailed`, `InstanceLost`, `MemoryExceeded`, `DestroyIncomplete`. A
non-zero exit code from the *command* is not an error — it is a `Result` with
`outcome='failed'`. Only the backend failing to supervise the command raises.

`Toolchain` is the declarative golden description and its `fingerprint()` is
the golden's identity: base image, packages, node and pnpm versions,
pre-pulled service images, the install command, a source identity and env.
Change any field and the next `prepare` builds a new golden instead of reusing
the old one (tested).

`Limits` carries two memory numbers that mean different things and are
enforced in different places — `memory_mib` is the learned reservation the
scheduler holds against the host budget, `ceiling_mib` is what the instance
cgroup refuses to exceed — plus `cpu_weight` (a share, never a quota) and
`cpus_hint`, which becomes `PANDORA_CPUS`.

### Where the driver runs, and why

**On the worker, not over SSH.** The driver samples the instance cgroup twice
a second and polls the run's log file at the same rate; an SSH round trip on
this host is ~90 ms, so a remote driver would spend more time in transport
than in work and would make a 0.06 s clone unmeasurable. The control plane
ships `experiments/executor/` to the worker and makes one SSH call per
operation, each of which runs the whole operation locally — the v0.1.1
`worker_bundle.py` idea (content-addressed payload over SSH stdin) with the
same shape. For this POC the ship is a plain `rsync -az`; the
content-addressing is not reimplemented here (**NOT RUN**), because nothing
measured depends on it.

---

## 2. Golden build

`prepare` launches `images:ubuntu/26.04` into project `pandora` with the
profile from §7, installs the toolchain, injects the source, starts the
nested dockerd, runs `pnpm install --frozen-lockfile`, pulls the three service
images, stops, and snapshots as `warm`.

| Step | Cold (first, incl. image download) | Warm archive/layer caches |
| --- | --- | --- |
| launch + systemd ready | 8.29 s | 0.37 s |
| apt toolchain + node 24.9.0 + pnpm 12.3.4 | 22.81 s | 22.93 s |
| eichler source in (375 MiB, 4,852 files) | 1.28 s | 1.70 s |
| dockerd up + `pnpm install --frozen-lockfile` + 3 image pulls | 18.74 s | 19.28 s |
| stop + `incus snapshot create warm` | 0.93 s | 1.05 s |
| **total** | **52.0 s** | **45.3 s** |

Size, read from the btrfs qgroup (Incus's own volume state reports
`usage: null` on a btrfs pool, so the qgroup is the only honest number):

| | Referenced | Exclusive |
| --- | --- | --- |
| `containers/pandora_golden-2775adec0be404dd` | 4,258,181,120 B (4.06 GiB) | 57,344 B |
| its `warm` snapshot | 4,258,177,024 B | 53,248 B |
| the base image volume | 566,255,616 B | 9,170,944 B |

A second `prepare` with the same fingerprint reuses the instance and returns
in well under a second. This is the (repo, install fingerprint) key the design
asked for: it is `Toolchain.fingerprint()`, and it is 16 hex characters of a
SHA-256 over the declarative description.

Inside the clone: `docker info` reports **Storage Driver overlayfs** (real
overlay2, not fuse-overlayfs, not vfs) and Cgroup Version 2, unprivileged,
with `security.idmap.isolated=true`.

---

## 3. Source injection into a clone

The golden already carries the source at its fingerprint; a run puts *its*
tree over the top. Four ways, all measured against eichler's 375 MiB /
4,852-file tracked tree, each into a fresh clone of the same golden.
"Exclusive" is the btrfs qgroup's exclusive bytes — what the clone costs on
top of the extents it shares with the golden.

| Method | Seconds | Exclusive bytes added | Writable? |
| --- | --- | --- | --- |
| `tar` piped through `incus exec` | **1.61** | 284 MiB | yes |
| `incus file push -r` | 6.48 | 378 MiB | yes |
| disk device, host tree mounted read-only | **0.04** | 0 | **no** |
| **disk device + `rsync` over the golden's copy** | **0.48** | **33 MiB** | yes |

The read-only disk device is the fastest and cheapest and is unusable on its
own: the run writes into its tree (build output, `.turbo`, test artifacts) and
two runs would share one host directory. Mounting it read-only at `/srcro`,
`rsync -a --delete --exclude node_modules --exclude .git` it over the
golden's `/work`, then removing the device gives a writable tree in **0.48 s
for 33 MiB** — an order of magnitude less disk than streaming the whole tree,
because rsync only writes what differs from the golden. That is what the
driver does and what every number below includes.

For comparison, the Firecracker spike's answer was a `dm-snapshot` at 0.06 s
plus 145 MiB of COW writes, and it needed `losetup`/`dmsetup` lifecycle
management that leaked a loop device once. This needs neither.

---

## 4. The memory-limit investigation

This was the open risk, and the answer is worse and clearer than expected.

### 4.1 What the spike actually saw

The spike ran `node -e "const a=[];for(;;){a.push(Buffer.alloc(64*1024*1024));}"`
against `memory.max=512MiB` and recorded 349,230 `max` events in 120 s with
`oom_kill 0`. Reproduced here exactly:

| Hog | Died? | `max` events / 120 s | `oom_kill` | PSI mem full avg10 | anon | file |
| --- | --- | --- | --- | --- | --- | --- |
| `Buffer.alloc(64M)` — the spike's command | **no** | 482,678 | **0** | 31.05 % | 275 MiB | 24 MiB |
| `Buffer.alloc(64M).fill(1)` — pages actually touched | **yes, rc=137 in <5 s** | 29,603 | **1** | 8.27 % | 50 MiB | 232 MiB |
| `cat` a 20k-file working set in a loop | **no** | 451,289 | **0** | 8.11 % | 55 MiB | 343 MiB |

The two `Buffer.alloc` rows differ only by `.fill(1)`. `Buffer.alloc(n)` for a
large `n` is `calloc` of a fresh anonymous mapping, so the pages are never
written and never become resident: it grows address space, not charge. **The
spike's repro was never an out-of-memory test.** What it measured was the
process thrashing its own file-backed pages — the node binary's text and its
shared objects — which is why `file` is only 24 MiB at the end: reclaim had
evicted almost all of it and it was refaulting continuously.

### 4.2 Why the kernel does not OOM

Direct from the cgroup, `file` hog, 90 s window:

```
pgscan  11,703,599      pgsteal 11,701,185      workingset_refault_file 11,491,495
```

Reclaim succeeded on **99.98 %** of the pages it scanned, and essentially
every reclaimed page was faulted straight back in. `try_charge()` only calls
the OOM killer after `MEM_CGROUP_MAX_RECLAIM_RETRIES` rounds that make *no*
progress. A working set of clean file pages several times the cap always gives
reclaim something to free, so progress is always made, so the OOM killer is
never reached. The cgroup is doing exactly what it was told; it is the run
that is stuck.

This matters far more than the spike's synthetic case, because **this is what
a real over-limit run looks like**: `pnpm install`, `turbo run typecheck` or a
test suite against a 1.2 GiB `node_modules` under a ceiling that is too small
does not allocate a huge heap — it reads a large working set. It will never
OOM. It will run at 1/50th speed until the wall clock kills it.

### 4.3 No cgroup arrangement fixes it

`file` hog, 512 MiB cap, 90 s window, same clone each time:

| Arrangement | Died? | `max` events | `high` events | `oom_kill` | PSI full avg10 | host MemAvailable |
| --- | --- | --- | --- | --- | --- | --- |
| `raw` — whatever Incus writes | no | 451,289 | 0 | 0 | 8.11 % | 14,221 MiB |
| `bare` — `+ memory.swap.max=0` | no | 350,934 | 0 | 0 | 7.68 % | 14,219 MiB |
| `oomgroup` — `+ memory.oom.group=1` | no | 347,797 | 0 | 0 | 7.21 % | 14,205 MiB |
| `high` — `+ memory.high = 0.9 × max` | no | **0** | 76,137 | 0 | 6.66 % | 14,221 MiB |
| `full` — high + oom.group + swap.max=0 | no | **0** | 75,586 | 0 | 5.36 % | 14,204 MiB |

- **`memory.swap.max=0` changes nothing here** because the host has no swap
  and no zswap (`SwapTotal: 0`, `/sys/module/zswap/parameters/enabled = N`).
  It stays in the arrangement because on a host *with* swap it is what stops
  the run paging instead of failing.
- **`memory.oom.group=1` changes nothing**, and cannot: it only decides *who*
  dies once the OOM killer fires, and the OOM killer never fires.
- **`memory.high` changes the shape and not the outcome.** The cgroup is
  reclaimed down to `high` and never reaches `max`, so `memory.events:max`
  stays at **0** while `high` climbs to 76,137. It keeps the host calmer (host
  PSI full avg10 6.01 % against 7.39 %) and it costs the run more, and the run
  still never dies.
- The **host stayed healthy throughout every one of these**: MemAvailable
  never fell below 14.2 GiB of 15.6 GiB, and host memory PSI full avg10 stayed
  under 7.4 %. Containment holds. Termination does not.

**Conclusion: a hard cgroup cap is not self-terminating for this workload, and
there is no setting that makes it so. A watchdog is mandatory, not an
optimisation.**

### 4.4 The arrangement the driver ships

`IncusDriver.apply` (before start) writes, through Incus:

```
limits.memory=<ceiling>MiB   limits.memory.enforce=hard   limits.memory.swap=false
limits.cpu.priority=<weight/10>
```

`IncusDriver.harden` (after start, because the cgroup does not exist before
it) writes, directly to `/sys/fs/cgroup/lxc.payload.<project>_<name>`:

```
memory.swap.max = 0                 nothing to page out to; fail rather than crawl
memory.high     = 0.9 × ceiling     reclaim early, keep the host calm, raise PSI sooner
memory.oom.group = 1                if the kernel ever does OOM, take the whole run
```

and `IncusDriver.supervise` runs the watchdog. A thrash episode is **three
things at once, sustained for 15 s**:

1. `memory.current ≥ 0.95 × min(memory.high, memory.max)` — pinned at the
   effective wall. Comparing against `memory.max` alone would never fire once
   `memory.high` is set, which is the trap this POC walked into first.
2. refused charges (`max` + `high` events) at **≥ 500/s**. Measured: hogs
   produce 1,700–4,000/s; a passing journey produces **0**.
3. cgroup `memory.pressure` full avg10 **≥ 2 %**. Measured: hogs 5.4–8.3 %; a
   passing journey 0.0 %. PSI alone is not usable as the trigger — a
   single-threaded thrasher on four CPUs only reaches 8 %, nowhere near the
   "90 % stalled" a naive threshold would want.

On a verdict the driver kills the run's process group (`kill -9 -<pgid>`,
falling back to the host's own `cgroup.kill` if `incus exec` cannot get in,
because an exec into a capped instance is itself charged to that cap), returns
`outcome='oom'` with the evidence dict, and the caller destroys the instance.

A run that is legitimately pinned at its ceiling with sustained reclaim churn
is killed. That is not a false positive: the ceiling is an operator decision,
the cgroup is already refusing the run what it asks for, and `oom` is the
correct verdict.

### 4.5 Watchdog and neighbour, measured

The `file` hog against a 512 MiB ceiling, with the full arrangement and the
watchdog live:

```
outcome oom   exit -9   21.5 s from start of execute to verdict
reason memory-thrash   thrashing_seconds 15.2   throttle_events_per_second 934.0
memory_current 482,230,272   memory_wall 482,344,960 (memory.high)   memory_max 536,870,912
psi_memory_full_avg10 5.65   events {high: 16756, max: 0, oom: 0, oom_kill: 0}
```

21.5 s is 15.2 s of sustained thrash plus the ~6 s the run took to reach the
wall in the first place. Destroying the thrashing instance afterwards took
**0.97 s**.

Two things the first attempt got wrong, both worth writing down because they
are the kind of thing a watchdog design gets wrong silently:

1. **The probe was throttled by the cap it was watching.** `incus exec` forks
   a process *inside* the instance's cgroup, so under `memory.high` throttling
   the driver's own log poll crawled — the first watchdog run sat at two
   samples in four minutes and reached no verdict at all. Supervision now
   reads the cgroup host-side every 0.5 s unconditionally and treats the
   guest-side poll as optional, timing it out at 20 s and backing it off to
   four times its last duration. Nothing inside a run can starve the verdict.
2. **The instantaneous event rate is far too noisy to threshold.** Consecutive
   samples of a real thrash read 1,462 / 680 / 660 / 1,511 / **163** / 1,219
   per second. A per-sample test resets its own timer on the dips: the run
   reached `outcome=timeout` at 300 s rather than `oom`. The rate is now
   smoothed over a 5 s trailing window.

**The neighbour is unaffected.** A 512 MiB run thrashing beside a normal
journey, both started at the same moment:

| | Thrashing run | Journey beside it |
| --- | --- | --- |
| outcome | **`oom`** after 23.5 s | **`ok`**, `S0-01: pass 40.7 s replayed`, 45/45 |
| evidence | 978.3 refused charges/s, 15.4 s sustained, PSI full avg10 5.41 %, `memory_current 482,365,440` at a `memory_wall` of 482,344,960 | — |
| `execute` seconds | 23.5 | 56.3 (against 51.4–54.1 alone) |
| `memory.peak` | 461 MiB of a 512 MiB ceiling | 3,772 MiB |

The journey's own time inside the run (40.7 s replayed) is within the 39.6–41.5 s
range it takes with the box to itself; the ~4 s on `execute` is the CPU the
thrasher takes, not anything it does to its neighbour's memory. Host
MemAvailable never dropped below 14.2 GiB during the episode.

---

## 5. One run, end to end

`poc.py run` — clone, inject, harden, execute, collect, destroy — against a
golden that already exists:

| Phase | Seconds |
| --- | --- |
| `incus copy golden/warm run-<id>` | **0.06–0.09** |
| `incus start` → systemd ready | 0.26–0.27 |
| source injection (disk device + rsync) | 0.47–0.51 |
| write the cgroup arrangement | <0.01 |
| `execute`: dockerd up + `S0-01` to exit 0 | 51.4–54.1 |
| `collect` outputs | included above |
| `destroy` + receipt | **0.89–0.96** |
| **wall** | **52.9–56.2** |

```
S0-01: pass 39.6-41.5s replayed
stage-routes: 45 observed, 45 selected, 45 replayed, 45/45 covered by passing replays
```

The driver's own work — clone, inject, destroy — is **1.5 s** of a 56 s wall.
Instance `memory.peak` for the run was 3,139–3,581 MiB. Every run in this note
returned `receipt_clean: true`.

For comparison with the Firecracker spike on the same host and the same
journey: 141.4 s wall / 132.4 s journey in a microVM, 62.7 s wall / 50.3 s
journey in its Incus trial. This is the same order as the spike's Incus
number, with the driver, the limits, the watchdog and the receipt added.

---

## 6. Concurrency

N clones of the golden running `S0-01` at once, each admitted by
`admission.py` against a 14,336 MiB host budget (15.6 GiB total, ~1 GiB left
outside the runs). History was seeded with three peaks of 3,800 MiB, which
makes `classify` choose the **`large`** class (ceiling 8,192 MiB) and
`reserve` return **4,750 MiB** — so three runs fit the budget and a fourth
does not.

| N | Admitted | Wall (s) | `execute` median (s) | `memory.peak` per run (MiB) | Σ peak (MiB) | clone (s) | start (s) | inject (s) | destroy (s) | passes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 56.7 | 54.5 | 3,190 | 3,190 | 0.07 | 0.25 | 0.50 | 0.91 | 1/1 |
| 2 | 2 | 70.7 | 68.0 | 3,099 / 3,266 | 6,365 | 0.09 | 0.34 | 0.69 | 0.94–0.98 | 2/2 |
| 3 | 3 | 89.2 | 84.0 | 2,917 / 2,982 / 3,007 | 8,906 | 0.11–0.12 | 0.42–0.45 | 0.77–0.84 | 0.93–0.99 | 3/3 |

Then the same thing with admission's budget raised deliberately, to measure
what it is protecting (`--force`). Note the lane counts: asking for 4 or 6 at
a 4,750 MiB reservation still only admits 3 and 5 respectively, because the
forced budget is computed from the *seeded* 3,800 MiB and the *learned*
reservation is larger. Admission refusing the extra lane is the point.

| Asked | Admitted | Wall (s) | `execute` median (s) | `memory.peak` per run (MiB) | Σ peak (MiB) | clone (s) | destroy (s) | passes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 4 | 3 | 86.3 | 82.6 | 3,052 / 2,970 / 3,324 | 9,346 | 0.10–0.11 | 0.94–0.97 | 3/3 |
| 6 | **5** | 141.3 | 136.0 | 2,420 / 2,327 / 2,461 / 2,539 / 2,594 | **12,341** | 0.15–0.20 | 0.85–1.31 | **5/5** |

**Five concurrent journeys fit this 15 GiB box and all five pass.** The
interesting number is Σ peak: 12,341 MiB for five runs, against 3,190 MiB for
one. **A run's peak is not a constant — it falls as the box fills**, from
3,190 MiB alone to ~2,480 MiB at five lanes, because most of it is page cache
that reclaim takes back when there is competition. A reservation learned from
solo runs therefore over-reserves under concurrency, which is safe and is also
why admission would only have let three of these five start.

Host, sampled once a second for the whole window (`max` / `mean`):

| N | cpu PSI some avg10 | mem PSI some avg10 | mem PSI full avg10 | io PSI some avg10 | loadavg | MemAvailable min |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 11.39 / 10.36 | 0.08 / 0.01 | 0.08 / 0.01 | 4.22 / 1.67 | 4.03 / 3.03 | 12,139 MiB |
| 2 | 32.40 / 26.85 | 0.18 / 0.02 | 0.18 / 0.02 | 5.32 / 2.39 | 4.06 / 3.12 | 10,049 MiB |
| 3 | 51.66 / 46.28 | 0.63 / 0.13 | 0.28 / 0.04 | 6.48 / 3.87 | 7.18 / 5.92 | 8,411 MiB |

| 3 (asked 4) | 52.27 / 43.90 | 0.51 / 0.07 | 0.18 / 0.02 | 8.12 / 4.15 | 7.01 / 4.80 | 8,379 MiB |
| 5 (asked 6) | 78.83 / 70.03 | 7.53 / 2.95 | **3.15 / 1.17** | 12.36 / 8.33 | 16.77 / 13.08 | **3,779 MiB** |

**Memory pressure is essentially absent up to N=3** — full avg10 peaks at
0.28 % — while CPU pressure is what actually rises, from 11 % to 52 %. Admit
on memory, CPU soft is the right shape for this workload on this box: memory
is what must not be oversubscribed, and CPU is what degrades gracefully.

The reservation is also **48 % larger than the observed peak** (4,750 MiB
reserved against ~3,000 MiB used). That is the cost of p95 × 1.25 over a
seeded history; with real history the margin would tighten, and it is the
direction to err in.

No failures at any N: every run exited 0 with `45/45 covered by passing
replays`, and every receipt came back clean.

### Mixed: two journeys beside a CPU-heavy job

Two `S0-01` journeys and one CPU-heavy job in a third clone: three passes of
`pnpm exec turbo run typecheck --force --concurrency=4`, 14 turbo tasks mostly
`tsc --noEmit`, 169 s and **450 CPU-seconds** on its own. All three runs share
four cores, all `PANDORA_CPUS=2`, memory ceiling 8,192 MiB each, nothing
capped on CPU.

| | Journey exec (s) | `S0-01` replayed (s) | Heavy exec (s) | Heavy CPU (s) | Journey peaks (MiB) |
| --- | --- | --- | --- | --- | --- |
| **two journeys alone** (N=2, hint 2) | 68.0 | — | — | — | 3,099 / 3,266 |
| **+ heavy at equal weight** (`allowance=100%`) | 93.0 / 91.4 | 69.6 / 69.8 | 169.0 | 450.0 | 3,114 / 3,291 |
| **+ heavy de-prioritised** (`allowance=10%`) | **82.8 / 83.3** | **62.9 / 64.3** | 171.5 | 446.6 | 3,213 / 3,561 |

- A CPU-heavy neighbour costs a journey **+37 %** at equal weight (68.0 → 93.0 s).
- Giving the heavy job a tenth of the weight recovers **11 %** of that
  (93.0 → 82.8 s exec, 69.7 → 63.6 s inside the journey) and costs the heavy
  job **1.5 %** (169.0 → 171.5 s). That is cheap insurance, and it is the
  entire argument for keeping a weight knob in `Limits`.
- Host memory PSI stayed at **0.00** throughout both, with cpu PSI some avg10
  at 68 % mean and loadavg ~12–13. Memory admission and CPU softness are
  doing exactly the jobs they were assigned.
- Every journey passed 45/45 in both arrangements. The heavy job exited 0.

**Use `limits.cpu.allowance=<N>%`, not `limits.cpu.priority`.** The percentage
form of allowance writes `cpu.weight=N` and leaves `cpu.max` unlimited, which
is the weight-not-quota model the design asked for. `limits.cpu.priority`
spans `cpu.weight` 90–100 only (the spike measured `priority=5 → weight 95`),
an 11 % differential that cannot express "this job matters less".

One incidental finding from making the heavy job run at all:
`@eichler/progress`'s typecheck shells out to `git rev-parse HEAD`, and
Pandora ships **tracked files, not a checkout**, so it fails with
`fatal: not a git repository` and then, after `git init`, with
`detected dubious ownership` and then `Command failed: git rev-parse HEAD`. It
is excluded from the measured job. This is a real constraint on the source
hand-off, not a driver bug: some of a repo's own jobs assume a git working
tree with at least one commit.

### What `PANDORA_CPUS` should be

**A share of the host, not the host's core count.** The same N with the only
difference being what the run is told:

| N | `PANDORA_CPUS` | `execute` median (s) | host cpu PSI some avg10 max/mean | loadavg max/mean | MemAvailable min |
| --- | --- | --- | --- | --- | --- |
| 2 | **2** (host/N) | **68.0** | 32.40 / 26.85 | 4.06 / 3.12 | 10,049 MiB |
| 2 | 4 (host count) | 108.0 | 88.85 / 62.86 | 16.29 / 14.62 | 6,133 MiB |
| 3 | **1** (host/N) | **84.0** | 51.66 / 46.28 | 7.18 / 5.92 | 8,411 MiB |
| 3 | 4 (host count) | 139.2 | 89.34 / 74.21 | 21.07 / 17.96 | 4,154 MiB |

Telling every run it has the whole box costs **59 % at two lanes and 66 % at
three**, and takes load average from 4 to 16 and from 7 to 21. CPU being soft
does not make oversubscription free: `cpu.weight` decides who wins a
contended slice, it does not stop a run from starting four `tsc` processes
and a `vitest` pool per lane. The scheduler knows N and the run does not, so
the driver must tell it — `PANDORA_CPUS = max(1, host_cores // lanes)` is the
rule these numbers support.

Two caveats. The hint only matters for jobs that read it (turbo's
`--concurrency`, vitest's pool size, `make -j`); nothing enforces it, which is
the point of CPU-soft. And this is one journey shape on a 4-core box; a
16-core worker with the same three lanes would leave cores idle under
`host // lanes` and probably wants `max(1, host // lanes)` with a floor of 2.
**NOT RUN:** anything other than 4 cores.

---

## 7. Admission

`admission.py`, ~180 lines, no ledger, no locks, no attempt identities — the
policy and its tests only, as asked.

```
reservation = clamp(p95(observed peaks) × 1.25, floor 512 MiB, class ceiling)
admit while  sum(reservations of running) + reservation ≤ host budget
```

- **Size classes** are the operator's decision and cap what learning can do:
  `small 1024 / medium 4096 / large 8192 / xlarge 12288` MiB. The class
  ceiling is *also* the run's cgroup `memory.max`, so the two numbers the
  scheduler and the kernel enforce come from one place.
- **Cold start** — fewer than 3 recorded peaks — reserves the **class
  ceiling**. Deliberately pessimistic: the first runs of an unknown job are
  the ones most likely to surprise, and over-reserving delays a run whereas
  under-reserving oversubscribes the host and slows every run on it.
- **p95 is nearest-rank**, so it is defined for one sample and deterministic.
  History is the last 50 peaks per (repo, job), in SQLite so a worker restart
  does not forget.
- **Over the reservation, under the ceiling: allowed.** The cgroup never
  refused anything; the run finishes, its peak is recorded, and the next run
  of that (repo, job) reserves more. `finish()` returns
  `over_reservation: true` for the record.
- **At or over the ceiling: killed as `oom`.** The peak is stored but
  `Store.peaks` excludes `oom` outcomes from what reservations are learned
  from — a run killed at its ceiling only tells you it wanted more than the
  ceiling, which is an operator decision, not a learned one. Tested: five
  consecutive OOMs leave the reservation unchanged.
- Refusals name their reason and show the arithmetic (`held_mib`,
  `budget_mib`), so a queued run has a legible explanation.

40 unit tests in `test_admission.py`, 20 in `test_incus_driver.py` for the
cgroup parsing, fingerprints, name validation, receipt logic and the watchdog
thresholds (each threshold test is pinned against the measured hog and
measured healthy values above, so a later tuning change has to face the
evidence). `python3 -m unittest discover` in `experiments/executor/`: 60
tests, all passing, no worker required.

---

## 8. Destroy receipts

`destroy` returns a `Receipt` that is `clean` only if four things are true and
no leftovers were named:

| Checked | How |
| --- | --- |
| instance gone | `incus info <name>` fails |
| storage volume gone | absent from `incus storage volume list <pool>` |
| veth gone | `volatile.eth0.host_name` absent from `ip -o link` |
| cgroup gone | `/sys/fs/cgroup/lxc.payload.<project>_<name>` absent |

Anything else raises `DestroyIncomplete` carrying the receipt. Every run in
this note returned a clean receipt; destroy times are in the tables.

Against the Firecracker spike, where a `SIGKILL` left four named host objects
to reap (netns, dm device, loop devices, COW file), an Incus run leaves
**none** — and the receipt proves it per run rather than by a periodic sweep.

---

## 9. Host setup

`setup.sh init`, all of it named after Pandora and removable by
`setup.sh teardown`:

- **btrfs pool on a loop file** — `~/incus-exec/pool.img`, 18 GiB, `losetup`
  to `/dev/loop0`, `incus storage create pandorapool btrfs source=/dev/loop0`.
  The host root is ext4 so there is no reflink; btrfs is what makes
  `incus copy` a snapshot rather than a copy.
- **A dedicated bridge** `pandorabr0` on 10.141.0.1/24, `ipv4.nat=true`,
  `ipv6.address=none`, plus two explicit `iptables -I FORWARD` ACCEPTs,
  because the host's FORWARD policy is DROP with only Docker's jumps
  installed. The host dockerd and its bridges were not touched.
- **Project `pandora`** with `features.images/profiles/storage.volumes=true`
  and `features.networks=false` (the bridge lives in the default project).
- **Profile `runner`**: root disk on the pool, `eth0` on the bridge, and four
  keys — `security.nesting`, `security.syscalls.intercept.mknod`,
  `security.syscalls.intercept.setxattr`, `security.idmap.isolated`.

Two things the Firecracker spike warned about were still true and are handled:
the managed dnsmasq answers AAAA on an IPv4-only bridge (the golden build
writes `Acquire::ForceIPv4` and uses `curl -4`), and the host FORWARD policy
is DROP.

One thing it recorded is **no longer true on this Incus version**: the
instance cgroup is at `/sys/fs/cgroup/lxc.payload.<project>_<name>`, not
`incus.slice/incus-<name>.scope`. The driver discovers it rather than guessing.

---

## 10. The canary

`canary.py` — 20 checks, 93 s, exit code = number of failures. This is what
would gate a worker image rebuild.

```
ok   incus present                         0.0s 6.0.5
ok   project pandora exists                0.0s
ok   pool pandorapool exists               0.1s
ok   golden golden-2775adec0be404dd ready   0.1s reused
ok   clone under 2s                        0.5s 0.07s
ok   start under 5s                        0.5s 0.30s
ok   cgroup arrangement written            0.5s {"memory.swap.max":"0","memory.high":"4831838208","memory.oom.group":"1"}
ok   source injected                       1.0s 0.51s
ok   nested dockerd up                     2.2s overlayfs 2
ok   compose stack up                      6.2s 3 containers
ok   fixed ports bound inside the run      6.2s 5 listeners
ok   compose stack down                    7.3s 0 containers left
ok   journey S0-01 passes                 60.7s outcome=ok exit=0 in 53.3s
ok   run stayed under its ceiling         60.7s peak 3856 MiB of 5120
ok   soft limit was crossed without killing the run  60.7s peak 3856 MiB vs reservation 3800
ok   destroy receipt clean                61.6s in 0.92s, leftovers=[]
ok   over-ceiling run is killed as oom    92.1s outcome=oom reason=memory-thrash
ok   oom verdict within 60s               92.1s 30.0s
ok   oom verdict carries evidence         92.1s throttle_events_per_second 1073.0, thrashing_seconds 15.5
ok   oom run destroyed cleanly            93.2s 0.99s

0 checks failed
```

Three of these are the ones worth having. `compose stack up` brings eichler's
own `tools/stack/compose.yml` up on the run's private dockerd with its fixed
ports and takes it down again — the property the whole design rests on.
`soft limit was crossed without killing the run` catches a regression where
the watchdog becomes trigger-happy: this run peaked at 3,856 MiB against a
3,800 MiB reservation and was not touched. And `over-ceiling run is killed as
oom` catches the opposite regression, which §4 spent most of its time on.

Separately verified, because the mixed test depends on it:
`limits.cpu.allowance=100%` writes `cpu.weight=100` and leaves
`cpu.max = "max 100000"` — a weight, not a quota, as claimed.

---

## 11. Lines of code

| File | Lines |
| --- | --- |
| `incus_driver.py` | 591 |
| `interface.py` | 185 |
| `admission.py` | 183 |
| `setup.sh` | 86 |
| **production subtotal** | **1,045** |
| `test_admission.py` | 225 |
| `test_incus_driver.py` | 171 |
| `canary.py` | 131 |
| **test and gate subtotal** | **527** |
| `poc.py`, `memtest.py`, `bench.py` (measurement only) | 545 |
| **total** | **2,117** |

For scale: the Docker API proxy POC was 1,152 production lines plus 884 test,
and the Firecracker spike was ~600 lines across 14 scripts plus a kernel and
rootfs pipeline. The four production files here are 1,045 lines, and
roughly half of `incus_driver.py` is the watchdog and the receipt —
i.e. the two things §4 and §8 showed were not optional.

---

## 12. What is left on the worker

`ubuntu@WORKER`, left deliberately so the owner can continue from the
golden. **No run instances are running and none exist**; `ip -o link` shows
zero veths and `/sys/fs/cgroup` has no `lxc.payload.*`.

| | State |
| --- | --- |
| Incus | **installed**, 6.0.5 from the Ubuntu archive; `systemctl is-active incus` → `active` (socket-activated, `is-enabled` → `indirect`) |
| Instances (project `pandora`) | `golden-2775adec0be404dd`, **STOPPED**, with snapshot `warm` |
| Volumes | that container, its `warm` snapshot, and the `ubuntu/26.04` image volume — nothing else |
| Storage pool | `pandorapool`, btrfs, on `/dev/loop0` → `~/incus-exec/pool.img` (18 GiB sparse, **4.9 GiB on disk**) |
| Pool contents | golden 4,258,406,400 B referenced / 57,344 B exclusive; snapshot 53,248 B exclusive; image 566,255,616 B |
| Network | `pandorabr0`, 10.141.0.1/24, `ipv4.nat=true`, `ipv6.address=none`, plus two `iptables -I FORWARD` ACCEPT rules for it |
| Project | `pandora` (`features.networks=false`, `features.images/profiles/storage.volumes=true`) with profile `runner` carrying `security.nesting`, `security.syscalls.intercept.mknod`, `security.syscalls.intercept.setxattr`, `security.idmap.isolated` |
| `~/incus-exec/driver` | 260 KiB — this experiment directory |
| `~/incus-exec/eichler` | 375 MiB — eichler's **tracked files only**, shipped with `git ls-files -z \| rsync --from0 --files-from=-`. No `.env`, no `.dev.vars`, no keys; `.git` was not shipped either |
| `~/incus-exec/logs` | 4.1 MiB — `poc.jsonl` (every measurement in this note), per-run journey logs, `memtrace-*.json` |
| `~/incus-exec/out` | 4.1 MiB — collected artifacts from `collect` |
| Disk | 29 GiB free of 96 GiB |

**Removed, as permitted:** `~/spike-fc` (11 GiB, the Firecracker spike's
binaries, kernel, rootfs and warm base) was deleted to make room for the pool.
`~/pandora-warm` (1.9 GiB, v0.1.1) was **not** touched. The host dockerd, its
configuration and its images were not touched, and `~/spike-proxy` was not
touched.

`bash ~/incus-exec/driver/setup.sh teardown` removes the project, the profile,
the golden, the bridge, the pool and the loop device, and leaves the package
installed.

**Not persistent across a reboot:** the loop device is attached by hand in
`setup.sh init`, so after a restart the pool is gone until `init` is run
again. See §13.

---

## 13. What would block this as v0.2's executor

In the order I would fix them.

1. **The watchdog's thresholds are tuned against one hog shape on one host.**
   500 refused charges/s, PSI full avg10 ≥ 2 %, 15 s sustained, 5 s smoothing.
   They are pinned in `test_incus_driver.py` against the measured hog and the
   measured healthy values, so a change has to face the evidence — but the
   evidence is one repo on a 4-vCPU box. A slower host, a faster disk, or a
   job that legitimately sits at its ceiling for 15 s have not been tried.
   This needs a soak across several repos before it kills real runs.
2. **No per-run disk quota.** This is the memory problem again with no work
   done on it at all: btrfs qgroups are read for measurement, nothing limits
   what a run writes, and a run that fills the pool takes its neighbours with
   it. `incus config device set <name> root size=` plus a pool-level qgroup is
   the obvious answer and is **NOT RUN**.
3. **No golden garbage collection.** One golden per fingerprint at ~4 GiB
   referenced, in an 18 GiB pool. No LRU, no "how many fit", no eviction under
   pressure. A branch that changes `pnpm-lock.yaml` mints a new golden.
4. **The pool is a loop file and does not survive a reboot.** `setup.sh init`
   runs `losetup` by hand and nothing re-attaches it at boot, so after a
   restart the pool is missing and every instance is unusable. A real worker
   wants a real device or a systemd unit. **NOT RUN:** reboot.
5. **Reattach is written but never exercised.** `execute(reattach=True)`
   re-attaches to the detached run's log and exit-code files, and the run is
   genuinely detached (its parent is the instance's init), but no test killed
   the driver mid-run and resumed. **NOT RUN.**
6. **Admission keeps `running` in memory.** The learned history is in SQLite
   and survives; what is currently admitted does not. A driver restart
   forgets its own reservations. The v0.1.1 ledger solves exactly this and
   was deliberately not ported.
7. **The bundle ship is `rsync`, not the content-addressed one.** v0.1.1's
   `worker_bundle.py` verifies every file against a digest before an attempt
   uses it; this POC does not. **NOT RUN.**
8. **Nothing is pinned.** `prepare` pulls `images:ubuntu/26.04` from the
   remote image server and `apt-get install` takes whatever the archive has
   today, so two goldens with the same fingerprint built a week apart are not
   the same machine. The fingerprint is honest about the *description* and
   silent about the *result*.
9. **`security.idmap.isolated=true` consumes a subuid range per instance.**
   Not measured; at some instance count the allocation fails and the failure
   mode is unknown.
10. **`collect` has no size limit and no streaming.** It shells
    `incus file pull -r` into a directory. A run that produces a large
    artifact tree has not been tried.
11. **One architecture, one distro.** x86_64 Ubuntu 26.04 only. **NOT RUN:**
    arm64, any other base image, any other repo than eichler.
12. **`MemoryExceeded` is declared and never raised.** The driver returns
    `Result(outcome='oom')` instead, because a killed run still has usage and
    evidence worth returning. Either the exception should go or `execute`
    should raise it; leaving both is the kind of ambiguity that gets a caller
    wrong later.
