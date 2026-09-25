# Operations

How to install, upgrade, run and remove Pandora on a Mac, what enrollment
writes, and the limits measured so far. The [README](../README.md) says what
Pandora does. [worker.md](worker.md) covers the Linux worker.

## Contents

* [Install on a Mac](#install-on-a-mac)
  * [Prerequisites](#prerequisites)
  * [1. Clone the checkout and install a version](#1-clone-the-checkout-and-install-a-version)
  * [2. Put the launchers first on PATH](#2-put-the-launchers-first-on-path)
  * [3. Write the client configuration](#3-write-the-client-configuration)
  * [4. Start the daemon](#4-start-the-daemon)
  * [5. Enroll each repository](#5-enroll-each-repository)
  * [6. Prove the install](#6-prove-the-install)
* [Upgrade](#upgrade)
  * [Migration from a checkout install](#migration-from-a-checkout-install)
* [The daemon](#the-daemon)
  * [Restart and drain](#restart-and-drain)
  * [When the daemon is installed but does not answer](#when-the-daemon-is-installed-but-does-not-answer)
  * [Removal and manual start](#removal-and-manual-start)
* [Enrollment and claim caches](#enrollment-and-claim-caches)
  * [Claim caches](#claim-caches)
  * [Migration from the old marker](#migration-from-the-old-marker)
  * [Unenrollment](#unenrollment)
  * [Old spellings](#old-spellings)
* [Operating limits](#operating-limits)
* [Layout](#layout)

## Install on a Mac

The install is machine-wide and changes no tracked file in the target
repository. Enrollment writes small files inside its Git directory. The install
has six parts: an installed version of Pandora, the two launchers on PATH, one
configuration file, the daemon, one enrollment per repository, done once, and
`pandora doctor` to prove the result.

### Prerequisites

* macOS. The daemon has run only on macOS. Its Linux peer-credential branch is
  untested.
* Python 3.11 or later as `python3` on PATH. Pandora imports `tomllib`.
  `/usr/bin/python3` on macOS is 3.9 and cannot run it. To use another
  interpreter, set `PANDORA_PYTHON` to its path.
* Git, rsync and OpenSSH.
* The real `pnpm` on PATH. The shim finds it by walking PATH past itself.
* An SSH key that logs in to the worker without a prompt. Pandora uses your SSH
  configuration. The daemon keeps one control master per worker. `pandora
  worker` verbs share another, never close it, and leave it to expire after ten
  idle minutes, so no verb cuts off the daemon's or another verb's transfers.
* A provisioned worker. See [The worker](worker.md#the-worker).

### 1. Clone the checkout and install a version

```sh
git clone https://github.com/gbasin/pandora.git ~/Code/pandora
```

The clone is on `main`, the current line. Install its HEAD as the version
Pandora runs.

```sh
~/Code/pandora/bin/pandora upgrade
```

`upgrade` copies the committed tree into
`~/.local/share/pandora/versions/<commit>/` and points
`~/.local/share/pandora/current` at it. The launchers and the daemon run from
`current`, never from the checkout. Pulling,
editing or switching branches in the checkout changes nothing live until the
next `pandora upgrade`. See [Upgrade](#upgrade). If `XDG_DATA_HOME` is set,
the directory is `$XDG_DATA_HOME/pandora` instead.

### 2. Put the launchers first on PATH

Link both launchers into one directory, through `current`.

```sh
mkdir -p ~/.local/bin
ln -s ~/.local/share/pandora/current/bin/pandora ~/.local/bin/pandora
ln -s ~/.local/share/pandora/current/bin/pnpm ~/.local/bin/pnpm
```

If `XDG_DATA_HOME` is set, link through `$XDG_DATA_HOME/pandora/current`
instead.

Do not link into the checkout or into a `versions/` directory. A link into the
checkout runs whatever the checkout holds now. A link into a version directory
stops at that version. `pandora upgrade` re-points a link into the checkout it
upgrades or into a version directory, and `pandora doctor` warns about either.

Create the shim marker beside them.

```sh
touch ~/.local/bin/.pandora-shim
```

The marker names the PATH entry that holds a command shim. A repository's own
job runner can read it, so that a child process the runner spawns does not route
a second time. The depth guard, `PANDORA_ROUTE_DEPTH`, stops that re-entry
without it. `pandora doctor` warns when the marker is missing beside the shim,
or when it sits in another PATH directory.

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
name = "eichler"                   # usually [repo] name in pandora.toml, or enroll --name
root = "/Users/YOU/Code/eichler"   # the main checkout or any worktree of it
# config = "~/.config/pandora/repos/eichler.pandora.toml"
#   only for a repository that has no pandora.toml at its root yet
```

Unknown keys inside `[worker]`, `[client]`, `[local]`, `[local.pause]`,
`[notify]` and `[[repos]]` are refused, with the allowed keys printed. An
unknown top-level table is ignored. The daemon reads this file on every
connection, so a new `[[repos]]` entry needs no restart. `[local]`,
`[local.pause]`, `health_interval_s` and `[notify]` take effect at the next
daemon start. You can
leave `[[repos]]` out: `pandora enroll` (step 5) appends the entry it needs.

The optional keys and their defaults:

| Table | Key | Default | Meaning |
|---|---|---|---|
| `[worker]` | `ssh_persist` | `10m` | SSH control-master lifetime. |
| | `health_interval_s` | `60` | How often the daemon polls the worker's health. |
| `[notify]` | `enabled` | `true` | macOS notification on a health transition (worker down or back, canary failed, disk floor, kernel drift). |
| `[client]` | `name` | `user@host` | Who this Mac is to a shared worker: your login name and the short host name. 1 to 64 letters, digits and `. _ @ + -`, starting with a letter or digit. See [Sharing a worker](worker.md#sharing-a-worker). |
| | `keep_runs_days` | `7` | The daemon removes a finished run's directory once all its dates are older than this, at start and every hour, and logs the setting at start. It never removes a live run, a conflicted write-back that waits for `pandora resolve`, or a run whose `meta.json` it cannot parse. A run directory with no `meta.json` goes once the directory is older than this. 0 keeps every run. `pandora stats` sees only what is kept. On the worker, `pandora worker retain` deletes attempt directories older than 24 h by default, and the ledger rows stay. |
| `[local]` | `budget_mib` | `0` | Local-lane memory budget. 0 means this Mac's RAM minus `reserve_mib`. |
| | `reserve_mib` | `4096` | Memory kept for agents, editors and the OS. |
| | `max_running` | `4` | Local-lane jobs at once. |
| | `one_active_per_worktree` | `true` | A second local job in one worktree exits 75. A `singleton` job does not count. |
| | `drift` | `warn` | `off`, `warn` or `fail` when the worktree changes during a local run. A job may override it. |
| | `queue_timeout_seconds` | `0` | 0 waits for the budget as long as it takes. A nonzero bound ends a waiting run with exit 75, and nothing runs. |
| `[local.pause]` | `enabled`, `sample_seconds`, `swap_growth_mib_per_minute`, `psi_full_avg10`, `free_percent`, `load_per_cpu`, `max_wait_seconds` | `true`, 3, 256, 20.0, 5.0, 8.0, 300 | The gate that stops new local jobs on a Mac under memory pressure. A job held past `max_wait_seconds` exits 70 and never runs. |

`pandora/client/settings.py` documents every key. It also accepts `[worker]
budget_mib`, `[client] max_wait_seconds`, `[client] fallback_slots` and
`[client] fallback_wait_seconds`, but none of them affects behavior.

### 4. Start the daemon

Install the launchd user agent. It runs the daemon from
`~/.local/share/pandora/current`, restarts it after a crash, starts it at login
after a reboot, and
logs to `<state>/logs/daemon.log`. Without an installed version, it runs the
daemon from the checkout the `pandora` you ran comes from.

```sh
pandora daemon --install
```

The command writes `~/Library/LaunchAgents/com.pandora.daemon.plist`, loads it,
and prints the launchd state line. When launchd already runs the daemon, the
command drains it first, the same way `pandora daemon --restart` does (see
[Restart and drain](#restart-and-drain). `--wait`, `--now` and `--idle-cancel`
work the same). It then boots
the old job out, waits until `launchctl print` says "Could not find service",
runs `launchctl enable` and bootstraps the new plist, and waits until launchd
lists the new job with a new pid. The wait for the old job and the bootstrap
retries share 300 s, with a progress line every 15 s. The new pid has 30 s
of its own. When a step fails, the command says what to run, and exits 1:
the `launchctl bootstrap` command once `launchctl print` says "Could not find
service", or `launchctl kickstart` and the log when the job is loaded but
its daemon does not start. Ctrl-C, SIGHUP or SIGTERM after the bootout still
bootstraps the new plist before the command exits.

Every line in `daemon.log` starts with a UTC time. The log records worker
health changes, each transfer's start, end or failure (run, worktree, input id, files, MiB, rsync exit, elapsed), each
refusal with its cause, and what a restart decided about each live run. A
failed transfer also leaves rsync's whole stderr in
`<state>/runs/<id>/transfer.stderr`. The plist pins `PANDORA_PYTHON` to the
interpreter that ran the install. If a hand-started daemon already holds the
lock, the install refuses. Stop that daemon first with `pandora daemon --stop`.

The plist sets `ProcessType` to `Interactive`. Every agent's command waits on
the daemon, so it must be scheduled on a loaded Mac. `Background`, the earlier
value, is Apple's class for batch work: low CPU, I/O and network priority, and
the first target of memory pressure. At load 90 it left the daemon unanswered
for 66 s. The daemon also runs its accept loop at user-interactive QoS. The
threads that serve each connection run at the default. There is no `Nice` key,
because a negative nice needs root. `pandora doctor` warns when the installed
plist still says `Background` or sets no `ProcessType`. Run `pandora daemon
--install` to rewrite it. It drains the daemon before it restarts it.

The daemon runs the version it started with. `pandora upgrade` restarts it into
a new version when no run would be lost. See [Upgrade](#upgrade). On an
older install that runs the checkout, restart it after you update the checkout.
`pandora doctor` then warns when a module the daemon loaded differs from the
same file in the checkout. A change to a module the daemon never loads, such
as the worker half, is not a reason to restart.

### 5. Enroll each repository

Enrolling is consent: it says that Pandora may route this repository on this
Mac. What is routed is each worktree's own `pandora.toml`, read again whenever
it changes. Write the client configuration
first, so the files enrollment writes point at the right socket. Then enroll
the repository from any of its worktrees. Do this once per repository.

```sh
pandora enroll ~/Code/eichler
```

If the repository has no `pandora.toml` at its root yet, name one.

```sh
pandora enroll ~/Code/eichler --config ~/.config/pandora/repos/eichler.pandora.toml
```

`enroll` loads and validates the repository's configuration. Then it does
three things:

1. It appends a `[[repos]]` table to `~/.config/pandora/config.toml` if the
   repository has none. It only appends, and it leaves the file untouched if
   the result would not load. It leaves an existing entry as it is and prints the
   block it would have written, if that differs. It refuses (exit 1) when an
   entry with the same name belongs to another repository. Use `--name`.
2. It writes `pandora-repo` into the Git common directory. This registration
   covers every worktree of the repository, including worktrees created later.
3. It writes the claim cache of the worktree you enrolled from, derived from
   the `[[repos]]` entry the daemon routes by, and asks the daemon to do the
   same.

It also removes the old marker, `<common>/pandora-enrolled`, if there is one.
[Claim caches](#claim-caches) explains how each worktree then routes by its own
`pandora.toml`. [Unenrollment](#unenrollment) stops routing a repository.

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
ok    pandora on PATH    ~/.local/bin/pandora imports ~/.local/share/pandora/versions/f91ef4e7a1c2 from any directory
ok    install            current is f91ef4e7a1c2, from ~/Code/pandora; `pandora` and the shim run through it
ok    daemon             pid 47841 on ~/.local/state/pandora/default/client.sock, worker ubuntu@WORKER_IP, runs current (f91ef4e7a1c2)
ok    worker             worker: reachable (disk 7.8 GiB free; polled 25s ago), from the daemon
ok    daemon supervision launchd runs pid 47841 as com.pandora.daemon, interpreter /opt/homebrew/bin/python3 (3.14.0); launchd starts it with /opt/homebrew/bin/python3; `pandora upgrade` after updating the checkout
ok    repository         enrolled as eichler, registration ~/Code/eichler/.git/pandora-repo
ok    claim cache        fresh: 20 claimed form(s), derived from ~/Code/eichler/pandora.toml; cache ~/Code/eichler/.git/pandora-claims
info  claim caches       57 worktree(s): 41 fresh, 2 stale, 14 without a cache; each refreshes on its next command
ok    daemon enrollment  [[repos]] eichler at ~/Code/eichler
ok    working directory  the worktree root, ~/Code/eichler
ok    variables          none of PANDORA_OFF, PANDORA_WHERE, PANDORA_SHARDS set
ok    shim markers       .pandora-shim beside the shim only

all checks passed
```

The first line is `ok` when the real pnpm is not a version manager's shim. A
`warn` there is acceptable. `claim caches` is information. On an install that
runs the checkout, the `install` line is `info` and says so. `restart drain`
shows only while `<state>/draining` exists: `info` during a restart, `warn` for
a marker older than 15 minutes. Every other line must be `ok`. `pandora doctor
--json` prints the same checks with their facts.

The repository rows:

| Row | `ok` | Otherwise |
|---|---|---|
| `repository` | `pandora-repo` is in the Git common directory. | `fail`: not enrolled. `warn`: only the old marker enrolls it, or a claim cache exists without a registration. Run `pandora enroll <root>` once. |
| `claim cache` | This worktree's cache is fresh. | `warn`: the cache is stale for this worktree, and the next command here refreshes it (the next claimed command, when only its digest shows it). Or there is no cache yet, and the next command writes it. `info`: no cache yet, and the old marker routes this worktree until then. Never `fail`. |
| `claim caches` | Always `info`: every worktree of the repository, counted as fresh, stale or without a cache. | |
| `client home` | Not shown. | `info`: the file the shim reads still has a `home` line. It is ignored. The row says what rewrites the file without it. |
| `client socket` | Not shown. | `warn`: the cache routes to another socket than the one the doctor checked. |
| `daemon enrollment` | The client configuration has a `[[repos]]` entry for the repository. | `fail`: it has none, so the daemon passes every command through. |

The version lines warn in these cases:

| Line | Warning | Do this |
|---|---|---|
| `install` | `pandora` on PATH or the shim runs a checkout or a version directory, not `current` | `pandora upgrade` re-points a link into the checkout it upgrades or into a version directory. Replace any other link with one through `current`. |
| `install` | `current` names a directory with no package (`fail`) | `pandora upgrade --from ~/Code/pandora` |
| `daemon` | `daemon runs <old>, current is <new>; restart it` | `pandora daemon --restart` under launchd, which drains the daemon first. Otherwise stop and start it. Or run `pandora upgrade`, which drains the daemon first. |
| `daemon` | `daemon runs <old>, current is <new>, and <checkout> is at <commit> since; run pandora upgrade` | `pandora upgrade` |
| `daemon` | `daemon runs the checkout <path>, current is <new>` | `pandora daemon --install`. It drains the daemon, then restarts it. |
| `daemon` | `daemon code differs from <version> on disk: something edited the version directory` | `pandora upgrade`. It builds the commit again under a new name. |

A checkout that has moved on since the last upgrade is not a warning. The
`install` line notes its commit.


## Upgrade

Pandora runs from a snapshot of the checkout, never from the checkout itself.
To move to new code, update the checkout, then install its HEAD.

```sh
git -C ~/Code/pandora pull
pandora upgrade
```

`pandora upgrade` does these steps:

1. It refuses if the checkout has uncommitted changes to tracked files.
   `--dirty` snapshots the working tree instead, as
   `<commit>-dirty-<digest>`. Untracked files are never copied.
2. It copies the committed tree at HEAD into `versions/<commit>/`, named by the
   first 12 hex digits of the commit. A version already built is reused while
   its files still match the digest written when it was built. An edited one
   is left alone and the commit is built again as `<commit>-<digest>`.
3. It imports the new version's client and daemon with the interpreter the
   plist pins and the one the launchers find. A version that cannot import is
   refused, and nothing changes.
4. It drains the daemon, as `pandora daemon --restart` does: new commands
   wait, and a queued local run is submitted again later. It waits until no
   local run is `running` and no remote run is `freezing`, `shipping` or
   `submitting`. It checks every second for up to `--wait`
   seconds (default 600) and prints the runs it waits for.
5. It points `current` at the new version with one rename. A reader sees the
   old version or the new one, never neither. The daemon is still draining, so
   no run can start before the restart. In the same step it re-points
   `pandora` and the shim on PATH through `current`, when they are symlinks
   into the checkout it upgrades or into a version directory, and the data
   directory is the default `~/.local/share/pandora`. With another data
   directory it prints the `ln -sf` to run. `--relink` moves the links anyway.
   It reports a copy or a missing launcher and leaves it. With `--version`,
   only links into a version directory are re-pointed.
6. It restarts the daemon with `launchctl kickstart -k`. It waits up to 20
   seconds for the new daemon to end the drain, then up to 10 seconds for it to
   answer from the new version.
7. It deletes old versions. It keeps the three most recently installed
   (`--keep N`, at least 2), `current`, the version before it, and the
   version the daemon runs.

`--now` skips the wait and restarts at once. A queued local run is submitted
again. A local run that is executing ends with exit 70. A remote run still `freezing` or `shipping` ends with exit 70. A remote run
`submitting` is looked up on the worker: followed if it started, closed if
not. Rerun what ended. Accepted remote runs continue on the worker.

`--no-restart` moves `current` without restarting the daemon. The daemon runs
its version until it restarts, and `pandora doctor` warns meanwhile.

It prints the new version with its tree digest, `current` before and after,
and the daemon's version before and after the restart. `--from <checkout>`
names the checkout. The default is the checkout the current version came
from.

| Exit | Meaning |
|---|---|
| 0 | The daemon runs the new version, or no daemon runs. |
| 75 | `current` did not move. The drain ended without a restart: a run still blocked it after `--wait`, and the daemon admits runs again (if the drain could not be ended, upgrade says so and the daemon ends it within 30 s). Or the daemon did not answer `ping` (for example, it is busy on a swapping Mac), or a daemon holds the lock but its socket is gone. The new version waits in `versions/`. Run `pandora upgrade` again later, or with `--now`. |
| 1 | Refused: uncommitted changes, not a Pandora checkout, or a version that cannot import. Nothing changed. Or `upgrade` cannot restart this daemon: it was started by hand, or its plist runs a checkout. Nothing changed unless `--no-restart`. Or the new daemon did not end the drain within 20 seconds, or did not answer from the new version within 10 seconds. The last lines say what to run. |

To go back, install a version that is still built:

```sh
ls ~/.local/share/pandora/versions
pandora upgrade --version <name>
```

`--version` takes the same wait, checks and restart as a new version. An
older commit that was pruned is built again with `pandora upgrade --from
<checkout at that commit>`.

The directories, under `$XDG_DATA_HOME/pandora`, else `~/.local/share/pandora`:

| Path | What it is |
|---|---|
| `versions/<commit>/` | One installed tree. Written once, then never changed. `.pandora-version` in it records the commit, the source checkout and the tree digest. |
| `current` | A relative symlink to one version. The plist and the links on PATH name paths through it, so neither goes stale when it moves. The registration and the claim caches name no client code at all. |

The launchers resolve `current` when they start, and start Python without the
working directory on its path. A daemon or a claimed command keeps importing
from the version it started with, even after `current` moves, and a command
typed inside a Pandora checkout still runs the installed version.

### Migration from a checkout install

An install from before `pandora upgrade` links the launchers into the checkout
and runs the daemon from it. It keeps working. To move it to `current`:

1. Run `~/Code/pandora/bin/pandora upgrade --no-restart`. It installs HEAD,
   points `current` at it, and re-points `~/.local/bin/pandora` and
   `~/.local/bin/pnpm`. Without `--no-restart` it refuses, because it cannot
   restart a daemon whose plist runs the checkout.
2. Run `pandora daemon --install`. It drains the daemon, then loads the new
   plist. The plist then runs `current`.
3. Run `pandora doctor`. The `install` and `daemon` lines must not warn. No
   file names a client home: the shim runs the client it is installed with.
   A `client home` `info` line means that an older registration or claim
   cache still has a `home` line, which nothing reads. `pandora enroll <repo>`
   rewrites the registration without the line. The next claimed command in a
   worktree rewrites that worktree's cache without it.


## The daemon

This section covers restarts, a daemon that does not answer, and removal.

### Restart and drain

A restart drains the daemon first, in three steps.

```sh
pandora daemon --restart
```

1. The daemon stops admitting runs. It writes `<state>/draining` with the
   time and the pid that asked. `pandora ps` shows `daemon: draining` on its
   first line.
2. The restart waits for the runs a restart would end: a local run that is
   executing, and a remote run still `freezing`, `shipping` or `submitting`.
   It prints the list each time the list changes.
   An accepted remote run does not block it.
3. When the list is empty, launchd restarts the daemon (`launchctl kickstart
   -k`). The new daemon takes over every row (local rows closed, remote rows
   followed or looked up), then removes the marker.

The wait is `--wait` seconds, 300 by default. When it runs out, the daemon
admits runs again, nothing is restarted, and the command exits 75 with the
runs that still block. Retry later, or add `--now`: with `--now` the restart
goes ahead when the wait runs out, and the table below applies to what is
still running. A kickstart that fails, Ctrl-C, SIGHUP or SIGTERM also ends
the drain. Under steady traffic, a restart costs at most the drain wait. A
daemon from before the drain is waited on through `pandora ps`, without
holding new commands. If a run starts as the restart is prepared, it waits
again.

A local run that does nothing does not hold the drain. The daemon samples
each local run's process tree every second. A run whose processes together
use less than 1 s of CPU, start or end no process, and write no output for
`--idle-cancel` seconds (600 by default, and 0 turns it off) is canceled. The
restart prints `canceling <id>: no CPU progress and no output for <time>`,
the run's log and its caller get the same reason, and the run ends with exit
130. The daemon checks the run again before it cancels it, so a run that
started to progress since the last poll keeps running. A run that `ps` cannot
measure is never idle. While the restart waits, each blocker line shows
`idle <time>` once a run has not progressed for a minute, and `pandora ps
--json` shows `cpu_seconds`, `last_active` and `idle_seconds` for each local
run that is executing. A run whose work is done by processes outside its
tree, for example in a Docker container, with no output, looks idle. Use
`--idle-cancel 0` when such a run must not be canceled. `pandora upgrade` and
`pandora daemon --install` apply the same rule and take the same option.

The drain is a lease. `--restart` renews it every second. A daemon that hears
nothing for 30 s ends the drain itself and admits runs again, so a restart
killed with SIGKILL, or by a tool timeout, holds commands for at most 30 s. If
the drain cannot be ended at the end of a failed wait, `--restart` says so and
the lease ends it.

`--restart` refuses before it drains when launchd does not run the daemon that
holds `daemon.lock`: a kickstart would not reach that daemon. When a daemon
holds the lock but its socket refuses connections, `--restart` treats it as a
daemon that does not answer and exits 75 without `--now`.

What a client sees during the drain and the restart:

* A new claimed command prints `pandora: daemon is restarting; waiting` once,
  before anything runs. It asks again every 2 seconds and is submitted as usual
  when the new daemon answers.
* A local run that was still queued is withdrawn and submitted again in the
  same way. It loses its place in the queue.
* A command the claim cache does not claim is not held: it runs at once.
* While no daemon listens, the client keeps asking only while the marker is
  younger than its wait. With no fresh marker, the rules in [When the daemon
  is installed but does not answer](#when-the-daemon-is-installed-but-does-not-answer)
  apply. A connection the stopping daemon closes before it answers is asked
  again, when the drain began before the command was sent.
* A client waits at most `PANDORA_DRAIN_WAIT` seconds, 660 by default: the
  longest restart wait (`pandora upgrade`, 600 s) and a minute for the restart.
  When the wait runs out, the command exits 75 and nothing ran: "still
  draining" when the daemon still answers `draining`, "did not come back" when
  no daemon listens. Retry. It never runs unmanaged during a drain.
* `pandora wait`, `ps`, `logs`, `result` and `cancel` work during the drain.

A marker older than 15 minutes is a restart that never finished. Clients
ignore it and `pandora doctor` warns about it. Any daemon that starts removes
it.

The new daemon may start before the old one has let go of `daemon.lock`. It
waits up to 120 s for the lock and logs `waiting for pid N to stop`.

A client older than the drain protocol reads the `draining` frame as a closed
connection and exits 70, "execution is uncertain". So on an older install that
runs the checkout, update the checkout first and restart the daemon second: the
shim runs the checkout's client code at once. `pandora upgrade` moves `current`
before the restart for the same reason.

What a restart does to each run:

| Run | After the restart |
|---|---|
| Remote, accepted | Continues on the worker. The next daemon follows it from its recorded log offset. `pandora wait <id>` re-attaches. |
| Remote, queued on the worker | Withdrawn when the drain starts. Its client submits it again at the back of the queue. A detached one keeps its place, and the next daemon follows it. |
| Remote, still freezing, shipping or submitting | The drain waits for it. With `--now` after the wait, or with a daemon killed some other way: still freezing or shipping, it ends `infra_failed`, exit 70, without asking the worker. Rerun it. Otherwise the next daemon asks the worker for it by request id. A run the worker started is followed, except a write-back run, which is stopped there and ends `infra_failed`. A run the worker never saw or refused ends `infra_failed`. Rerun it. A run the worker cannot account for, or a worker that cannot be asked, ends `infra_failed` with "check `pandora ps` before retrying". |
| Local, executing | The drain waits for it to finish. With `--now` after the wait: it ends `infra_failed`, exit 70, and its process tree is stopped, by the stopping daemon itself before it exits. The next daemon sweeps any row its predecessor did not get to. Rerun it. |
| Local, queued | Ends `withdrawn` when the drain starts. Its client submits it again to the next daemon. |
| A claimed command typed during the drain or the 1-2 s without a daemon | Waits for the new daemon, then runs as usual. A daemon stopped without a drain gets the 5 s wait, then exit 70 (see [When the daemon is installed but does not answer](#when-the-daemon-is-installed-but-does-not-answer)). |

A client attached to a run that ends this way exits 70. It does not wait.
`kill -USR1 <daemon pid>` writes every thread's stack to the daemon log, for a
run that looks stuck while the daemon is still driving it.

### When the daemon is installed but does not answer

A claimed command that finds no daemon on the socket, or a socket that
refuses it, takes one of three paths, in this order:

1. A fresh `<state>/draining` marker: a restart is in progress. The command
   waits for the new daemon, up to `PANDORA_DRAIN_WAIT`, then exits 75 with
   "did not come back" without running the command.
2. No fresh marker, and the client configuration, `~/.config/pandora/config.toml`,
   exists: the daemon was installed on this Mac. The command waits up to five
   seconds, which covers a restart without a drain. Then it exits 70 without running
   the command and prints ``pandora: hint: run `pandora doctor` ``.
3. No fresh marker and no client configuration: Pandora was never installed
   here. A claimed command in an enrolled clone runs here as if Pandora were
   not installed, with one notice and a row in `<state>/passthrough.jsonl`.

`PANDORA_OFF=1` bypasses all three: the command runs here with no Pandora.

### Removal and manual start

`pandora daemon --uninstall` unloads the agent and deletes the plist. It does
not drain: check `pandora ps` first. A local run that is executing ends with
exit 70. A remote run continues on the worker while no daemon runs, as after a
restart.

To run the daemon by hand instead, for example on a machine where launchd is not
wanted, start `pandora --config ~/.config/pandora/config.toml daemon` in the
foreground or under `nohup`. `pandora doctor` then warns that nothing restarts
it.


## Enrollment and claim caches

### Claim caches

Each worktree has its own claim cache, `pandora-claims`, in the worktree's own
Git directory (`git rev-parse --git-dir`): `<common>/worktrees/<name>/` for a
linked worktree, `<common>/` for the main one. The daemon writes it from that
worktree's `pandora.toml`, whenever it classifies a command from that worktree.
The shim reads it with shell builtins and forks nothing. A worktree on a branch
with a different `pandora.toml` routes by its own file.

The cache derives from `pandora.toml`, and the shim checks it against the
files it came from. You do not enroll again after a change to the file. The
shim compares dates: a cache older than
the worktree's `pandora.toml`, the `--config` file it came from, or the client
configuration is stale. On a stale or missing cache the shim starts Python
once. The daemon rewrites the cache and says whether the command is claimed.
The command then routes, or runs as if Pandora were not installed. Every
claimed command also reaches the daemon, which derives the cache again from the
file's content, so a file replaced by one with an older date is caught there.
A cache written within 2 s of an edit is dated 2 s early, so the next command
refreshes it once more. Whenever a rewrite changes the cache, even for an edit
to a comment, the caller sees one line:

```
pandora: claim cache refreshed from pandora.toml
```

The next command reads the new cache. Until a claimed command runs, `pandora
doctor` reports a cache whose file changed without a newer date: the cache
records the file's SHA-256 digest. Without a daemon, the client derives the
cache itself from the worktree's `pandora.toml` and writes it. With
`PANDORA_OFF=1` and no fresh cache, the passthrough logger decides the same way
and logs only a claimed command.

Two worktrees on branches with different `pandora.toml` files do not share a
cache. Each cache is in its own worktree's Git directory, so neither rewrites
the other's.

The shim runs the client from where it is itself installed: the file the `pnpm`
link on PATH resolves to. After `pandora upgrade` that link goes through
`~/.local/share/pandora/current`, so the shim follows every upgrade, and it
imports from the version directory `current` names when the command starts. No
file names a client home. An older cache or registration can still have a
`home` line. Routing never uses it. A claimed command prints one notice when
the line names another package, and `pandora doctor` shows a `client home`
`info` row. The daemon's next rewrite drops the line from a cache. `pandora
enroll <root>` drops it from the registration. A shim link that still points
into a checkout runs that checkout's code. `pandora doctor` warns about it.
`pandora upgrade` re-points it when it points into the checkout being upgraded,
and prints the `ln -sf` otherwise.

### Migration from the old marker

To move from the old marker, bring the daemon up to the new version first.
Run `pandora upgrade`. On an install that still runs the checkout, follow
[Migration from a checkout install](#migration-from-a-checkout-install)
instead. Then run `pandora enroll <root>` once per repository. The daemon comes
first because a daemon from before claim caches answers the shim's question
with "unknown op". Every command in a worktree without a cache then pays a
Python start and two daemon round trips. `enroll` asks the daemon
and warns when it does not know the question. Until you enroll again the shim
reads the old marker in each worktree that has no cache, and the marker can be
stale. The daemon writes a worktree's cache on its first claimed command. From
then on that worktree routes by its own `pandora.toml`. The old marker is read
for one more release.

### Unenrollment

To stop routing a repository, unenroll it.

```sh
pandora unenroll ~/Code/eichler
```

`unenroll` removes the registration, the old marker and every worktree's claim
cache it finds under the Git common directory. It leaves the `[[repos]]` entry
in the client configuration and says so. Remove that entry too: `pandora run`
still routes while it is there.

### Old spellings

`pandora enrol` and `pandora unenrol`, the old British spellings, are hidden
aliases for one release. Each prints a one-line deprecation notice on stderr and then runs
`enroll` or `unenroll`. Change scripts to the new spelling.


## Operating limits

Measured on one worker (4 vCPU EPYC-Milan, 15.6 GiB, Incus 6.0.5, an 18 GiB
loop-file pool) and one M1 Pro Mac with 16 GiB shared with many agents. Local
numbers were taken under that load. They are not a quiet-machine baseline.

| Workload | Remote | Local | Source |
|---|---|---|---|
| `pnpm check`, cold turbo cache | 80 s | 317 s | [check profile](../notes/v0.2-check-profile-2026-09-22.md) |
| `pnpm check`, warm | 20 s | 57 s (21-22 s on a Mac at load ~6) | same |
| `pnpm check`, one edit in `packages/domain` | 85 s | 251 s | same |
| `pnpm check` ×2 concurrent, warm | 29.5 s | 49 s | same |
| `pnpm test:native-unit` | 54-56 s | 109-194 s | same |
| `pnpm journey S0-01` | 56-81 s | not measured | [slice](../notes/v0.2-slice-2026-09-22.md), [pilot](../notes/v0.2-pilot-live-2026-09-23.md) |
| `pnpm test:surface desk`, 302 tests, 1 / 2 / 4 shards | 510 / 304 / 253 s | not measured | [sharding](../notes/v0.2-sharding-2026-09-22.md) |

A warm remote `check` spends about 1.4 s before `accepted`, 0.35 s on clone and
start, 0.8 s on injection, 3.1 s on the synthetic Git repository, 11-12 s in
eichler's own uncached checks, and 1 s on destroy. On four vCPU, one, two
and four shards were measured, and four was the best of those.

Sizes that matter:

* A golden is 4-5 GiB. The pool is 18 GiB, so it holds about three goldens and
  the runs cloned from them. Admission refuses new single runs below
  `disk_floor_gib` (4 GiB). A sharded run's shards are not checked against it.
* A cold `pnpm check` with typecheck at `--concurrency=$PANDORA_CPUS` peaks at
  6.5 GiB and needs `size = "large"`. Warm runs peak near 2 GiB. The example
  configuration still says `medium`. Eichler's own `pandora.toml` says `large`.
* Journeys peak at 3-4.5 GiB. Surface shards peak at about 2.5 GiB.
* The source cache grows with every fresh worktree, because `--link-dest`
  deduplicates only on equal mtimes. Before it was collected, it grew from 1.2
  to 2.3 GiB in one afternoon. The worker now deletes a snapshot that no live
  run needs, that is not the `latest` link-dest base, and that is older than
  an hour. It keeps the newest 4 such snapshots per repository. It runs this
  collection at most every 5 minutes on the health poll, and `pandora worker
  retain` runs it too. `pandora worker gc` does not.
* The turbo cache is bounded at 4 GiB. Eichler's `check` uses under 1 MiB of it (0.6 MiB measured).

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
* Neither golden is pinned. `pandora worker pins` resolves the inputs. The live
  goldens have not been rebuilt with them. `pandora.toml` has no pins key yet,
  so a routed run always builds the unpinned golden.
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
| `bin/pandora`, `bin/pnpm` | The two POSIX launchers. `pnpm` is the shim. Its non-enrolled path, and a fresh cache's unclaimed light commands, fork nothing. |
| `pandora/cli.py`, `errors.py`, `exits.py` | The one `pandora` command, the typed exceptions and the exit table. |
| `pandora/client/` | Runs on the Mac: the daemon, the shim client, enrollment, the local lane, fallback, placement, write-back settlement, health, stats, hints, `doctor`, `upgrade` (`install.py`). |
| `pandora/config/` | Runs on the Mac: the `pandora.toml` loader and the argv classifier. |
| `pandora/snapshot/` | Runs on the Mac: the manifest freeze and the transfer into the worker's source cache. |
| `pandora/engine/` | Runs on the worker: the ledger, admission, scheduler, the queue waiter, per-run supervisor, fan-out, retry, write-back proposal and turbo cache server. |
| `pandora/executor/` | Runs on the worker: the Incus driver and its memory watchdog. |
| `pandora/worker/` | Both halves: `provision`, `versions`, `remote`, `enrolled` and `cli` run on the Mac. `service`, `canary`, `gc`, `goldens`, `facts`, `pins` and `provision.sh` run on the worker. |
| `pandora/tests/` | `python3 -m unittest discover -s pandora`. |
| `scripts/versions.toml` | The worker's package pins. |
| `docs/` | The reference documents the README links, the paragraphs a repository may copy into its agent instructions, and the worker rebuild procedure. |
| `notes/` | Dated measurement and decision logs. The `v0.2-*` notes are the evidence for the README and the documents in `docs/`. |
| `experiments/` | Retained prototypes and the v0.1 runtime. Nothing in v0.2 imports from them. |

The client ships the worker half as a content-addressed bundle of worker code over
SSH on first use, from the version the daemon or the `pandora worker` verb runs. A
new version reaches the worker's runner on the first run after the daemon
restarts into it. The turbo cache server keeps the bundle it started with until
its unit restarts. `provision` restarts it when the unit file changes.
