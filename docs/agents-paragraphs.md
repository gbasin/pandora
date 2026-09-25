# Agent instruction paragraphs

This file holds the paragraphs a repository that uses Pandora may copy into its
own agent instructions. It is the source of truth for that text. When Pandora's
behavior changes, this file changes with the README in the same pull request.
A repository that copies a paragraph owns its copy and updates it from here.

Agents keep typing the commands they type today. The paragraphs tell them only
what changes when a command runs somewhere else. They agree with
[What agents type](../README.md#what-agents-type) in the README and with
`pandora --help`. The fan-out verbs (`pandora run --detach`, several ids to
`pandora wait`, `PANDORA_SHARDS`) are left out on purpose. They are the `FANOUT`
section of `pandora --help`, for orchestrators that ask for it.

Replace `<validation notes>` with the path of the repository's own validation
document, and pick the subdirectory bullet that matches its `pandora.toml`.

Eichler is one consumer. Its `pandora.toml` sets `subdirectory =
"passthrough"`. Its `tools/notes/local-validation.md` covers what eichler owns
and points to the README and `pandora --help` for the rest.

## Short paragraph for `AGENTS.md`

> Validation runs where it runs best: broad suites on the Pandora worker,
> focused ones on this machine. Use the same commands as before, from the
> repository root. Results, reports and artifacts are in your worktree before
> the command returns, and the exit code is the command's own. Exit 70 is an
> infrastructure failure, never a test verdict: retry, or run the command here
> with `PANDORA_WHERE=local`, which keeps it in the queue, when the refusal
> offers it. When the worker is
> full, the command waits in the worker's queue and prints `pandora: queued on
> the worker behind N runs`. That is normal, so let it wait. If the message says
> this machine is under memory pressure, wait a few minutes and retry. Do not
> bypass it. Exit 75 means a validation is already active in this worktree,
> the source changed during the run, a write-back conflicted, or a restart ran
> long. `--update` runs on the worker too. Do not
> edit the worktree while it runs, then review `git diff` of the files it wrote
> back. When Pandora has advice, it is the last line, `pandora: hint: ...`. Act
> on it. Read `<validation notes>` for suite selection, cancellation, and
> recovery.

## Longer section for the validation notes

> Validation runs through Pandora. Type the same commands as before. A broad
> suite runs on the Pandora worker. A focused one runs on this machine in the
> same queue.
>
> ## What changes for you
>
> Nothing about the commands. Run them from the repository root. There, Pandora
> claims them. Below the root, the `[matching] subdirectory` mode in
> `pandora.toml` decides what a claimed command does:
>
> - `passthrough`: nothing is claimed below the root. The command runs as typed,
>   on this machine, as if Pandora were not installed. It gets no queue and no
>   receipt. `pnpm test` in a package runs that package's own script. To route a
>   suite, run it from the root.
> - `reroot`, the default: the command runs from the root. If an argument names
>   a path, the command is refused with exit 64 and `pandora: run from the repo
>   root to route`, because a run from the root would read that path
>   differently.
> - `reject`: every claimed command typed below the root is refused with
>   `Run this command from the repository root.`
>
> Results, reports and artifacts are in your worktree before the command
> returns. A report the runner did not write is reported as missing. Missing is
> not zero failures.
>
> The exit code is the command's own. Five codes are Pandora's:
>
> | Exit | Meaning | Do this |
> |---|---|---|
> | 64 | The command cannot run as typed: a path argument below the repository root, a placement the job cannot take, or an invalid `PANDORA_WHERE`. Nothing ran. | Run it from the repository root, or drop the override. |
> | 70 | Infrastructure failure. Not a test verdict. | Retry. Or run it in the queue here with `PANDORA_WHERE=local <command>`, unless the job is sharded (that gives 64). If the message says this machine is under memory pressure, wait a few minutes, then retry. Do not bypass it. If it says the daemon does not answer, or does not understand `pandora.toml`, run `pandora doctor` and tell the owner. Do not bypass it. |
> | 75 | A validation is already active in this worktree, or the source changed during the run. After `--update`, nothing was written back. Or the daemon was still restarting, and nothing ran. | Wait for the other run, or retry after a restart. Do not edit the worktree while a validation runs. After an `--update` conflict, follow the printed `pandora resolve <id>` step. |
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
> `pandora.toml`, the message names the key and the fix. The fix updates
> Pandora on this machine, so leave it to the owner.
>
> ## Queueing
>
> When the worker is full, a command waits in the worker's queue before it
> starts. It prints `pandora: queued on the worker behind N runs (position P)`,
> with an estimate when one exists, and then `still queued` at most once a
> minute. This is normal. Let it wait, and do not run the command here instead.
> There is one queue for everybody, first come, first served. The wait has a
> limit that comes from how long the job usually takes, between 2 and 30
> minutes. At the limit the command exits 70 with `queue-timeout`, and nothing
> ran. Retry later. `pandora cancel <id>` takes a queued command out of the
> queue. `pandora ps` shows a queued command as `queued #P`.
>
> Each job starts with the `size` in `pandora.toml`. After a few runs, the
> worker sizes the job from what it actually used, larger or smaller. When the
> size changes, the command prints `pandora: size for <job>: <old> -> <new>`.
> No action is needed. After an `oom`, the job goes back to its declared size.
>
> `pandora ps` lists runs. `pandora logs <id>` replays one. `pandora cancel <id>`
> stops one. `pandora result <id>` shows its outcome and hint. `pandora stats`
> shows what ran, where, and what fell back. `pandora doctor` checks that this
> shell and worktree are set up to route. It changes nothing. `pandora --help`
> lists the rest.
>
> To choose where one command runs, set `PANDORA_WHERE=local` or
> `PANDORA_WHERE=remote` before it. The run keeps its queue, its receipt and its
> exit code. Exit 64 means the job cannot run there, and the message says why.
> Nothing falls back from an explicit `remote`. If the worker cannot take it,
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
