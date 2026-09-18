# Pandora design (v1)

Remote execution for coding agents. An agent in a git worktree runs
`pandora run -- <cmd>`; the command executes on a remote Linux host against a
sealed copy of that worktree, in a private copy-on-write layer over a
workspace that is already warm with the repo's dependencies. Many agents in
many worktrees fan out to the same host and never think about local machine
load.

**Invariant:** a workspace may affect how fast a run executes; it must never
determine which source a run executes or whether its result is trustworthy.
Correctness comes from sealed inputs and provenance in results; workspaces,
snapshots and caches are only speed.

Tier: persistent host, CoW workspaces, cgroup-limited runs. MicroVM-per-run is
a later executor swap behind the same client contract (section 9). First
target repo: `eichler` (pnpm + turbo monorepo, Docker Postgres, Playwright).

v1 supersedes v0. Decisions were settled through a grill session and two
independent red-team critiques (`tmp/critique-sidekick.md`, ChatGPT review);
where v0 said otherwise, this document wins.

## 1. Agent-facing contract

```
pandora init                      enrol this machine (client UUID, SSH key, API token, host)
pandora run [flags] -- <argv...>   sync, run, wait; exit with the command's exit code
pandora run -c "<cmd>" -c "<cmd>"  one sealed input, N runs, waits for all, per-command summary
pandora wait <run-id...>          per-id outcome lines; non-zero if any did not pass
pandora logs <run-id> [--follow] [--tail N] [--range a-b]
pandora result <run-id> [--json]  provenance + diagnostics + artifacts (exit 0 if retrieved)
pandora ps [--all]                runs for this worktree, from the server
pandora cancel <run-id>
pandora plan -- <argv>            dry run: resolved cwd, limits, sync size, prepare state
pandora apply <run-id>            write a run's source changes back into the worktree
pandora reset                     new workspace epoch for this worktree; next run is warm-from-base
pandora capabilities --json       limits, retention, supported diagnostic adapters
```

Flags on `run`: `--detach` (print id, return), `--max-wait <dur>` (default
8m), `--wait-forever`, `--mem <size>` (default 12g), `--cpus <n>` (default 4),
`--timeout <dur>` (execution clock, default 20m), `--include <glob>` (opt an
ignored path into the sync), `--write-back` (apply source changes on success),
`--json` (machine mode: stdout is JSONL, raw command output goes to the log).

### Blocking with a safety valve

`run` blocks by default: first line of output is `pandora: run <id>
input <input-id>`, then streamed logs, then the summary, then exit with the
observed command exit code. Fanout is the point, so waiting must never lose a
run: agent tool calls have hard timeouts (often 10 min).

- The run id is written to `~/.pandora/pending/<worktree-hash>` before any
  network call, so a killed tool call can recover via `pandora ps`.
- At `--max-wait`, the CLI exits **124** with a final machine-parseable line
  `pandora: still-running id=<run-id>`. Exit 0 is never printed for an
  unfinished run.
- A transport disconnect never cancels a run; reconnecting resumes observation.

Three separate fields, never conflated: **observed command exit code**
(nullable; absent on host loss), **CLI exit code**, **outcome**:
`passed | command_failed | timed_out | oom | cancelled | infra_failed`.
Each outcome carries `layer` (command / run / host) and `evidence`
(e.g. `memory.events oom_kill=1`).

### Execution semantics

- `argv` is passed verbatim; no shell. Shell syntax needs an explicit
  `sh -c '...'`. stdin closed, no TTY, `--json` reserves stdout.
- The worktree root is discovered from `cwd`; the caller's relative cwd is
  recorded and the command executes there.
- Two `pandora run` calls are independent runs. The second does not see the
  first's outputs. Commands that depend on each other run in one run
  (`-- sh -c 'pnpm build && pnpm test'`) or rely on the repo's own task graph.
- Outside a supported worktree: fail before any upload with the probes that
  failed (git toplevel, origin, enrolment).

### Result

```json
{
  "schema_version": 1,
  "run_id": "r_...", "input_id": "s_...", "request_id": "q_...",
  "state": "finished", "outcome": "command_failed", "layer": "command",
  "command": { "argv": ["pnpm","validate","postgres","api"], "cwd": "." },
  "exit_code": 1, "cli_exit_code": 1,
  "failed_command": { "index": 3, "argv": ["..."] },
  "environment": { "runtime_id": "node24.x-pnpm12.3.4-...", "prepare_fingerprint": "p_...",
                   "workspace_generation": "v17", "base_generation": "gen42" },
  "limits": { "requested": {"mem":"12g","cpus":4}, "effective": {"mem":"12g","cpus":4} },
  "durations_ms": { "queue": 1200, "prepare": 0, "execute": 84000 },
  "diagnostics": {
    "status": "complete | partial | missing | unsupported",
    "adapter": "vitest-json",
    "summary": "1 failed, 212 passed",
    "failures": [ { "file": "...", "test": "...", "message": "...", "log_span": [12040, 14300] } ],
    "truncated": false
  },
  "log": { "path": "~/.pandora/runs/r_.../log", "bytes": 183211 },
  "artifacts": [ { "glob": "tmp/validation/**", "files": 4, "synced_to_worktree": true } ],
  "source_changes": { "files": 2, "patch": "~/.pandora/runs/r_.../changes.patch", "applied": false }
}
```

Diagnostics come from explicit adapters over run-owned report files (Vitest,
Jest, Playwright JSON). Eichler's validation already writes `tests.json`; the
adapter reads it. A missing or inherited report is `missing`, never "zero
failures". Summary fields are sanitised of control characters.

Logs and result JSON live under `~/.pandora/runs/<run-id>/` on the client.
The server is the source of truth for run state; the client dir is a cache.

### Source changes and write-back

Some commands mutate source: formatters, `journey --update`, codegen,
snapshot updates. After every run the server diffs client-owned paths in the
run's writable layer against the sealed input and stores the result as
`changes.patch`. `--write-back` (or `pandora apply <id>`) applies it to the
worktree **only if the worktree still matches `input_id`**; otherwise it
refuses and leaves the patch. There is no implicit reverse sync of source.

A local `node_modules` is no longer required for validation. It remains useful
for IDE typechecking and quick local formatting; the agent decides.

### Idempotency and retries

`request_id` is generated client-side before any network operation. The
lifecycle is `begin(request_id) -> upload -> seal(input_id) -> submit`.
Resubmitting the same `request_id` with the same spec returns the same
`run_id`; with a different spec it is an error. Repeating a test on purpose is
a new request. A retry after `infra_failed` references the original
`input_id`; if it has expired, say so, never resync silently.

## 2. Workspace identity

```
workspace_id = sha256(account_id, client_instance_id, canonical_origin_url, realpath(worktree_root))
```

`client_instance_id` is a UUID in `~/.pandora/config` (so two Macs with the
same path do not collide). Branch is recorded as metadata, not identity, so
`git switch` / `branch -m` do not orphan a workspace. No file is written into
the repo or its git dir. Same-path reincarnation reuses caches; that is
harmless because sync reconciles source and `input_id` guards correctness.

`pandora reset` bumps the workspace **epoch**: new head from the current base
generation; runs already holding the old generation finish unaffected; the
old generation is garbage-collected when unreferenced.

No `--workspace <name>` in v1.

## 3. Sync: sealed inputs, explicit ownership

Three owners, never mixed:

| Owner  | Contents | Who writes |
|---|---|---|
| client | tracked files + non-ignored untracked files + `--include`/config allowlist | sync only |
| server | `node_modules`, `.git` (from base), prepared state | prepare only |
| run    | reports, build outputs, temp files, databases | the run, in its writable layer |

Capture:

1. Enumerate client-owned paths with `git ls-files -co --exclude-standard`
   plus allowlisted ignored paths. Symlinks pointing outside the worktree are
   rejected.
2. Build a manifest: path, size, mtime, mode, symlink target, content hash for
   small/changed files. `input_id = sha256(manifest)`.
3. rsync `--files-from=<manifest>` into the workspace head over SSH (rrsync
   jailed to that workspace's inbox, forced command per key).
4. Re-enumerate. If the tree changed during transfer, retry once, then fail
   `input_unstable`. Immutability is promised **after sealing**, not at the
   moment of invocation; this is stated in the docs.
5. Deletions come from diffing the previous sealed manifest against the new
   one, never from ignore patterns. Server-owned and run-owned paths are never
   touched by sync.

Secrets: ignored files stay home by default. Eichler's validation stack
generates its own throwaway secrets, so it needs no secret sync. A repo can
allowlist specific ignored paths in `.pandora.toml`; `.example` files are
tracked and travel normally.

## 4. Workspace model: base, head, generations, runs

All on one btrfs filesystem at `/work`. `/var/lib/docker` and pandorad's
SQLite live on ext4/xfs (CoW hurts both).

```
/work/repos/<repo>/base@gen<N>          read-only snapshot: clone of origin/<default>, installed
/work/ws/<workspace>/head               writable candidate, only pandorad writes here
/work/ws/<workspace>/v<N>               read-only snapshot = sealed generation
/work/ws/<workspace>/current            stable mount path every run executes at
/work/runs/<run-id>/upper               per-run overlayfs upper + work dirs (plain dirs)
```

**Base.** Per repo, a real git clone tracking `origin/<default branch>` via a
read-only deploy key. The updater builds `base.next` (fetch, checkout,
prepare), then publishes it atomically as `base@gen<N+1>`. Workspace creation
snapshots only a committed generation and records `base_generation`. Fetch on
daemon start and every 15 min; publish only on change.

**One lock over sync + prepare + seal.** Per workspace:

1. take lock
2. rsync client-owned paths into `head`, apply manifest-diff deletions
3. compute the **prepare fingerprint** = runtime versions (node, pnpm) +
   lockfile + every `package.json` + `pnpm-workspace.yaml` + `.npmrc` +
   `patches/**` + install flags + environment-profile version. If it differs
   from head's recorded fingerprint, run `pnpm install --frozen-lockfile
   --prefer-offline` in `head` (at the stable path), with a network-failure
   class and a timeout. Record the fingerprint only after success.
4. `btrfs subvolume snapshot -r head v<N>`; record `v<N>` as ready
5. release lock

A failed prepare leaves `v<N-1>` as the ready generation and the run is
reported `infra_failed` with layer `prepare`. The lock may be held ~30 s when
the lockfile changed; runs from other workspaces are unaffected. Queued runs
reference a sealed `(input_id, v<N>)`, never "whatever head holds".

**Runs.** Each admitted run gets `overlayfs(lower=v<N>, upper=/work/runs/<id>/upper)`
mounted at `/work/ws/<workspace>/current`. Identical mount path for prepare
and every run is a hard invariant: pnpm's workspace-state file and eichler's
`check-worktree-deps.mjs` compare recorded install paths against the running
path by realpath, and vite/tsbuildinfo caches embed paths. Ten runs of the
same generation share one snapshot. Cleanup is `rm -rf upper`; no per-run
subvolume deletion churn.

There is **no promotion**. Arbitrary command side effects never become the
base for later runs. Warmth comes from: `node_modules` in the generation,
shared pnpm store, shared turbo cache.

**Shared, repo-scoped caches** (outside every subvolume):

- pnpm store: shared per account. btrfs refuses hardlinks across subvolumes,
  so pnpm is configured with `package-import-method=clone` (reflink); this is
  verified on the box, and the install time claim (15-30 s) is measured, not
  assumed.
- turbo cache (`TURBO_CACHE_DIR`): shared per repo. Turbo hashes content and
  repo-relative paths (eichler uses `$TURBO_ROOT$`), so identical inputs in
  different worktrees hit. Turbo restores declared outputs only; it is not
  general incremental compiler memory, and we do not claim otherwise.
- Docker images: one host daemon. Databases, volumes and containers are
  run-owned (per-run Compose projects on ephemeral ports).
- Playwright browsers: versioned shared install; profiles are run-owned.
  Playwright's own browser GC is disabled on the box; pandorad owns retention.

**GC.** Reference-counted: a generation is deleted when no run references it
and it is not the newest; a workspace when unsynced for N days and epoch-less
of active runs; base generations when no workspace records them. Deletion is
throttled (btrfs reclaims asynchronously). Never delete anything an active
run references.

## 5. Host daemon (`pandorad`)

TypeScript/Node, runs as root (it mounts overlays and manages cgroups),
spawns commands as the unprivileged `pandora` user. SQLite state on ext4.

**Supervision.** Each run is a transient systemd unit
`pandora-run-<id>.service`, so a pandorad crash does not kill runs. Logs
stream to `/var/lib/pandora/runs/<id>/log`; the terminal result is written
durably before the run is announced complete. On start, pandorad reconciles
SQLite against live units, durable result files, mounted overlays, and
Docker resources labelled `pandora.run=<id>`, adopting survivors and marking
the rest `infra_failed`, **before** admitting new work.

**cgroup hierarchy** (cgroups v2, via systemd):

```
pandora.slice                         MemoryMax = host RAM - reserve (dockerd, pandorad, OS)
  ws-<workspace>.slice                CPUWeight per workspace (fair share between agents)
    pandora-run-<id>.service          MemoryMax=--mem, CPUQuota=--cpus*100%, TasksMax
```

`--mem` is the budget for the whole run including its services. Containers
are created by dockerd and land under dockerd's tree by default, so the run
passes its slice name to the repo's test stack (`PANDORA_CGROUP_PARENT`), and
a cooperating stack sets Compose `cgroup_parent` accordingly (eichler hook
below). Uncooperative stacks run with the limit covering only the command's
own processes; the result records `accounting: partial`.

OOM detection: `memory.events` on the run's cgroup. A guest-visible kill
becomes outcome `oom`, layer `run`. A few GB of zram is configured as a shock
absorber, not as the admission mechanism: swap under this workload converts
one clean `oom` into slow, flaky failures across every run.

**Admission.** Global cap on concurrent runs (default 8), per-account cap,
queue of sealed `(input_id, generation, spec)` entries. Oldest-fit with
bounded bypass; a request that can never fit is rejected at submit. Prepare
has its own pool (default 2) and requests for the same workspace generation
coalesce. `wait` reports why a run is waiting (`memory`, `cap`, `prepare`,
`disk`). Runner parallelism (Vitest workers, Playwright workers) is configured
by the repo adapter; a cgroup limit is not a worker count.

**Disk.** Watch `btrfs filesystem usage` (data and metadata), Docker's
filesystem, and pending deletions; `df` lies on btrfs. High/low watermarks
stop admission before the control plane cannot record failures. Caps on
upload bytes/files, per-run log and artifact size, retained generations.
ENOSPC inside a run is `infra_failed`, layer `host`.

**Run environment.** Private `HOME`, `TMPDIR`, `XDG_*` per run under the
upper layer; `PANDORA_RUN_ID`, `PANDORA_CGROUP_PARENT`, `TURBO_CACHE_DIR`,
`PLAYWRIGHT_BROWSERS_PATH`, `npm_config_store_dir` set; repo-declared
environment profile applied. All timestamps and ordering are server-side.

**Transport.** rsync over SSH with a per-account key and a forced
`rrsync` command jailed to that account's inbox; the client never chooses the
destination path. HTTPS API with bearer tokens (hashed in SQLite) for
begin/seal/submit/wait/logs/result/cancel; accepts argv arrays, never shell
strings, never absolute paths or `..`. SSH identity and API token resolve to
the same account.

## 6. Security posture (v1, single tenant)

- The `pandora` user is in the `docker` group. **This is root-equivalent on
  the host.** Acceptable only because every run is your own agents' code
  (threat model: bugs, not adversaries). Documented in the runbook; moving
  dockerd inside the isolation boundary (rootless per run, or Docker-in-VM) is
  the first item on the multi-tenant list.
- Deploy key is repo-scoped, read-only, lives only on the host.
- Hooks in base's `.git` are disabled (`core.hooksPath=/dev/null`).
- Runs have unrestricted egress in v1; egress policy is a multi-tenant item.
- Logs are untrusted content: control characters stripped from summaries.

## 7. Repo integration (eichler)

`.pandora.toml` at the repo root, an explicit execution profile:

```toml
[sync]
include = []                               # ignored paths allowed to travel (none needed)

[env]
EICHLER_VALIDATION_DIRECT = "1"
EICHLER_COMPOSE_OVERRIDE = "${PANDORA_COMPOSE_OVERRIDE}"

[artifacts]
pull = ["tmp/validation/**", "**/playwright-report/**", "**/test-results/**"]

[profiles.heavy]
match = ["pnpm validate postgres", "pnpm validate journey", "pnpm validate journeys",
         "pnpm validate surface", "pnpm validate browser-integration", "pnpm validate mockup-browser"]
mem = "16g"
```

Profile matching is on the resolved argv prefix, not substring search.

One eichler PR:

1. `EICHLER_VALIDATION_DIRECT=1` gates **only** `validate.mjs:84` (run
   `execute()` inline instead of Pueue `submit()`). `plan.mjs` keeps
   `heavy.mjs` / `surface.mjs` (the `GITHUB_ACTIONS` branches swap in raw
   `pnpm --filter` commands that expect CI service containers and fixed
   ports; those must not be taken). Fingerprint and receipts stay: the run
   tree is immutable, so the drift check is trivially satisfied, and the
   receipt (`tests.json`) is what the diagnostics adapter reads.
2. `EICHLER_COMPOSE_OVERRIDE`: if set, `tools/stack/instance.mjs` appends
   `-f $EICHLER_COMPOSE_OVERRIDE` to its compose invocations. Pandora writes a
   per-run override setting `cgroup_parent` on each service.

`.git` exists in every generation because base is a real clone; HEAD points at
base's commit and the agent's files are overlaid. Git-dependent tooling that
needs the agent's actual commits is unsupported in v1 (upgrade path: client
pushes its HEAD to the box, rsync carries only the dirty delta).

Agent instructions change from "`pnpm install --frozen-lockfile` then
`pnpm check`" to `pandora run -- pnpm check` (plus `-c` fanout before a PR).
`pnpm test:ios` fails fast remotely (eichler already throws off-darwin).
`pnpm dev:stack` stays local.

What eichler stops needing locally: Pueue, the validation state dir, the
light/heavy/surfaces groups, Docker Desktop for tests. Pandora adds no test
result cache; turbo task caching remains visible and governed by the repo.

## 8. Host

Hetzner dedicated (AX line, NVMe, KVM-capable for the later executor).
Ubuntu LTS. `/work` btrfs; `/`, `/var/lib/docker`, `/var/lib/pandora` ext4.
Provisioned by a checked-in script: Node (per `engines`), pnpm (per
`packageManager`), Docker CE (rootful, overlay2), Playwright system deps,
btrfs-progs, systemd units for pandorad, zram. Toolchain versions are part of
the prepare fingerprint, so a bump invalidates generations instead of
silently mismatching.

## 9. Executor boundary (what tier 2 swaps)

The stable boundary is:

```
ExecutionSpec = sealed input_id + argv + relative cwd + runtime/profile + limits + artifact globs
```

Everything below it is executor-private: head/generation subvolumes, overlay
mounts, shared dockerd, `TURBO_CACHE_DIR` as a directory, the base
subvolume, cgroup-derived outcome evidence. A microVM executor would
materialise the same sealed input into a guest disk, run Docker inside the
guest (removing the compose hook and the docker-group problem), expose caches
via virtiofs or a service, and report `oom` with layer `guest`. The CLI,
result schema, identity and idempotency rules do not change.

## 10. Not in scope for v1

MicroVMs, multi-tenant isolation, egress policy, billing, GitHub Actions
adapter, test selection or result caching, macOS/iOS suites, multi-host
placement, IDE/LSP integration, named shared workspaces, promotion of run
state.

## 11. Build order

1. Host provisioning script; verify on the box: reflink installs across
   subvolumes, overlay-at-stable-path with eichler's `check-worktree-deps`,
   snapshot and mount latency under 10 concurrent runs, Compose
   `cgroup_parent` placement.
2. Eichler PR (direct mode + compose override).
3. pandorad: SQLite schema, begin/seal/submit, lock + prepare + generation,
   overlay + systemd unit + cgroups, result persistence, restart
   reconciliation.
4. CLI: enrol, capture/seal, `run`/`wait`/`logs`/`result`/`ps`/`cancel`,
   pending file, exit 124 path, `-c` fanout, artifacts pull, `changes.patch`
   + `apply`.
5. Diagnostics adapters (Vitest, Playwright, Jest JSON).
6. Admission, prepare pool, disk watermarks, GC.
7. Dogfood: replace `pnpm check` in one agent's instructions; measure cold,
   warm, lockfile-change, and 8-way fanout; compare against local Pueue.
