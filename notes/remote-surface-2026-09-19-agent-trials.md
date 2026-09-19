---
status: log
---

# Remote surface experiment: Codex and Claude Opus, 2026-09-19

The initial agent trials support keeping normal commands and remote execution:
after launcher fixes, Codex and Claude Opus used the existing surface command,
waited for results, and reported the correct outcome. They did not need a new
service command. This is a small pilot, not a production reliability result.
Gary selected SSH for the first agent trial and explicitly requested Opus coverage.

The [machine-readable evidence](../experiments/routing/evidence/2026-09-19.json)
records source identities, timings, terminal states, test counts, and memory
measurements. Source and dependency warming are documented in the
[warm-path note](remote-surface-2026-09-19-warm-path.md).

## Trial configuration

Agents stayed on the Mac in independent, bootstrapped Eichler worktrees at
`b18725e9c`. Normal `pnpm test:surface borrower-web <file>` invocations were routed
through session-scoped executables. No tracked Eichler changes or global shell
settings were needed. All heavy test execution used the same dedicated VM and
one worker slot, with two CPUs, 6 GiB RAM, and one Playwright worker per container.

The existing subscribed Codex CLI used `gpt-6-astra`; authentication reported
ChatGPT. Claude authenticated through claude.ai/Max and the requested `opus`
alias resolved to `claude-opus-5`. The agent-fanout skill supervised the agents.
No model API key was configured for the trial.

The informed brief explained remote routing and normal queueing, required a
pnpm-resolution preflight, and prohibited local fallback. The fresh Opus brief
only asked for the ordinary test command and its result, with a validation-only
scope. The failure brief asked for diagnosis without editing code or changing
the environment. These are different treatments, not matched statistical samples.

## Outcomes

| Agent scenario | Result | Queue | Total command time |
| --- | --- | ---: | ---: |
| Informed Opus | Correctly reported 45 passed, exit 0 | <0.01 s | 114.8 s |
| Fresh Opus, ordinary instructions | Correctly reported 45 passed, exit 0 | <0.01 s | 116.9 s |
| Informed Codex after launcher fixes | Correctly reported 45 passed, exit 0 | 100.0 s | 215.9 s |
| Fresh Opus, evaluator-controlled failure | Correctly reported 1 failed, exit 1 | 40.0 s | 68.9 s |

The informed Codex and fresh Opus runs overlapped. A separate scripted failure
also contended for the slot. The lock is not FIFO; observed wait duration must
not be interpreted as strict submission order.

Each successful test suite took approximately 85–87 seconds in Playwright.
Source capture took 4–5 seconds and transfer approximately three seconds.
Dependency preparation hit the warm image and took less than 0.1 seconds.

The queued Codex agent reported that the worker was occupied and kept waiting on
the original invocation. Neither tested harness cancelled, restarted, submitted
a duplicate, or launched a local heavy fallback after its request was accepted.
No human intervention was needed inside the four completed validation scenarios.
That excludes the launcher fixes described below; it is not a claim of zero
setup intervention.

The failing spec compared two fixed unequal strings. Opus identified it as an
intentional evaluator failure, distinguished it from infrastructure failure,
reported exit 1, and inspected the returned evidence directory. It did not try
to fix the spec or change the worker. The evaluator-owned spec was never a
product change or committed to Eichler.

## Failures found before the successful Codex run

Three Codex preflight sessions stopped because `pnpm` resolved to Homebrew rather
than the session wrapper. Those are failed routing trials even though the agent
process exited successfully. None launched validation locally.

There were several layers to fix: the supervisor's environment, where the Codex
executable override is applied, CLI config placement on the `exec` subcommand,
and this Mac's `.zshenv` prepending Homebrew on every shell startup. Disabling
login shells alone did not fix the last problem.

The final launcher supplies explicit Codex shell environment settings and a
session-specific ZDOTDIR. Its startup files source the original files, then
restore the trial PATH prefix. Both login and non-login shell probes resolve
the wrapper. The agent-fanout adapter selects the private Codex executable at
the native watchdog entry point without editing the shared skill.

Opus's first launch also encountered the launcher's overly strict nested-session
check before a model session started. It was fixed to retain original executable
paths while assigning the new trial session. The subsequent Opus sessions routed
correctly.

## Scripted coordination and cancellation

A duplicate request using a different selector returned exit 75 and reported the
existing active invocation. It neither submitted another job nor replaced the
original. A successful routed command returned exit 0 and all 45 tests.

Queued cancellation completed with verified cleanup. The first running-cancel
probe stopped the container but failed to persist its terminal record: closing
SSH broke the output pipe during cleanup. The local guard left the request
unresolved and blocked replacement, rather than claiming cleanup succeeded.

The worker now writes raw stdout/stderr to regular files and uses separate tail
processes for live delivery. After this correction, explicit running cancellation
returned 130 and verified no owned test container remained running. Abrupt
SIGKILL, network partition, and artifact-download interruption have not yet been
validated. Their behavior must not be inferred from the explicit-cancel test.

## Agent UX observations

Opus needed no backend tutorial in the ordinary-command scenario. However, it
piped the command through `tail -80`, hiding live progress until completion.
It still waited and reported the final result. A design that depends on agents
always seeing queue heartbeats would be fragile. Pipelines also deserve explicit
failure-status tests; this passing pipeline did not establish their exit semantics.

The informed Opus run appended an exit-code echo. The failure run did likewise
and correctly reported the failing status. The informed agent also noticed that
the source digest, attempt ID, and local evidence-directory identifiers are hard
to connect. The manifest links them, but the terminal presentation can improve.

Eight resource samples over approximately 40 seconds observed one experiment
container at a time, zero current Linux memory-pressure averages, and Mac
`memory_pressure -Q` free-percentage readings of 64–66%. These are limited
system-wide observations with other local activity uncontrolled. They do not
establish responsiveness or safe concurrency for twelve agents.

## Implication and boundaries

The supported command can feel ordinary to the agent, and warm remote execution
works. The main problems found here were launcher integration and lifecycle
bookkeeping, not agents becoming impatient during the measured queue waits.

Keep the current pilot scoped. Before broader adoption, test abrupt client loss,
network loss, bounded OOM, interrupted artifact retrieval, and repeated recovery.
Add and compare the block-bypass and redirect-bypass treatments. The current
normal-command wrapper does not intercept absolute executable paths or arbitrary
shell constructions. Long queues also need testing against harness watchdogs.
Do not infer twelve-agent readiness from this pair.

Cold dependency-image preparation is refused in the agent trial until its
independent deadline and cancellation are implemented. Disk retention is manual.
GitHub Actions, other control planes, and automatic provider deletion are not
part of this pilot. Gary remains responsible for VM deletion within the agreed
trial window. No PR was merged and required Eichler CI remains unchanged.

## Evidence locations

The durable supervisor run is `pandora-informed-20260919`, under
`~/.local/state/agent-fanout/runs/ce71eef1efa1/`. Its process-level success labels
must be read alongside agent outcomes; a preflight abort is not a successful
validation trial. Raw logs, manifests, JUnit, artifacts, and terminal records for
the four completed validation scenarios were copied to
`~/.local/state/pandora/evidence/2026-09-19/<attempt>/` before cloud cleanup.

An unrelated supervisor status-JSON failure was filed as
[Papercut #4](https://github.com/gbasin/pandora/issues/4), without an inline fix.
