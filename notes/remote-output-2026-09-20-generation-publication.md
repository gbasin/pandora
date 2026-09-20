# Generation publication and repeated agent samples

Dated evidence, 2026-09-20 UTC. This experiment replaces the conservative
existing-output refusal in the previous output UX fixture. The user requested
three independent samples. We ran three fresh Codex sessions and three fresh
Claude Opus sessions, each in its own worktree with an existing Draft output.
This is an evaluator-only POC, not a production routing change.

## Publication behavior

The profile owns one generated-output root, dist. Successful remote results are
saved separately, verified, and copied into a new local generation. A native
atomic directory exchange replaces dist while retaining the previous directory.
First publication uses atomic no-replace rename. dist remains an ordinary
directory, so consumers do not need to understand a new path or symlink layout.
Files absent from the new generation disappear from dist and remain available
in the retained old generation.

A prepared receipt records the incoming and previous directory identities before
exchange. If the process exits after exchange but before the published receipt,
retry recognizes the incoming directory at dist and does not exchange it again.
A worktree lock serializes cooperating publishers. The bridge retains a completed
remote result during local delivery and recovers that result on retry without
submitting another build. Different inputs cannot consume a pending delivery.

This is a narrow protocol. It assumes trusted local state, an exclusively managed
generated-output directory, and a filesystem supporting the atomic operation.
It does not establish power-loss durability, arbitrary concurrent-writer safety,
remote execution recovery, automatic retention limits, or atomic publication of
several independent output roots. Client failure before completed remote output
is recorded remains outside this fixture's recovery scope.

## Preflight evidence

Seven deterministic tests passed on macOS and Linux:

- First publication creates the expected output.
- Existing output is replaced and retained, including obsolete files.
- Process exit before and after exchange recovers without swapping back.
- An open writer can continue writing into the retained prior directory.
- A corrupt download leaves the previous output untouched.
- Replacing the destination directory before exchange stops publication.
- A symlink destination is rejected without changing its target.

The open-writer test preserves bytes, not canonical-path intent: edits through an
old handle land in the retained directory. Writers that continue resolving the
canonical path can modify the new output. An advisory lock does not prevent
uncooperative editors from doing either. The ownership boundary is still needed.

Three live-VM bridge preflights cover ordinary publication and injected process
exit immediately before and after exchange. Every case ran exactly one remote
build. Both interrupted cases recovered through the same pnpm build command,
without another remote submission. The new title was readable through Node's
filesystem API and a local Python HTTP server. Previous output was retained and
an obsolete file was removed from the new directory. These are ordinary
single-file consumers, not a test of Vite watchers or a live multi-file preview.

A generated-source regression also passed: generation left source unchanged,
returned its review/apply result, and the test passed after applying that result.

## Agent experiment

All lanes started from commit b375c23. They used fresh worktrees and CLI sessions,
with two agents active at a time. The brief matches the previous existing-output
trial: change the heading to Ready, run pnpm build, inspect dist/report.json, and
report its exact hash and title. It forbids direct edits to generated output and
changes to supporting tooling. No recovery hints or follow-up prompts were given.
Agents could read the implementation. The model and general task were repeated;
separate contexts are not a statistical guarantee of independent behavior.

The six requests execute the real fixture transformation in fresh bounded Node
containers on the authorized VM. The remote program is unchanged from the prior
trial. Each returned output should hash to
`e4b39c0e4584c5267f59941ad716277441072ecb1b3464f001ac5b5330a84970` and contain Ready.
This is a small JSON-output fixture, not a representative compiled-build or
large-artifact performance benchmark.

## Results

All six samples passed: Codex 3/3 and Opus 3/3. Each ran exactly one remote
build, received shell success, read the correct output at dist/report.json, and
reported the expected title and hash. Every previous Draft output was retained.
No agent needed a manual cleanup, artifact copy, repeat build, or human hint.
Only the requested schema title changed in each worktree.

These are six fresh samples of one simple task, not a reliability estimate for
all workflows. There was no randomized comparison with the earlier refusal
fixture, which had only one existing-output trial per agent.

## Interpretation

The previous refusal fixture led Codex to stop with stale output and Opus to move
the old file aside and repeat completed remote work. Generation publication is
intended to remove that manual decision from an ordinary rebuild. It leaves the
separate generated-source review/apply behavior unchanged.

This experiment can support the normal generated-output publication path. It
cannot authorize general source overwrite or settle behavior for concurrent
editors, multiple output roots, large transfers, and cached preview processes.
The dated previous trials retain their original outcomes and limitations.

## Evidence and cleanup

[Evidence directory](../experiments/output-ux/evidence/2026-09-20-publication)
contains preflight reports and each agent's exact brief, actions, final report,
source diff, local output, retained previous output, publication receipt, and
separate remote-execution and delivery events. Complete raw CLI events and
controller collections are retained under
`~/.local/state/pandora/evidence/2026-09-20-publication/`.

All execution containers were removed. The controller was cleaned while retaining
its six worktrees and their edits. The VM remains available under the user's
authorization. No ordinary agent session or target repository was reconfigured.
