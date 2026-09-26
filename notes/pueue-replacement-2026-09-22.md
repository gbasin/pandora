---
status: log
---

# Replacing Pueue with the client daemon's local lane, 2026-09-22

Acme schedules local validation with Pueue: four machine-wide groups, a
worktree reservation, a source fingerprint, a receipt directory, and a health
gate that pauses a group when a job dies without proving its cleanup. Pandora's
client daemon already schedules the *remote* half of the same repository. Two
queues on one machine cannot see each other, so neither can say what the Mac is
actually doing.

This note inventories what Pueue does today, maps each responsibility onto a
pandora mechanism or records that it is dropped and why, and states what the
deletion PR removes. The local executor is built and proved; the acme side is
a feature-flagged seam, and nothing is deleted yet.

## What Pueue does today

Every line verified in `/Users/you/Code/acme-wt/journey-runner-proxy`
on `agent/journey-runner-proxy`, not assumed.

| # | Responsibility | Where | Who consumes it |
|---|---|---|---|
| 1 | Four machine-wide groups: `acme-light` 2, `acme-heavy` 1, `acme-surfaces` 2, `acme-stacks` 1 | `tools/validation/queue.mjs:50-70` (`setup`), limits re-asserted by `tools/stack/stack.mjs:47-52` | `plan.mjs` picks the group per suite; agents read the limits in `local-validation.md:19-31` |
| 2 | Which group a suite lands in | `tools/validation/plan.mjs:37` (default `acme-heavy`), `:75` surfaces, `:249,255,280,291,301,314` light | the queue; nothing else |
| 3 | Worker count per job, captured at submission | `queue.mjs:72-79` (`workers()`) from `<home>/config.json`, threaded through `plan.mjs:10,282,292` and `SURFACE_WORKERS` | vitest/jest `--maxWorkers`, `node --test-concurrency`, the surface runner |
| 4 | One active validation per worktree | `queue.mjs:104-114,128-144` — a symlink named by `sha256(worktree)` pointing at the job directory; `EEXIST` is the refusal | agents; `validate.mjs` surfaces the message |
| 5 | Source fingerprint before and after | `state.mjs:15-34` (`fingerprint`), taken at `validate.mjs:148`, re-checked at `run.mjs:61-64` and `:133-137` | the receipt's `status` |
| 6 | Receipts under `~/.local/state/acme-validation/jobs/<uuid>/` — `request.json`, `running.json`, `cancel.json`, `result.json`, `recovery.json`, `cleanup-required` | written `queue.mjs:127,132,177,197,227`, `run.mjs:57,176`; read `state.mjs:56-58` | `pnpm validate result/status/cancel/recover`; `heavy.mjs` and `tools/browser-integration/run.mjs:188-229` write `cleanup-required` into it via `ACME_VALIDATION_DIRECTORY` |
| 7 | `assertHealthy`: an unclean job pauses its group and blocks new submissions until `validate recover <id> --cleanup-confirmed` | `queue.mjs:81-102`, escalation at `run.mjs:47,166`, recovery at `queue.mjs:212-239` | agents, after a crash |
| 8 | `safeEnvironment()`: a 16-name allowlist, minus any PATH entry holding a `.pandora-shim` marker | `state.mjs:96-119`, `withoutCommandShims` at `:89-94` | every queued job |
| 9 | `GITHUB_ACTIONS === 'true'` direct execution | `validate.mjs` (the `direct` branch), `plan.mjs:161,174,190`, `stack.mjs:296-303` | CI |
| 10 | The `pnpm dev:stack` slot | `tools/stack/stack.mjs` — group of one, `stack:<root>` label, `pueue add … --escape exec node dev-stack.mjs`, `follow`/`kill`/`clean`, orphan reaping | `AGENTS.md:188-197`; every agent that needs a server |
| 11 | Nested-validation refusal | `ACME_VALIDATION_ACTIVE` set at `run.mjs:85`, refused at `validate.mjs` | a queued job that calls `pnpm validate` again |
| 12 | Cancellation with a per-suite signal and grace | `plan.mjs` `cancelSignal`/`cancelGraceMs`, enforced `run.mjs:30-54` | journeys (240 s), surfaces (SIGINT, 60 s) |
| 13 | Log following | `pueue follow <task>` spawned at `validate.mjs` | the terminal |
| 14 | A checksum-pinned Pueue 4.0.4 for CI integration tests | `tools/validation/test-pueue.sh`, run by `.github/workflows/ci.yml:159` | CI only |

Two findings that shrink the risk surface materially:

* **No receipt reader outside `tools/validation/`.** `tools/proj.mjs` (82 KB)
  has zero matches for `pueue`, `validation` or `acme-validation`. The
  `.claude/settings.json` and `.codex/hooks.json` hooks run only
  `tools/oxc-hook.mjs`. No workflow reads the jobs directory; CI takes the
  direct path and writes its receipt into `tmp/validation/<uuid>` inside the
  checkout.
* **The only out-of-tree consumers are cleanup markers**, not receipts:
  `tools/browser-integration/run.mjs` and `apps/agent/tools/test-ios.mjs` write
  into `ACME_VALIDATION_DIRECTORY`. That variable has to survive the
  migration; the receipt *format* does not.

## The mapping

| Pueue responsibility | Pandora mechanism | Note |
|---|---|---|
| 1 Groups with fixed slots | `[local] budget_mib` + `engine.admission`: memory held against a host budget, learned per (repo, job) | A group of N slots is a guess about size that is wrong for every member of the group. `acme-light` held two jobs whether they were a 130 MiB `node --test` or a Turbo typecheck. |
| 2 Suite → group | `[[jobs]] size` in `pandora.toml` | The ceiling is declared; the reservation is learned from observed peaks. |
| 3 Worker count | Unchanged — acme's planner still sets it | `PANDORA_CPUS` is a hint beside it. |
| 4 One active per worktree | `Budget.reserve`, keyed on the resolved worktree; `[local] one_active_per_worktree` | Same rule, same exit 75, without a symlink to leak. Now a config option. |
| 5 Source fingerprint | `snapshot.freeze` before and after; `[local] drift = off\|warn\|fail` | The same manifest that identifies a remote run's input. `warn` is a note, `fail` is exit 75. |
| 6 Receipts | `<state>/runs/<id>/{meta.json,log,result.json}`, one shape for both lanes | `pandora ps/logs/result/wait/cancel` replace `validate status/result/cancel`. |
| 7 `assertHealthy` + `recover` | **Dropped, because** the failure it guards is Pueue's: a daemon that marks a task killed while its children run. The local executor *is* the supervisor — it holds the process group, kills it with `killpg`, and waits. There is no second daemon to disagree with. What survives is the honest half: a run that does not reach a verdict gets a named non-passing outcome (`cancelled`, `timed_out`, `infra_failed`), never a zero. |
| 8 `safeEnvironment()` | `local.child_environment`: a platform allowlist plus the job's declared `env`/`unset`/`passthrough`, and `PANDORA_ROUTE_DEPTH=1` | The `withoutCommandShims` hack can go: the guard now travels, because the daemon sets it directly rather than hoping it survives a scrubbing hop. |
| 9 `GITHUB_ACTIONS` direct | Kept, generalised to `ACME_QUEUE=direct` | CI is unchanged. The local executor uses the same door. |
| 10 `dev:stack` slot | `[[jobs]] singleton = true`, `where = "local"`, `stack.mjs --foreground` | One per machine; `pandora ps` lists it, `pandora cancel` stops it with its own teardown. `dev:stack status\|logs\|stop\|prune` are rejected with that sentence. |
| 11 Nested-validation refusal | Kept as it is | Cheap, and still correct. |
| 12 Cancel signal and grace | Partly. `Supervisor` sends SIGTERM to the group and escalates after 15 s | **Open:** the per-suite grace (240 s for journeys, SIGINT for surfaces) is not yet expressible in `pandora.toml`. See open questions. |
| 13 `pueue follow` | The daemon's own log file, streamed by byte offset | Strictly better: re-attach after a client dies is a byte offset, and `pandora logs` replays from disk. |
| 14 `test-pueue.sh` in CI | **Dropped with Pueue**, replaced by `pandora/tests/test_local.py` | 30 tests against real processes. |

## What acme deletes

The deletion PR is not this one. What it would remove, measured:

| File | Lines | Why it goes |
|---|---|---|
| `tools/validation/queue.mjs` | 239 | Every line is Pueue: the client wrapper, groups, the reservation symlink, `assertHealthy`, `recover`. |
| `tools/validation/queue.test.mjs` | 165 | Tests a live `pueued`. |
| `tools/validation/test-pueue.sh` | 46 | Downloads and checksums Pueue 4.0.4 for CI. |
| `tools/validate.mjs` | ~85 of 220 | `throughPueue`'s submit-and-follow half, `status`, `cancel`, `recover`, `result`, `_run`. What stays is the planner, `direct`, and the help. |
| `tools/validation/run.mjs` | ~25 of 179 | The `pueue pause` escalation and `releaseReservation`; the supervision itself is kept until the drain/cleanup contract moves. |
| `tools/validation/state.mjs` | ~40 of 119 | `safeEnvironment`, `withoutCommandShims`, `identifiedTask`. `fingerprint` and the receipt writers stay while receipts do. |
| `tools/stack/stack.mjs` | ~200 of ~310 | Everything but `foreground`: the group, the label, the submit, the follow, the orphan reaping. |
| `.github/workflows/ci.yml` | 2 | The "Test local queue integration on Linux" step. |
| **Total** | **~800** | plus `local-validation.md`'s Pueue sections |

Nothing outside `tools/` changes. `package.json` scripts stay exactly as they
are, and so does every command an agent types.

## What agents type, before and after

| Before | After | Difference |
|---|---|---|
| `pnpm check` | `pnpm check` | none |
| `pnpm test:unit --project @acme/domain x.test.ts` | same | none |
| `pnpm validate node tools/x.test.mjs` | same | none |
| `pnpm journey S0-01` | same | already routed remotely |
| `pnpm dev:stack` | same | none |
| `pnpm validate status` | `pandora ps` | one queue, both lanes, one table |
| `pnpm validate result <uuid>` | `pandora result <id>` | |
| `pnpm validate cancel <uuid>` | `pandora cancel <id>` | |
| `pnpm dev:stack status\|logs\|stop` | `pandora ps` / `pandora logs <id>` / `pandora cancel <id>` | the stack stops being special |
| `pnpm validate recover <uuid> --cleanup-confirmed` | — | gone; see mapping row 7 |
| `pnpm validation:setup`, `brew install pueue` | — | gone |

No new incantation. The only new spelling is for the three inspection verbs,
and they are the *same* three verbs an agent already uses for a remote run.

## Migration order

1. **Now (this branch).** `ACME_QUEUE=pandora|direct|pueue` in
   `tools/validate.mjs`, defaulting to `pueue`. Nothing else changes. Both
   queues coexist; an agent opting in changes one environment variable.
2. **`pandora.toml` into the acme repository root**, with the local jobs
   beside the remote ones. Today the file lives outside the repository and is
   named by the enrolment.
3. **Flip the default** to `pandora` once the per-suite cancel grace (row 12)
   is expressible, and once `check`, `unit` and `surface` have run through the
   local lane for a week. Keep `ACME_QUEUE=pueue` working.
4. **Move `dev:stack`** to the singleton job. This is the one with a real user
   consequence — the ports and the teardown — so it moves alone.
5. **The deletion PR**: the table above, plus the Pueue sections of
   `local-validation.md` and `AGENTS.md:53`, `:192`.

## Risks

* **Receipt readers.** Grepped: none outside `tools/validation/`.
  `tools/proj.mjs` and every `.github/workflows/*.yml` are clean. The two
  out-of-tree consumers write cleanup markers into
  `ACME_VALIDATION_DIRECTORY`, which the local lane must keep setting —
  today it does not, and a browser-integration or native run through the local
  lane would lose its cleanup marker. **This is why `check`, `unit` and `node`
  go first and the heavy suites go last.**
* **One daemon is one point of failure.** Pueue's daemon survived a client
  crash; so does this one, and a restart re-adopts remote runs. It does *not*
  re-adopt local ones: a local run's supervisor is a thread in the daemon, so
  killing the daemon orphans the process group. Pueue had the same hole from
  the other side (a killed task with live children), which is what
  `assertHealthy` existed for. Named, not fixed.
* **The budget is per-daemon, not per-machine.** Two daemons with two state
  directories would double the budget, exactly as two Pueue daemons would have
  doubled the slots. The `daemon.lock` makes one daemon per state directory,
  not one per Mac.
* **Peaks are sampled at 1 Hz with `ps`.** A job that spikes and returns inside
  a second is under-measured, so its learned reservation is optimistic. The
  remote lane reads a cgroup and does not have this problem.
* **`pnpm check` is `large` and cold-reserves 8 GiB.** On a 16 GiB Mac with a
  4 GiB reserve that is two-thirds of the budget for the first three runs. It
  learns down; it will feel slow first.

## Open questions

1. **The per-suite cancel contract.** `cancelSignal: 'SIGINT'` and
   `cancelGraceMs: 240000` are real facts about what a suite needs to clean up.
   They belong in `pandora.toml` as `cancel_signal` and `cancel_grace_seconds`,
   but that is a schema change and the sharding work is in that file now.
2. **`ACME_VALIDATION_DIRECTORY`.** Should the local lane set it to the run
   directory (cheap, keeps the cleanup markers working) or should the marker
   protocol move into `pandora.toml` as a declared evidence path?
3. **Does the drift check earn its cost on every job?** Freezing acme is
   ~4,900 files. It is right for a 60-second suite and questionable for a
   4-second `node --test`. Maybe `drift` belongs on the job, not the machine.
4. **Should a local failure ever pause anything?** `assertHealthy` was blunt but
   it stopped an agent from piling onto a broken machine. Nothing replaces it.

## Proof

Daemon on an isolated state directory with `budget_mib = 1024`, no worker host.
Pueue was read-only throughout: groups unchanged (`light` 2, `heavy` 1,
`surfaces` 2, `stacks` 1, all `Running`), and no task carries a label from this
work. The machine's task count moved 219 → 220 from another agent's job.

| # | What | Result |
|---|---|---|
| 1 | `ACME_QUEUE=pandora pnpm validate node tools/check-clock-discipline.test.mjs` | **pass**, exit 0, 4.3 s wall. 11 Node tests. Receipt: `lane: local`, `size_class: small`, `reservation_mib: 1024`, `peak_mib: 132`, `drifted: false` |
| 2 | Two worktrees, one 1,024 MiB budget, two `small` jobs | **serialised.** First 361→369 s; second admitted at 370, 15.9 s wall against 8 s of execution. The wait is the queue, not the command |
| 3 | Second job in a worktree that already has one | **exit 75**, `this worktree already has an active local job (6ef3edc9ac93)`. No row in `pandora ps` for the refusal |
| 4 | A job that edits its own tracked file, `drift = warn` | **pass**, plus `the worktree changed while this job ran` |
| 5 | A `singleton` long-lived job, started twice from two worktrees | first **running** in `pandora ps`; second **exit 75** naming it and `pandora cancel <id>` |
| 6 | `pandora cancel` on that singleton | **exit 130**, `cancelled in 2.6s`, process group gone |
| 7 | `ACME_QUEUE=pandora pnpm validate node --watch` | **exit 1 in 0.3 s** with acme's own message. Nothing queued, nothing admitted |
| 8 | `ACME_QUEUE=direct` and the default `pueue` path | both unchanged |

`pandora/tests/test_local.py`: 30 tests against real processes, including a
cancel that has to reach a grandchild and a peak that has to be observed
through `ps`. Whole suite 209, green.

## Cost

| Part | Lines |
|---|---|
| `pandora/client/local.py` (new) | 413 |
| `pandora/client/daemon.py` (the local lane) | +113 / −3 |
| `pandora/client/settings.py` (`[local]`) | +17 / −2 |
| `pandora/client/shim.py` (the `busy` verdict, the lane in the notice) | +26 / −8 |
| `pandora/cli.py` (the lane column, the local line in `stats`) | +12 / −4 |
| `pandora/config/loader.py` (`where`, `singleton`, two refusals) | +14 |
| `pandora/engine/admission.py` (`check_same_thread`) | +5 / −1 |
| **implementation** | **~600** |
| `pandora/tests/test_local.py` | 340 (30 tests) |
| `pandora/config/examples/acme.pandora.toml` (the local jobs) | +72 |
| `tools/validate.mjs` (the acme seam) | +132 / −74, of which 74 is the Pueue path moved verbatim into `throughPueue()` |

Two commits on `v0.2/assembly`: `84a517a`, `3e027b1`. The sharding agent works
the same branch, and each of us swept a few of the other's uncommitted lines
into a commit (`ed040a5` carries seven of my `daemon.py` lines; `84a517a`
carries their `loader.py` work). Harmless, but the per-commit numbers above are
the file's whole delta, not only mine.
