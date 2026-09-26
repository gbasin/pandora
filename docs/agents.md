# What agents type

`pandora --help` is the source of this contract. This file follows it, and
changes with it in the same pull request. The [README](../README.md#for-agents)
has the short version.

An agent keeps typing the commands it types today, from the
repository root. [`docs/agents-paragraphs.md`](agents-paragraphs.md) holds
the paragraphs a repository may copy into its own agent instructions.

## Invariants

The invariants, as `pandora --help` states them, with their exceptions spelled
out:

* Same exit code as a local run, and the environment the repository declares
  (see `[env]`). `$?` and traps behave.
* Declared results are in your worktree before the command exits. A missing
  report is reported as missing, never as zero failures.
* Run from the repository root. Below it, `[matching] subdirectory` decides.
  `reroot` (the default) runs a routed command from the root, and refuses one
  whose arguments name a path (exit 64) rather than run it locally. `reject`
  refuses every routed command. `passthrough` claims nothing there, so the
  command runs as if Pandora were not installed.
* Exit codes that are not the command's own:
  * 64: a path argument below the root, a placement the job cannot take, or
    an invalid `PANDORA_WHERE`. A job's own argument refusal exits 1.
  * 70: infrastructure failure, including `oom` and `timed_out`. It includes a
    daemon installed here that does not answer within 5 s. In that case
    nothing ran, and the next step is `pandora doctor`.
  * 75: busy or stale.
  * 124: `--max-wait` elapsed, and the run was not stopped.
  * 130: canceled.
* `--update` runs on the worker unless `PANDORA_WHERE=local` places it here.
  On the worker, its files come back only from a passing run (every shard) over
  a tree you did not edit meanwhile. A stale tree or a conflict exits 75, with
  your files untouched and the next step printed. `PANDORA_WHERE=local` and
  passthrough write in place with no check.
* `PANDORA_WHERE=local|remote <command>` moves one run between lanes and keeps
  its receipt and the stats. It exits 64 if the job cannot run there, and never
  falls back. `PANDORA_OFF=1 <command>` runs it here with no Pandora at all: a last resort,
  never a way around the queue or a memory-pressure refusal.
* Pandora's own lines go to stderr as `pandora: ...`. The last one may be
  `pandora: hint: ...`: the next action, derived from evidence.
* Each worktree routes by its own `pandora.toml`. An edit takes effect on the
  next command in that worktree, at the cost of one Python start. Enrolling a
  repository is consent, given once, and never again after a change.

## Exit codes

| Exit | Meaning | Do this |
|---|---|---|
| the command's own | The command's verdict. | As without Pandora. |
| 1 | The job refused these arguments (`args`, `reject`, `reject_if_set`, `subdirectory = "reject"`). Nothing ran. A validator's refusal exits with the validator's own code instead. | Read the usage line. |
| 64 | A path argument below the repository root, a placement the job cannot take, or an invalid `PANDORA_WHERE`. Nothing ran. | Run it from the repository root, or drop the override. |
| 70 | Infrastructure failure, including `oom` and `timed_out`. Not a test verdict. | Read the `pandora: hint:` line first: an `oom` needs a larger `size`. Otherwise retry. Or run it in the local queue with `PANDORA_WHERE=local <command>`. If the message says this Mac is under memory pressure, wait a few minutes, then retry. Do not bypass it. |
| 75 | A local job is already active in this worktree, the worktree changed during a local run under `drift = "fail"`, a write-back was refused as stale or conflicted, or shards wrote one path differently. Or the local queue did not admit the run within `queue_timeout_seconds`, or a restart did not finish within `PANDORA_DRAIN_WAIT`, and nothing ran. | Wait for the other run, or retry after a restart. Do not edit the worktree while a validation runs. After a write-back conflict, follow the printed `pandora resolve` step. |
| 124 | `--max-wait` elapsed. The run was not stopped. | `pandora wait <id>` re-attaches. |
| 130 | Canceled. | Nothing. |

## Variables

| Variable | Effect |
|---|---|
| `PANDORA_OFF=1` | The shim execs the real pnpm: no queue, no memory gate, no receipt. On a claimed command in an enrolled repository it first starts the passthrough logger, which runs the real pnpm and appends one row to `<state>/passthrough.jsonl`. `pandora stats` counts it as bypassed with `PANDORA_OFF`. A last resort, for a job the local lane cannot run (a sharded suite) or to debug a routed failure. Never use it to skip the queue or after a memory-pressure refusal. |
| `PANDORA_WHERE=local` or `remote` | Place this one run. It keeps its queue, receipt and exit code. Exit 64 if the job cannot run there. An explicit `remote` never falls back. If the worker cannot take it, the exit is 70. |
| `PANDORA_SHARDS=N` | Shard count for this run of a sharded job, clamped to the job's `max` and to free lanes. |
| `PANDORA_DRAIN_WAIT=S` | How long a command waits for a daemon restart, in seconds. Default 660. Then it exits 75, and nothing ran. |
| `PANDORA_SESSION=<id>` | Names the session that submitted the run. The run records it as `submitter`. Without it, `CLAUDE_CODE_SESSION_ID` (Claude Code) or `CODEX_COMPANION_SESSION_ID` (the Codex plugin) is used. Without any of them, the daemon records the top interactive process above the caller, as `name:pid`, from the socket's peer pid and one `ps` of its own. The client runs none. |
| `PANDORA_KEEP_GOING=1` | A fan-out keeps dispatching shards after one fails. |
| `PANDORA_CONFIG=<path>` | Names the client configuration instead of `~/.config/pandora/config.toml`. |
| `PANDORA_HOME=<dir>` | Sets the package directory the launchers run, instead of the one they are installed in. |

The caller's `PANDORA_*` variables never reach a routed run. Pandora sets its
own: `PANDORA_CPUS`, a shard's `PANDORA_SHARD_INDEX` and `PANDORA_SHARD_TOTAL`,
a queue-fed batch's `PANDORA_BATCH_FILE`, `PANDORA_BATCH_INDEX` and
`PANDORA_BATCH_REPORT`, and in the local lane `PANDORA_RUN` and
`PANDORA_RUN_DIR`. A command run with `PANDORA_OFF`, passed through or not
claimed gets the whole environment.

## Verbs

| Command | What it does |
|---|---|
| `pandora ps [--json] [--limit N]` | Every active run and the latest 20 completed runs. `--limit` selects 0–200 completed runs, for both text and JSON. Worker health includes the client name and other clients' live counts. Pressure includes the sample age. While a restart drains, `daemon: draining` comes first. A remote run not yet accepted shows `freezing`, `shipping`, `submitting` or `queued #N` (its place in the worker queue, `queue` in JSON). JSON includes each run's `submitter` and `client`. |
| `pandora wait <id> [--max-wait S]` | Re-attach and exit as the run exits. Several ids print one outcome line each and exit non-zero if any did not pass. A run no daemon follows any more is taken over, or closed with exit 70. A wait never hangs on it. |
| `pandora logs <id>` | Replay a run's output. Who submitted it goes to stderr first. |
| `pandora result <id> [--json]` | Outcome, exit, submitter, client, attempts, flaky pairs and hint, and the path to the run's `trace.json` -- a Perfetto timeline of its phases that opens at ui.perfetto.dev. `--json` prints the whole result, with per-shard outcomes and the input digest. A run refused before it reached the worker has no result: this prints the refusal's cause and detail and exits 70. |
| `pandora cancel <id>` | Stop a run. A remote instance is destroyed. A run in the worker queue is withdrawn, and nothing ran. A local run whose daemon has exited ends `cancelled`, exit 130, and its process tree is stopped when it is still the run's. |
| `pandora resolve <id> --keep-local` or `--take-worker` | Settle a conflicted `--update` write-back. |
| `pandora stats [--since 24h] [--json]` | What routed, where, how long it waited and ran, how long runs waited in the worker queue (p50, p95) and how many ended `queue-timeout`, what fell back and why, which hint rules fired and on which jobs, what claimed commands were bypassed with `PANDORA_OFF`, what heavy commands ran here unclaimed, and the worker's disk, goldens, ready state and runs per client. Its `history:` line says how many days of runs are kept (`keep_runs_days`) and when the oldest kept run started. |
| `pandora doctor [--json]` | Check this shell and worktree. Changes nothing. |
| `pandora selftest [--update] [--json]` | One real submission through the whole routed path on the real worker: the shim claims `pnpm selftest` in a scratch repository, an isolated test daemon on a scratch socket submits it, the engine runs it in an incus instance, and the receipt comes home. It costs one small incus run, recorded on the worker as client `e2e-<host>`, and it never touches the live daemon, config or state. Exits 0 the path worked, 1 a run failed, 70 the path could not be exercised. `--update` adds a second run whose declared write-back must land in the scratch worktree. |
| `pandora run --detach -- <pnpm args>` | Submit, print the run id, return: at `accepted`, or at once when the run is queued on the worker. For orchestrators. `--local` and `--remote` place the run. |

`ps` reads the daemon's published status without probing the host or worker.
Pressure is sampled when local admission needs it. Its age can therefore grow
while the local lane is idle. The daemon retains up to 200 completed rows for
status and rebuilds that view at startup. Older results remain available by
run ID. `ps` allows two seconds for a daemon response. An unavailable or
unresponsive daemon returns exit 70 and reports run status as unknown. JSON
sets `daemon.responding` to false. Its empty `runs` list does not mean idle.
No historical files are read as a fallback.

Ctrl-C on a routed command cancels it. Killing the shim (for example, when an
agent's tool call times out) does not. The run continues, and `pandora wait
<id>` finishes it.

## Caller output

Pandora's lines go to stderr and start with `pandora:`. The command's own
output stays on stdout. A remote command's stderr is merged into that stream. A remote run prints progress lines: `syncing N files`
on a source-cache miss (also kept in the run's log for `pandora logs`),
`instance ready in N s`, `running (typical 4m10s for check; cpus hint 2)` once
three earlier runs exist, queue lines for the worker queue, shards and the
local lane, and a retry line when one happens. The last line may be a hint:

```
pandora: hint: review `git diff` of 2 updated files, then validate without --update
```

A hint comes from evidence. The rules, worst first: `oom` (with the peak and a
larger size class), `timed_out`, `drifted` (under `drift = "fail"` only),
`flaky`, a program the worker does not have, a missing report, a write-back to
review, a path in the log that Git ignores and the snapshot therefore did not
ship.

A job that runs on the worker gets `PANDORA_CPUS`, the host's cores divided by
the runs admitted when it starts. A runner can use it for its own parallelism.
