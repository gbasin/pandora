# pandora.toml

What a repository declares in `pandora.toml`, and how the daemon behaves on
fallback, queueing, write-back, retry and placement overrides.
[agents.md](agents.md) is what an agent types against it.

## Contents

* [The repository side](#the-repository-side)
  * [Ownership](#ownership)
  * [Top-level tables](#top-level-tables)
  * [Source preparation](#source-preparation)
  * [Job keys](#job-keys)
* [Behavior tables](#behavior-tables)
  * [Fallback](#fallback)
  * [Queueing](#queueing)
  * [Write-back (`--update`)](#write-back---update)
  * [One automatic infrastructure retry](#one-automatic-infrastructure-retry)
  * [Placement override refusals](#placement-override-refusals)

## The repository side

A repository declares which commands Pandora routes in `pandora.toml` at its
root. The
loader looks there first, and uses the enrollment's `--config` path only when
the root has none. The schema is closed: an unknown key is refused with the
allowed set printed.

The file is the contract between the repository and the daemon. When the
running daemon does not understand a key or a value, for example a daemon
started before `subdirectory = "passthrough"` existed, each claimed command is
refused with exit 70 and nothing runs. The refusal names the key and the value
and gives the fix. On an installed version the fix is `git -C <source
checkout> pull && pandora upgrade`, which restarts the daemon at a safe moment.
On an older install that runs the checkout it is `git -C <checkout> pull &&
pandora daemon --restart`, once `pandora ps` shows nothing running. If the key
is a mistake, fix the file. The claim cache keeps the file's claimed forms meanwhile, so these
commands reach the refusal instead of running unmanaged. A file that is not
valid TOML, or that the daemon cannot read, claims nothing, and every command
runs as if Pandora were not installed. The reference is
[`pandora/config/examples/eichler.pandora.toml`](../pandora/config/examples/eichler.pandora.toml),
with the sharded surface job in
[`eichler-surfaces.pandora.toml`](../pandora/config/examples/eichler-surfaces.pandora.toml).
Both carry the reasoning behind each value as comments.

### Ownership

The repository owns the runner. It decides what a suite is, which arguments are
legal, how a suite is cut into shards, and what it writes. Pandora reads the
command line only as far as the job declares: the claimed prefix, the declared
options and value flags, and the `args` and `reject` rules.

Pandora owns placement, the snapshot, admission and delivery. It decides where a
command runs, freezes the worktree into a manifest, ships it into a
content-addressed source cache, admits the run against a memory budget, clones
an instance, streams the output, copies the declared paths into the worktree,
and exits with
the command's code.

Sharding has two tiers. A tier-1 job appends a shard flag, or sets shard
variables, and runs N instances.
Its result says `unverified`, because nothing describes the partition. A tier-2
job also declares a `plan` step that builds once and lists the partition. The
run is `verified` only when every shard filed a report and the observed test ids
equal the planned partition exactly.

### Top-level tables

| Table | What it holds |
|---|---|
| `version` | `1`. |
| `[repo]` | `name`, `entrypoints` (today `["pnpm"]`), `root_markers`. |
| `[matching]` | `strip_prefixes` (wrapper tokens removed before matching, such as `run` and `validate`). `subdirectory = "reroot"`, `"reject"` or `"passthrough"`: what a claimed command typed below the worktree root does. `reroot` runs it from the root unless an argument names a path, then exits 64. `reject` refuses it with exit 1. `local` is an old name for `reroot`. `passthrough` claims it only at the root, so below it the command runs unchanged, like an unclaimed one, decided in the shim with no fork. Use it when bare root forms (`test`, `build`) mean a package's own script in a subdirectory. |
| `[feedback]` | `reject_suffix`, `extra_message`: text added to refusals. |
| `[env]` | `set`, `passthrough`, `unset`, `reject_if_set`. Only the caller's variables named in `passthrough` reach the run, under `set` and the job's `run.env`. `unset` applies to all three. A declared name that is secret-shaped or describes this Mac (`PATH`, `LANG`, `NODE_OPTIONS`) is never forwarded, and is named on stderr. `reject_if_set` is checked against the caller's whole environment, so it can refuse on those names too. |
| `[secrets]` | `exclude_globs`: paths never frozen or shipped. |
| `[worker]` | Required. Golden toolchain, with `base_image` required: `base_image`, `packages`, `node_version`, `pnpm_version`, `service_images`, `install_command`, `source_id`, `env`, `workdir`. `workdir` sets only the canary's working directory. Routed runs ignore it. `prepare_command` is an optional per-run hook described below. It does not change the golden fingerprint. |
| `[fallback]` | Optional repository-wide fallback. Eichler declares none on purpose. |
| `[[jobs]]` | One entry per routed job. |

The invoking worktree's `pandora.toml` owns its routing. An enrollment config
remains valid for the enrolled checkout. Sibling worktrees using that fallback
must contain the declared root markers and directly named runner scripts.
Otherwise ordinary commands pass through locally before submission. Explicit
remote and armed writeback requests are refused instead.

A connection lost after sending a submission has an uncertain outcome. The
client exits 70 without replaying locally. Check `pandora ps` before retrying.

### Source preparation

Set `prepare_command` under `[worker]` when a repository needs to refresh
dependencies after each source transfer. It is a shell command string, for
example `prepare_command = "my-package-manager install"`. Pandora runs it once
inside each remote run's private clone, from `/work`, after injecting the frozen
source and before starting the job. It runs even when the golden is warm. The
local lane does not run it.

The hook receives the run's environment and uses the same memory ceiling and
cancellation supervision as a remote command, and a wall-time limit of its own
equal to the command's. Its output appears in the run log. A failed hook stops
the job, and the result records the preparation outcome and exit in
`evidence.preparation`. A nonzero exit, an `oom` or a timeout gives an
infrastructure result with CLI exit 70, not the job command's exit. A
cancel gives CLI exit 130. The clone is destroyed after either result.

### Job keys

| Key | Meaning |
|---|---|
| `id`, `summary`, `usage` | Name, one-line description, and the usage line printed on a refusal. |
| `forms` | The argv prefixes the job claims, such as `[{ prefix = ["journey"] }]`. |
| `where` | `remote` (default) or `local`. Local is the daemon's own lane: the same kind of queue, admission, receipt and exit contract, on the Mac. |
| `size` | `small`, `medium` (default), `large` or `xlarge`: memory ceilings of 1, 4, 8 and 12 GiB. The declared size is the starting class. On the worker, after 3 clean runs under the current declaration, the class becomes the one that p95 of those peaks times 1.25 fits, up or down, and never one whose ceiling is below the newest peak times 1.25. A fan-out's plan step and its shards learn separately. The class sets the ceiling, which is the hard cap. The reservation is p95 of the job's recent peaks times 1.25, capped at that ceiling, or the whole ceiling for its first 3 runs. An `oom` resets the class to the declared one, and learning starts again after 3 more clean runs. A change prints `pandora: size for <job>: <old> -> <new> (p95 N MiB over K runs)`, and `pandora result --json` records `size_declared` and `size_used`. A changed `size` restarts learning from the new value. The declared size also decides fallback. |
| `args` | `none` (default), `required` or `optional`. `on_extra = { action = "local" }` lets extra arguments fall out of the claim instead of being refused. A refusal exits 1. |
| `validate` | `{ argv, timeout_ms, cwd, env }`: the repository's own pre-flight check, run in the worktree before anything is frozen or queued. `timeout_ms` defaults to 5000, range 50 to 60000. Exit 0 means "I would run this". Anything else is the repository's refusal, shown as is, with the validator's own exit code. |
| `run` | `{ argv, env, unset, cwd }`: the command. `{args}` places the caller's arguments. `cwd` applies to local runs only. A remote run always starts in `/work`, the worktree root. |
| `outputs` | Entries `{ kind, paths, requires_option }`. `kind` is `artifacts` (remote paths brought home), `writeback` (files an armed option may rewrite, described below) or `evidence` (local jobs only: paths the receipt records as present or absent). A `writeback` output must name a `requires_option` that a `writeback = true` option sets. |
| `options` | `{ name, sets, forward, writeback }`. `writeback = true` arms the job's `writeback` outputs when the option is typed. Eichler's `--update` is one. |
| `value_flags` | Flags whose value is not a path, so the subdirectory rule does not check it. |
| `shards` | `strategy` (`argv` or `env`), `template` (`--shard={i}/{n}`), `env` (required with `strategy = "env"`), `default`, `max`, and for tier 2 `plan`, `expect_flag`, `report`, `plan_outputs`. Every shard also gets `PANDORA_SHARD_INDEX` and `PANDORA_SHARD_TOTAL`. Remote only. |
| `singleton` | One at a time on this Mac across every worktree. Local only. For a job that holds ports, such as a dev stack. It does not take its worktree's `one_active_per_worktree` slot, so other local jobs still run there while it lives. |
| `reject` | `[{ args, message }]`: arguments the job refuses, with the reason. Exit 1. |
| `reject_if_set` | Environment variables that make the job refuse. Exit 1. |
| `drift` | `off`, `warn` or `fail` for a local run whose worktree changed meanwhile. Local runs only. It loads on a remote job and has no effect there. Use `off` for `small` jobs. Freezing a 4,900-file worktree twice costs more than they do. |
| `cancel` | `{ signal, grace_ms }`: `SIGINT`, `SIGTERM` (default), `SIGHUP` or `SIGQUIT`, then SIGKILL after the grace (default 15,000 ms). |
| `fallback` | `local` or `refuse`, or the long form `{ action, on, notice }`. Undeclared means the size class decides. |
| `git` | `none` (default) or `synthetic`: the worker builds a one-commit repository over the tree, indexed as this worktree's tracked set, for suites that ask git what is tracked or changed. About 3 s per run. Remote only. |
| `timeout_minutes` | Wall-clock limit for the local lane, 1 to 1440. Default 30. Remote runs are capped at 30 minutes today, whatever this says. |
| `tool`, `on_extra` | Accepted at job level. |

A write-back path may glob only its last component, below a directory:
`fixtures/*.ledger.jsonl` loads. `*.json` and `fixtures/**/x.json` are refused.


## Behavior tables

### Fallback

The daemon sends `accepted` only after the worker has admitted and named the
run. Before `accepted`, the command may run here instead, but only once the
daemon knows the worker has not started it. After `accepted`, the command never
runs here. `decide()` in `pandora/client/fallback.py` answers for every cause
the daemon sees. The two `daemon-unreachable` rows are the shim's (`no_daemon`
in `pandora/client/shim.py`).

| Cause | `small` / `medium` | `large` / `xlarge` | with `--update` |
|---|---|---|---|
| `worker-down` (known from the health poll), `worker-unreachable`, `snapshot-failed`, `transfer-failed`, `engine-error` (any other worker refusal, such as the disk floor) | local lane | refuse, 70 | refuse, 70 |
| the worker is full (memory or slots) | queues on the worker, 70 after the bound (`queue-timeout`) | queues, 70 after the bound | queues, 70 after the bound |
| `admission-refused`: the reservation is larger than the worker's whole budget | refuse, 70 | refuse, 70 | refuse, 70 |
| `engine-version`: this client's bundle is older than the worker's `min_engine_version` | refuse, 70 | refuse, 70 | refuse, 70 |
| `daemon-unreachable`, the daemon installed here (the client configuration exists) | 70 after a 5 s wait, with the doctor hint | 70 | 70 |
| `daemon-unreachable`, never installed here (no client configuration) | passthrough: runs here as if Pandora were not installed, no slot, one notice | passthrough | passthrough (writes in place) |
| the `submit` call fails and the worker cannot then be asked whether it started the run | 70 | 70 | 70 |
| any failure after `accepted` | 70 | 70 | 70 |

A failed `submit` call is not a refusal: the worker may have started the run
and lost only the reply. The daemon asks the worker once, by request id. A run
the worker started is attached to as if `accepted` had arrived. A request the
worker never received is fenced, so a late copy cannot start, and falls back as
above. A worker that cannot be asked ends the run with exit 70 and the message
"execution is uncertain; check `pandora ps` before retrying".

A job's `fallback = "local"` or `"refuse"` overrides the size column, except
for a busy worker: `admission-refused` and `queue-timeout` refuse whatever the
job declares. The local
lane is the same kind of queue, memory admission and receipt as any local job, recorded
as `fallback:<cause>`. A refusal prints the cause and the next step, and
nothing runs. An `engine-version` refusal's next step is `pandora upgrade`,
not a lane. Otherwise the next step is "retry, or run it in the local queue with
`PANDORA_WHERE=local`" when the job can run in the local lane. Only for a job
that cannot (a sharded one) does it name `PANDORA_OFF=1`, as a last resort. A
local run refused by the memory-pressure gate says to wait and retry, and not to
bypass it.

### Queueing

When the worker's memory or its run slots (8 at once) are full, the worker
queues the run, and it waits there before `accepted`. The caller sees
`pandora: queued on the worker behind 3 runs (position 2), ~4m10s;
gives up after 10m00s`, then at most once a minute `still queued behind N
(position P)`. The heartbeat continues under the wait, so the client does not
time out. The worker has one queue for every client, every job and every shard, in
arrival order. The oldest waiting run is admitted first. A large run at the
head blocks smaller runs behind it, even when they would fit. There is no
priority and no per-client share.

The wait is bounded by the job's own history, not by a configuration key:
`max(120 s, min(1800 s, 3 x p50))` of how long the job's last runs held the
worker, from admission to finish. With fewer than 3 such runs, the bound is
600 s. At the bound the run ends `infra_failed`, cause `queue-timeout`, exit 70.
It never falls back and is never retried. `pandora cancel` withdraws a queued
run. A daemon restart that drains withdraws it too, and the caller submits it
again at the back of the queue. A daemon that crashes leaves the run queued on
the worker, and the next daemon adopts it. The original command exits 70, and
`pandora wait <id>` follows the run. A queued write-back run is stopped
instead. A reservation larger than the worker's
whole budget still refuses at once with exit 70, because waiting cannot fix it.
The disk floor also refuses a single run at once. A sharded run's shards are
not checked against it. The client treats that refusal as
`engine-error`, so the [Fallback](#fallback) table decides.

`pandora run --detach` returns at the first queue line with the run id. The run
stays queued. `pandora wait <id>` follows it through the queue. A drained
restart does not withdraw a detached run. The next daemon follows it.

### Write-back (`--update`)

A write-back is one publication: every declared file the run changed, or none.
Each file is written by temporary file, `fsync` and rename. A crash mid-publication
can leave part of the set written. The run's record says which. Write-back never
deletes.

| Case | Written | Exit |
|---|---|---|
| Run passed, and every changed declared file is still as frozen here | The worker's version of each | 0 |
| Run passed and changed nothing declared | Nothing | 0 |
| A declared file the run changed was edited here during the run | Nothing | 75, with `pandora resolve <id> --keep-local` or `--take-worker` |
| A declared file absent at freeze was created by the run and is still absent here | The new file | 0 |
| The same, but a file now exists here at that path | Nothing | 75, as a conflict |
| A declared file present at freeze is absent after the run | Nothing | 70 |
| A local file is already byte-equal to the proposal | Skipped for that file | 0 |
| Stale: any file outside the declared paths changed during the run | Nothing | 75. Re-run with `--update` |
| A shard failed, was never dispatched, filed no report, or the partition is unverified | Nothing from any shard | The command's own non-zero, or 70 |
| Two shards changed one declared file differently | Nothing | 75 |
| Two shards changed disjoint top-level keys of one JSON object written in a standard `json.dumps` layout | The merged file | 0 |
| Run failed or canceled | Nothing | The run's own, or 130 |
| Run ended `oom` or `timed_out` | Nothing | 70 |
| Proposed bytes arrive with the wrong digest, or the fetch fails | Nothing | 70 |
| Infrastructure failure before `accepted` | Nothing, and nothing runs here | 70 |

`--keep-local` records that the declared files as they are now are the answer,
writes nothing, and says the result was not validated. `--take-worker` publishes
the whole proposal, but only over files still exactly as the conflict report saw
them. Validate without `--update` after every update.

### One automatic infrastructure retry

A remote run that ends `infra_failed` is resubmitted once, remote to remote,
only when no line of the command's own output reached the caller and the cause
is retryable. A sharded run retries the failed shard alone. Nothing on this path
runs locally.

| Cause | Retried | Why |
|---|---|---|
| `clone-failed` | yes | A clone of an existing golden is cheap and independent. |
| `instance-lost` | yes | A fresh instance shares nothing with the lost one. |
| `execution-failed` | yes | The host could not start or feed the command. |
| `supervisor-gone` | yes | Says nothing about the input. |
| `prepare-failed` | no | A golden that failed to build fails again, at minutes a try. |
| `prepare-command-failed` | no | The transferred source preparation exited nonzero. The job did not start. |
| `prepare-command-execution-failed` | no | The worker could not supervise the source preparation. |
| `disk-quota` | no | The same quota refuses again. |
| `destroy-incomplete` | no | The command reached a verdict. |
| `partition-unverified` | no | The shards ran. Their reports will not change. |
| `shard-failed` | no | The shard already had its own retry. |
| `admission-timeout` | no | A retry joins the same full queue. |
| `queue-timeout` | no | The worker queue did not admit it within its bound. A retry joins the same queue. |
| `engine-error` | no | An unrecognized failure is not retried. |
| `worker-lost` | no | The run may still be executing. |

`oom`, `timed_out`, `cancelled` and `command_failed` are verdicts and are never
retried. A run whose verdict differs from the previous attempt on the same
input, argv, environment and cwd is recorded as a flaky pair, per shard for fan-outs.
Recording a flaky pair does not trigger another run. The hint and `pandora
stats` report it.

### Placement override refusals

Every refusal exits 64 before the validator, the freeze or the queue.

| Job | Override | Result |
|---|---|---|
| remote | `local` | Local lane with the job's size class. The job's `fallback` and size rules do not apply: this is not a fallback. With `--update`, the run writes its files in place. |
| remote, `shards` declared | `local` | 64: the job is a fan-out across worker instances. |
| remote | `remote` | As with no override, except that no pre-accept failure falls back: 70. |
| local | `remote` | Worker, with `git = "synthetic"` forced. Any pre-accept failure: 70. |
| local, `singleton` | `remote` | 64: it holds resources on this Mac. |
| local, `evidence` outputs | `remote` | 64: nothing brings evidence home from the worker. |
| not claimed | either | Runs unchanged, and is counted in `pandora stats`. |
| any, daemon not running | `remote` | 70. |
| any | anything but `local` or `remote` | 64. |
