---
status: log
---

# Client daemon and pnpm shim: a measured POC, 2026-09-21

Built and run on this Mac. No remote host was contacted; the worker is faked
inside the daemon. No dotfile, `~/.local/bin`, launchd job, `~/.codex` or
`~/.claude` configuration was modified. Every test builds its own temporary
state directory, fake repository and fake `pnpm`, and passes an explicit `PATH`.

Code: `experiments/client/`. Tests: 85, all passing, twice in a row, in 64 s.
Benchmark: `experiments/client/bench.py`, 250 invocations per case.

## What was built

A long-lived per-user daemon (`daemon.py`) that owns configuration, the
enrolment list, the run registry, the fallback policy and the passthrough log;
and a thin `pnpm` shim (`bin/pnpm`, POSIX sh) that decides per invocation
whether to hand off or to `exec` the real pnpm.

In production the daemon would also own the SSH ControlMaster, the snapshot and
the transfer. Here `backend.py` fakes a worker with nine configurable modes
(`ok`, `bytes`, `signal`, `slow`, `interleave`, `unreachable`, `queue-timeout`,
`hang`, `accept-then-drop`), because what this POC measures is client
ergonomics and side effects.

### Classification is two-tier, and it had to be

The config-driven classifier was reused, not restubbed.
`experiments/client/repo_config/` holds verbatim copies of
`classify.py`, `config.py`, `ci_import.py`,
`examples/acme.pandora.toml` and `fixtures/acme/.github/workflows/ci.yml`
from `poc/ci-import` (commit `d6e0464`). Nothing in them was edited. The acme
configuration loads and classifies correctly here: 13 jobs, 21 claimed argv
forms.

It cannot live in the shim. Loading it costs **~50 ms cold and ~10 ms warm**,
against a whole-shim budget of 10 ms. So:

* the **daemon** holds the configuration and is the authority — it
  re-classifies every request and may still refuse, before acceptance;
* the **shim** holds a derived flat list of claimed prefixes, written into the
  enrolment marker, and answers only "could this be claimed?".

`test_claims.py` asserts the two agree on 14 argv spellings, and asserts that
when the index over-claims, the daemon's refusal still leaves the command
running locally.

### Enrolment is one file per repository, not per worktree

`pandora enrol <repo>` writes `<git common dir>/pandora-enrolled`. Every
worktree of a repository shares one common directory, so one file covers all of
them, including worktrees created later.

The shim finds that directory with no process at all: walk up for `.git`; if it
is a directory that is the common dir; if it is a **file** (every linked
worktree) read the `gitdir:` pointer and strip the `/worktrees/<name>` tail.

Checked against the real thing, read-only: **all 57 acme worktrees** resolve
to `/Users/you/Code/acme/.git`, agreeing with
`git rev-parse --git-common-dir` in 57 of 57 cases, in 24.5 ms total in-process.

### The protocol

NDJSON over a `SOCK_STREAM` unix socket in a 0700 directory, socket mode 0600.
`run` / `attach` / `cancel` / `detach`, plus `ping` and `stats`. Payload bytes
travel base64 inside a `log` frame.

Each run's frames are appended to a **file**; clients are served by copying byte
ranges out of it. That one choice buys three things at once: flat daemon memory
on a 50 MB run, re-attach as a byte offset rather than a replay buffer, and
exact ordering across stdout and stderr.

The fallback rule is enforced structurally, not by convention:

* before `accepted` — the command provably has not run, so any problem at all
  means one line on stderr and a local `exec`;
* after `accepted` — never local. Lost connection means re-attach by run id;
  ten attempts over ~2.5 s, then exit 70 with a message naming the run;
* `--update` runs never fall back at all, because a local run would write files
  the worker should have written. They exit 70 instead.

Client disconnect is detach. Only an explicit `cancel` stops a run.

## Results

| # | Side effect | Verdict | Numbers |
| --- | --- | --- | --- |
| 1 | Shim latency, non-enrolled | **PASS** | +4.39 ms p50, **+4.82 ms p95**, +15.6 ms max over 250 invocations. Budget was p95 ≤ 10 ms. |
| 1 | Shim latency, enrolled but unclaimed | **PASS** | +4.85 ms p50, +6.57 ms p95 (repo root); +4.93 / +6.24 (worktree); +4.84 / +5.90 (5 levels deep). |
| 1 | `PANDORA_OFF=1` | **PASS** | +4.19 ms p50, +5.14 ms p95. |
| 1 | Python shim, same decisions | **FAIL (budget)** | +36.94 ms p50, +39.19 ms p95. Ten times over. Interpreter start-up alone kills it. |
| 1 | Repo-root discovery method | **PASS** | walk-up in-process 5.18 ms p50 vs sh baseline ~5.3 ms, i.e. ~0 marginal. `git rev-parse --git-common-dir` 16.35 ms p50 — more than the whole budget on its own. |
| 1 | Git worktrees (`.git` is a file) | **PASS** | 57/57 real acme worktrees resolved correctly; relative and absolute `gitdir:` pointers both tested. |
| 1 | Routed round trip (context only) | n/a | +55.7 ms p50, +59.0 ms p95. Not on the budget: a claimed command is a test suite. |
| 2 | PATH order, plain login zsh | **PASS** | `~/.local/bin` is index 1, `/opt/homebrew/bin` index 2. A shim there wins. |
| 2 | PATH order, Agentboard tmux pane | **PASS** | tmux global env has `~/.local/bin` at 7, Homebrew at 11; Agentboard never touches PATH (`src/server/tmuxEnv.ts:19-25`, asserted by its own test at `__tests__/tmuxEnv.test.ts:15,97`). A shim there wins. |
| 2 | PATH order, Claude Code bash tool | **FAIL** | Shell snapshot pins `/opt/homebrew/bin` at index 1 and `~/.local/bin` at index 9. A `~/.local/bin` shim loses. |
| 2 | PATH order, Codex lanes and `--yolo` | **FAIL** | Tested directly: a temp shim dir exported by the launcher **was** inherited (position ~28) and still lost to `/opt/homebrew/bin/pnpm`. |
| 2 | Root cause of both failures | **found** | `~/.zshenv:6-9` prepends `/opt/homebrew/bin` unconditionally, with no dedup guard. `.zshenv` runs for *every* zsh including nested `zsh -lc`, which is how Codex and Claude Code run commands. `~/.local/bin` is prepended by `.zprofile:13` (login only) and `.zshrc:15,51` (interactive only). |
| 2 | corepack | **PASS** | `/opt/homebrew/bin/pnpm` is a symlink to `corepack/dist/pnpm.js`. It re-execs a per-repo version from `packageManager`. Through the shim, unchanged: `8.15.0` in a scratch dir, `12.3.4` inside acme. The shim adds no version skew. |
| 2 | Nested `pnpm` re-entry | **PASS (guard works)** | ~20 root acme scripts call `pnpm` from inside a pnpm script (`"check": "pnpm validate check"` and so on). `PANDORA_ROUTE_DEPTH` is set on every hand-off and checked first; tested at one, two and three levels, routing exactly once each time. |
| 3 | Pueue double-routing, chain A | **PASS (guarded)** | `pnpm check` → `pnpm validate check` in the root package.json. Without a guard that is two routes for one typed command. Guarded and tested. |
| 3 | Pueue double-routing, chain B | **FAIL (hole, documented and tested)** | `pnpm validate unit` → `pueue add … exec <abs node> validate.mjs _run` → `tools/validation/run.mjs:77` spawns a **bare** `pnpm` from `plan.mjs:5`. `PANDORA_ROUTE_DEPTH` does not survive: `queue.mjs:34` submits with `safeEnvironment()`, a 16-name allowlist at `state.mjs:83-106`. Confirmed empirically from `pueue status --json`: task env is 11 vars, exactly that allowlist. `PATH` **is** on it (`state.mjs:86`), so the shim is still reachable inside the job while the guard is not. `test_shim.py::test_an_env_scrubbing_hop_loses_the_guard` reproduces this and asserts the double route. |
| 3 | Pueue daemon's own PATH | n/a | `/usr/bin:/bin:/usr/sbin:/sbin` (launchd default, `~/Library/LaunchAgents/sh.brew.pueue.plist`, no `EnvironmentVariables` key). Irrelevant: pueue execs tasks with the **submitting client's** env snapshot, not its own. `pueue status` read-only, 221 tasks, untouched. |
| 4 | Socket missing | **PASS** | Falls back locally with one line. |
| 4 | Socket present, no listener | **PASS** | `ECONNREFUSED` → local, one line. |
| 4 | Daemon accepts nothing (wedged) | **PASS** | 300 ms handshake deadline fires, then local. Whole invocation under 3 s wall. |
| 4 | Daemon restarts mid-run | **PASS** | Client re-attaches by run id from its byte offset; output `a..f` arrives exactly once, no duplication, exit 0. |
| 4 | Daemon dies for good after accepting | **PASS** | Exit 70, no local run, stderr names the run id. |
| 4 | Version skew | **PASS** | Daemon refuses `v100` with `{"t":"error","code":"version"}` before acceptance; a future shim still runs the user's command locally. |
| 4 | Two daemons racing | **PASS** | `flock` on `daemon.lock`. The second exits non-zero with `already running`; the first keeps serving. |
| 4 | Stale socket cleanup | **PASS** | On start, under the lock, an unconnectable socket file is unlinked and rebound. |
| 4 | Sandboxed caller (EPERM connect) | **PASS, with a bug found** | Stand-in test with a mode-0 directory reproduces the sandbox's `EPERM`, per the matrix in `notes/codex-sandbox-routing-2026-09-21.md`. See "Surprises" — the first version crashed instead of running the command. |
| 5 | 20 simultaneous invocations | **PASS** | 20 distinct runs, 20 correct outputs, no crossing. |
| 5 | 50 MB of output | **PASS** | 0.44–0.75 s end to end, 70–118 MB/s, byte-exact. Daemon RSS 22.8 → 28.4 MB (+5.6 MB), well under the 40 MB assertion. |
| 5 | Stream ordering | **PASS** | 50 `out`/`err` pairs arrive in the worker's exact order on a merged fd; separate fds keep their streams. |
| 5 | Exit codes | **PASS** | 0, 1, 2, 42, 70, 75, 127, 128, 200, 254, 255 all preserved. |
| 5 | Signal deaths | **PASS** | SIGKILL, SIGTERM, SIGABRT reproduced as real signal deaths (`-9`, `-15`, `-6`), not as 128+n. |
| 5 | Ctrl-C → cancel | **PASS** | SIGINT sends `cancel`; run state becomes `cancelled`; client exits 130. |
| 5 | SIGTERM/SIGKILL → detach | **PASS** | Run continues to completion; `pandora wait <id>` re-attaches and returns 0. |
| 5 | stdin closed / non-TTY | **PASS (documented, not solved)** | `tty` is reported in the request and the run completes. No stdin channel exists: an interactive command would see EOF. See "Remaining risks". |
| 6 | Socket directory 0700, socket 0600 | **PASS** | Asserted. |
| 6 | Peer credential check | **PASS** | `LOCAL_PEERCRED` at `SOL_LOCAL` returning `struct xucred` works on this macOS; the test asserts it returns a real uid, so the check cannot silently no-op. |
| 6 | Token auth | **PASS** | Optional; wrong token gets `unauthorized` before acceptance, so the command still runs locally. |
| — | Fallback bounding | **PASS** | K=2: at most 2+1 of 6 concurrent claimed fallbacks ran, the rest exited 75 with `slots are all busy` naming `PANDORA_OFF=1`. Wait mode (K=1, 30 s): all 3 ran, serialised. Unclaimed commands never take a slot. Slots released on failure and on process death (`flock` on an fd). |
| — | Passthrough log and `pandora stats` | **PASS** | Heavy unclaimed commands appended to JSONL with duration and exit; 12 concurrent appends produced 12 intact rows; light commands not logged; fallbacks logged with their reason. `stats` prints runs by state and local commands by p50/p95/total. |

## Surprises

**A `~/.local/bin` shim loses in exactly the two places that matter most.**
The earlier probe's finding ("login zsh puts `~/.local/bin` first") is true and
misleading. `~/.zshenv:6-9` re-prepends Homebrew for *every* zsh, and both
Claude Code and Codex run commands through a nested `zsh -lc`. Exporting PATH
from the launcher does not help — it was tested and lost. The only location that
wins everywhere is an export in `~/.zshenv` itself, after the Homebrew block.
That is a dotfile change and it is not mine to make.

**The recursion guard cannot cross Pueue, and the shim can.** This is the
sharpest result. `safeEnvironment()` drops `PANDORA_ROUTE_DEPTH` but keeps
`PATH`, so a Pueue-run child re-enters the shim guard-free. Chain A (a
package.json script calling `pnpm`) is fully guarded by an env marker. Chain B
is not. Three fixes exist and all of them are one line; two need an acme
edit and one does not:

1. Filter the shim directory out of `PATH` inside `safeEnvironment()`
   (`state.mjs:86`). Kills re-entry for the whole Pueue subtree at once.
2. Add `PANDORA_ROUTE_DEPTH` to the allowlist (`state.mjs:85-102`). Exactly the
   pattern `ACME_VALIDATION_HOME` already uses; `run.mjs:79-87` then forwards
   it to every plan command.
3. Install the shim as a zsh function rather than a PATH entry. `spawn('pnpm',…)`
   is a bare `execvp` with no shell, so nested spawns never see a function. This
   needs no acme change, but it also means Codex and Claude Code — which do
   go through `zsh -lc` — would need the function defined in `.zshenv` anyway.

**A bug the sandbox case exposed, now fixed.** The fallback budget's lock files
live in the daemon's state directory. A shim inside a Codex `workspace-write`
sandbox cannot write there without `--add-dir` — and that is precisely the
situation where the daemon is unreachable and fallback fires. The first version
raised `PermissionError` out of `acquire()` and the command did not run at all.
Fixed: `fallback.Unbounded` is caught, the client warns that the limit is not in
force, and it runs the command. Refusing to run is worse than running unbounded.
Tested. Note the consequence for the earlier note's claim that design B "removes
the `--add-dir` requirement": it removes it for *routing*, but the fallback
budget is not enforceable from inside a sandbox without it.

**The whole client runs on macOS system Python 3.9.6.** Both halves were
exercised under `/usr/bin/python3` throughout — the shim resolves `python3` from
the test PATH, which is `/usr/bin`. No Homebrew Python, no third-party package.
That matters for the launchd plist, which can hardcode `/usr/bin/python3`.

**`git rev-parse` is not a cheap way to find a repo root.** 16.35 ms p50, on its
own, more than the entire per-invocation budget. The walk-up costs nothing
measurable.

**corepack is transparent to the shim.** `pnpm` is a corepack symlink that
re-execs a per-repo version from `packageManager`. The shim finds it by PATH
scan, execs it, and per-repo version selection still works (8.15.0 outside,
12.3.4 in acme). No special handling was needed.

## Recommended install location, per entry point

| Entry point | PATH today | `~/.local/bin` shim | Recommendation |
| --- | --- | --- | --- |
| Plain terminal | `.local/bin`(1), Homebrew(2) | wins | `~/.local/bin`, no dotfile change. |
| Agentboard tmux pane | interactive login; `.local/bin` ahead of Homebrew | wins | `~/.local/bin`. Agentboard passes PATH through untouched. |
| Interactive Claude Code | snapshot-pinned; Homebrew(1), `.local/bin`(9) | **loses** | Needs `export PATH="$HOME/.local/bin:$PATH"` in `~/.zshenv`, after the Homebrew block at line 9. |
| agent-fanout codex lanes | `zsh -lc`; `.zshenv` re-prepends Homebrew | **loses** | Same `.zshenv` line. A launcher-exported PATH was tested and does not work. |
| `codex --yolo` | same as lanes; sandbox mode only | **loses** | Same `.zshenv` line. |

One `.zshenv` line covers all five. It is the only location that does. The
alternative that needs no dotfile — intercepting at the corepack layer, since
all five funnel through one `corepack/dist/pnpm.js` — was not built or measured
here.

Sandbox requirement is unchanged from `notes/codex-sandbox-routing-2026-09-21.md`
and this POC does not improve it: a Codex lane needs
`sandbox_workspace_write.network_access = true` for the shim to reach the unix
socket at all. Under `read-only` or `workspace-write` without network, connect
fails with `EPERM`, and the shim degrades to "always local" at +4.4 ms p50 —
correct behaviour, silent except for one stderr line, and now tested.

## Remaining risks

1. **Chain B double-routing is real and unguarded** until one of the three
   one-line fixes lands. Today it would route a Pueue-spawned
   `pnpm exec vitest run` as if an agent had typed it. Do not enrol acme
   before this is closed.
2. **The `.zshenv` edit is load-bearing** for three of five entry points, and it
   is a global change to every zsh on the machine. It also silently changes
   `pnpm` for everything else the user runs.
3. **No stdin channel.** A routed command that expects a TTY sees EOF. Nothing
   in acme's claimed jobs is interactive, and the surface job already refuses
   `--ui`, `--debug` and `--update-snapshots`. Still unsolved, not merely
   untested.
4. **The marker lives inside `.git/`.** Cheap and it covers every worktree, but
   it is invisible to `git status`, survives `git gc`, and is absent from a
   fresh clone. A user who does not know it exists cannot find why routing
   stopped. `pandora stats` does not currently show enrolment.
5. **Fake backend.** Queue admission, snapshot, transfer and real cancellation
   latency are not measured here at all. The daemon's own restart-resume works
   only because the fake backend is deterministic; a real one must either
   re-attach to the worker or close the run as an infrastructure failure.
6. **Re-attach window is ~2.5 s.** A daemon restart slower than that ends the
   run as exit 70 from the client's side even though the run may still be alive.
7. **Peer credentials were verified on macOS only.** The Linux `SO_PEERCRED`
   branch is written and not exercised.

## Go / no-go

**Go**, conditional on two things being settled before anything is installed:

1. The owner decides on the `~/.zshenv` line. Without it, routing covers the
   terminal and Agentboard panes and misses Claude Code and every Codex lane —
   which is most of the actual traffic.
2. Chain B is closed by one of the three fixes, preferably filtering the shim
   directory out of `safeEnvironment()`, which needs no new contract between the
   two repositories.

Everything the design was uncertain about held up. The latency budget is met
with better than twice the headroom. The after-`accepted`-never-fall-back rule
is enforceable and enforced. Detach, re-attach, cancel and signal fidelity all
work. Memory is flat on 50 MB. One daemon per state directory is enforced by a
lock, not by hope. The parts that did not hold up — PATH ordering and the Pueue
env hop — are both environmental, both one line to fix, and both would have been
found only in production.
