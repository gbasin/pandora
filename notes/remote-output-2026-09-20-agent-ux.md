# Agent UX for returned build outputs and generated source

Dated evidence, 2026-09-20 UTC. Six fresh coding-agent trials exercised a small
remote fixture: first build, source generation, and build with an existing output,
once each with Codex and subscribed Claude Opus. Claude's opus alias resolved to
claude-opus-5. Both used the user's existing CLI setup. No mid-trial hints,
restarts, or human interventions were supplied.

## Method

The agent-fanout controller created six separate worktrees from fixture commit
46755e6. Two agents ran at a time. Each fixture had its own bootstrap and empty
node_modules directory; there are no package dependencies. The local pnpm
launcher was 8.15.0. Agent lifecycle used the skill's controller, including its
normal Codex progress watchdog. The run was pandora-output-ux-20260920.

The evaluator-only package scripts call a Python bridge. The bridge sends schema
and fixture bytes to the authorized VM over SSH. A bounded fresh Node container
performs each build, generation, or test with no network, half a CPU, 128 MiB RAM,
and 64 PIDs. Requests serialize on the pilot worker lock. The cached Node image
resolved to sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6.
The bridge records remote and local status separately and returns files locally.
This is not the production PATH router, Docker command interception, a general
sync implementation, or an interruption/recovery protocol.

The brief asks for the task outcome and names the ordinary project command. It
contains no recovery strategy. The build task changes Draft to Ready, runs pnpm
build, and reports the local output's title and SHA-256. It says not to edit
generated outputs directly. The generation task asks for pnpm generate, preserving
a handwritten note, and a passing pnpm test. Supporting tooling is out of edit
scope, but agents can inspect it. Opus read the bridge implementation.

Build outputs are written automatically only when the destination does not exist.
For an existing file, this conservative fixture preserves it and returns a path
to the new artifact with status 75. Generation always returns a diff and generated
file without modifying source, with an explicit review/apply/test message and
status 75. Local pnpm normalizes that script status to shell status 1 and prints
ELIFECYCLE failure text. A preflight assertion expecting shell status 75 failed;
its expectation was corrected before agent trials. Production interception
outside pnpm need not inherit that package-script presentation.

The repeat-build worktrees start with an evaluator-created Draft build output.
They receive the same brief as first-build worktrees. The extra trials followed
Opus's first-build observation that the fixture would refuse a second build.
They use fresh agent sessions, not follow-up coaching of the first sessions.

## Outcomes

| Story | Codex | Opus |
| --- | --- | --- |
| First build | Completed; one remote build; correct local output and hash | Completed; one remote build; correct local output and hash |
| Returned generated source | Reviewed artifact and diff, copied returned file, then test passed | Reviewed artifact and diff, edited the changed field, checked equality, then test passed |
| Existing build output | Inspected old and new outputs, reported stale canonical output, and stopped | Moved old output to /tmp, ran the remote build a second time, then reported the correct local output |

Both successful first-build reports matched SHA-256
`e4b39c0e4584c5267f59941ad716277441072ecb1b3464f001ac5b5330a84970` and title Ready.
Both source-generation trials changed only Legacy to Draft in the receipt file,
preserved its note, and ran one generation plus one passing test. Neither retried
generation after its nonzero status. Both understood that generation succeeded
remotely while local application remained necessary. Opus explicitly criticized
the contradictory package-manager failure text.

Codex's repeat-build local file stayed Draft. It correctly identified the separate
Ready artifact and did not falsely report the local file as updated. The task
remained incomplete. Opus's second build produced the same bytes as its first
remote build, so the repeated remote work was avoidable. Opus retained the old
file at /tmp/pandora-stale-report-90f1e3ba.json. Neither modified supporting tools
or launched a local build fallback.

All six controller lanes reported succeeded because each CLI completed normally.
That is not the task score: the Codex repeat-build lane stopped short, and the
Opus repeat-build lane incurred an unnecessary remote build. Distinguish agent
process completion, remote execution success, delivery, and task completion.

## Interpretation and limits

The simple generated-source workaround was understandable to both agents. The
fixture's diff is one line, the tool feedback states the next action, and the
implementation is readable. This does not establish usability for large codegen
changes, deletions, concurrent source edits, or ambiguous snapshot updates.
There is one trial per agent per condition, no randomized ordering, and no
statistical reliability claim. These requests take roughly a second each; this
trial does not test patience during long builds or queue contention.

The repeated-build result argues against treating every existing generated file
as a manual delivery problem. One agent stopped and another repeated completed
work. The instruction against editing generated outputs may have influenced both
responses. This supports automatic delivery for declared generated outputs, with
explicit ownership and recovery rules, rather than proving one universal agent
response to conflicts. The previous race probes still apply: arbitrary editors
cannot be excluded by an advisory lock, and retaining old generations preserves
bytes without guaranteeing edits stay at the canonical path.

Source-generation review/apply can remain a candidate behavior. Automatic
replacement in mixed source/output directories remains unapproved and unproven.
A production result should clearly separate remote command status from local
delivery or application status, and provide the existing artifact as the next
action when no new computation is necessary. Never change a non-success delivery
into a false claim that the workspace is updated merely to avoid a red exit code.

## Evidence and cleanup

[Selected evidence](../experiments/output-ux/evidence/2026-09-20/summary.json)
includes exact briefs, command/edit actions, final reports, remote command events,
resulting files, and source diffs. Complete raw CLI events and controller
collections are retained at
`~/.local/state/pandora/evidence/2026-09-20-output-ux/`.

Every owned remote execution container was removed. The controller was cleaned
without dropping worktrees, preserving all six trial worktrees and their edits.
The VM remains available under the user's authorization. This experiment does
not enable automatic workspace replacement or change normal agent sessions.
