---
status: log
---

# Remote surface baseline: corrected browser pin, 2026-09-19

The retry of the [first baseline](remote-surface-2026-09-19-baseline.md) passed
all 45 tests in `smoke.spec.ts`. JUnit reported zero failures, zero errors, and
82.557 seconds of test execution with one Playwright worker.

The source revision and compressed archive were unchanged. The archive already
on the worker was copied into a new attempt directory, avoiding another upload.
The image now installs Playwright 1.62.1, matching the target lockfile. Dependencies
were installed into a fresh workspace again; this was not a warm-dependency run.

Cgroup peak memory was 3,747,618,816 bytes, approximately 3.49 GiB, under the
6 GiB limit. Docker no longer listed a running experiment container after the
run. The full returned evidence is at `/tmp/pandora-surface-retry-01/` on the
Mac; the worker attempt directory is `~/pandora-smoke/browser-pin-retry-01/`.

This establishes a passing bounded remote smoke baseline. It does not measure
warm edit-to-result latency, establish capacity for concurrent jobs, or validate
routing and cancellation semantics. Initial full-source transfer and the browser
version correction account for delays beyond the successful test execution.
