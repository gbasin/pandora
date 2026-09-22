# What agents are told

Two drafts for the target repository's `AGENTS.md`. The owner chose **A**: agents
keep typing the commands they type today and learn only what changes when a
command runs somewhere else. **B** is the fan-out vocabulary; it lives in
`pandora --help` and in the fanout skill, and is promoted to `AGENTS.md` only if
the pilot shows agents reaching for it.

## A — nothing changes for you (final text)

> Validation runs where it runs best: broad suites on the Pandora worker, focused
> ones on this machine. Use the same commands as before. Results, reports and
> artifacts are in your worktree before the command returns, and the exit code
> is the command's own. Run validation from the repository root; a command typed
> in a subdirectory with a path in its arguments runs locally and says so.
>
> Exit codes that are not the command's: 70 is an infrastructure failure, never a
> test verdict (retry, or run it here with `PANDORA_OFF=1 <command>`); 75 means a
> validation is already active in this worktree or your source changed during the
> run; 124 means the run is still going (`pandora wait <id>` re-attaches); 130
> means you cancelled it. `pandora ps` lists runs, `pandora logs <id>` replays
> one, `pandora cancel <id>` stops one. Do not edit the worktree while its
> validation runs. When Pandora has advice it is the last line, `pandora: hint:
> ...`; it is derived from evidence, so act on it.

## B — the fan-out vocabulary (in `pandora --help`, not in AGENTS.md)

> `pandora run --detach -- <command>` submits and returns an id at once.
> `pandora wait <id...>` blocks on a set and exits non-zero if any did not pass,
> printing one outcome line per id. `PANDORA_SHARDS=8 <command>` overrides the
> shard count for one run. `pandora result <id> --json` gives the outcome, the
> per-shard results and the input digest, so two runs of the same tree can be
> told apart from a change. `pandora fetch <id> <path>` pulls any file from a
> failed run's tree.

Why B stays out of `AGENTS.md` for now: every extra verb is something an agent
can get wrong, and `--detach` is exactly the shape that produced orphaned runs
and duplicate submissions under the previous queue. The fanout skill can use it
deliberately; an agent reading the repository's instructions should not need it.
