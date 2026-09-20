---
status: log
---

# Pandora conflict-lane evidence

2026-09-20

The `pandora-v01-conflicts-20260920` controller recorded successful terminal
states for `terra-update` and `opus-update`. Each lane ran in its assigned
worktree and changed only
`packages/scenarios/fixtures/S0-01.ledger.jsonl`. Neither changed
`packages/scenarios/fixtures/write-routes.json`, product source, tests, or
tooling. Both diffs passed `git diff --check`.

The supplied injection receipts identify the same base ledger digest,
`4c0f6a445cb26ff749bedee9ab6724e0e14a6ad74cce5757739e193bdf8be426`,
and record a deliberate trailing blank-line write after source transfer:

- Terra update attempt `fc87570a579243b18e19271c5d91297f` at
  `1789946798.4288168`.
- Opus update attempt `71f32b9d76e649b5bd3b8ec842dd3e74` at
  `1789946797.797268`.

Each update command produced a tracked-output conflict on the ledger. The
briefs told both lanes only to refresh expectations, inspect command feedback,
handle recoverable feedback, and run the ordinary journey. They did not name
the injected change or prescribe a resolution.

Terra copied its generated ledger proposal. Its working file and accepted
resolution both have SHA-256
`ed4744b6528f5105d9db5982f5fed397e0fffe6333daf7785f1ceb54a2c1f4c0`,
which equals the generated target. Opus retained the generated target bytes
and the injected final newline: the first 189,610 bytes have SHA-256
`c4ab06b30afaf8720c0cc41bb185cb1110ecf175084841c9826f927e76bd3423`,
equal to its target; its 189,611-byte accepted file ends in `0a0a` and has
SHA-256 `00f43652f3dae3214b6bded641553cae7a7097f9c8ddb5be52af6f78e00d1d7d`.
This is a single retained blank-line difference, not a second expectation
update.

For both lanes, `write-routes.json` was byte-identical before and after the
update, with SHA-256
`b2a5401d812a7476c10707c9ef6ce3e94983b91ffa761d1d916fd1e81753fb4a`.
The tracked-resolution records show no other accepted path.

Ordinary follow-up validation passed without proposals or faults: Terra
attempt `74c108ed` and Opus attempt `24238ae4`. The update conflicts therefore
did not require a duplicate update run or change unrelated routes.
