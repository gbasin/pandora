---
status: log
---
# Transport latency, 2026-09-20

A traced repeat of the compiled-build trial took 21.6 seconds. Local source capture
took 4.0 seconds, source transfer 3.4 seconds, worker-script uploads 5.2 seconds,
remote setup and launch about 3.5 seconds, waiting for completion about 3.5 seconds,
and verified result retrieval 2.1 seconds. The worker executed for 2.1 seconds,
within the completion-wait interval. The earlier identical-build sample was 24.0
seconds. Neither measurement includes local publication or release acknowledgement.

The revised transport caches worker helpers by SHA-256 of their encoded contents.
Each use verifies the cached manifest and every file before copying helpers into
an attempt. A missing or corrupt cache requests the payload, verifies its checksum,
and publishes a complete cache entry under a per-bundle lock. Runtime Dockerfile
changes also change the bundle identity. Completed attempts never depend on mutable
shared helper files. Cached versions remain on disk; no helper-cache GC is included.

A private per-client SSH control socket reuses authentication across SSH, scp,
and rsync calls. The connection may persist for up to 60 idle seconds. Normal client
exit removes its private socket directory; a killed client may leave a directory.
The durable systemd worker remains independent of that connection. Setup combines
helper selection, attempt creation, and source-seed preparation. Source-cache
publication and worker launch share a later SSH call, which is never automatically
retried after ambiguous acknowledgement.

The state trace is unchanged at the execution boundary:

- A cache miss creates no attempt. The client may send the complete helper payload.
- A verified cache creates one new attempt and copies its helpers. A duplicate
  attempt identity fails instead of replacing files or relaunching a worker.
- Source and submission metadata transfer before cache publication and launch.
- Connection loss after launch leads to same-attempt status queries. It does not
  authorize another launch. Result checksums and terminal cleanup still gate success.

VM measurements used the existing compiled application image and unchanged source:

| Case | Request time | Full shell call | Worker execution |
| --- | ---: | ---: | ---: |
| First helper-cache upload | 16.8 s | Not recorded | 1.8 s |
| Warm build 1 | 13.1 s | 14.7 s | 1.9 s |
| Warm build 2 | 13.4 s | 15.2 s | 1.9 s |
| Warm build 3 | 15.8 s | 17.7 s | 1.8 s |
| Image run with output return | 5.2 s | 6.1 s | 0.4 s |

All requests passed. The three warm builds reported helper-cache hits. Source
capture still took 3.5–3.8 seconds, and source transfer took 2.1–4.4 seconds. The
median warm request fell from the earlier single 24.0-second observation to 13.4
seconds. These sequential samples are not a randomized benchmark or a concurrency
test; the earlier same-day traced baseline was 21.6 seconds.

A separate fault probe confirmed explicit cancellation returned 130 with cleanup
verified. Killing the local transport during a delayed image run returned 137;
retrying the original command recovered the same attempt and returned zero. The
compiled output returned at the normal worktree path. Local validation passed
18 warm-worker tests, 13 routing tests, and 7 output-publication tests, including
bundle corruption repair, checksum rejection, duplicate-attempt refusal, and
isolation of attempt helper copies.

The remaining cost is largely source handling, polling, and evidence round trips.
Further source optimization must preserve dirty/untracked input capture, mutation
checks, and exclusion rules. This change does not use timestamp-only freshness
or remove checksum verification.
