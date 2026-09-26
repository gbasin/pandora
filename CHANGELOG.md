# Changelog

## v0.2 (2026-09)

The machine-wide `pnpm` shim, the per-user daemon, per-repository
`pandora.toml` and enrollment, snapshot installs under
`~/.local/share/pandora/versions` with `current` flipped after a drain,
a local lane with one memory budget, remote runs in fresh Incus instances
cloned from a golden image, sharded fan-out, and two-phase write-back for
`--update`. Exit codes 64, 70, 75, 124 and 130 are the caller contract.
The v0.1.1 Docker profile is not carried over.

## v0.1.1 (2026-09)

Per-session launcher routing (`experiments/routing/launch.py`) with a
Docker execution profile. Twelve-agent baseline in
`notes/v0.1.1-validation-2026-09-21.md`.

## v0.1 (2026-09)

First contract: a scheduler that claims a repository's heavy commands and
runs them on a shared worker. `notes/v0.1-contract.md`.
