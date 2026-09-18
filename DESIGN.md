# Pandora design (v2)

Remote execution for coding agents. An agent in a git worktree runs
`pandora run -- <cmd>`; the command executes on a remote Linux host against a
sealed copy of that worktree, in its own container over a copy-on-write
snapshot of a continuously mirrored workspace that is already warm with the
repo's dependencies. Many agents in many worktrees fan out to the same host
and never think about local machine load.

**Invariant:** a workspace may affect how fast a run executes; it must never
determine which source a run executes or whether its result is trustworthy.
Correctness comes from sealed inputs and provenance in results; mirrors,
snapshots, generations and caches are only speed.

**Agent-UX invariant:** remote execution should be indistinguishable from
local execution wherever that is achievable — same cwd, same env (minus
platform variables), same exit code, same output paths, files changed by the
command on disk before the command returns.

Tier: persistent host, continuously mirrored workspaces, btrfs generations,
container-per-run with cgroup limits. MicroVM-per-run (Firecracker on Linux,
Tart on macOS) is a later executor swap behind the same client contract
(section 12). First target repo: `eichler` (pnpm + turbo monorepo, Docker
Postgres, Playwright).

v2 supersedes v1. Changes settled by grill and verified by local POCs
(`tmp/poc/`): continuous Mutagen mirror replaces capture-time rsync; real git
on the box via pushed HEAD + alternates gitdir; container-per-run mounted at
the local absolute path replaces overlay-at-stable-path; Docker API proxy
replaces the eichler compose hook; `-c` fanout removed; write-back is
automatic. POC evidence cited inline.

## 1. Agent-facing contract

```
pandora init                       enrol repo (writes .pandora.toml scaffold) / machine (SSH key, client UUID)
pandora run [flags] -- <argv...>   converge mirror, seal, run, wait; exit with the command's exit code
pandora wait <run-id...>           block on a set; per-id outcome lines; non-zero if any did not pass
pandora ps [--all]                 runs for this worktree, merged local-pending + server state
pandora logs <run-id> [--follow] [--tail N] [--range a-b]
pandora result <run-id> [--json]   provenance + diagnostics + hint (exit 0 if retrieved)
pandora cancel <run-id>
pandora fetch <run-id> <path>      pull any file from a kept run tree (failed runs kept 24h)
pandora sync status|reset          mirror convergence detail; terminate + recreate session
pandora secret set NAME            server-side secret, injected as env into runs
pandora host provision|update|status
pandora cache clear                shared caches (turbo/pnpm store) for this repo
pandora capabilities --json        limits, retention, supported diagnostic adapters
```

Flags on `run`: `--detach`, `--stream`, `--max-wait <dur>` (default 8m),
`--wait-forever`, `--mem`, `--cpus`, `--timeout` (server-side kill, default
30m), `--profile <name>`, `--json`.

### What a run feels like

- **Blocking by default.** First stderr line: `pandora: run <id> input
  <input-id>`. Without `--stream`, stdout receives **head 50 + tail 150**
  lines of combined command output with a `pandora: … N lines omitted (log:
  ~/.pandora/runs/<id>/log)` marker on stderr; `--stream` gives the raw
  stream. Queued runs announce `pandora: queued (position 3, ~2m)`. All
  pandora lines are stderr, prefixed `pandora:`; stdout is the command's.
- **Exit code mirrors the command** exactly. Reserved CLI codes: **124**
  still-running at `--max-wait` (final line `pandora: still-running
  id=<run-id>`), **125** infra/prepare failure, **126** mirror not converged.
- **Interrupt detaches.** SIGINT/SIGTERM/harness kill never cancels the run;
  the pending record lets `pandora ps`/`wait` recover it. `pandora cancel` is
  the only stop.
- **Return barrier.** Before `run` exits, the run's source changes have been
  applied to the mirror and flushed to the Mac: a `git diff` in the next tool
  call sees them. Adds ~1 sync cycle when files changed.
- **cwd mirrors.** `cd apps/web && pandora run -- pnpm test` runs in the same
  relative directory remotely.
- **Paths mirror.** The run container mounts its snapshot at **the local
  worktree's absolute path** (`/Users/gary/code/wt-a`). Every path any tool
  emits — stdout, stack traces, Playwright HTML, JUnit XML, sourcemaps,
  tsbuildinfo — is a valid local path. There is no `/workspace` convention
  and no rewrite layer.
- **Env mirrors, sanitised.** The caller's shell env is forwarded minus a
  denylist of platform vars (`PATH`, `HOME`, `SHELL`, `TMPDIR`, `USER`,
  `SSH_*`, `TERM*`, `XDG_*`, `HOMEBREW_*`, `LC_*`, `_`, harness markers) and
  minus secret-looking names (`TOKEN|SECRET|KEY|PASSWORD` — dropped with a
  one-line notice pointing at `pandora secret set`). So `DEBUG=pw:api pandora
  run -- pnpm test` just works. `.pandora.toml [env]` values layer on top;
  the forwarded set is recorded in the run spec.
- **stdin closed, no TTY.** Prompt-based hangs hit the wait deadline;
  `--stream` shows the stuck prompt. Non-interactive by contract.
- **Fresh run, always.** Same command + same `input_id` executes again;
  result notes `same input as run <prev>` so agents can tell a flake from a
  change. Turbo's own cache still skips unchanged tasks inside the run.
- **Fanout** is multiple invocations (`--detach` + `pandora wait <ids>`).
  There is no `-c` and no server-side group object.
- **Unenrolled repo:** refuse with `pandora: not a pandora repo (no
  .pandora.toml up-tree); run \`pandora init\` at the repo root`.
- **Hint channel.** Results carry a `hint` string from server-side
  heuristics: OOM → raise `--mem`; timeout → raise `--timeout`; output
  mentioning a path that exists locally but is gitignored → `pandora:
  tmp/fixture.json exists locally but is gitignored; add to sync.include`.

Three separate fields, never conflated: **observed command exit code**
(nullable), **CLI exit code**, **outcome**: `passed | command_failed |
timed_out | oom | cancelled | prepare_failed | infra_failed`, each with
`layer` (command / run / host) and `evidence` (e.g. `memory.events
oom_kill=1`). `infra_failed` is reported, never retried silently.

### Idempotency and recovery

`request_id` is generated client-side before any network call and stored as
one atomic record per request in `~/.pandora/pending/<request-id>.json`
(request id, spec digest, upload state, acknowledged run id). Lifecycle:
`flush+seal -> submit -> observe`. Re-submitting the same `request_id` with
the same spec returns the same `run_id`; different spec is an error.
Submission-unknown after a disconnect is resolved by re-presenting the
`request_id`. `pandora ps` merges pending records with server state.

### Result schema

```json
{
  "schema_version": 2,
  "run_id": "r_...", "input_id": "t_<tree-hash>", "request_id": "q_...",
  "state": "finished", "outcome": "command_failed", "layer": "command",
  "same_input_as": "r_...", "hint": null,
  "command": { "argv": ["pnpm","validate","postgres","api"], "cwd": "apps/api" },
  "exit_code": 1, "cli_exit_code": 1,
  "environment": { "image": "pandora/eichler-runtime@sha256:...",
                   "prepare_fingerprint": "p_...", "generation": "v17",
                   "host_kind": "linux" },
  "limits": { "requested": {"mem":"12g","cpus":4}, "effective": {"mem":"12g","cpus":4} },
  "durations_ms": { "converge": 40, "queue": 1200, "prepare": 0, "execute": 84000 },
  "diagnostics": { "status": "complete|partial|missing|unsupported",
                   "adapter": "vitest-json", "summary": "1 failed, 212 passed",
                   "failures": [...], "truncated": false },
  "log": { "path": "~/.pandora/runs/r_.../log", "bytes": 183211 },
  "artifacts": { "files": 6, "written_in_place": true, "kept": "~/.pandora/runs/r_.../artifacts/" },
  "source_changes": { "applied": 2, "conflicts": [{ "path": "...", "reason": "local edit during run" }],
                      "patch": "~/.pandora/runs/r_.../changes.patch" }
}
```

Diagnostics adapters (Vitest, Jest, Playwright JSON) read run-owned report
files; eichler's `tests.json` receipt is one. A missing report is `missing`,
never "zero failures". `RunResult` (server facts) is separate from the CLI's
observation record (local exit, downloads).

## 2. Workspace identity

```
workspace_id = sha256(client_instance_id, canonical_origin_url, realpath(worktree_root))
```

`client_instance_id` is a UUID in `~/.pandora/config`; two Macs with the same
path do not collide, and the same local path on two machines maps to distinct
workspaces (inside each container the path is still the local absolute path —
per-host run dirs disambiguate on the box). Branch is metadata only.
Worktree removal: a `pre-worktree-remove`-style git hook installed by
`pandora init` terminates the Mutagen session and asks the box to retire the
workspace; a 14-day sweeper covers hooks that never ran. Nothing is written
into the repo's `.git`.

## 3. Source sync: continuous Mutagen mirror

One Mutagen session per worktree: alpha = local worktree, beta =
`/work/mirrors/<ws>` on the box, `two-way-resolved` (alpha wins). The mirror
*is* the worktree's remote twin; `pandora run` does not capture anything —
it converges and seals what is already there.

- **Session lifecycle.** Created lazily on first `pandora run` in the
  worktree (or eagerly by the post-checkout hook, section 9). Terminated on
  worktree removal or `pandora sync reset`.
- **Ignore set.** Mutagen does not read `.gitignore` (POC 1a). The ignore
  list is generated: `git ls-files -o -i --exclude-standard --directory`,
  plus `.git` always, minus `.pandora.toml [sync] include` allowlisted
  ignored paths. Regenerated at each `pandora run`; if the set changed, the
  session is terminated and recreated (there is no live-update; recreation
  is one rescan, POC 1f). Nested `.gitignore` per-dir patterns need
  translation to root-relative patterns — harness item.
- **Seal.** `pandora run` issues `mutagen sync flush`, then checks
  convergence via `sync list`: `Connected: yes` on both ends and no
  `Conflicts:` line — flush's exit code alone does not report conflicts
  (POC 1c/e). Unconverged → refuse, exit 126, `pandora sync status` for the
  cause. Never seal an input that may not equal the local tree.
- **Conflicts.** `two-way-resolved` (Mac wins) discards box-side edits
  silently between cycles (POC 1d). Box-side writes therefore never go
  through the session's beta directly: run-produced source changes are
  applied to the mirror by pandorad under a lock with a per-file guard
  (section 5), so nothing races with the resolver.
- **Ignored files** fall into three classes: derivable on the box
  (`node_modules`, `dist/`, `.turbo/`, `.wrangler/`, reports — created in
  the generation or the run, never synced); allowlisted in `[sync] include`
  (fixtures, a local `.dev.vars` — travel like source); account secrets
  (`pandora secret set`, env-injected). Eichler's validation generates
  throwaway secrets and needs neither.

## 4. Git on the box

Files sync; `.git` never does (worktree `.git` is a pointer to a Mac path;
two-way-syncing a live gitdir is unsafe). Eichler's tooling asks git real
questions (`fingerprint()`: `rev-parse HEAD`, `diff HEAD --binary`,
`ls-files --others`; also `cat-file <base-sha>`, `git log`, remote config),
so the box needs a real repo, not files and not a synthetic commit.

Mechanics (verified end to end, POC 3):

- One bare **object store** per repo: `/work/repos/<repo>/store.git`,
  fetching `origin` on a deploy key.
- Per run, the client does `git push ssh://box/.../store.git
  HEAD:refs/pandora/<ws>/head` — a no-op when HEAD hasn't moved.
- Per workspace, a real gitdir `/work/ws/<ws>/ws.git`:
  `objects/info/alternates` → store objects; `symbolic-ref HEAD` → the
  branch name; `refs/heads/<branch>` = pushed SHA (alternates share
  objects, not refs — resolve the store-side ref to a SHA first). A
  **gitfile** `<mirror>/.git` → the gitdir makes plain `git` work in cwd.
  The gitfile is box-generated and excluded from sync.
- Seal-time: `read-tree HEAD` + `update-index --refresh` (exit 1 listing
  `needs update` doubles as the drift detector against the manifest).
- Per run, the small gitdir (index + refs + config, objects shared via
  alternates) is copied into the run's tree and the gitfile repointed —
  a run's `git commit`/`checkout` is run-local. Rule: **runs can change
  your files, never your git history.** Objects written mirror-side can
  never corrupt the store (alternates are read-fallback); store `gc` is
  safe because pushed commits stay reachable through
  `refs/pandora/<ws>/head`.
- `input_id` = `git write-tree` against a temp index after `add -A` (with
  `-f` for allowlisted ignored paths) in the sealed snapshot: a real
  content hash of the exact tree the run sees.
- LFS: the mirror gets smudged files via sync, no smudge needed; set
  `GIT_LFS_SKIP_SMUDGE=1` in run env for any run-side git operation that
  would materialise blobs (POC 3, eichler uses LFS).

Detached HEAD, amend, rebase: HEAD is pushed by SHA; branch ref attached
when one exists; detached stays detached.

## 5. Workspace layout, generations, prepare

All mutable trees on one btrfs filesystem at `/work`; `/var/lib/docker` and
pandorad's SQLite on ext4.

```
/work/repos/<repo>/store.git              shared object store (origin fetch + pushed refs)
/work/repos/<repo>/base/gen<N>            sealed base generation: origin/<default>, prepared
/work/mirrors/<ws>                        writable mirror subvolume (Mutagen beta)
/work/ws/<ws>/ws.git                      workspace gitdir (alternates → store)
/work/ws/<ws>/v<N>                        read-only sealed generation: mirror content + prepared state
/work/runs/<run-id>/                      writable snapshot of v<N> + run-owned files + run gitdir
/work/cache/<repo>/{pnpm-store,turbo,browsers}
```

**Prepare = dependency/environment setup** before execution: install from
the lockfile into the candidate at its canonical path, plus pre-pulling
compose service images. Prepare is a **run with profile `prepare`** in the
same scheduler (same cgroup limits, same container mechanism, image =
runtime image).

**Transactional generation pipeline** (per-workspace lock):

1. candidate = writable btrfs snapshot of newest ready `v<N>` (or seeded
   from `base/gen<M>` for a new/changed workspace: clone the base tree,
   then `pnpm install --frozen-lockfile --offline` relinks
   `.pnpm-workspace-state-v1.json` to the candidate path in ~ms — verified
   POC 2: 1.3 GB clone + 56 ms relink, `check-worktree-deps` passes; on
   btrfs the clone is an O(1) snapshot).
2. dirty marker written; mirror content is reconciled into the candidate
   (rsync of the converged mirror, manifest-diff deletions; the mirror
   itself is never executed).
3. `read-tree`/`update-index` refresh in the candidate's gitdir copy.
4. prepare fingerprint = runtime image digest + lockfile + every
   `package.json` + `pnpm-workspace.yaml` + `.npmrc` + `patches/**` +
   install flags + compose `image:` digests. Differs → run prepare
   (install + image pre-pull) with network-failure class and timeout.
5. seal: `input_id` via `git write-tree`; `btrfs subvolume snapshot -r`
   → `v<N>`; record `(input_id, v<N>, fingerprint)`; publish. Any failure
   discards the candidate; `v<N-1>` stays ready; the run reports
   `prepare_failed` with a reason (`install` / `network` / `timeout`).

Prepares for the same fingerprint coalesce; a fresh lockfile never
invalidates the last ready generation. `origin/<default>` base generation:
the box polls origin every ~10 min and rebuilds base as a low-priority
prepare when its fingerprint changes.

**Runs.** Each admitted run gets a writable snapshot of its sealed `v<N>`
at `/work/runs/<id>` — no overlayfs (POC-verified relink makes
path-identity cheap; btrfs snapshot is O(1), host-inspectable, no whiteout
semantics). Concurrent runs from one worktree each get their own snapshot
of whatever generation was sealed at their submit; identical submissions
share `input_id` and dedupe the seal step.

**No promotion.** Run side effects never become a later run's base. Warmth:
`node_modules` in the generation; shared pnpm store (reflink imports);
shared turbo cache (`TURBO_CACHE_DIR`, repo-scoped, content-addressed —
poisoning is possible, `pandora cache clear` exists); shared browser
install; dockerd's image cache.

**Source-change return.** After a run, pandorad diffs the run tree's
client-owned paths against the sealed input (`git status --porcelain` +
`git diff HEAD --binary` in the run's gitdir — new files and binaries
included). Under the workspace apply-lock: for each changed file, apply
into the mirror **only if mirror content still equals the sealed version**
(per-file guard); files a concurrent local edit already changed are
skipped, listed as conflicts, patch retained server-side and under
`~/.pandora/runs/<id>/changes.patch`. Then flush → Mutagen propagates to
the Mac before `run` exits (return barrier).

**Artifacts.** Every run-created/changed file outside the derivable-state
denylist (`node_modules`, `.turbo`, `dist`, `.wrangler`, `tmp/validation`'s
ephemeral stack dirs, …) comes back to its worktree-relative path —
including gitignored outputs like `playwright-report/`. Concurrent runs
writing the same path: last writer wins in the worktree; every run's full
artifact set is kept under `~/.pandora/runs/<id>/artifacts/` and referenced
in the result. No size cap in v1; `[sync] artifact_exclude` is the only
knob.

## 6. Execution: container per run

```
docker run --rm --name pandora-run-<id> \
  --network host \
  -v /work/runs/<id>:<local-worktree-path> -w <local-worktree-path>/<rel-cwd> \
  -v /work/runs/<id>/docker.sock:/var/run/docker.sock \
  -v /work/cache/<repo>/pnpm-store:<store> -v .../turbo:... -v .../browsers:... \
  --memory 12g --cpus 4 --cgroup-parent pandora-ws-<ws>.slice \
  -e TURBO_CACHE_DIR -e PLAYWRIGHT_BROWSERS_PATH -e npm_config_store_dir \
  -e GIT_LFS_SKIP_SMUDGE=1 -e PANDORA_RUN_ID ... \
  pandora/<repo>-runtime@sha256:<digest>  <argv>
```

- The mount target is the **local absolute path** — inside the Linux
  container `/Users/gary/code/wt-a` is just a directory. Path-identity for
  output, artifacts, and pnpm's realpath checks, for free.
- **Runtime image** (digest-pinned, built by pandorad from a repo
  `Dockerfile` fragment + base): OS libs, Node (per `engines`), pnpm (per
  `packageManager`), Playwright browsers/system deps. Contains the
  toolchain, **not** project dependencies — those live in the generation.
  Toolchain changes → new image → new prepare fingerprint → new
  generations.
- Docker gives: private mount namespace, memory/CPU limits + OOM evidence
  (`memory.events`), durable exit code via `docker inspect` even if
  pandorad was down, `docker kill` = cancel.
- dockerd `live-restore` on so daemon restart doesn't kill runs.

### Per-run Docker API proxy

The container's `docker.sock` is **not** the host socket: it's a per-run
unix socket (`/work/runs/<id>/docker.sock`) served by one pandorad-owned
process — one listener per run, so the socket itself identifies the run.
On `POST /containers/create` it:

- rewrites `HostConfig.Binds`/`Mounts[].Source` under
  `<local-worktree-path>` → `/work/runs/<id>`;
- injects `HostConfig.CgroupParent` = the run's slice and
  `Labels["pandora.run"]=<id>` (also onto the container);
- 403s mounts of `/`, the real docker socket, other runs' paths;
- rewrites `Content-Length`, handles chunked bodies, passes through
  streaming and HTTP-upgrade (`attach`, `exec`, `docker run -i`).

Compose speaks the same API, so `docker compose up` is covered with no
repo changes — this **replaces the v1 `EICHLER_COMPOSE_OVERRIDE` hook**.
At run end pandorad removes everything labelled `pandora.run=<id>`;
reconciliation at restart does the same. Fixed host ports collide as they
do locally (clear error, run-scoped). Long-lived dev services are a later
feature (`pandora service`); v1 targets finite commands. POC script
written (`tmp/poc/docker-proxy.mjs`); daemon-side verification is a
harness item — on Docker Desktop `CgroupParent` may be ignored, Linux is
the real test.

## 7. Resources and admission

cgroups v2, host → workspace → run:

```
pandora.slice                       MemoryMax = host RAM - reserve
  pandora-ws-<ws>.slice             per-workspace weight
    run container + its proxy-labelled children (CgroupParent)
```

- **Admit on expected usage, enforce on ceiling.** Per profile (inferred
  from command shape: first tokens of argv), pandorad records observed
  peak memory; admission sums p95 expectations against `RAM − reserve`.
  `--mem` is the hard per-run ceiling so a misbehaving run dies alone.
- CPU oversubscribes freely (`--cpus` is a time quota); memory
  oversubscription factor starts ~1.0, tunable from data.
- Global concurrency cap derived at provisioning:
  `floor((RAM − 16 GB reserve) / default_mem)`; the ~8 figure is a
  128 GB-class assumption, not a constant.
- FIFO queue, `pandora: queued (position N, ~Mm)`; per-account cap;
  prepare jobs compete in the same scheduler (profile `prepare`) and count
  against the shared memory budget.
- OOM → outcome `oom`, evidence `memory.events`; zram configured as a
  small shock absorber, never the admission mechanism.
- Disk: watch `btrfs filesystem usage` (data + metadata), Docker's fs;
  watermarks stop admission before the control plane can't record
  failures; caps on per-run log size, retained generations.

## 8. pandorad

Node/TypeScript, runs as root (snapshots, containers, cgroups), spawns
runs as an unprivileged in-container user. SQLite on ext4.

- **Restart reconcile, before admitting:** every `running` row is matched
  against `docker ps -a --filter label=pandora.run`; present → re-adopt;
  exited → collect normally (inspect gives exit + OOM); absent →
  `infra_failed(daemon_lost)`. Runner-side terminal status written durably
  before completion is announced. Runs are never orphaned or
  double-collected.
- **Retention.** Run snapshots deleted after collection; **failed run
  trees kept 24h** (`pandora fetch <id> <path>` inspects them). Results +
  logs 30d. Last 2 sealed generations per workspace. Workspace GC'd 14d
  after its session disappears (hook + sweeper). Generations are
  reference-counted; never delete what a run references.
- **Transport: SSH only.** Three SSH users of one connection
  (ControlMaster, keepalive): Mutagen session; `git push` to the store;
  pandorad API over a forwarded unix socket
  (`ssh -L ~/.pandora/sock:/run/pandora/api.sock`, HTTP+JSON, bearer token
  bound to the SSH key account). No open TCP port besides sshd.
- **Versions.** The box serves the matching CLI binary; the CLI
  self-updates at connect; protocol-major mismatch refuses with a named
  fix. `pandora host update` deploys pandorad + CLI bundle atomically.

## 9. Client footprint (Mac)

- `mutagen` binary + its daemon (installed via brew, version-pinned).
  No pandora-resident daemon: housekeeping (session GC, ignore refresh,
  convergence checks) runs opportunistically inside any `pandora`
  invocation.
- `~/.pandora/`: config (client UUID, host, token), `pending/<req>.json`,
  `runs/<id>/{log,result.json,artifacts/,changes.patch}`, CLI binary
  cache, SSH socket.
- Git hooks installed by `pandora init` per repo (non-destructive, chained
  if a hook exists): post-checkout/worktree-add → background
  `pandora sync init` (mirror + base-seeded prepare warm by the time the
  agent's first command lands; cold inline path remains the fallback);
  worktree-remove → session terminate.
- Discovery: agent-oriented `--help` stating the invariants (remote, cwd/
  env/exit mirror local, changes come back before exit, `still-running`
  recovery, `wait`/`cancel`/`logs`/`fetch`), one paragraph in the repo's
  AGENTS.md, optional thin repo wrappers (`pnpm rcheck`) so agents keep
  existing habits.

## 10. Security posture (v1, single tenant)

- One SSH key = the only authn/authz. Runs are root-equivalent on the box
  (docker group): accepted, documented — every run is the operator's own
  agents' code. Isolation exists for correctness and capacity, not
  adversarial containment; microVMs are the multi-tenant upgrade.
- Deploy key read-only, repo-scoped, host-side only.
- `core.hooksPath=/dev/null` in store/workspace gitdirs.
- Logs/results sanitised of control characters in summaries.
- Unrestricted egress in v1.

## 11. Repo integration (eichler)

`.pandora.toml`, intentionally small — everything else is inferred:

```toml
[runtime]
# derived if absent: node from engines, pnpm from packageManager
dockerfile = ".pandora/Dockerfile"      # optional repo fragment

[env]
EICHLER_VALIDATION_DIRECT = "1"

[sync]
include = []                            # ignored paths allowed to travel
```

Inference: prepare = `pnpm install --frozen-lockfile` when
`packageManager: pnpm@…`; fingerprint from lockfile + all `package.json` +
`pnpm-workspace.yaml` + `.npmrc` + `patches/**` + compose images;
artifacts = every run-changed file minus the derivable denylist; profile
from argv shape (`pnpm validate*`/`pnpm test:e2e*` → heavy defaults).

One eichler PR, one seam: `EICHLER_VALIDATION_DIRECT=1` skips only the
Pueue `submit()` in `validate.mjs` — `heavy.mjs`/`surface.mjs`, ephemeral
Compose stacks, generated secrets, fingerprinting and receipts all stay.
`GITHUB_ACTIONS` must NOT be set (it swaps in CI raw commands without the
stack). The compose override hook is gone — the proxy owns cgroups, paths
and labels.

Agent instructions: `pandora run -- pnpm check` replaces the
install-then-check incantation; `pnpm test:ios` fails fast remotely
(eichler throws off-darwin — correct); `pnpm dev:stack` stays local.
Eichler stops needing locally: Pueue for validation, Docker Desktop for
tests, per-worktree `node_modules` for validation (still useful for IDE).

## 12. Executor boundary and platforms

```
ExecutionSpec = sealed input_id + argv + relative cwd + runtime/profile
              + limits + forwarded env + host_kind
Executor interface: prepare / snapshot / execute / collect / cleanup
```

- **`linux` (v1):** container executor, sections 5–7.
- **`macos` (follow-on):** native executor on a Mac mini-class host —
  in-place runs in the prepared tree (no per-run copies: `sandbox-exec` FS
  restrictions, process-group kill, per-workspace serialization, ~2
  concurrent; the stable-path problem disappears because there is no
  copy). Needed for Xcode/iOS-simulator suites (`pnpm test:ios`,
  `test:native-*`); Docker Desktop provides the compose stack as on a
  dev machine. APFS `cp -c` clones give O(files)-not-O(1) copies if
  per-run copies are ever wanted.
- **`linux-vm` / `macos-vm` (tier 2):** Firecracker / Tart VM-per-run.
  Same contract; workspace hand-off becomes disk-image based (reflink
  image copies, virtio-blk), a guest agent runs the command, Docker lives
  inside the guest. The uniform "VM per run" model — but note
  Firecracker is Linux/KVM-only and Tart is Apple-Silicon-only with a
  2-VM licensing cap, so "uniform" means same shape, two integrations.
  Costs deferred knowingly: image-based workspace, in-guest dockerd,
  tap/NAT per VM, kernel/rootfs pipelines, no host-side tree inspection.

**Platform truth for agents:** a Linux container cannot run Xcode builds,
iOS simulators, or macOS SDKs — ever; that's not a container limitation,
it's an OS one, and eichler already fails fast off-darwin. Headless
browsers, Postgres, Node are all fine. Android emulators would work
(KVM-passthrough) if ever needed.

## 13. Out of scope for v1

Multi-tenant isolation, egress policy, billing, GitHub Actions adapter,
test selection/result caching (beyond turbo), long-lived services
(`pandora service` + port forwarding), interactive shells/PTY, the macOS
and VM executors, multi-host placement (the host-kind field reserves it).

## 14. Build order

1. **Adversarial harness on hand-wired pieces** (before any daemon code):
   provision script on the box; Mutagen over SSH incl. nested-.gitignore
   ignore generation and convergence predicate; gitdir provisioning;
   btrfs snapshot → container at local path → eichler `check-worktree-deps`
   passes; proxy rewrite/cgroup/label/deny on real dockerd; compose via
   proxy; OOM classification; torn-prepare recovery; concurrent runs with
   distinct sentinels; `pandora run -- pnpm check` by hand.
2. Eichler PR (`EICHLER_VALIDATION_DIRECT`).
3. pandorad: SQLite, sessions/mirror state, seal pipeline, scheduler +
   admission, container + proxy lifecycle, result store, restart
   reconcile, retention.
4. CLI: enrol, mirror mgmt, `run`/`wait`/`ps`/`logs`/`result`/`cancel`/
   `fetch`, pending records, env denylist, head+tail, reserved exit
   codes, self-update.
5. Diagnostics adapters, `hint` heuristics.
6. Dogfood: one agent's instructions → `pandora run -- pnpm check`;
   measure cold, warm, lockfile-change, 8-way fanout vs local Pueue.
