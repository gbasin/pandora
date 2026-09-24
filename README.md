# Pandora

Pandora runs a repository's heavy validation on a Linux worker while coding
agents, source edits and Git worktrees stay on the Mac. An agent types the
command it already types (`pnpm check`, `pnpm journey S0-01`). A POSIX `pnpm`
shim first on PATH asks a per-user daemon whether the repository claims that
command. A claimed command is frozen, shipped, admitted on memory and run in a
fresh Incus instance cloned from the repository's golden image, or in the
daemon's local lane when the job belongs on the Mac. Declared results come home
before the command exits, with the command's own exit code. An unclaimed command
runs as if Pandora were not installed. The repository declares what it owns in
`pandora.toml`; the Mac declares where the worker is in
`~/.config/pandora/config.toml`.

The invariants, as `pandora --help` states them:

* Same cwd, environment and exit code as a local run; `$?` and traps behave.
* Declared results are in your worktree before the command exits. A missing
  report is reported as missing, never as zero failures.
* Run from the repository root. Below it, `[matching] subdirectory` decides.
  `reroot` (the default) runs a routed command from the root, and refuses one
  whose arguments name a path (exit 64) rather than run it locally. `reject`
  refuses every routed command. `passthrough` claims nothing there, so the
  command runs as if Pandora were not installed.
* Exit codes that are not the command's own: 70 infrastructure failure, never a
  test verdict; 75 busy or stale; 124 `--max-wait` elapsed and the run was not
  stopped; 130 canceled.
* `--update` runs on the worker, never here. Its files come back only from a
  passing run (every shard) over a tree you did not edit meanwhile; otherwise
  exit 75, your files untouched, and the next step printed.
* `PANDORA_WHERE=local|remote <command>` moves one run between lanes and keeps
  the queue and the stats; 64 if the job cannot run there, never a fallback.
  `PANDORA_OFF=1 <command>` runs it here with no Pandora at all: a last resort,
  never a way around the queue or a memory-pressure refusal.
* Pandora's own lines go to stderr as `pandora: ...`. The last one may be
  `pandora: hint: ...`: the next action, derived from evidence.

v0.2 is proved against one repository (eichler), one worker (4 vCPU, 15.6 GiB,
x86_64 Ubuntu 26.04) and one Mac. Read [Operating limits](#operating-limits)
before you rely on it.

## Install on a Mac

The install is machine-wide and changes nothing in the target repository. It
has five parts: the two launchers on PATH, one configuration file, the daemon,
one enrollment per repository, and `pandora doctor` to prove the result.

### Prerequisites

* macOS. The daemon has run only on macOS; its Linux peer-credential branch is
  untested.
* Python 3.11 or later as `python3` on PATH. Pandora imports `tomllib`.
  `/usr/bin/python3` on macOS is 3.9 and cannot run it. To use another
  interpreter, set `PANDORA_PYTHON` to its path.
* Git, rsync and OpenSSH.
* The real `pnpm` on PATH. The shim finds it by walking PATH past itself.
* An SSH key that logs in to the worker without a prompt. Pandora uses your SSH
  configuration. The daemon keeps one control master per worker; `pandora
  worker` verbs keep their own, so they never close the daemon's.
* A provisioned worker. See [The worker](#the-worker).

### 1. Clone the checkout

```sh
git clone https://github.com/gbasin/pandora.git ~/Code/pandora
```

Check out the v0.2 line. Until it merges to `main`, that is `v0.2/assembly`.

```sh
git -C ~/Code/pandora checkout v0.2/assembly
```

The launchers run the package from this checkout. Do not delete or move it
while the daemon runs.

### 2. Put the launchers first on PATH

Link both launchers into one directory.

```sh
mkdir -p ~/.local/bin
ln -s ~/Code/pandora/bin/pandora ~/.local/bin/pandora
ln -s ~/Code/pandora/bin/pnpm ~/.local/bin/pnpm
```

Create the shim marker beside them.

```sh
touch ~/.local/bin/.pandora-shim
```

The marker tells a repository's own job runner which PATH entry holds a command
shim, so that a child process it spawns does not route a second time. Eichler's
`tools/validation/state.mjs` reads it. `pandora doctor` checks for it.

`~/.local/bin` must come before the directory of the real `pnpm` in every shell
an agent uses. That includes non-interactive shells, such as the Claude Code
bash tool and Codex lanes. On a Mac whose `~/.zshenv` prepends
`/opt/homebrew/bin`, a PATH line in `~/.zshrc` is not enough. Add the line to
`~/.zshenv`, after the Homebrew line.

```sh
printf 'export PATH="$HOME/.local/bin:$PATH"\n' >> ~/.zshenv
```

Open a new shell. Confirm that the shim wins in a non-interactive shell.

```sh
zsh -c 'command -v pnpm'
```

The output must be `~/.local/bin/pnpm`, expanded.

The shim skips its own directory by comparing strings. If your PATH spells that
directory differently (another symlink, a trailing slash), set
`PANDORA_SHIM_DIR` to the spelling on PATH.

### 3. Write the client configuration

Create `~/.config/pandora/config.toml`. This is a complete minimal file.

```toml
[worker]
host = "ubuntu@WORKER_IP"          # the SSH destination
engine_root = "pandora-engine"     # relative to the worker user's home

[client]
state = "~/.local/state/pandora/default"   # socket, run logs, receipts

[[repos]]
name = "eichler"                   # must match [repo] name in pandora.toml
root = "/Users/YOU/Code/eichler"   # the main checkout or any worktree of it
# config = "~/.config/pandora/repos/eichler.pandora.toml"
#   only for a repository that has no pandora.toml at its root yet
```

Unknown keys are refused, with the allowed keys printed. The daemon reads this
file on every connection, so a new `[[repos]]` entry needs no restart.

The optional keys and their defaults:

| Table | Key | Default | Meaning |
|---|---|---|---|
| `[worker]` | `ssh_persist` | `10m` | SSH control-master lifetime. |
| | `health_interval_s` | `60` | How often the daemon polls the worker's health. |
| `[notify]` | `enabled` | `true` | macOS notification on a health transition (worker down or back, canary failed, disk floor, kernel drift). |
| `[client]` | `fallback_slots` | `2` | Local runs allowed at once when the daemon itself is gone. |
| | `fallback_wait_seconds` | `0` | How long such a run waits for a slot. 0 refuses at once. |
| `[local]` | `budget_mib` | `0` | Local-lane memory budget. 0 means this Mac's RAM minus `reserve_mib`. |
| | `reserve_mib` | `4096` | Memory kept for agents, editors and the OS. |
| | `max_running` | `4` | Local-lane jobs at once. |
| | `one_active_per_worktree` | `true` | A second local job in one worktree exits 75. A `singleton` job does not count. |
| | `drift` | `warn` | `off`, `warn` or `fail` when the worktree changes during a local run. A job may override it. |
| | `queue_timeout_seconds` | `0` | 0 waits for the budget as long as it takes. |
| `[local.pause]` | `enabled`, `sample_seconds`, `swap_growth_mib_per_minute`, `psi_full_avg10`, `free_percent`, `load_per_cpu`, `max_wait_seconds` | `true`, 3, 256, 20.0, 5.0, 8.0, 300 | The gate that stops new local jobs on a Mac under memory pressure. A job held past `max_wait_seconds` exits 70 and never runs. |

`pandora/client/settings.py` documents every key. It also accepts `[worker]
budget_mib` and `[client] max_wait_seconds`; nothing in v0.2 reads them.

### 4. Start the daemon

Install the launchd user agent. It runs the daemon from this checkout, restarts
it after a crash or a reboot, and logs to `<state>/logs/daemon.log`.

```sh
pandora daemon --install
```

The command writes `~/Library/LaunchAgents/com.pandora.daemon.plist`, loads it,
and prints the launchd state line. The plist pins `PANDORA_PYTHON` to the
interpreter that ran the install. If a hand-started daemon already holds the
lock, the install refuses; stop that daemon first with `pandora daemon --stop`.

After you update the checkout, restart the daemon. It runs the code it started
with.

```sh
pandora daemon --restart
```

`pandora daemon --uninstall` unloads the agent and deletes the plist. A remote
run continues on the worker while no daemon runs. The next daemon adopts it from
its recorded log offset, and `pandora wait <id>` re-attaches.

To run the daemon by hand instead, for example on a machine where launchd is not
wanted, start `pandora --config ~/.config/pandora/config.toml daemon` in the
foreground or under `nohup`. `pandora doctor` then warns that nothing restarts
it.

### 5. Enroll each repository

Write the configuration first, so the marker points at the right socket. Then
enroll the repository from any of its worktrees.

```sh
pandora enroll ~/Code/eichler
```

If the repository has no `pandora.toml` at its root yet, name one.

```sh
pandora enroll ~/Code/eichler --config ~/.config/pandora/repos/eichler.pandora.toml
```

`enroll` loads and validates the repository's configuration, then writes one
marker file, `pandora-enrolled`, into the Git common directory. One marker
covers every worktree of the repository, including worktrees created later. It
prints the `[[repos]]` block the client configuration needs. Add that block if
it is not there.

Enrollment is manual. Run it once per repository, not once per worktree: the
marker is in the Git common directory, so every worktree shares it.

Enroll again after any change to the claimed forms or to `[matching]
subdirectory` in `pandora.toml`. The shim reads the claim list and the
subdirectory mode from the marker, not from `pandora.toml`, so until you enroll
again the shim acts on the old values. `pandora doctor` does not compare the
marker with `pandora.toml` and does not report a stale claim list or mode. It
reports only a marker whose `home` names a removed checkout, or whose socket is
not the one the doctor checked.

To stop routing a repository, remove the marker.

```sh
pandora unenroll ~/Code/eichler
```

`pandora enrol` and `pandora unenrol`, the old spellings, still work for one
release. Each prints a one-line deprecation notice on stderr and then runs
`enroll` or `unenroll`. Change scripts to the new spelling.

### 6. Prove the install

Run the doctor from the root of an enrolled worktree.

```sh
cd ~/Code/eichler
pandora doctor
```

The doctor changes nothing. It asks the daemon only `ping`. It exits 1 on any
`fail`. A correct install prints this (paths shortened):

```
warn  pnpm on PATH       shim ~/.local/bin/pnpm, real pnpm /opt/homebrew/bin/pnpm (-> .../corepack/dist/pnpm.js); the real pnpm is a corepack shim, which chooses a pnpm per directory, so a local run and a worker run can use different versions
ok    recursion guard    PANDORA_ROUTE_DEPTH is not set
ok    pandora on PATH    ~/.local/bin/pandora imports ~/Code/pandora from any directory
ok    daemon             pid 47841 on ~/.local/state/pandora/default/client.sock, protocol v2, worker ubuntu@WORKER_IP, same package as the client
ok    worker             worker: reachable (disk 7.8 GiB free; polled 25s ago), from the daemon
ok    repository         enrolled as eichler: 20 claimed form(s), marker ~/Code/eichler/.git/pandora-enrolled
ok    marker forms       the marker matches this worktree's pandora.toml
ok    daemon enrollment  [[repos]] eichler at ~/Code/eichler
ok    working directory  the worktree root, ~/Code/eichler
ok    variables          none of PANDORA_OFF, PANDORA_WHERE, PANDORA_SHARDS set
ok    shim markers       .pandora-shim beside the shim only

all checks passed
```

The first line is `ok` when the real pnpm is not a version manager's shim. A
`warn` there is acceptable. Every other line must be `ok`. `pandora doctor
--json` prints the same checks with their facts.

## The worker

A worker is a disposable Linux machine. Nothing on it is the only copy of
anything: the ledger records attempts, the source cache is a cache, and each
golden rebuilds from its toolchain description. Rebuild a worker rather than
repair it. [`docs/worker-rebuild.md`](docs/worker-rebuild.md) is the full
procedure, with cut-over and upgrade cadence.

### What it needs

* x86_64 Ubuntu 26.04, at least 4 vCPU, 15 GiB of memory and 96 GB of disk.
* A spare block device of at least 40 GB for the Incus storage pool. Without
  one, a loop file works, but it is slower and adds a boot dependency.
* A `ubuntu` user with your SSH key and passwordless sudo. Check with
  `ssh ubuntu@WORKER_IP sudo -n true`.

Do not install Incus or Docker on the host. `provision` installs Incus at the
pinned version. Docker runs only inside each run's instance.

### Provision

Copy [`scripts/versions.toml`](scripts/versions.toml) to a file for this
worker. Set `device` to the spare block device. Pin `incus` and `incus-client`
to exact dpkg versions. Set `run_disk_gib`, the per-run root quota. The quota
counts bytes the run shares with its golden, so a 12 GiB quota over a 4 GiB
golden leaves the run about 8 GiB of its own writes.

Run `provision` from the Mac. `--host` belongs to `worker`, before the verb.
It defaults to `[worker] host`.

```sh
pandora worker --host ubuntu@WORKER_IP provision --versions ./versions.toml --no-canary
```

`provision` installs the declared packages, disables unattended upgrades,
creates or adopts the pool, creates the bridge, its forwarding rules, the
`pandora` project and the `runner` profile, installs the boot units, and
writes the manifest. Every step reports `present`, `created`, `changed` or
`skipped`. Run it again. The second run must report `0 changed`.

Use `--loop-file 18G` instead of `device` only when there is no spare device.

### Prove it with the canary

The canary is the health gate: about 26 checks in about 100 seconds. It reads
every enrolled repository's `pandora.toml` through the daemon's loader and
proves each distinct `[worker]` golden: it builds or reuses the golden, runs a
real journey with its compose stack in one clone, runs the surface job's
`validate` step in another, then checks the disk quota and drives a memory hog
until the watchdog kills it as `oom`.

```sh
pandora worker canary --mark
```

* What runs comes from `[worker.canary]` in the repository's `pandora.toml`:
  `journey = "S0-01"` is spliced into the `journey` job's `run` argv, with that
  job's environment. `compose = "tools/stack/compose.yml"` is brought up and
  down first. `surface = "borrower-web"` is given to the `surface` job's
  `validate`, or to its shard `plan` with one shard when it has no `validate`.
  `journey_job` and `surface_job` name different jobs. The table is not part of
  the golden's fingerprint. A key left out is a check not run, and the verdict
  says so.
* The golden is built from `<engine_root>/src/<repo>/latest` on the worker. The
  client writes that tree after each transfer, so on a fresh worker it exists
  only after the first routed run. If it is absent and the golden is not built,
  the canary fails with that reason. Run one claimed command from an enrolled
  worktree first, or pass `--source <a tree on the worker>`.
* `--journey F` and `--surfaces F` are overrides for a worker no repository is
  enrolled against yet. Each names a toolchain JSON file with the `[worker]`
  keys. A path that exists on the Mac is shipped; any other path is read on the
  worker.
* `--mark` writes the ready state from the verdict. Without it the canary only
  reports.

`provision` without `--no-canary` runs the same canary and marks the verdict.
It takes the same `--journey`, `--surfaces` and `--source` flags.

A worker that fails the canary is never `ready`. Reboot a new worker once
before it takes work. Then confirm the state again.

```sh
pandora worker status
```

`status` prints the ready state, the host and kernel, installed versions against
the manifest, pool use, the admission gate, the goldens and the last canary. A
package or kernel that differs from what the canary ran on reads `drifted`, not
`ready`. Re-run the canary with `--mark` after any change to the machine.

### Keep it

Sweep leaked instances, leaked volumes and old goldens once a week. Read the
dry run first.

```sh
pandora worker gc --dry-run
pandora worker gc
```

`gc` keeps the `--keep N` most recently used goldens per toolchain family. A
family is one repository's `[worker] source_id`, so a rebuilt toolchain pushes
out its own older goldens and never another toolchain's
([#81](https://github.com/gbasin/pandora/issues/81)). A toolchain with no
`source_id` is its own family and is never pruned by `--keep`. Goldens no
recorded attempt explains share one `(unknown)` family. The default for `N`
comes from `golden_keep` in the versions manifest, which is 2.

`gc` never removes these goldens, whatever `--keep` says:

* one a live attempt needs;
* one whose fingerprint an enrolled repository's `pandora.toml` names. The
  client computes these from `[[repos]]` and passes each as `--protect`. The
  receipt says `kept ... named by <repo> pandora.toml`. An enrolled
  configuration that does not load stops `gc` before it asks the worker;
* one named by `--protect FINGERPRINT` on the command line;
* a pinned one. Remove a pinned golden by hand.

`gc` writes a receipt under `~/pandora/worker/receipts/` on the worker.

The other worker verbs:

| Verb | What it does |
|---|---|
| `pandora worker goldens` | Each golden: repository, referenced and exclusive bytes, pinned or not, last use. |
| `pandora worker pins --toolchain F [--source D]` | Resolve a toolchain to its base-image fingerprint, lockfile digest and registry manifest digests. |
| `pandora worker reconcile` | Adopt or fail runs whose supervisor is gone after an engine restart. |
| `pandora worker retain` | Delete old attempt directories. |
| `pandora worker stats` | The engine's scheduler picture and outcome counts. |
| `pandora cache stats`, `pandora cache clear [--repo R]` | The worker's turbo remote cache, served on the runs' bridge. |

`--json`, like `--host`, goes before the verb: `pandora worker --json status`.

## The repository side

A repository declares its routed boundary in `pandora.toml` at its root. The
loader looks there first, and uses the enrollment's `--config` path only when
the root has none. The schema is closed: an unknown key is refused with the
allowed set printed. The reference is
[`pandora/config/examples/eichler.pandora.toml`](pandora/config/examples/eichler.pandora.toml),
with the sharded surface job in
[`eichler-surfaces.pandora.toml`](pandora/config/examples/eichler-surfaces.pandora.toml).
Both carry the reasoning behind each value as comments.

### Who owns what

The repository owns the runner. It decides what a suite is, which arguments are
legal, how a suite is cut into shards, and what it writes. Pandora never parses
the repository's command line beyond matching the declared prefix.

Pandora owns placement, the snapshot, admission and delivery. It decides which
lane runs a command, freezes the worktree into a manifest, ships it into a
content-addressed source cache, admits the run against a memory budget, clones
an instance, streams the output, brings the declared paths home, and exits with
the command's code.

Sharding has two tiers. A tier-1 job appends a shard flag and runs N instances;
its result says `unverified`, because nothing describes the partition. A tier-2
job also declares a `plan` step that builds once and lists the partition; the
run is `verified` only when every shard filed a report and the observed test ids
equal the planned partition exactly.

### Top-level tables

| Table | What it holds |
|---|---|
| `version` | `1`. |
| `[repo]` | `name`, `entrypoints` (today `["pnpm"]`), `root_markers`. |
| `[matching]` | `strip_prefixes` (wrapper tokens removed before matching, such as `run` and `validate`); `subdirectory = "reroot"`, `"reject"` or `"passthrough"`: what a claimed command typed below the worktree root does. `reroot` runs it from the root unless an argument names a path, then exits 64. `reject` refuses it. `passthrough` claims it only at the root, so below it the command runs unchanged, like an unclaimed one, decided in the shim with no fork. Use it when bare root forms (`test`, `build`) mean a package's own script in a subdirectory. |
| `[feedback]` | `reject_suffix`, `extra_message`: text added to refusals. |
| `[env]` | `set`, `passthrough`, `unset`, `reject_if_set`. Only the caller's variables named in `passthrough` reach the run, under `set` and the job's `run.env`; `unset` applies to all three. A declared name that is secret-shaped or describes this Mac (`PATH`, `LANG`, `NODE_OPTIONS`) is never forwarded, and is named on stderr. `reject_if_set` is checked against the caller's whole environment, so it can refuse on those names too. |
| `[secrets]` | `exclude_globs`: paths never frozen or shipped. |
| `[worker]` | Golden toolchain: `base_image`, `packages`, `node_version`, `pnpm_version`, `service_images`, `install_command`, `source_id`, `env`, `workdir`. `prepare_command` is an optional per-run hook described below; it does not change the golden fingerprint. |
| `[fallback]` | Optional repository-wide fallback. Eichler declares none on purpose. |
| `[[jobs]]` | One entry per routed job. |

The invoking worktree's `pandora.toml` owns its routing. An enrollment config
remains valid for the enrolled checkout. Sibling worktrees using that fallback
must contain the declared root markers and directly named runner scripts.
Otherwise ordinary commands pass through locally before submission. Explicit
remote and armed writeback requests are refused instead.

A connection lost after sending a submission has an uncertain outcome. The
client exits 70 without replaying locally. Check `pandora ps` before retrying.

### Preparing transferred source

Set `prepare_command` under `[worker]` when a repository needs to refresh
dependencies after each source transfer. It is a shell command string, for
example `prepare_command = "my-package-manager install"`. Pandora runs it once
inside each remote run's private clone, from `/work`, after injecting the frozen
source and before starting the job. It runs even when the golden is warm. The
local lane does not run it.

The hook receives the run's environment and uses the same memory ceiling,
wall-time limit and cancellation supervision as a remote command. Its output
appears in the run log. A failed hook stops the job; the result records the
preparation outcome and exit in `evidence.preparation`. A nonzero exit gives
an infrastructure result with CLI exit 70, not the job command's exit. A
cancel gives CLI exit 130. The clone is destroyed after either result.

### What a job declares

| Key | Meaning |
|---|---|
| `id`, `summary`, `usage` | Name, one-line description, and the usage line printed on a refusal. |
| `forms` | The argv prefixes the job claims, such as `[{ prefix = ["journey"] }]`. |
| `where` | `remote` (default) or `local`. Local is the daemon's own lane: the same queue, admission, receipt and exit contract, on the Mac. |
| `size` | `small`, `medium` (default), `large` or `xlarge`: memory ceilings of 1, 4, 8 and 12 GiB. The reservation is learned from observed peaks after 3 samples; the ceiling is the hard cap. The size also decides fallback. |
| `args` | `none` (default), `required` or `optional`. `on_extra = { action = "local" }` lets extra arguments fall out of the claim instead of being refused. |
| `validate` | `{ argv, timeout_ms }`: the repository's own pre-flight check, run in the worktree before anything is frozen or queued. Exit 0 means "I would run this"; anything else is the repository's refusal, shown as is. |
| `run` | `{ argv, env, unset, cwd }`: the command. `{args}` places the caller's arguments. |
| `outputs` | `artifacts` (remote paths brought home), `writeback` (files an armed option may rewrite; see below), `evidence` (local jobs only: paths the receipt records as present or absent). |
| `options` | `{ name, sets, forward, writeback }`. `writeback = true` arms the job's `writeback` outputs when the option is typed. Eichler's `--update` is one. |
| `value_flags` | Flags whose value is not a path, so the subdirectory rule does not check it. |
| `shards` | `strategy` (`argv` or `env`), `template` (`--shard={i}/{n}`), `default`, `max`, and for tier 2 `plan`, `expect_flag`, `report`, `plan_outputs`. Every shard also gets `PANDORA_SHARD_INDEX` and `PANDORA_SHARD_TOTAL`. Remote only. |
| `singleton` | One at a time on this Mac across every worktree. Local only. For a job that holds ports, such as a dev stack. It does not take its worktree's `one_active_per_worktree` slot, so other local jobs still run there while it lives. |
| `reject` | `[{ args, message }]`: arguments the job refuses, with the reason. |
| `reject_if_set` | Environment variables that make the job refuse. |
| `drift` | `off`, `warn` or `fail` for a local run whose worktree changed meanwhile. Use `off` for `small` jobs; freezing a 4,900-file worktree twice costs more than they do. |
| `cancel` | `{ signal, grace_ms }`: `SIGINT`, `SIGTERM` (default), `SIGHUP` or `SIGQUIT`, then SIGKILL after the grace (default 15,000 ms). |
| `fallback` | `local` or `refuse`. Undeclared means the size class decides. |
| `git` | `none` (default) or `synthetic`: the worker builds a one-commit repository over the tree, indexed as this worktree's tracked set, for suites that ask git what is tracked or changed. About 3 s per run. Remote only. |
| `timeout_minutes` | Wall-clock limit, 1 to 1440. Default 30. |

A write-back path may glob only its last component, below a directory:
`fixtures/*.ledger.jsonl` loads; `*.json` and `fixtures/**/x.json` are refused.

## What agents type

Nothing new. An agent keeps typing the commands it types today, from the
repository root. [`docs/agents-paragraphs.md`](docs/agents-paragraphs.md) is the
final text for a repository's `AGENTS.md` and its validation notes.

### Exit codes

| Exit | Meaning | Do this |
|---|---|---|
| the command's own | The command's verdict. | As without Pandora. |
| 64 | The command cannot run as typed: a path argument below the repository root, a placement the job cannot take, or an invalid `PANDORA_WHERE`. Nothing ran. | Run it from the repository root, or drop the override. |
| 70 | Infrastructure failure. Not a test verdict. | Retry. Or run it in the local queue with `PANDORA_WHERE=local <command>`. If the message says this Mac is under memory pressure, wait a few minutes, then retry; do not bypass it. |
| 75 | A local job is already active in this worktree, the worktree changed during a local run under `drift = "fail"`, a write-back was refused as stale or conflicted, or shards wrote one path differently. | Wait for the other run. Do not edit the worktree while a validation runs. After a write-back conflict, follow the printed `pandora resolve` step. |
| 124 | `--max-wait` elapsed. The run was not stopped. | `pandora wait <id>` re-attaches. |
| 130 | Canceled. | Nothing. |

### Variables

| Variable | Effect |
|---|---|
| `PANDORA_OFF=1` | The shim execs the real pnpm: no queue, no memory gate, no receipt. On a claimed command in an enrolled repository it first starts the passthrough logger, which runs the real pnpm and appends one row to `<state>/passthrough.jsonl`; `pandora stats` counts it as bypassed with `PANDORA_OFF`. A last resort, for a job the local lane cannot run (a sharded suite) or to debug a routed failure. Never use it to skip the queue or after a memory-pressure refusal. |
| `PANDORA_WHERE=local` or `remote` | Place this one run. It keeps its queue, receipt and exit code. Exit 64 if the job cannot run there. An explicit `remote` never falls back; if the worker cannot take it, the exit is 70. |
| `PANDORA_SHARDS=N` | Shard count for this run of a sharded job, clamped to the job's `max` and to free lanes. |

`PANDORA_*` variables are read by the outermost shim and never reach the run.

### Verbs

| Command | What it does |
|---|---|
| `pandora ps [--json]` | What is running and what just ran, with the worker's health on the first line. A remote run not yet accepted shows its step: `freezing`, `shipping` or `submitting`. |
| `pandora wait <id> [--max-wait S]` | Re-attach and exit as the run exits. Several ids print one outcome line each and exit non-zero if any did not pass. |
| `pandora logs <id>` | Replay a run's output. |
| `pandora result <id> [--json]` | Outcome, exit, attempts, flaky pairs and hint. `--json` prints the whole result, with per-shard outcomes and the input digest. A run refused before it reached the worker has no result: this prints the refusal's cause and detail and exits 70. |
| `pandora cancel <id>` | Stop a run. A remote instance is destroyed. |
| `pandora resolve <id> --keep-local` or `--take-worker` | Settle a conflicted `--update` write-back. |
| `pandora stats [--since 24h] [--json]` | What routed, where, how long it waited and ran, what fell back and why, what claimed commands were bypassed with `PANDORA_OFF`, what heavy commands ran here unclaimed, and the worker's disk, goldens and ready state. |
| `pandora doctor [--json]` | Check this shell and worktree. Changes nothing. |
| `pandora run --detach -- <pnpm args>` | Submit, print the run id, return. For orchestrators. `--local` and `--remote` place the run. |

Ctrl-C on a routed command cancels it. Killing the shim (for example, when an
agent's tool call times out) does not; the run continues, and `pandora wait
<id>` finishes it.

### What the caller sees

Pandora's lines go to stderr and start with `pandora:`. The command's own
output stays on stdout. A remote run prints progress lines: `syncing N files`
on a source-cache miss (also kept in the run's log for `pandora logs`), `instance ready in N s`, `running (typical 4m10s for
check; cpus hint 2)` once three earlier runs exist, queue lines for shards and
the local lane, and a retry line when one happens. The last line may be a hint:

```
pandora: hint: review `git diff` of 1 updated files, then validate without --update
```

A hint comes from evidence. The rules, worst first: `oom` (with the peak and a
larger size class), `timed_out`, `drifted` (under `drift = "fail"` only),
`flaky`, a missing report, a write-back to review, a path in the log that Git
ignores and the snapshot therefore did not ship.

A job that runs on the worker gets `PANDORA_CPUS`, the host's cores divided by
the runs admitted when it starts. A runner can use it for its own parallelism.

## Behavior tables

### Fallback

`accepted` is the line the design hangs on. The daemon sends it only after the
engine has admitted and named the run. Before it, the command provably has not
run anywhere, so it may run here instead. After it, the command never runs
here. `decide()` in `pandora/client/fallback.py` answers whether it should.

| Cause | `small` / `medium` | `large` / `xlarge` | with `--update` |
|---|---|---|---|
| `worker-down` (known from the health poll), `worker-unreachable`, `snapshot-failed`, `transfer-failed`, `queue-timeout`, `admission-refused`, `engine-error` | local lane | refuse, 70 | refuse, 70 |
| `daemon-unreachable` (no daemon answers the socket) | passthrough: runs here as if Pandora were not installed, no slot, one notice | passthrough | passthrough (writes in place) |
| the `submit` call fails and the engine cannot then be asked whether it started the run | 70 | 70 | 70 |
| any failure after `accepted` | 70 | 70 | 70 |

A failed `submit` call is not a refusal: the engine may have started the run
and lost only the reply. The daemon asks the engine once, by request id. A run
the engine started is attached to as if `accepted` had arrived. A request the
engine never saw is fenced, so a late copy cannot start, and falls back as
above. A worker that cannot be asked ends the run with exit 70 and the message
"execution is uncertain; check `pandora ps` before retrying".

A job's `fallback = "local"` or `"refuse"` overrides the size column. The local
lane is the same queue, memory admission and receipt as any local job, recorded
as `fallback:<cause>`. A refusal prints the cause and the next step, and
nothing runs. The next step is "retry, or run it in the local queue with
`PANDORA_WHERE=local`" when the job can run in the local lane. Only for a job
that cannot (a sharded one) does it name `PANDORA_OFF=1`, as a last resort. A
local run refused by the memory-pressure gate says to wait and retry, and not to
bypass it.

### Write-back (`--update`)

A write-back is one publication: every declared file the run changed, or none.
Each file is written by temporary file, `fsync` and rename. Write-back never
deletes.

| Case | Written | Exit |
|---|---|---|
| Run passed; every changed declared file is still as frozen here | The worker's version of each | 0 |
| Run passed; changed nothing declared | Nothing | 0 |
| A declared file the run changed was edited here during the run | Nothing | 75, with `pandora resolve <id> --keep-local` or `--take-worker` |
| A declared file absent at freeze was created by the run and is still absent here | The new file | 0 |
| The same, but a file now exists here at that path | Nothing | 75, as a conflict |
| A declared file present at freeze is absent after the run | Nothing | 70 |
| A local file is already byte-equal to the proposal | Skipped for that file | 0 |
| Stale: any file outside the declared paths changed during the run | Nothing | 75; re-run with `--update` |
| A shard failed, was never dispatched, filed no report, or the partition is unverified | Nothing from any shard | The command's own non-zero, or 70 |
| Two shards changed one declared file differently | Nothing | 75 |
| Two shards changed disjoint top-level keys of one JSON object | The merged file | 0 |
| Run failed, oom, timed out or canceled | Nothing | The run's own |
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
| `prepare-command-failed` | no | The transferred source preparation exited nonzero; the job did not start. |
| `prepare-command-execution-failed` | no | The worker could not supervise the source preparation. |
| `disk-quota` | no | The same quota refuses again. |
| `destroy-incomplete` | no | The command reached a verdict. |
| `partition-unverified` | no | The shards ran; their reports will not change. |
| `shard-failed` | no | The shard already had its own retry. |
| `admission-timeout` | no | A retry joins the same full queue. |
| `engine-error` | no | An unrecognized failure is not retried. |
| `worker-lost` | no | The run may still be executing. |

`oom`, `timed_out`, `cancelled` and `command_failed` are verdicts and are never
retried. A run that failed and then passed on the same input, argv, environment
and cwd is recorded as a flaky pair, per shard for fan-outs. Nothing re-runs;
the hint and `pandora stats` report it.

### Placement override refusals

Every refusal is exit 64, before the validator, the freeze and the queue.
Nothing runs.

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

## Operating limits

Measured on one worker (4 vCPU EPYC-Milan, 15.6 GiB, Incus 6.0.5, an 18 GiB
loop-file pool) and one M1 Pro Mac with 16 GiB shared with many agents. Local
numbers were taken under that load; they are not a quiet-machine baseline.

| Workload | Remote | Local | Source |
|---|---|---|---|
| `pnpm check`, cold turbo cache | 80 s | 317 s | [check profile](notes/v0.2-check-profile-2026-09-22.md) |
| `pnpm check`, warm | 20 s | 57 s (21-22 s on a Mac at load ~6) | same |
| `pnpm check`, one edit in `packages/domain` | 85 s | 251 s | same |
| `pnpm check` ×2 concurrent, warm | 29.5 s | 49 s | same |
| `pnpm test:native-unit` | 54-56 s | 109-194 s | same |
| `pnpm journey S0-01` | 56-81 s | not measured | [slice](notes/v0.2-slice-2026-09-22.md), [pilot](notes/v0.2-pilot-live-2026-09-23.md) |
| `pnpm test:surface desk`, 302 tests, 1 / 2 / 4 shards | 510 / 304 / 253 s | not measured | [sharding](notes/v0.2-sharding-2026-09-22.md) |

A warm remote `check` spends about 1.4 s before `accepted`, 0.35 s on clone and
start, 0.8 s on injection, 3.1 s on the synthetic Git repository, 11-12 s in
eichler's own uncached checks, and 1 s on destroy. Four shards on four vCPU is
where sharding stops paying.

Sizes that matter:

* A golden is 4-5 GiB. The pool is 18 GiB, so it holds about three goldens and
  the runs cloned from them. Admission refuses new runs below `disk_floor_gib`
  (4 GiB).
* A cold `pnpm check` with typecheck at `--concurrency=$PANDORA_CPUS` peaks at
  6.5 GiB and needs `size = "large"`. Warm runs peak near 2 GiB. The example
  configuration still says `medium`; eichler's own `pandora.toml` says `large`.
* Journeys peak at 3-4.5 GiB; surface shards at about 2.5 GiB.
* The source cache grows with every fresh worktree, because `--link-dest`
  deduplicates only on equal mtimes. It grew from 1.2 to 2.3 GiB in one
  afternoon. `gc` has no source-cache rule yet.
* The turbo cache is bounded at 4 GiB. Eichler's `check` uses about 1 MiB of it.

Known caveats:

* The canary's surface check runs the surface job's `validate` (or a one-shard
  `plan`), not a browser suite. A real Playwright run does not fit its budget.
* The surface job declares both apps' output paths, so a one-app run reports
  the other app's paths as missing.
* `PANDORA_CPUS` is fixed when a command starts. A run admitted alone keeps its
  larger hint when others join.
* A remote run's verdict is not checked against the worktree afterward. Only
  write-back re-freezes. Do not edit a worktree while a remote validation runs.
* One remote run per worktree is not enforced. Only the local lane holds a
  worktree lock.
* Neither golden is pinned. `pandora worker pins` resolves the inputs; the live
  goldens have not been rebuilt with them.
* The `--update` proposal for `S0-01` rewrote all 295 lines of its fixture
  although the plain run passed. Review `git diff` before you commit a worker
  update.
* Not yet run against the live worker: `pandora resolve` on a real conflict, the
  infrastructure retry on a real failure, a remote cancel with a non-default
  signal, a tier-1 sharded job, a real `--device` pool, `gc` with per-family
  ranking and `--protect`, and the canary derived from `[worker.canary]`.

## Layout

| Path | What it is |
|---|---|
| `bin/pandora`, `bin/pnpm` | The two POSIX launchers. `pnpm` is the shim; its non-enrolled path forks nothing. |
| `pandora/cli.py`, `errors.py`, `exits.py` | The one `pandora` command, the typed exceptions and the exit table. |
| `pandora/client/` | Runs on the Mac: the daemon, the shim client, enrollment, the local lane, fallback, placement, write-back settlement, health, stats, hints, `doctor`. |
| `pandora/config/` | Runs on the Mac: the `pandora.toml` loader and the argv classifier. |
| `pandora/snapshot/` | Runs on the Mac: the manifest freeze and the transfer into the worker's source cache. |
| `pandora/engine/` | Runs on the worker: the ledger, admission, scheduler, per-run supervisor, fan-out, retry, write-back proposal and turbo cache server. |
| `pandora/executor/` | Runs on the worker: the Incus driver and its memory watchdog. |
| `pandora/worker/` | Both halves: `provision`, `versions`, `remote` and `cli` run on the Mac; `service`, `canary`, `gc`, `goldens`, `facts` and `pins` run on the worker. |
| `pandora/tests/` | `python3 -m unittest discover -s pandora`. |
| `scripts/versions.toml` | The worker's package pins. |
| `docs/` | Final text for a repository's agent instructions, and the worker rebuild procedure. |
| `notes/` | Dated measurement and decision logs. The `v0.2-*` notes are the evidence for this README. |
| `experiments/` | Retained prototypes and the v0.1 runtime. Nothing in v0.2 imports from them. |

The client ships the worker half as a content-addressed engine bundle over SSH
on first use. A new checkout reaches the worker's runner on the next run. The
turbo cache server keeps the bundle it started with until its unit restarts;
`provision` restarts it when the unit file changes.

## History

v0.1 and v0.1.1 used a per-session launcher,
`experiments/routing/launch.py`, which gave each agent session a private PATH.
v0.2 replaces it with the machine-wide shim, the daemon, per-repository
enrollment and `pandora.toml`. v0.2 ships only a `pnpm` shim. The v0.1.1
Docker profile is not carried over.

* [v0.1 contract](notes/v0.1-contract.md) and its twelve-agent baseline,
  [issue #27](https://github.com/gbasin/pandora/issues/27).
* [v0.1.1 contract](notes/v0.1.1-contract.md) and its
  [validation evidence](notes/v0.1.1-validation-2026-09-21.md),
  [issue #64](https://github.com/gbasin/pandora/issues/64).
* [DESIGN.md](DESIGN.md) is the superseded design rationale for the original
  SSH pilot.
