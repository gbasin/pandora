# Manifest-directed transfer evidence

This directory archives an operator POC. It is not a supported backend or launcher.
The [dated findings](../../notes/manifest-transfer-2026-09-20.md) explain why the
live-source transfer was not adopted.

`evidence/2026-09-20/summary.json` contains three preparation samples per path,
three complete comparison builds per path, three preliminary builds, and mutation
probe outcomes. Per-run files retain transfer statistics and worker receipts.

The scripts are tied to Pandora base `c7d65ba`, the existing evaluation checkout,
and the dedicated trial VM. They assume the repository root is their parent's
parent. To reproduce, review the paths and host first, then copy the archived
scripts to a fresh `.poc-stress-test/` directory in a worktree at that base.
`probe.py` supports preparation and fixture probes. `final_builds.py` performs six
sequential builds, updates the evaluation worktree's `compiled:test` mapping, and
releases completed requests. It requires the archived Docker profile and the
original evaluation checkout. `builds.py` is the preliminary driver; its original
exclusions inefficiency was corrected in the archived `warm_probe.py`.

Do not use the experimental warm client through the production launcher. Its
prelaunch failures lack a durable rejection receipt. The ancestor-swap probe uses
only synthetic markers and demonstrates that rejected execution does not imply
safe input admission. All scripts create disposable data inside the POC directory;
remove that directory after preserving the evidence you need.
