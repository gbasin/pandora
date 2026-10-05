# Signed run verdicts: design decisions and the first live proof (2026-10-05)

A log. The design is in DESIGN.md and docs/operations.md once PRs #210, #211
and #212 merge; this note records why, and what the first live run showed.

## The question

Agents run `pnpm check` through Pandora, push, and then wait for GitHub
Actions to run the same check on the same tree. Could a Pandora result stand
in for the CI job?

## Evidence from one consumer repository, week of 2026-09-28

* 293 pushes from agent sessions. 53 followed a passing Pandora `check` with
  no edit in between (exact-tree matches); 84 followed any passing Pandora
  suite. About half the pushes went out after edits that no check had seen.
* 225 completed pull_request CI runs, median 4.7 minutes each. The PR event
  runs only the static checks, which is what Pandora's `check` job is. The
  full suite runs on merge_group, over a merged tree Pandora never saw.
* The repository's own ci.yml already reuses verdicts inside Actions: a main
  push skips the suite when the merge queue verified the same SHA.

So the reachable win is the PR-level static check, 18 to 30 percent of pushes
today, and the ceiling rises if a passing `check` means a green PR on push.

## Prior art, in one line each

Bazel, Nx, Gradle, Pants and Earthly steer toward "CI writes, laptops read";
Nx's CREEP CVE is what happens otherwise. Bazel remote execution, Garnix, Nix
signed caches and Chromium's LUCI tryjob reuse let a trusted builder sign the
result, which is Pandora's shape. Gerrit copies a Verified vote to an
unchanged patch set. fkirc/skip-duplicate-actions skips a workflow when the
same tree already passed, inside Actions only. Phabricator's `arc unit`
results from laptops were advisory and never gated.

## Decisions

1. Goal: cut CI minutes and the agent's second wait. Actions stays the
   required check.
2. Target for the first slice: Pandora's own test suite on the ubuntu leg of
   tests.yml. A consumer repository follows once the path is proven.
3. Key: the git tree of the dirty worktree, computed on the worker from the
   transferred tracked set with `git write-tree`, never trusted from the Mac.
   Secret-filtered tracked files make such a tree unmatchable by construction.
4. The verdict binds tree, job, argv and golden fingerprint. The lockfile hash
   was dropped as redundant with the tree. Golden pinning is a follow-up; until
   then the fingerprint names a recipe, not a result.
5. Pass only, no TTL. Failures never transfer.
6. Trust: an Ed25519 key the engine generates on first need under its engine
   root, signed and verified with OpenSSH's `ssh-keygen -Y`. The public key is
   tracked under `.github/pandora/allowed_signers` and read from the base
   branch, never from a pull request's head.
7. Transport: the client daemon pushes `refs/pandora/verdicts/<tree>/<job>`,
   a parentless commit, after a passing run, when the repository opts in with
   `[verdicts] publish = true`.
8. Rollout: skip immediately. The early exit covers only the step the verdict
   names; conditional steps keep their own `if:`.
9. Test bed: the `pandora-ci` engine root on the shared host, driven from this
   Mac with a scratch config and a scratch state dir. The live engine was not
   touched.

The DESIGN.md paragraph that said Pandora results cannot satisfy GitHub PR
checks is reversed by PR #210 under these rules.

## First live run

Integration branch: #212 (engine signing) + #211 (client publication) + #210
(dogfood toml, action, tests.yml), plus `[verdicts] publish = true` and
`fallback = "refuse"` on Pandora's own `suite` job.

* Full local suite on the integration branch: 1534 tests, OK.
* Run 1 on pandora-ci: golden `golden-672df269a1785b89` built in 14.8 s from
  the Ubuntu 26.04 image plus git, python3, rsync, openssh-client, docker.io.
  Suite: 1534 tests in 81 s, passed, peak 682 MiB. `tree` was
  `eb544a55cfbc96c17798f8df9019ce4ab8347ab9`, equal to `git rev-parse
  HEAD^{tree}` in the worktree and to a `git add -A; git write-tree` over it.
  Verdict skipped as `worker_not_ready`: the pandora-ci engine had no ready
  state.
* Canary on pandora-ci: passed everything except the three OOM checks. The
  memory hog was not killed in 120 s. That engine's user units
  (`pandora-engine.service`, `pandora-pool.service`) are not enabled, so the
  check cannot pass there; unrelated to this change. The ready marker was
  written by hand for the test engine, labeled as forced, so the signing path
  could be exercised.
* Run 2: source cache hit, instance ready in 0.6 s, suite passed in 79 s. The
  worker signed; the daemon pushed
  `refs/pandora/verdicts/eb544a55cfbc96c17798f8df9019ce4ab8347ab9/suite` to
  origin as commit `e3ded26ea9c251431a4569870b7276a9bf3c024a`. `ssh-keygen -Y
  verify` accepts the payload against the engine's signer and rejects it after
  one appended byte.
* Observed noise: the run's stderr carried two `daemon socket /tmp/tmp...`
  passthrough lines and one "daemon closed the connection after submission"
  line. Those come from the suite's own tests running inside the instance and
  printing to the same stream, not from this run's daemon.

## The GitHub Actions proof

A pull request from a commit whose tree has a published verdict, against a
base branch that carries the pandora-ci signer, so that the merge commit's
tree equals the head tree and the signers file read from the base holds the
key. The expected outcome on the ubuntu leg is `verified=true reason=match`
and a skipped unittest step; the macOS leg runs as before. The result is
recorded in the pull request.

## Cut-over, for the owner

1. Merge #212, #211, #210 (and the integration commits).
2. `pandora upgrade`. The first signed run creates the live engine's key.
3. `pandora worker status` prints `verdict signer:`. Put that line into
   `.github/pandora/allowed_signers` on main and remove the pandora-ci line.
4. Remove this Mac's key from `/home/pandora-ci/.ssh/authorized_keys`, the
   forced `state.json` under `/home/pandora-ci/pandora/worker/`, and the
   registration this session's scratch enroll left in
   `~/Code/pandora/.git/pandora-repo`.

## Open follow-ups

* Golden pinning, so the fingerprint in the payload names a result.
* A consumer repository: composite action, ci.yml step, signers file.
* Why the pandora-ci engine cannot pass the OOM canary check.
* The merge-commit tree: a branch behind its base never matches. Agents who
  rebase before the final run get the skip.
