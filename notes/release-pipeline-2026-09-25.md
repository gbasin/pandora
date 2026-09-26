# Release pipeline: the decided shape

2026-09-25. Interview notes; what was decided and why, for the day the rest of
it gets built.

## What a release is

One artifact: the source tree (`bin/` + `pandora/` + `pyproject.toml`).
The worker never installs; the client ships it the engine bundle per
connection, so a release has no worker leg. Golden images and
`deploy/workers/*.toml` are provisioning concerns, not release ones.

## Decisions

* Audience: public eventually, so named versions and a changelog are real
  requirements, not vanity.
* Version lives in `pyproject.toml`, bumped by release-please (python
  strategy), printed by `pandora --version`. Conventional commits, enforced
  by a PR-title lint since squash merges make the title the commit.
* release-please keeps a standing release PR; merging it cuts the tag and
  the GitHub release. Cadence is "when the PR is merged", not scheduled.
* `pandora upgrade` bare fetches the latest release tarball over HTTPS
  (implemented in this change). Pandora owns activation (drain, `current`
  flip, launchd restart); nothing else may swap the code under a running
  daemon. Homebrew, if added, is install ergonomics only, never the updater.
* Release installs are named by tag (`versions/v0.3.0`); checkout installs
  keep the sha12 name. `META` records `release` vs `source` so doctor and
  `update_fix` can name the right way back.
* Contract surface (`exits.py`, `docs/agents-paragraphs.md`,
  `docs/pandora-toml.md`, `bin/pnpm`, the claim-cache format) gets a CI gate:
  a PR touching those files must carry a marker that the contract changed.
  Not yet implemented.
* Release gate before publish: build the tarball, install from it on the
  self-hosted e2e runner, run `selftest`. Not yet implemented.
* A `pandora-*.tar.gz` asset on the release is preferred over GitHub's
  automatic source archive, because the asset is what the gate ran against.

## Done in this change

`pyproject.toml` at 0.3.0, `pandora --version`, `--release [TAG]` and
bare-means-latest in `pandora upgrade`, META `release` field, doctor and
operations.md updated to match the new default.

## Still open

The release-please workflow itself, the PR-title lint, the contract gate,
the tarball gate on the e2e runner, the Homebrew tap, and a CHANGELOG.md.
