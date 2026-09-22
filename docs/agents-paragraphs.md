# What agents are told

The owner chose **A**: agents keep typing the commands they type today and learn
only what changes when a command runs somewhere else. The text below is final.
It is written for eichler and lands in eichler's own PR; nothing here edits
eichler.

The fan-out vocabulary (draft **B**) is not in either file. It is the `FANOUT`
section of `pandora --help`, for orchestrators that ask for it.

## `AGENTS.md`

Replaces the sentence "Local validation shares machine capacity through Pueue.
Read [...] for setup, suite selection, cancellation, and recovery." in the
"Run `pnpm check`" bullet.

> Validation runs where it runs best: broad suites on the Pandora worker,
> focused ones on this machine. Use the same commands as before, from the
> repository root. Results, reports and artifacts are in your worktree before
> the command returns, and the exit code is the command's own. Exit 70 is an
> infrastructure failure, never a test verdict: retry, or run the command here
> with `PANDORA_OFF=1`. Exit 75 means a validation is already active in this
> worktree or the source changed during the run. When Pandora has advice, it is
> the last line, `pandora: hint: ...`; act on it. Read
> [`tools/notes/local-validation.md`](tools/notes/local-validation.md) for
> suite selection, cancellation, and recovery.

## `tools/notes/local-validation.md`

Replaces the opening paragraph and the whole "Machine setup" section. The
"Commands during iteration" table and everything after it stay.

> Local validation runs through Pandora. Type the same commands as before. A
> broad suite runs on the Pandora worker; a focused one runs on this machine in
> the same queue. GitHub Actions executes the same commands directly.
>
> ## What changes for you
>
> Nothing about the commands. Run them from the repository root. A validation
> typed in a subdirectory with a path in its arguments is refused (exit 64) with
> `pandora: run from the repo root to route`; it never runs locally by accident.
>
> Results, reports and artifacts are in your worktree before the command
> returns. A report the runner did not write is reported as missing. Missing is
> not zero failures.
>
> The exit code is the command's own. Four codes are Pandora's:
>
> | Exit | Meaning | Do this |
> |---|---|---|
> | 70 | Infrastructure failure. Not a test verdict. | Retry. Or run it here with `PANDORA_OFF=1 <command>`. |
> | 75 | A validation is already active in this worktree, or the source changed during the run. | Wait for the other run. Do not edit the worktree while a validation runs. |
> | 124 | `--max-wait` elapsed. The run was not stopped. | `pandora wait <id>` re-attaches. |
> | 130 | You cancelled it. | Nothing. |
>
> Pandora's own lines go to stderr and start with `pandora:`. The last one can
> be `pandora: hint: ...`. A hint comes from measured evidence, such as a memory
> peak or a report that is absent. Act on it.
>
> `pandora ps` lists runs. `pandora logs <id>` replays one. `pandora cancel <id>`
> stops one. `pandora result <id>` shows its outcome and hint. `pandora stats`
> shows what ran, where, and what fell back. `pandora --help` lists the rest.
>
> To run a command on this machine with no Pandora at all, set `PANDORA_OFF=1`.
> Use it to debug a routed failure, never to skip the queue.
