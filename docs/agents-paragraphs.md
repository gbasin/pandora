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
> with `PANDORA_WHERE=local`, which keeps it in the queue. If the message says
> this machine is under memory pressure, wait a few minutes and retry; do not
> bypass it. Exit 75 means a validation is already active in this worktree or
> the source changed during the run. `--update` runs on the worker too: do not
> edit the worktree while it runs, then review `git diff` of the files it wrote
> back. When Pandora has advice, it is the last line, `pandora: hint: ...`; act
> on it. Read
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
> Nothing about the commands. Run them from the repository root. There, Pandora
> claims them. Below the root, the `[matching] subdirectory` mode in
> `pandora.toml` decides what a claimed command does:
>
> - `passthrough`, the mode this repository uses: nothing is claimed below the
>   root. The command runs as typed, on this machine, as if Pandora were not
>   installed. It gets no queue and no receipt. `pnpm test` in a package runs
>   that package's own script. To route a suite, run it from the root.
> - `reroot`: the command runs from the root. If an argument names a path, the
>   command is refused with exit 64 and `pandora: run from the repo root to
>   route`, because a run from the root would read that path differently.
> - `reject`: every claimed command typed below the root is refused with
>   `Run this command from the repository root.`
>
> Results, reports and artifacts are in your worktree before the command
> returns. A report the runner did not write is reported as missing. Missing is
> not zero failures.
>
> The exit code is the command's own. Four codes are Pandora's:
>
> | Exit | Meaning | Do this |
> |---|---|---|
> | 70 | Infrastructure failure. Not a test verdict. | Retry. Or run it in the queue here with `PANDORA_WHERE=local <command>`. If the message says this machine is under memory pressure, wait a few minutes, then retry. Do not bypass it. If it says the daemon does not answer, or does not understand `pandora.toml`, run `pandora doctor` and tell the owner; do not bypass it. |
> | 75 | A validation is already active in this worktree, or the source changed during the run. After `--update`, nothing was written back. Or the daemon was still restarting; nothing ran. | Wait for the other run, or retry after a restart. Do not edit the worktree while a validation runs. After an `--update` conflict, follow the printed `pandora resolve <id>` step. |
> | 124 | `--max-wait` elapsed. The run was not stopped. | `pandora wait <id>` re-attaches. |
> | 130 | You canceled it. | Nothing. |
>
> Pandora's own lines go to stderr and start with `pandora:`. The last one can
> be `pandora: hint: ...`. A hint comes from measured evidence, such as a memory
> peak or a report that is absent. Act on it. `pandora: daemon is restarting;
> waiting` is normal and needs no action: the command runs when the restart ends.
>
> Each worktree routes by its own `pandora.toml`. If you change it, the change
> applies from the next command in that worktree. That one command starts a
> little slower while Pandora reads the new file, and may print `pandora: claim
> cache refreshed from pandora.toml`. There is nothing to enroll again. If
> Pandora refuses a claimed command because it does not understand a key in
> `pandora.toml`, the message names the key and the fix; the fix updates Pandora
> on this machine, so leave it to the owner.
>
> `pandora ps` lists runs. `pandora logs <id>` replays one. `pandora cancel <id>`
> stops one. `pandora result <id>` shows its outcome and hint. `pandora stats`
> shows what ran, where, and what fell back. `pandora doctor` checks that this
> shell and worktree are set up to route; it changes nothing. `pandora --help`
> lists the rest.
>
> To choose where one command runs, set `PANDORA_WHERE=local` or
> `PANDORA_WHERE=remote` before it. The run keeps its queue, its receipt and its
> exit code. Exit 64 means the job cannot run there; the message says why.
> Nothing falls back from an explicit `remote`: if the worker cannot take it,
> the exit is 70.
>
> `--update` runs on the worker. It writes its declared files back only after
> a passing run, and only when you did not edit the worktree during the run. If
> you edited a declared file, Pandora keeps your version, prints the path of the
> worker's version, and exits 75. Merge the two by hand. Then run
> `pandora resolve <id> --keep-local`. Validate without `--update` after every
> update.
>
> `PANDORA_OFF=1` runs a command on this machine with no Pandora at all: no
> queue and no memory gate. Use it only when a refusal names it as the last
> resort, or to debug a routed failure. Never use it to skip the queue, and
> never after a refusal that says this machine is under memory pressure.
