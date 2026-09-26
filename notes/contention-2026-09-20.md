---
status: log
---
# Agent contention and client recovery, 2026-09-20

Four coding agents completed their repair loops through the existing SSH backend
under sustained contention. Two Codex sessions and two Claude Opus sessions each
submitted exactly two builds and two runs, observed the intended failure, repaired
only their own value file, and read the correct local output. No agent cancelled,
restarted, submitted a replacement, used direct SSH, or fell back to local validation.
This supports the waiting UX at four agents. It does not establish twelve-agent
capacity or justify unattended everyday use.

## Setup

The implementation was Pandora `11aba59`. Agent-fanout supervised run
`pandora-contention-20260920`, with separate worktrees, private command routing,
and shared Pandora state on the Mac. Codex used the native runner with its existing
model configuration. Claude used the `opus` alias, which reported `claude-opus-5`.
No queue-management instructions were added to the agents' task briefs.

The pinned Node fixture waited 30 seconds before returning each test result. Each
worktree initially contained `broken-<phase>` and used the same logical `app:test`
tag. The external profile allowed no mounts and returned `/workspace/dist` to local
`dist` after success. The deliberate delay exercised waiting, not CPU or RAM load.
A real Acme S0-01 journey ran through the same slot during the trial. A fifth,
scripted lane injected client loss without signalling any coding agent.

## Results

| Agent | Accepted requests | Intended failures | Final output | Longest queue wait |
| --- | ---: | ---: | --- | ---: |
| Codex 1 | 4 | 1 | `codex-1` | 90.4 s |
| Codex 2 | 4 | 1 | `codex-2` | 60.5 s |
| Opus 1 | 4 | 1 | `opus-1` | 70.4 s |
| Opus 2 | 4 | 1 | `opus-2` | 280.4 s |

All four final image IDs differed. Final local `dist/result.json` values matched
the correct worktree. The Dockerfile and test script remained unchanged. The
orchestrator had seeded the untracked fixture files and `.gitignore` additions
before launch; those are not agent modifications.

S0-01 passed with cleanup verified: 50.3 seconds of execution and 62.8 seconds
through evidence retrieval. Its dependency image remained warm. No target-repo
configuration changed.

The queue is a polling file lock, not FIFO admission. Opus 2's repaired build waited
280.4 seconds and then executed in approximately 1.8 seconds. Newer requests can
acquire the lock ahead of existing waiters. Opus waited successfully, but this is
an operational fairness gap, not a desirable agent experience. FIFO admission and
bounded, visible waiting deserve priority before a larger rollout. One execution
slot still bounds throughput even with fair ordering.

## Recovery under contention

The scripted lane built its own image, then tested two client-loss cases. It killed
only its owned warm-client process group, leaving the durable remote worker alive.
It did not cancel a coding-agent lane or kill the remote worker.

| Injection | Original queue wait | Duplicate command | Changed command after loss | Identical retry |
| --- | ---: | --- | --- | --- |
| Client lost while queued | 120.4 s | Exit 75, no submission | Exit 75, no replacement | Original attempt, exit 0 |
| Client lost while executing | 0.4 s | Exit 75, no submission | Exit 75, no replacement | Original attempt, exit 0 |

The lost local clients exited 137. Both remote runs finished their 30-second test,
returned the expected output, and reported verified cleanup. Retries retained the
original attempt IDs. There were exactly three accepted scripted requests: one
build and two runs. This tests client-process disappearance, not every network
partition, machine reboot, or remote-worker failure. Existing explicit cancellation
behavior was not changed or re-evaluated here.

## Agent friction

Codex 2 tried an output-directory bind mount, then a worktree-root mount, before
using plain `docker run`. Opus 1 tried an output-directory mount. Opus 2 planned a
mount but first hit the unsupported `docker version` command and then used the
supported forms. These rejections submitted no remote work. All agents eventually
used automatic artifact publication, but the rejection messages did not explain
that generated outputs return without a mount.

The image-only run also emitted contradictory wording: it first said no local
source was captured or injected, then said it was transferring changed source with
the empty manifest's digest. Opus 2 explicitly flagged the inconsistency. Opus 1
inferred that rebuilding might be redundant because local source was injected.
That inference was wrong: an unmounted run executes the resolved built image.
The agent nevertheless rebuilt as required and produced the correct result.
Clearer status and output guidance are warranted by observed misunderstanding.

Both Opus sessions piped some Docker commands through `tail`, which hides intermediate
queue feedback and changes shell pipeline exit semantics. They identified the
intended failure from Pandora's recorded exit line. This trial does not prove that
all agents will preserve failures correctly through arbitrary shell pipelines.

## Resources and evidence limits

There were 116 valid resource samples over 434 seconds. At most one execution
owner was observed at a time: either the BuildKit daemon, one Docker run, or the
journey's four containers belonging to one attempt. Sampling can miss short overlap;
the shared worker lease is also the code-level serialization mechanism.

The Mac's reported free-memory percentage ranged from 44% to 54% in those samples.
This is not an interactive-latency measurement, a CPU contention benchmark, or
proof that twelve real compiled workflows fit. Twelve earlier samples had an
invalid Docker output-template expression. Their raw evidence is retained, but
they are excluded from container-concurrency claims. The corrected sampler captured
part of the journey and the subsequent Docker activity.

Evidence collection checked every agent's four accepted requests, intended failed
run, output value, and cleanup receipts. It verified distinct final image IDs,
unchanged fixed fixture inputs, both recovery cases, and the passing journey.
Task-relevant transcripts, briefs, receipts, resource samples, and scripted fault
logs are in `experiments/contention/evidence/2026-09-20`.

The controller was cleaned up after collection. Dirty fixture worktrees and durable
reports were retained. No remote containers remained running; the VM had 46 GiB free.
Production execution and capture behavior were unchanged by this evidence change.
