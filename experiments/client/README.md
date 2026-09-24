# Client daemon POC

A long-lived per-user daemon plus a thin `pnpm` shim, replacing the per-session
launcher in `experiments/routing/`. The backend is faked in-process: this POC
measures client ergonomics and side effects, not remote execution.

Results and the go/no-go are in `notes/client-daemon-poc-2026-09-21.md`.

## Files

| File | What it is |
| --- | --- |
| `bin/pnpm` | The shim. POSIX sh. The non-enrolled path forks nothing. |
| `bin/pnpm-python` | The same decisions in Python, kept only to be measured. |
| `daemon.py` | Socket server, run registry, lockfile, peer-credential check. |
| `backend.py` | The fake worker and its failure modes. |
| `client.py` | The routed half of the shim: handshake, stream, re-attach, cancel. |
| `protocol.py` | NDJSON frames and the pre-accept error list. |
| `enrolment.py` | Git common directory resolution and the marker format. |
| `claims.py` | Daemon-side classification; derives the shim's claim index. |
| `fallback.py` | Bounded local fallback slots and the passthrough log. |
| `passthrough.py` | Runs heavy unclaimed commands and records that they ran. |
| `cli.py` | `pandora enrol / ping / wait / stats`. |
| `harness.py` | Temporary sandbox: state dir, fake repo, fake real pnpm. |
| `bench.py` | Shim latency, ≥200 invocations per case. |
| `repo_config/` | Copied verbatim from `poc/ci-import` (`classify.py`, `config.py`, `ci_import.py`, the eichler example config). The ci.yml fixture, a copy of a private repository's workflow, was removed before this repository went public, so `test_claims.py` here and `test_ci_import.py`, `test_classify.py`, `test_config.py` and `test_parity.py` in `experiments/repo-config/` no longer run. |
| `launchd/` | A sample plist. Never loaded by this POC. |
| `install.md` | The install procedure. Never executed by this POC. |

## Run the tests

```sh
cd experiments/client
python3 -W ignore::ResourceWarning -m unittest discover -p 'test_*.py'
python3 bench.py --iterations 250
```

Every test builds its own temporary state directory, fake repository and fake
`pnpm`, and passes an explicit `PATH`. Nothing touches `$HOME`, `~/.codex`,
`~/.claude`, `~/.local/bin` or launchd.
