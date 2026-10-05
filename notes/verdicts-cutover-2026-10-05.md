# Signed verdicts: cut-over to the live worker (2026-10-05)

A log. The design and the first proof are in `verdicts-e2e-2026-10-05.md`.

## What landed on main

#212 engine signing, #211 client publication with the selftest proving it
against a scratch bare origin, #210 Pandora's own `pandora.toml` plus the
composite action and the tests.yml skip, #214 the canary hog that writes its
own working set, #216 a test timing fix, #217 the live signer.

Review fixes before landing: two paths by which a client could pull the
engine's key (a `source_path` outside `src`, rsync `-L` through a symlink) are
refused; the payload binds `cwd` and `env_digest`; git can never prompt during
publication; a push race reports `present`; CI reads the allowed signers from
the default branch only, through fully qualified refs.

## Why #214 was needed

The canary's `file` hog read `/work/node_modules`. Pandora's own golden has
no Node, so the hog applied no pressure and the three OOM checks failed. With
the hog writing its own 1.5 GiB working set, the canary on the pandora-ci
engine passed: killed as `oom` in 21.6 s, `memory-thrash`, about 4,500
throttle events per second. That engine is now marked ready by a real
canary, so the e2e selftest signs and publishes on every pull request.

## Cut-over

* No runs were in flight. `pandora upgrade --from ~/Code/pandora` moved the
  live install from v0.3.11 to 3ac4c2f73bfe; the daemon drained nothing and
  came back admitting runs.
* `pandora enroll ~/Code/pandora`. The live worker read `ready` from its
  2026-09-28 canary.
* `pandora run -- python3 -m unittest discover -s pandora`: 1580 tests in
  85 s on the live worker, golden `672df269a1785b89` reused. The engine
  created its key on this run and signed. The daemon published
  `refs/pandora/verdicts/7cb0708a.../suite`. The signature verifies against
  the signer `pandora worker status` prints. That tree differs from main's
  commit tree because the main checkout holds an untracked note, which is the
  intended behavior: the verdict names what the run saw.
* #217 put the live signer on main and removed the pandora-ci test line.
* This note's own pull request is the first one whose tree was proven on the
  live worker before the push. Its ubuntu leg is expected to read
  `verified=true reason=match` and skip unittest.

## Still open

* Golden pinning, so `golden` in the payload names a result.
* The live worker's next `canary --mark` uses the new hog; it has not run yet.
* The eichler slice: composite action call, ci.yml step on `code` and `docs`,
  signers file.
