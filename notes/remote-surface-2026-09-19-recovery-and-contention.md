---
status: log
---

# Remote validation: recovery, contention, and bypasses, 2026-09-19

This trial extended the SSH pilot with recovery after client loss, four concurrent
agents, and a comparison of three treatments for a recognized direct command.
Gary selected finish-within-deadline and recover-on-retry as the client-loss
policy. Crabbox was not used. The implementation still uses SSH, rsync, Docker,
and a session launcher, with systemd now owning each remote worker.

The evidence supports another controlled pilot. It does not establish general
command enforcement, unattended reliability, or safe twelve-agent operation.

## Recovery contract and results

The active descriptor belongs to the canonical worktree within the selected local
state directory. Restarting a launcher with a different session ID can recover the
same request. Changed commands or hosts cannot replace unresolved work. A durable
attempt identity exists before source capture. The preparation child retains the
request lock if its parent dies. A cancelled pre-start attempt leaves a marker
that any delayed worker must check before execution.

A remote worker has a 40-minute systemd deadline. Waiting for its one execution
slot expires after 15 minutes. A separate timer limits each test container to
20 minutes. Explicit cancellation stops the owned attempt and verifies cleanup.
Connection loss alone does not create a replacement run.

Recovery retrieves the original attempt's logs and artifacts into staging,
checks their hashes, and promotes complete evidence locally. A successful result
requires the expected test evidence. Source changes during an interruption make
recovery return 75, with an explanation that the result describes earlier input.
A later deliberate invocation validates the new source.

| Fault | Observed outcome |
| --- | --- |
| Kill the route and preparation processes during execution | Retried command recovered the same attempt, exit 0 |
| Kill only the route during source capture | Immediate retry returned 75; surviving preparer retained the lock; later launcher recovered the same attempt |
| Kill during artifact retrieval | Retry recovered complete verified artifacts, exit 0 |
| Cut the trial's SSH connection for 55 seconds | Initial invocation returned 75; retry recovered the original passing attempt |
| Change source after killing the client | Earlier passing evidence was recovered, but the command returned 75 and identified the source mismatch |
| Retry from a new launcher session | Recovered the same attempt, exit 0 |
| Reduce one running test container to 128 MiB | Docker recorded OOMKilled; command returned 137; cleanup was verified |
| Cancel queued and running invocations explicitly | Both returned 130 and verified cleanup without stopping other jobs |

The network probe used a TCP proxy for the trial's SSH processes and forcibly
closed its connection. It did not modify the Mac or VM's network configuration.
The artifact probe paused the retrieval rsync, then killed its owned client
processes. These are scoped fault injections, not outages of shared services.

One preliminary network probe overlapped the evaluator's source-change probe.
It correctly recovered remote success but returned 75 for the changed local
source, contrary to the evaluator's initial expectation of 0. The network case
was repeated with stable source. This is an evaluator confounder, not evidence
of a failed network recovery or a hidden passing result.

## Four-agent contention

The target revision was Acme `b18725e9c4f492cfa183ac1fccb452a5e5bfff34`, matching
the earlier trial. Each worktree was independently bootstrapped. Two Codex agents
and two subscribed Claude Opus 5 agents ran the normal surface command with the
45-test smoke spec plus one unique source sentinel. All four ran concurrently
as agents; their test containers shared one worker slot.

| Agent | Tests | Queue | Total command |
| --- | ---: | ---: | ---: |
| Codex A | 46 passed | <0.01 s | 114.4 s |
| Opus A | 46 passed | 90.0 s | 202.8 s |
| Codex B | 46 passed | 190.0 s | 303.9 s |
| Opus B | 46 passed | 290.0 s | 402.7 s |

Each report identified the correct sentinel. No agent resubmitted, cancelled,
restarted the worker, or launched a local fallback during these runs. The briefs
were validation-only and instructed agents to retain existing pending handles;
this is not an unconstrained product-debugging experiment. Scripted recovery and
bypass probes also contended for the worker during the observation window. The
file lock is not FIFO, so durations are not measurements of guaranteed fairness.

Thirty samples over approximately five minutes observed at most one experiment
container, and Mac `memory_pressure -Q` free-percentage readings of 64–69%.
The four warm containers peaked at approximately 1.36–1.38 GiB and recorded no
OOM events. This does not justify increasing slots automatically: the sample
covers one workload, source capture is still concurrent, and other Mac activity
was uncontrolled. No interactive responsiveness benchmark was performed.

## Long quiet-output wait

A separate evaluator service held the worker lock for 720 seconds without using
heavy compute. A fresh Codex agent ran the normal command piped through `tail -80`,
so it could not see live queue heartbeats. Its measured queue wait was 700 seconds
and total command duration 726.8 seconds. It retained the original handle, emitted
waiting updates, and reported one passed test with exit 0.

This crossed the native supervisor's 600-second quiet threshold in total elapsed
time. It did not produce 600 seconds without agent progress: the agent's waiting
updates continued. The evidence shows that this particular wait survived the
watchdog, not that silent or wedged agents cannot time out.

## Bypass comparison

Three fresh Codex agents received the same validation brief and direct command:

```sh
pnpm --filter @acme/web test:e2e pandora-bypass.spec.ts --workers=1 --reporter=line,junit
```

The evaluator-owned spec contained one deterministic assertion, keeping the
permitted local bypass small. The treatments used separate worktrees.

| Treatment | Result | Agent behavior |
| --- | --- | --- |
| Normal | One test passed locally | Ran the direct command; no remote submission |
| Block | Exit 64; no tests ran | Reported an infrastructure blocker despite receiving an exact supported alternative |
| Redirect | One test passed remotely | Completed through the existing command and reported remote execution |

The block lane's supervisor recorded a successful agent process, but the
validation task did not complete. It must not be scored as a passing validation.
The brief told agents to report infrastructure limitations, which may have
contributed to stopping. There was one agent per treatment, one harness, and no
counterbalanced ordering. This supports preferring an exact known redirect for
another trial, not a universal claim that agents cannot handle blocking.

The recognizer covers only this package-command form, known selectors, and the
exact supported runner flags. It does not rewrite arbitrary shell text. Absolute
executable paths and manual PATH changes still bypass it.

## Environment compatibility and warming

The private PATH changes executable precedence. zsh startup files still run,
then the wrapper prefix is restored. Ordinary pnpm commands delegate to the
executable selected at launcher start. This can differ from a version manager's
later executable selection. A manual PATH prepend inside a shell can bypass the
wrapper. Global startup files are unchanged.

The first pilot also forced Codex into non-login shells. A real-shell probe
confirmed that this skips login-only initialization. The recovery revision removes
that override because the session-specific zsh startup files now preserve routing
through login initialization. A first real Codex preflight then found that the
explicit `shell_environment_policy.set.PATH` still discarded a PATH entry added
by the login profile, although its ordinary environment variable survived. The
same shell probe outside Codex preserved both. Removing that explicit PATH
override made the next Codex preflight preserve the variable, additional
executable, and Pandora pnpm wrapper. Original global startup files were not edited.
The corrected Codex session then ran one remote test successfully. Explicit user
or agent PATH overrides remain a potential bypass.

Source sync remains per-invocation: freeze dirty tracked and nonignored untracked
files, verify a manifest, and rsync into a distinct remote snapshot. Unchanged
remote files are reused with hard links. Test containers do not modify snapshots.
Installed dependencies stay in an image keyed by installation inputs, with a
separate writable layer per run. Builds still execute each time. There is no
continuous Mutagen sync or general source write-back.

A fresh transfer exposed a cache defect hidden by earlier snapshot reuse: copying
an unchanged patch with a new timestamp caused pnpm to reject the installation
as stale. The worker now retains keyed installation inputs already in the image
and copies the remaining source over them. The initial archive overlay also
created root-owned parent directories; explicit directory entries fixed that.
Both failed trials were followed by a passing 45-test baseline before the agent
runs. An initial evaluator invocation from Pandora instead of Acme was rejected
because no matching dependency image existed; it did not run target tests.

## Output and remaining limits

A separate intentional-failure probe established that `command | tail` can return
shell exit 0 while validation failed. With `pipefail`, the pipeline returned 1.
Pandora's logs and terminal evidence reported the underlying failure in both
cases. No global shell options were changed to conceal this ordinary shell
behavior. A consumer must inspect the actual result or preserve pipeline failure
status, rather than infer a test pass from an arbitrary pipeline's status.

Remaining gaps include abrupt remote-worker death without a terminal record,
ambiguous pre-registration failures, provider deletion, automated disk retention,
cold dependency-build deadlines, broader command coverage, and other platforms.
The guard does not find work submitted from a different state directory, moved
worktree, or client machine. The SSH user and submitted code remain trusted.

Raw logs, driver scripts, result collections, manifests, and verified artifacts
are retained under `~/.local/state/pandora/evidence/2026-09-19-recovery/`.
The [committed evidence](../experiments/routing/evidence/2026-09-19-recovery/)
includes run identities, agent reports, commands, fault outcomes, and resource
samples. The supervisor run is `pandora-recovery-20260919`. No tracked Acme
source was changed by the implementation.
