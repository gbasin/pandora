# Pandora DESIGN.md (v0 + amendments) — red-team critique

Reviewed against the amended decisions: derived workspace_id including branch, no promotion, base-subvolume + rsync-delta head creation, blocking `pandora run` with 8m `--max-wait`, structured results with reporter parsing + artifact rsync-back, `git ls-files -co --exclude-standard` sync scope, no limits in MVP, rsync/SSH + token HTTPS API, `EICHLER_VALIDATION_DIRECT=1` PR, rootful dockerd with `pandora` in the docker group.

---

## 1. Agent-UX

**1.1 — The "still running" exit-0 path is a footgun. Severity: should-fix.**
The amended contract: `--max-wait` expires → exit 0, print "still running, id=...". An agent's tool-call timeout will usually cut the connection *before* pandora's own timer fires, so the more common path is "process killed, output truncated, run id possibly never seen." The run id is printed as the first line, which is good, but only helps if the agent captured partial stdout — many harnesses discard output of a killed call. Two fixes, both cheap: (a) write the run id to a deterministic local file (`./.pandora/last-run` or `~/.pandora/worktrees/<id>/pending`) *before* submitting, so `pandora ps` can recover it even after a hard kill; (b) make `pandora run` default to printing the id to **stderr unbuffered** before any log line. Without this, agents will leak orphan runs and then resubmit duplicates — exactly the failure mode eichler's local-validation.md documents for Pueue ("do not resubmit an invocation just because the client timed out"). Pandora needs the same receipt discipline.

**1.2 — Exit-0-on-timeout inverts the usual convention; agents will misread it. Severity: should-fix.**
Agents pattern-match exit codes. `exit 0` + a text line is the worst possible encoding: a lazy agent sees success and reports "tests passed." Prefer exit code `124` (timeout convention) or a distinct code with the id on the last line in a machine-parseable `pandora: still-running id=…` marker. `--wait-forever`/`--detach` are fine; the ambiguous middle state is the problem.

**1.3 — No way to discover "what commands exist / which are heavy." Severity: should-fix.**
Eichler's agent contract is `pnpm validate <suite>`; the natural translation is `pandora run -- pnpm validate <suite>`, which works. But pandora advertises per-command memory classes in `.pandora.toml` ("map validate postgres|journey|… to 8g") — a mapping the *agent* can't see unless it reads the toml, and can't verify took effect. Add `pandora plan <cmd>` (dry-run: resolved mem/cpus, sync file count, estimated cold/warm) or at minimum echo the resolved class into the run's first output. Agents will otherwise guess `--mem` flags or, worse, discover the flag and set `--mem 64g` on everything.

**1.4 — Fan-out ergonomics are under-specified. Severity: should-fix.**
"Run `pnpm check` and affected tests before opening the PR" (AGENTS.md) is typically 3-5 commands. The contract forces: N×`--detach`, capture N ids (from stdout, per 1.1 fragile), then `pandora wait id1 id2 …`. Two gaps: (a) `wait` exiting non-zero "if any failed" doesn't say *which* — it must print per-id status, else the agent re-queries N times; (b) no batch submit (`pandora run -- pnpm check :: pnpm test:unit foo.test.ts`) means each submission does its own rsync against a moving local tree — fork N may sync a different source state than fork 1, and `result` won't tell the agent the runs saw different code. Include the synced source fingerprint in every result so the agent can detect this.

**1.5 — Wrong-directory / detached-HEAD behavior. Severity: nit.**
"Works from any subdirectory of a worktree" is good. Unspecified: running outside a git worktree entirely (error message must name what it probed — origin URL, toplevel), and running in the *main checkout* — eichler forbids working there but agents will do it; a main-checkout workspace is just another workspace, fine, but the error/help text should distinguish "not a repo" from "repo but no pandora config" from "SSH key not enrolled." First-run UX (`pandora run` before any `pandora init`) needs a single clear bootstrap path — the design never defines enrollment.

**1.6 — `result` content: right direction, missing two fields. Severity: should-fix.**
Failing test names + messages + log tail + artifact globs is exactly what a fixing agent needs. Missing: (a) **which command index failed** — eichler's `check` is ~10 sequential commands (plan.mjs docs+code arrays); "command_failed" without the argv and per-command boundary markers in the log forces log spelunking anyway. Emit `failed_command: {index, argv}` and delimit stdout per command. (b) **the source fingerprint** (per 1.4). Also: parsing Vitest/Jest/Playwright JSON requires those reporters to be requested — eichler's validation *already* injects `--reporter=json --outputFile` (run.mjs:70-73) into `tests.json` under the job dir; pandora should read that file rather than parse stdout. Note the job dir path is `tmp/validation/<uuid>` inside the worktree — declare it as an artifact glob in `.pandora.toml`.

**1.7 — Idempotency: none specified. Severity: nit.**
A retry of `pandora run` is a new run — fine. But a retry of the *submit API call* after a network blip creates two runs (same as the Pueue "submission may have reached the daemon" bug eichler documents). Give submit a client-generated idempotency key (uuid in request) — trivial in SQLite, prevents duplicate billing/runs later.

---

## 2. Eichler integration — checked against the code

**2.a — `EICHLER_VALIDATION_DIRECT=1` is correctly scoped, and `GITHUB_ACTIONS=true` was correctly rejected. Verified.**

- `validate.mjs:84`: `direct = GITHUB_ACTIONS === 'true'` selects `execute()` inline vs `submit()` to Pueue. The new flag must gate *this line only* — run `execute(request, dir, /*queued=*/false)`.
- `plan.mjs:71` (surface), `plan.mjs:142` (journeys), `plan.mjs:158` (postgres): under `GITHUB_ACTIONS`, commands become `pnpm --filter @eichler/<app> test:e2e|journey|test:postgres` directly — **no ephemeral Compose stack is booted** (that's `heavy.mjs`'s job), and surface runs on the fixed CI port. On the pandora box these would fail or collide. The new flag must NOT take these branches: keep `node tools/validation/heavy.mjs` / `surface.mjs`.
- `plan.mjs:129`: `--update` rejected under `GITHUB_ACTIONS||CI`. With `EICHLER_VALIDATION_DIRECT`, `--update` remains legal — but note it writes `result.outputs` and status `updated`, which pandora must treat as a distinct outcome (it rsyncs fixtures back; the design's artifact glob covers this if `.pandora.toml` includes `packages/scenarios/fixtures/**` for update runs).
- `stack.mjs:297-301`: `direct` = `--foreground` || `GITHUB_ACTIONS` || `CI` || `PUEUE_WORKER_ID`. On the pandora host none of these are set, so `pnpm dev:stack` would try to submit to a nonexistent Pueue daemon. Either agents don't run dev:stack remotely (likely — it's an interactive tool) or pandora sets a passthrough. Flag it in docs; don't set `CI=true` blindly because `plan.mjs:129` and other gates read it.
- `run.mjs:85-86`: every child gets `EICHLER_VALIDATION_ACTIVE` — nested `pnpm validate` inside a run is rejected. Good: prevents a fork from trying to queue.
- **`safeEnvironment` (state.mjs:83-105)** strips everything except an allowlist when submitting to Pueue. Under DIRECT, `execute()` inherits `process.env` — meaning pandora must provide `DOCKER_HOST`/`DOCKER_CONTEXT` if dockerd isn't on the default socket, and the env allowlist question moves to pandora's own spawn environment.

**2.b — `check-worktree-deps.mjs` breaks on a snapshot at a different path. Severity: blocker.**

`installationBelongsToRoot` (lines 58-74) reads `node_modules/.pnpm-workspace-state-v1.json` and requires `realpathSync(project) === realpathSync(root)` for some entry. If `pnpm install` ran in `base` at `/work/base/eichler` and the run executes at `/work/runs/<id>/eichler`, the recorded path ≠ run path → `dependencyReadinessProblems` reports "Dependency metadata does not identify a complete installation" → every `pre*` script (prebuild, pretest, prelint, …, and `validate` itself, package.json:10-40) exits 1. **Every pandora run of any pnpm script fails.** `installedLockMatches` (38-56) is path-independent and fine.

Fix options, in order of preference:
1. **Stable mount path per run**: bind-mount the fork at the same path the install ran at — i.e., always execute at `/work/exec/eichler` (or per-workspace `/work/ws/<id>/current`) regardless of which subvolume backs it. Runs in a fork: `mount --bind fork /work/ws/<id>/current`. This also fixes any other path-sensitive state (`.turbo` logs, `tsbuildinfo` is content-addressed but vite caches can embed paths). Requires privileged mount in the run sandbox — fine for systemd-run units, awkward inside an unprivileged container; pandorad must own the bind mount.
2. Install at the *head* path (`/work/ws/<id>/head`) and snapshot head→run subvolume, then run **in the head path via bind mount** — same trick.
3. Patch eichler to accept `EICHLER_WORKTREE_ROOT_OVERRIDE` — avoids a mount dependency but adds repo surface area and doesn't fix other tools that embed realpath (pnpm's own `modules-dir` checks, vite `cacheDir`).
Recommend (1): it makes "the run sees a stable path" a pandora invariant, which also protects future repos.

Related: pnpm's own behavior — the workspace state file is keyed by *importer* path; pnpm itself doesn't realpath-check, but `--frozen-lockfile` re-resolution happens relative to cwd, so running `pnpm install` inside the fork (the "prepare" step when lockfile changed since base) writes correct-for-fork state only if run from the bind-mounted stable path. Same fix covers it.

**2.c — Shared `TURBO_CACHE_DIR` across worktrees: works, with caveats. Severity: nit.**

Turbo 2.5 hashes inputs by content relative to the workspace root; `inputs`/`outputs` in turbo.json are repo-relative, and `$TURBO_ROOT$` is explicitly the *portable* way to reference root files (it hashes the target's content, not its absolute path). So a typecheck task in worktree A and identical-content worktree B produce the same hash → cache hit. Cross-worktree sharing works. Caveats:
- `@eichler/api#typecheck` outputs `.wrangler/export-validator/**` and brief/mockup output `src/catalog.generated.ts` — these are generated files restored from cache on hit. `.wrangler/` and `*.generated.ts` are presumably gitignored → excluded from rsync → not present in head → turbo will restore them on cache hit or regenerate on miss; either is correct, but `check`'s catalog validation *depends* on them existing (local-validation.md:101: "Catalog generation runs before catalog validation"). Ensure `.pandora.toml` does not `run` the catalog validator before the typecheck task that produces it — or just rely on `turbo run typecheck` ordering, which handles it.
- Concurrent writes to one `TURBO_CACHE_DIR` from many forks: turbo uses file locks but busy-box contention with 10+ forks can stall; acceptable, monitor.
- Env/`.env` hash inputs: if any task hashes `/.env*` (turbo hashes `.env` by default for affected tasks), secret exclusion means hash differs from local runs — harmless (remote cache is its own universe) but worth noting.

**2.d — Ports and Compose naming under N forks: mostly fine. Severity: nit.**

- Compose projects: `ike-validation-<uuid8>` (instance.mjs:151) — collision-safe.
- Postgres/pgbouncer host ports: `127.0.0.1::5432` / `::80` (compose.ephemeral.yml) — Docker-assigned ephemeral, collision-safe.
- API port: `apiPort = 0` → ephemeral. Good.
- Surface suites: `surface.mjs` picks a free loopback port, then **releases it before Vite binds** (documented TOCTOU in its own comment). With 10+ concurrent runs the race window is real; `--strictPort` turns a loss into a failed run, not a silent move — so worst case is flaky `command_failed`, acceptable but worth a retry hint in the outcome class.
- **Real constraint**: workerd/miniflare + a Postgres container + Playwright per fork — see §5.
- `pruneProjects(VALIDATION_PROJECT_PREFIX)` (stack.mjs:272, instance.mjs:85) removes leftover projects by name prefix — pandora's reconciler should reuse the same prefix convention (`pandora-<run-id>` label is planned; also prune `ike-validation-*` older than X) or leaked instances accumulate.

**2.e — macOS assumptions in eichler:**

- `heavy.mjs:83`: `native` suite throws on `process.platform !== 'darwin'` — already gated, runs stay local. Fine; document that `pandora run -- pnpm test:ios` fails fast with a clear error (it will, via that throw — good).
- `tools/onboarding/install-managed-mac-toolchain.sh`, `device-lease.py`, simctl/Expo flows — local-only, never on the box.
- `groupAlive`/`signalGroup` comments note macOS EPERM quirks (instance.mjs:46) — Linux semantics differ subtly but the code handles both.
- `safeEnvironment` passes `DEVELOPER_DIR`, `JAVA_HOME` — harmless absent on Linux.
- Biggest implicit Mac assumption: **`pnpm dev:stack` and `wrangler dev` workflows** assume local dev ports 8787/5173/5174 — not a pandora problem (design excludes dev servers), but agents instructed to "replace pnpm install && pnpm check with pandora run" may also try `pandora run -- pnpm dev:stack` to iterate. Decide: document "dev servers stay local" or provide port-forward. At minimum the error should be legible.

---

## 3. btrfs / storage operator

**3.1 — Fork cost claims are right-sized but deletion is not O(1). Severity: should-fix.**
Snapshot creation is O(1) metadata. `btrfs subvolume delete` of a 1.2 GB `node_modules` tree (pnpm's many-small-files layout, ~100k+ inodes) is *not* free: extent-tree cleanup runs in the background cleaner thread, and under 10-20 concurrent forks being created/deleted per minute, deletions queue and `btrfs` metadata (the fs-tree) grows. Real risk is metadata fragmentation, not latency per se. Mitigations: mount with `-o ssd_spread` defaults are fine; enable `btrfs quota`/qgroups per workspace to bound a runaway fork (a run writing 50 GB of test output otherwise fills the fs — btrfs with shared extents makes `df` lie; **qgroups are the only honest accounting**, and they have their own performance cost — measure). "Delete the fork (async)" should be a *throttled* reconciler, not inline.

**3.2 — Postgres data: correctly outside the fork, but check where dockerd puts it. Severity: nit.**
Design: `/work` on btrfs; test DBs are Docker named volumes. If `/var/lib/docker` is also btrfs, Docker picks the **btrfs graph driver** (slow for image layer ops vs overlay2; container layer COW churn on Postgres images is exactly btrfs's worst case: fsync-heavy small writes inside COW files → write amplification). Recommendation: keep `/var/lib/docker` on ext4/xfs (or a separate LV), or force `overlay2` storage-driver — overlay2-on-btrfs works but lowerdirs on a COW fs still double-COWs image writes. Postgres fsync correctness is fine on either; it's a throughput issue.

**3.3 — Disk full on btrfs is uglier than ext4. Severity: should-fix.**
ENOSPC on btrfs can hit at "85% used" due to metadata allocation (`btrfs fi df` truth vs `df` lie). The daemon must watch **btrfs allocation**, not statfs, and `infra_failed`-class the run rather than letting rsync/pnpm die with cryptic ENOSPC. Add: periodic `btrfs balance` (metadata only) or `btrfs filesystem usage` alerting; reserved slack; head GC order = oldest-unused workspace first, never the base.

**3.4 — GC policy needs the deletion order specified. Severity: nit.**
Deleting a head subvolume while a run's fork is open is fine (snapshot persists), but deleting `base` while a new workspace snapshots it races (see §4). Also: nested subvolumes — if `node_modules/.pnpm` or anything is itself a subvolume it won't be snapshotted (snapshots don't descend into child subvolumes); ensure installs never create subvolumes (they won't — only `btrfs`/`systemd-nspawn`/machinectl do), and that Docker volumes can't land under `/work`.

**3.5 — fsync/Postgres inside forks: moot** (DB lives in Docker volumes, not the fork) — the design got this right; flag only that nothing else fsync-heavy (SQLite state of pandorad!) should live on the same btrfs without `chattr +C` or nodatacow consideration — actually SQLite on btrfs COW is *correct* but slow; put pandorad's SQLite on the root ext4 or use `NOCOW` attribute on its dir.

---

## 4. Base-tracking

**4.1 — Race: base moving while a workspace snapshots it. Severity: should-fix.**
"Fetch + install when base moves" vs "snapshot base → rsync delta." If the base updater swaps base content mid-snapshot — `btrfs subvolume snapshot` is atomic w.r.t. the fs, but if the updater mutates base in place (rsync into base, `pnpm install` into base), a snapshot taken mid-install captures a torn `node_modules` (half-written `.pnpm-workspace-state-v1.json` → fails `installationBelongsToRoot` even at the right path). Fix: updater builds `base.next`, then **atomic swap**: snapshot `base.next` → rename; workspace creation only ever snapshots a *committed* base generation. Version base (`base@gen<N>`) and record `base_gen` in the workspace so staleness is observable (`pandora reset` → snapshot latest gen).

**4.2 — Stale-base branches. Severity: nit — behavior is correct, cost isn't measured.**
Agent's branch from a 2-week-old main: rsync delta is just file content (correct), but lockfile diff vs base's installed `node_modules` forces a full `prepare` install into head on first run — 15-30 s per the design, fine — **except** `pnpm install --frozen-lockfile --prefer-offline` into a head whose node_modules came from a different lockfile works but rewrites state; subsequent runs of *that workspace* stay warm. Edge: two workspaces of the same user on divergent lockfiles each pay install once — fine. The claim holds; just instrument "cold vs warm" per run.

**4.3 — Deploy key hygiene. Severity: nit.**
Read-only deploy key on the box is right. It must be scoped to the single repo (a repo-scoped deploy key, not a user SSH key), non-rotate-forgetting, and fetch should be `git fetch --depth` bounded — the box doesn't need history, only `origin/<default>` tree + `.git` presence for fingerprint (see §6.1). Note: `git fetch` on a shallow/bare base is fine, but `fingerprint()` needs `git rev-parse HEAD` **in the worktree** — decide whether head carries a real `.git` (see 6.1).

**4.4 — base tracking cadence**: who triggers fetch — cron, webhook, or lazy-on-run? Lazy adds minutes to a run; cron adds staleness. For MVP: fetch on daemon start + every 15 min, snapshot on demand.

---

## 5. Concurrency with NO cap

Concrete prediction for a 64 GB / 16-core box, 12 concurrent forks of eichler heavy suites:

- Each heavy fork: Postgres container (~0.5-1 GB active), pgbouncer, workerd API (~0.5 GB), Playwright Chromium workers (0.8 GB each × up to 4), plus the node/vitest parent (~1-2 GB). Realistic 4-7 GB per heavy run → **12 forks ≈ 50-80 GB → OOM-killer, not scheduler, decides.** The outcome class `oom` exists but with no cgroup `memory.max`, the *kernel* OOM-killer picks the victim — possibly pandorad or dockerd, not the fat run. `memory.events` detection requires the cgroup the design deferred. **This is the sharpest contradiction in the amendments**: outcome classes promise `oom` classification while removing the mechanism that detects it.
- dockerd serialization: `compose up --wait` × 12 → image/network/container create are serialized through dockerd's locks; expect 30-90 s added startup, occasional `docker: pool overlaps` / iptables stalls. Docker network creation per project is cheap; ephemeral port allocation from `::5432` binds is fine (ephemeral range ~28k ports).
- pnpm installs into several heads sharing one store: pnpm hardlinks from the store; concurrent `pnpm install` processes on the same store are designed-safe (store has locks), but hardlink-heavy bursts on btrfs COW fs: hardlinks share extents — fine and actually cheap.
- CPU: vitest `--maxWorkers` comes from `workers()` = local config `{"workers":2}` — on the box this config **doesn't exist** (`~/.local/state/eichler-validation/config.json` is per-machine; `validate.mjs:90` passes `workers()` only in the non-direct path — check: direct path uses `plan(root, suite, args, direct ? 2 : workers())` → fixed 2 under DIRECT. Good — bounded by default.)

**Minimal safety valve recommendation**: skip cgroups if you must, but keep (a) a **global run cap** (e.g. 8 concurrent, FIFO queue — the SQLite job table makes this ~20 lines) and (b) run each execute under `systemd-run --scope -p MemoryMax=<declared>` anyway — it's one flag, gives you both OOM containment *and* the `memory.events` signal your outcome classes already promise. The design says "no limits" but the outcome taxonomy was built assuming them; MemoryMax is the cheapest way to keep the contract honest. Without it, the first bad day is a host-wide OOM taking down all runs + the daemon, all classified `infra_failed`.

---

## 6. Failure modes

**6.1 — Partial sync → fork of a half-synced tree. Severity: blocker.**
Sequence: `sync` rsyncs worktree → head (under per-workspace lock), then fork snapshots head. If the client disconnects mid-rsync, head is torn. Does the lock protect the *fork* step, or does a retry sync resume onto a torn head? rsync is resumable-ish but the first run's fork — if already taken — captured torn state. Design must specify: **fork only after a sync completes a commit marker** (e.g. rsync to `head.staging`, then `mv`/snapshot-swap atomically; or write `sync.complete` last and refuse to fork without it). And the bigger hole: `fingerprint()` in eichler's run.mjs:61 needs `.git` — `git ls-files -co` sync scope **does not include `.git`**, so `git rev-parse HEAD` fails on head and `pnpm validate` dies before running anything. Options: (a) also sync `.git` (worktree `.git` is a *file* pointing at the real gitdir — sync the real gitdir; size is repo history, significant but one-time + delta); (b) eichler PR lets `EICHLER_VALIDATION_DIRECT` skip fingerprint or accept a pandora-supplied source hash env var; (c) pandora computes an equivalent fingerprint client-side and injects it. Recommend (b)+(c): keep the drift check, source the hash from pandora's own sync manifest.

**6.2 — Host reboot mid-run. Severity: should-fix.**
Runs are in-flight state in SQLite + forks on disk + containers. On boot: mark all `running` runs `infra_failed` (agent sees clean class — good design already), delete stale forks, prune `pandora-*`/`ike-validation-*` compose projects, resume queue. btrfs survives reboot fine; the orphan-cleanup loop design covers it — just ensure it runs *before* accepting new submissions on boot.

**6.3 — Client disconnect mid-rsync / Ctrl-C during wait**: run continues server-side (correct — the whole point). Agent recovers via `pandora ps` + the pending-run file (1.1). `pandora cancel` must exist and work — it does.

**6.4 — Clock skew** (agent Mac vs host): run ids and ordering should be server-assigned monotonic; durations server-measured. If any TTL/lease uses client time it breaks — spec should say "all timestamps server-side." Nit.

**6.5 — Disk full**: covered in 3.3 — must surface as `infra_failed`, and the daemon should refuse new syncs below a watermark rather than corrupting heads.

**6.6 — rrsync partial writes**: rsync writes into `head.staging` per 6.1 also fixes the "interrupted rsync leaves head readable-but-torn" case.

---

## 7. Security (single-tenant now, product later)

**Day-one musts:**
- **rrsync jail**: restrict to `-ro`? No — pandora needs write. rrsync in write mode jails path but is still "rsync as that user to anywhere under root": ensure the jailed root contains *only* workspace trees, not `.ssh`, not pandorad's SQLite, not tokens. Also `--protect-args`/no `--delete` outside staging? Client controls rsync flags → it can pass `--delete` and wipe other workspaces if the jail root is shared. **Use a per-workspace staging dir and server-side rsync invocation** (client uploads a file list/delta, server runs rsync) or at minimum force rsync options server-side via the SSH forced-command (`command="rrsync /work/sync-inbox/<workspace>"` per-key). Forced command per key is the clean jail; don't let the client choose the target path.
- **docker group == root**: stated knowingly, but write it down as a loaded gun: any run can `docker run -v /:/host` → full host compromise, read other workspaces, steal the deploy key. Single-tenant + trusted-agent makes this acceptable *only because the threat model is "agent bugs, not adversary."* The day untrusted code or PR content runs (product later), dockerd must move inside the isolation boundary (rootless docker per run, or microVM — conveniently the tier-2 path). Don't defer rootless-docker evaluation: rootless dockerd per-run would make docker group moot and is testable now.
- **Token API**: bearer token per user, hashed in SQLite, TLS. Must never accept: absolute paths, `..` segments in workspace/artifact glob params, shell strings (submit takes argv array only — never a string to `sh -c`), run-ids of other users (authorize by token→user→workspace binding).
- **Secrets**: sync scope excludes gitignored — `.dev.vars`/`.env*` are presumably gitignored so excluded by default; `.pandora.toml` allowlist is opt-in. Good. Also exclude `.git` hooks? `.git` sync (if added per 6.1) must not sync hooks/config containing credentials — sync gitdir minus `hooks/`, or set `core.hooksPath=/dev/null` on head.
- **Log rendering**: agent CLIs render logs raw — ANSI escape injection from test output could rewrite a terminal; low severity, but strip/sanitize control chars in `result` summary fields.

**Later:** per-user subvolume quotas (qgroups), network policy for runs (today a run can egress anywhere — product needs egress control), audit log, deploy-key → per-repo app tokens.

---

## 8. Tier-2 swap audit — does the client contract survive microVM-per-run?

Mostly yes; the *contract* is stable but several internals the design treats as free become expensive:

| Element | Survives? | Note |
|---|---|---|
| `pandora run/wait/logs/result/ps/cancel`, outcome classes | ✅ | The whole point of the contract — holds. |
| rsync-into-head workspace model | ⚠️ | MicroVM-per-run wants a **disk image checkpoint**, not a mutable head subvolume. Head becomes "base image + worktree delta" applied inside the VM. `pandora reset` still maps. |
| Shared `TURBO_CACHE_DIR`, pnpm store | ⚠️ | VMs don't share a host fs; caches become virtiofs mounts (fine) or image-baked (staleness). Contract unaffected; performance story unchanged in kind. |
| Shared dockerd | ❌ | Docker must run *inside* the microVM (docker-in-VM) or nested — solves the docker-group=root problem for free, but eichler's compose stack then boots inside each VM: slower, and `docker compose` ephemeral ports live inside VM netns — fine since nothing host-references them. |
| btrfs fork-per-run | ❌ | Replaced by snapshot-of-image or fresh boot + cache mounts. Fork semantics (frozen-at-sync) preserved by design. |
| `base` subvolume tracking origin | ⚠️ | Becomes base *image* rebuilds — heavier; the gen-versioning idea carries over. |
| `--max-wait`/still-running, id-first-line | ✅ | |
| Artifact rsync-back | ✅ | |
| cgroup `MemoryMax` outcome classes | ✅→better | VM memory is the limit; `oom` becomes VM-OOM. |

Verdict: the client contract survives cleanly — this part of the design is right. What doesn't survive is the *operational* investment: rrsync jails, btrfs GC, dockerd tuning, qgroups — all tier-3-specific. Which feeds §9.

---

## 9. Contrarian

**9.1 — Is fork-per-run still right without promotion? Severity: should-fix (challenge the default).**
Without promotion, a fork buys exactly one thing: isolation of a run from *subsequent syncs* (a sync during a run doesn't disturb it) plus garbage-free head. But notice: the amended base-tracking already gives you `head.staging → atomic swap`. If syncs are atomic-swap, then **the head is already immutable-per-version** — a run can just execute in `head@gen<N>` (a read-only snapshot taken per sync, not per run) and a per-run writable overlay (overlayfs upper on a tmpdir, or a snapshot — same thing, but now the snapshot is *one per sync version*, not one per run). Ten runs of the same synced state share one snapshot. This removes: per-run snapshot+delete churn, promotion logic entirely, and the "fork vs head" mental model — replaced by "versioned heads." Runs that need to write (`pnpm install` side effects, build outputs) get the overlay. This is strictly simpler and strictly less btrfs metadata churn. The counterargument: per-run forks let each run write into node_modules naturally — but with turbo cache shared, runs *shouldn't* mutate the tree anyway; the overlay catches mistakes. **Recommend: snapshot-per-sync-version + overlay-per-run.** If runs must mutate real node_modules (eichler's prepare step is the only one — and prepare runs against head intentionally), keep fork-per-run only for prepare.

**9.2 — Branch in workspace_id: right call, wrong reason. Severity: nit.**
`sha256(user, origin, realpath, branch)` — but agents rename branches (`git branch -m`), and branch-switching a worktree (`git switch`) changes identity silently → orphan workspace + cold start. That's "correct" (new branch ≈ new context) but the *real* invariant the agent cares about is "my worktree," and realpath already captures it since eichler = one branch per worktree (AGENTS.md mandates it). Keep branch in the hash only if you also handle `git switch` — otherwise it's orphan-generation for zero benefit on the first target repo.

**9.3 — Single simplification removing most risk: drop the mutable head entirely.**
"Head = staging area only; runs execute in versioned snapshots; prepare runs in a dedicated `install` snapshot" collapses: no promotion question, no partial-sync-then-fork bug (6.1), no "edits during run" question, and GC becomes "delete versions older than newest." Combined with 9.1 this deletes roughly half of §2's machinery. The second-best simplification: **no detached/async mode at all in v1** — `run` always blocks with `--max-wait`, `ps`+`logs`+`result` exist only for recovering killed calls. Async is where agent footguns live.

---

## Top 10 (ranked)

1. **check-worktree-deps realpath check breaks every run** (2.b, blocker): node_modules installed at `base`/`head` path fails `installationBelongsToRoot` when executed from a snapshot path. Fix: bind-mount each run at a stable per-workspace path so install path == run path.
2. **No `.git` in sync scope breaks `fingerprint()`** (6.1, blocker): `run.mjs` calls `git rev-parse/diff HEAD` — every `pnpm validate` dies on a head without gitdir. Fix: `EICHLER_VALIDATION_DIRECT` accepts a pandora-injected source hash, or sync gitdir (minus hooks).
3. **Partial-sync → torn fork** (6.1, blocker): rsync must land in `head.staging` with an atomic swap/commit marker before any snapshot; client disconnect must never leave a forkable torn head.
4. **Base-update race** (4.1, should-fix→blocker at scale): build `base.next`, snapshot only committed generations; a mid-install snapshot yields a torn node_modules.
5. **No-cap MVP contradicts the `oom` outcome class** (5, should-fix): kernel OOM-killer will pick victims host-wide. Minimal valve: global run cap + `systemd-run MemoryMax` (one flag, also restores oom detection).
6. **Run-id loss on tool-call kill** (1.1, should-fix): write pending-run file locally before submit; "still running" exit-0 → use exit 124 + machine-parseable marker. Otherwise orphaned runs and duplicate submissions, the exact bug eichler's docs warn about.
7. **Docker socket = root on host** (7, must-document day one): acceptable only under trusted-agent threat model; evaluate rootless dockerd now because tier-2/multi-tenant depends on it.
8. **Per-run fork churn on btrfs metadata + misleading disk accounting** (3.1/3.3, should-fix): throttled async fork deletion, qgroups or at least `btrfs fi usage` monitoring; ENOSPC → `infra_failed`.
9. **Snapshot-per-sync-version instead of fork-per-run** (9.1, should-fix): removes promotion machinery, halves snapshot churn, and fixes #3's ordering story in one move.
10. **Batch/fan-out contract** (1.4, should-fix): `pandora wait` must report per-id outcomes; embed source fingerprint in each result so agents can detect "my three runs saw different trees."
