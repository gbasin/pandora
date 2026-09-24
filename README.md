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
* Each worktree routes by its own `pandora.toml`. An edit takes effect on the
  next command in that worktree, at the cost of one Python start. Enrolling a
  repository is consent, given once; never again after a change.

v0.2 is proved against one repository (eichler), one worker (4 vCPU, 15.6 GiB,
x86_64 Ubuntu 26.04) and one Mac. Read [Operating limits](#operating-limits)
before you rely on it.

## Install on a Mac

The install is machine-wide and changes nothing in the target repository. It
has six parts: an installed version of Pandora, the two launchers on PATH, one
configuration file, the daemon, one enrollment per repository, done once, and
`pandora doctor` to prove the result.

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
  worker` verbs share another, never close it, and leave it to expire after ten
  idle minutes, so no verb cuts off the daemon's or another verb's transfers.
* A provisioned worker. See [The worker](#the-worker).

### 1. Clone the checkout and install a version

```sh
git clone https://github.com/gbasin/pandora.git ~/Code/pandora
```

Check out the v0.2 line. Until it merges to `main`, that is `v0.2/assembly`.

```sh
git -C ~/Code/pandora checkout v0.2/assembly
```

Install the checkout's HEAD as the version Pandora runs.

```sh
~/Code/pandora/bin/pandora upgrade
```

`upgrade` copies the committed tree into
`~/.local/share/pandora/versions/<commit>/` and points
`~/.local/share/pandora/current` at it. The launchers, the daemon and the
claim caches all run from `current`, never from the checkout. Pulling,
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

Do not link into the checkout or into a `versions/` directory. A link into the
checkout runs whatever the checkout holds now. A link into a version directory
stops at that version. `pandora upgrade` re-points a link into the checkout it
upgrades or into a version directory, and `pandora doctor` warns about either.

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
file on every connection, so a new `[[repos]]` entry needs no restart. You can
leave `[[repos]]` out: `pandora enroll` (step 5) appends the entry it needs.

The optional keys and their defaults:

| Table | Key | Default | Meaning |
|---|---|---|---|
| `[worker]` | `ssh_persist` | `10m` | SSH control-master lifetime. |
| | `health_interval_s` | `60` | How often the daemon polls the worker's health. |
| `[notify]` | `enabled` | `true` | macOS notification on a health transition (worker down or back, canary failed, disk floor, kernel drift). |
| `[client]` | `name` | `user@host` | Who this Mac is to a shared worker: your login name and the short host name. 1 to 64 letters, digits and `. _ @ + -`. See [Sharing a worker](#sharing-a-worker). |
| | `fallback_slots` | `2` | Local runs allowed at once when the daemon itself is gone. |
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

Install the launchd user agent. It runs the daemon from
`~/.local/share/pandora/current`, restarts it after a crash or a reboot, and
logs to `<state>/logs/daemon.log`. Without an installed version, it runs the
daemon from the checkout the `pandora` you ran comes from.

```sh
pandora daemon --install
```

The command writes `~/Library/LaunchAgents/com.pandora.daemon.plist`, loads it,
and prints the launchd state line. Every line in `daemon.log` starts with a UTC
time. The log records worker health changes, each transfer's start, end or
failure (run, worktree, input id, files, MiB, rsync exit, elapsed), each
refusal with its cause, and what a restart decided about each live run. A
failed transfer also leaves rsync's whole stderr in
`<state>/runs/<id>/transfer.stderr`. The plist pins `PANDORA_PYTHON` to the
interpreter that ran the install. If a hand-started daemon already holds the
lock, the install refuses; stop that daemon first with `pandora daemon --stop`.

The plist sets `ProcessType` to `Interactive`. Every agent's command waits on
the daemon, so it must be scheduled on a loaded Mac. `Background`, the earlier
value, is Apple's class for batch work: low CPU, I/O and network priority, and
the first target of memory pressure. At load 90 it left the daemon unanswered
for 66 s. The daemon also runs its accept loop at user-interactive QoS; the
threads that serve each connection run at the default. There is no `Nice` key,
because a negative nice needs root. `pandora doctor` warns when the installed
plist still says `Background` or sets no `ProcessType`. Run `pandora daemon
--install` to rewrite it. That restarts the daemon without a drain, so check
`pandora ps` first.

The daemon runs the version it started with. `pandora upgrade` restarts it into
a new version when no run would be lost; see [Upgrade](#upgrade). On an
install that runs the checkout, restart it after you update the checkout.
`pandora doctor` then warns when a module the daemon loaded differs from the
same file in the checkout. A change to a module the daemon never loads, such
as the worker half, is not a reason to restart.

You do not need a quiet moment. A restart drains the daemon first.

```sh
pandora daemon --restart
```

1. The daemon stops admitting runs. It writes `<state>/draining` with the
   time and the pid that asked. `pandora ps` shows `daemon: draining` on its
   first line.
2. The restart waits for the runs a restart would end: a local run that is
   executing, and a remote run not yet accepted (`queued`, `freezing`,
   `shipping`, `submitting`). It prints the list each time the list changes.
   An accepted remote run does not block it.
3. When the list is empty, launchd restarts the daemon (`launchctl kickstart
   -k`). The new daemon settles every row, then removes the marker.

The wait is `--wait` seconds, 300 by default. When it runs out, the daemon
admits runs again, nothing is restarted, and the command exits 75 with the
runs that still block. Retry later, or add `--now`: with `--now` the restart
goes ahead when the wait runs out, and the table below applies to what is
still running. A kickstart that fails, Ctrl-C, SIGHUP or SIGTERM also ends
the drain. Under steady traffic, a restart costs at most the drain wait. A
daemon from before the drain is waited on through `pandora ps`, without
holding new commands; if a run starts as the restart is prepared, it waits
again.

The drain is a lease. `--restart` renews it every second. A daemon that hears
nothing for 30 s ends the drain itself and admits runs again, so a restart
killed with SIGKILL, or by a tool timeout, holds commands for at most 30 s. If
the drain cannot be ended at the end of a failed wait, `--restart` says so and
the lease ends it.

`--restart` refuses before it drains when launchd does not run the daemon that
holds `daemon.lock`: a kickstart would not reach that daemon. A socket that
refuses connections while a daemon holds the lock is a daemon that does not
answer, not a missing one: `--restart` exits 75 without `--now`.

What a client sees during the drain and the restart:

* A new claimed command prints `pandora: daemon is restarting; waiting` once.
  Nothing has run. It asks again every 2 seconds and is submitted as usual
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

The client code that answers `draining` is new in this release. A client from
before it reads the frame as a closed connection and exits 70, "execution is
uncertain". So on an install that runs the checkout, update the checkout first
and restart the daemon second: the shim runs the checkout's client code at
once. `pandora upgrade` moves `current` before the restart for the same reason.

What a restart does to each run:

| Run | After the restart |
|---|---|
| Remote, accepted | Continues on the worker. The next daemon follows it from its recorded log offset. `pandora wait <id>` re-attaches. |
| Remote, not yet accepted | The drain waits for it. With `--now` after the wait, or with a daemon killed some other way: still freezing or shipping, it ends `infra_failed`, exit 70, without asking the worker. Rerun it. Otherwise the next daemon asks the worker for it by request id. A run the worker started is followed, except a write-back run, which is stopped there and ends `infra_failed`. A run the worker never saw or refused ends `infra_failed`; rerun it. A run the worker cannot account for, or a worker that cannot be asked, ends `infra_failed` with "check `pandora ps` before retrying". |
| Local, executing | The drain waits for it to finish. With `--now` after the wait: it ends `infra_failed`, exit 70, and its process tree is stopped, by the stopping daemon itself before it exits. The next daemon sweeps any row its predecessor did not get to. Rerun it. |
| Local, queued | Ends `withdrawn` when the drain starts. Its client submits it again to the next daemon. |
| A claimed command typed during the drain or the 1-2 s without a daemon | Waits for the new daemon, then runs as usual. A daemon stopped without a drain gets the 5 s wait, then exit 70 (see [When the daemon is installed but does not answer](#when-the-daemon-is-installed-but-does-not-answer)). |

A client attached to a run that ends this way exits 70. It does not wait.
`kill -USR1 <daemon pid>` writes every thread's stack to the daemon log, for a
run that looks stuck while the daemon is still driving it.

`pandora daemon --uninstall` unloads the agent and deletes the plist. A remote
run continues on the worker while no daemon runs, as after a restart.

To run the daemon by hand instead, for example on a machine where launchd is not
wanted, start `pandora --config ~/.config/pandora/config.toml daemon` in the
foreground or under `nohup`. `pandora doctor` then warns that nothing restarts
it.

### 5. Enroll each repository

Enrolling is consent: it says that Pandora may route this repository on this
Mac. It is not configuration. What is routed is each worktree's own
`pandora.toml`, read again whenever it changes. Write the client configuration
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
   repository has none. It only appends, and it puts the file back if the
   result does not load. It leaves an existing entry as it is and prints the
   block it would have written, if that differs. It refuses (exit 1) when an
   entry with the same name belongs to another repository; use `--name`.
2. It writes `pandora-repo` into the Git common directory. This registration
   covers every worktree of the repository, including worktrees created later.
3. It writes the claim cache of the worktree you enrolled from, derived from
   the `[[repos]]` entry the daemon routes by, and asks the daemon to do the
   same.

It also removes the old marker, `<common>/pandora-enrolled`, if there is one.

Each worktree has its own claim cache, `pandora-claims`, in the worktree's own
Git directory (`git rev-parse --git-dir`): `<common>/worktrees/<name>/` for a
linked worktree, `<common>/` for the main one. The daemon writes it from that
worktree's `pandora.toml`, whenever it classifies a command from that worktree.
The shim reads it with shell builtins and forks nothing. A worktree on a branch
with a different `pandora.toml` routes by its own file.

The cache is a cache of `pandora.toml` and checks itself. You do not enroll
again after a change to the file. The shim compares dates: a cache older than
the worktree's `pandora.toml`, the `--config` file it came from, or the client
configuration is stale. On a stale or missing cache the shim starts Python
once. The daemon rewrites the cache and says whether the command is claimed.
The command then routes, or runs as if Pandora were not installed. Every
claimed command also reaches the daemon, which derives the cache again from the
file's content, so a file replaced by one with an older date is caught there.
Whenever a rewrite changes the claims, the caller sees one line:

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
file names a client home. A cache or registration written before this change
can still have a `home` line. The line is read and ignored. A claimed command
says so when the line names another package. The daemon's next rewrite drops it
from a cache; `pandora enroll <root>` drops it from the registration. A shim
link that still points into a checkout runs that checkout's code; `pandora
doctor` warns about it, and `pandora upgrade` prints the `ln -sf` that fixes it.

To move from the old marker, update the checkout, restart the daemon (`pandora daemon
--restart`), then run `pandora enroll <root>`
once per repository. Restart first: a daemon from before claim caches answers
the shim's question with "unknown op", so every command in a worktree without a
cache pays a Python start and two daemon round trips. `enroll` asks the daemon
and warns when it does not know the question. Until you enroll again the shim
reads the old marker in each worktree that has no cache, and the marker can be
stale. The daemon writes a worktree's cache on its first claimed command; from
then on that worktree routes by its own `pandora.toml`. The old marker is read
for one more release.

### When the daemon is installed but does not answer

A claimed command that finds no daemon on the socket, or a socket that
refuses it, takes one of three paths, in this order:

1. A fresh `<state>/draining` marker: a restart is in progress. The command
   waits for the new daemon, up to `PANDORA_DRAIN_WAIT`, then exits 75 with
   "did not come back". Nothing runs.
2. No fresh marker, and the client configuration, `~/.config/pandora/config.toml`,
   exists: the daemon was installed on this Mac. The command waits up to five
   seconds, which covers a restart without a drain. Then it exits 70 and prints
   ``pandora: hint: run `pandora doctor` ``. Nothing runs.
3. No fresh marker and no client configuration: Pandora was never installed
   here. A claimed command in an enrolled clone runs here as if Pandora were
   not installed, with one notice and a row in `<state>/passthrough.jsonl`.

`PANDORA_OFF=1` bypasses all three: the command runs here with no Pandora.

To stop routing a repository, unenroll it.

```sh
pandora unenroll ~/Code/eichler
```

`unenroll` removes the registration, the old marker and every worktree's claim
cache it finds under the Git common directory. It leaves the `[[repos]]` entry
in the client configuration and says so. Remove that entry too: `pandora run`
still routes while it is there.

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
| `repository` | `pandora-repo` is in the Git common directory. | `fail`: not enrolled. `warn`: only the old marker enrolls it; run `pandora enroll <root>` once. |
| `claim cache` | This worktree's cache is fresh. | `warn`: the cache is stale for this worktree, and the next command here refreshes it (the next claimed command, when only its digest shows it); or there is no cache yet, and the next command writes it. `info`: no cache yet, and the old marker routes this worktree until then. Never `fail`. |
| `claim caches` | Always `info`: every worktree of the repository, counted as fresh, stale or without a cache. | |
| `client home` | Not shown. | `info`: the file the shim reads still has a `home` line. It is ignored; the row says what rewrites the file without it. |
| `client socket` | Not shown. | `warn`: the cache routes to another socket than the one the doctor checked. |
| `daemon enrollment` | The client configuration has a `[[repos]]` entry for the repository. | `fail`: it has none, so the daemon passes every command through. |

The version lines warn in these cases:

| Line | Warning | Do this |
|---|---|---|
| `install` | `pandora` on PATH or the shim runs a checkout or a version directory, not `current` | `pandora upgrade` re-points a link into the checkout it upgrades or into a version directory. Replace any other link with one through `current`. |
| `install` | `current` names a directory with no package (`fail`) | `pandora upgrade --from ~/Code/pandora` |
| `daemon` | `daemon runs <old>, current is <new>; restart it` | `pandora daemon --restart` under launchd, which drains the daemon first; else stop and start it. Or run `pandora upgrade`, which drains the daemon first. |
| `daemon` | `daemon runs <old>, current is <new>, and <checkout> is at <commit> since; run pandora upgrade` | `pandora upgrade` |
| `daemon` | `daemon runs the checkout <path>, current is <new>` | `pandora daemon --install`. It restarts the daemon; check `pandora ps` first. |
| `daemon` | `daemon code differs from <version> on disk: something edited the version directory` | `pandora upgrade`. It builds the commit again under a new name. |
| `client home` | the registration or the claim cache pins the client to a path other than `current` | Registration: `pandora enroll <repo>`. Cache: once the daemon runs `current`, delete the cache; the next command writes it again. |

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
   local run is `running` and no remote run is `queued`, `freezing`,
   `shipping` or `submitting`. It checks every second for up to `--wait`
   seconds (default 600) and prints the runs it waits for.
5. It points `current` at the new version with one rename. A reader sees the
   old version or the new one, never neither. The daemon is still draining, so
   no run can start before the restart.
6. It restarts the daemon with `launchctl kickstart -k`. It waits up to 20
   seconds for the new daemon to end the drain, then up to 10 seconds for it to
   answer from the new version.
7. It re-points `pandora` and the shim on PATH through `current`, when they
   are symlinks into the checkout it upgrades or into a version directory,
   and the data directory is the default `~/.local/share/pandora`. With
   another data directory it prints the `ln -sf` to run; `--relink` moves the
   links anyway. It reports a copy or a missing launcher and leaves it.
8. It deletes old versions. It keeps the three most recently installed
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
names the checkout; the default is the checkout the current version came
from.

| Exit | Meaning |
|---|---|
| 0 | The daemon runs the new version, or no daemon runs. |
| 75 | `current` did not move. The drain ended without a restart: a run still blocked it after `--wait`, and the daemon admits runs again (if the drain could not be ended, upgrade says so and the daemon ends it within 30 s). Or the daemon did not answer `ping` (for example, it is busy on a swapping Mac), or a daemon holds the lock but its socket is gone. The new version waits in `versions/`. Run `pandora upgrade` again later, or with `--now`. |
| 1 | Refused: uncommitted changes, not a Pandora checkout, or a version that cannot import; nothing changed. Or `upgrade` cannot restart this daemon: it was started by hand, or its plist runs a checkout; nothing changed unless `--no-restart`. Or the new daemon did not end the drain within 20 seconds, or did not answer from the new version within 10 seconds. The last lines say what to run. |

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
| `current` | A relative symlink to one version. The plist, the launchers on PATH, the registration and the claim caches name paths through it, so none of them goes stale. |

The launchers resolve `current` when they start, and start Python without the
working directory on its path. A daemon or a claimed command keeps importing
from the version it started with, even after `current` moves, and a command
typed inside a Pandora checkout still runs the installed version.

### Move an install that runs the checkout

An install from before `pandora upgrade` links the launchers into the checkout
and runs the daemon from it. It keeps working. To move it to `current`:

1. Run `~/Code/pandora/bin/pandora upgrade --no-restart`. It installs HEAD,
   points `current` at it, and re-points `~/.local/bin/pandora` and
   `~/.local/bin/pnpm`. Without `--no-restart` it refuses, because it cannot
   restart a daemon whose plist runs the checkout.
2. Run `pandora ps`. Wait until no local run is `running` or `queued` and no
   remote run is `queued`, `freezing`, `shipping` or `submitting`.
3. Run `pandora daemon --install`. The plist then runs `current`, and the
   daemon restarts.
4. Run `pandora enroll <repo>` for each enrolled repository. The
   registration's `home` becomes `current`, and the daemon writes `current`
   into each claim cache it refreshes.
5. Run `pandora doctor`. The `install`, `daemon` and `client home` lines must
   not warn.

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

### Sharing a worker

Several Macs, each with its own client daemon and its own user, can share one
worker and one `engine_root`. Each daemon names itself to the engine with every
submission: `[client] name`, or `user@host` by default (`gary@studio`). The
engine records the name on the run's ledger row and in its result, and a
shard's row carries its parent's name.

What holds between clients:

* Runs never collide. Every attempt has its own row, directory, log, result and
  instance, even when two Macs submit the same tree at the same moment. The
  source cache and the turbo cache are content-addressed and written by
  temporary file and rename, so two writers of one entry leave one whole entry.
* A request id held by one client is never attached to by another. The engine
  refuses the second submission as `request-collision`; nothing starts.
* One memory budget covers every client's runs. Admission counts them all.
* `cancel`, `lookup` and the automatic retry act only on the calling client's
  runs. Another client's run is refused as `not-yours`. A run submitted before
  attribution existed has no client, and any client may cancel it.
* Reconcile and retention act on what a row records (live, finished,
  orphaned), never on who submitted it.

What does not hold yet ([#73](https://github.com/gbasin/pandora/issues/73)):
there is no fair share between clients, so one Mac can fill the budget. Every
client logs in as the same worker user, with that user's SSH key. And a Mac
still running older Pandora code sends no name: its runs record no client, and
it can cancel anyone's run. The guarantees above hold once every Mac runs this
code. Change `[client] name` only while `pandora ps` shows nothing live: runs
submitted under the old name answer cancel and lookup only to that name.

Where the name shows:

* `pandora ps` ends its first line with `as <name>`, and says how many runs each
  other client has live on the worker. `--json` has `client` on each row.
* `pandora result <id>` prints `client <name>`. `--json` has `client`.
* `pandora stats` names this client on its first line and lists every client the
  worker has seen, with runs and live runs.

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
allowed set printed.

The file is the contract between the repository and the daemon. When the
running daemon does not understand a key or a value, for example a daemon
started before `subdirectory = "passthrough"` existed, each claimed command is
refused with exit 70 and nothing runs. The refusal names the key and the value
and gives the fix: when `pandora ps` shows nothing running, `git -C <the
daemon's checkout> pull && pandora daemon --restart`. If the key is a mistake,
fix the file. The claim cache keeps the file's claimed forms meanwhile, so these
commands reach the refusal instead of running unmanaged. A file that is not
valid TOML, or that the daemon cannot read, claims nothing, and every command
runs as if Pandora were not installed. The reference is
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
| 75 | A local job is already active in this worktree, the worktree changed during a local run under `drift = "fail"`, a write-back was refused as stale or conflicted, or shards wrote one path differently. Or a restart did not finish within `PANDORA_DRAIN_WAIT`; nothing ran. | Wait for the other run, or retry after a restart. Do not edit the worktree while a validation runs. After a write-back conflict, follow the printed `pandora resolve` step. |
| 124 | `--max-wait` elapsed. The run was not stopped. | `pandora wait <id>` re-attaches. |
| 130 | Canceled. | Nothing. |

### Variables

| Variable | Effect |
|---|---|
| `PANDORA_OFF=1` | The shim execs the real pnpm: no queue, no memory gate, no receipt. On a claimed command in an enrolled repository it first starts the passthrough logger, which runs the real pnpm and appends one row to `<state>/passthrough.jsonl`; `pandora stats` counts it as bypassed with `PANDORA_OFF`. A last resort, for a job the local lane cannot run (a sharded suite) or to debug a routed failure. Never use it to skip the queue or after a memory-pressure refusal. |
| `PANDORA_WHERE=local` or `remote` | Place this one run. It keeps its queue, receipt and exit code. Exit 64 if the job cannot run there. An explicit `remote` never falls back; if the worker cannot take it, the exit is 70. |
| `PANDORA_SHARDS=N` | Shard count for this run of a sharded job, clamped to the job's `max` and to free lanes. |
| `PANDORA_DRAIN_WAIT=S` | How long a command waits for a daemon restart, in seconds. Default 660. Then it exits 75; nothing ran. |
| `PANDORA_SESSION=<id>` | Names the session that submitted the run. The run records it as `submitter`. Without it, `CLAUDE_CODE_SESSION_ID` (Claude Code) or `CODEX_COMPANION_SESSION_ID` (the Codex plugin) is used. Without any of them, the daemon records the top interactive process above the caller, as `name:pid`, from the socket's peer pid and one `ps` of its own; the client runs none. |

`PANDORA_*` variables are read by the outermost shim and never reach the run.

### Verbs

| Command | What it does |
|---|---|
| `pandora ps [--json]` | What is running and what just ran, with the worker's health on the first line, which ends `as <client name>` and counts other clients' live runs on a shared worker. While a restart drains, `daemon: draining` comes before it. A remote run not yet accepted shows its step: `freezing`, `shipping` or `submitting`. `--json` also shows each run's `submitter` and `client`; the table has no room for them. |
| `pandora wait <id> [--max-wait S]` | Re-attach and exit as the run exits. Several ids print one outcome line each and exit non-zero if any did not pass. A run no daemon follows any more is taken over, or closed with exit 70; a wait never hangs on it. |
| `pandora logs <id>` | Replay a run's output. Who submitted it goes to stderr first. |
| `pandora result <id> [--json]` | Outcome, exit, submitter, client, attempts, flaky pairs and hint. `--json` prints the whole result, with per-shard outcomes and the input digest. A run refused before it reached the worker has no result: this prints the refusal's cause and detail and exits 70. |
| `pandora cancel <id>` | Stop a run. A remote instance is destroyed. A local run whose daemon has exited ends `cancelled`, exit 130, and its process tree is stopped when it is still the run's. |
| `pandora resolve <id> --keep-local` or `--take-worker` | Settle a conflicted `--update` write-back. |
| `pandora stats [--since 24h] [--json]` | What routed, where, how long it waited and ran, what fell back and why, what claimed commands were bypassed with `PANDORA_OFF`, what heavy commands ran here unclaimed, and the worker's disk, goldens, ready state and runs per client. |
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
| `daemon-unreachable`, the daemon installed here (the client configuration exists) | 70 after a 5 s wait, with the doctor hint | 70 | 70 |
| `daemon-unreachable`, never installed here (no client configuration) | passthrough: runs here as if Pandora were not installed, no slot, one notice | passthrough | passthrough (writes in place) |
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
| `bin/pandora`, `bin/pnpm` | The two POSIX launchers. `pnpm` is the shim; its non-enrolled and fresh-cache paths fork nothing. |
| `pandora/cli.py`, `errors.py`, `exits.py` | The one `pandora` command, the typed exceptions and the exit table. |
| `pandora/client/` | Runs on the Mac: the daemon, the shim client, enrollment, the local lane, fallback, placement, write-back settlement, health, stats, hints, `doctor`, `upgrade` (`install.py`). |
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
on first use, from the version the daemon or the `pandora worker` verb runs. A
new version reaches the worker's runner on the first run after the daemon
restarts into it. The
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
