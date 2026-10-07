---
status: log
---
# Source-transfer profile, 2026-10-07 UTC

Do not implement CAS from this measurement. Shipping dominates recent source
preparation, but the saved records do not isolate checksum processing. The
worker-local experiment finds fast warm rsync processing and leaves Mac sender,
SSH, and network costs unmeasured. The next useful measurement is phase timing
on real transfers. Issues #182 and #75 remain open.

## Retained run evidence

The read-only audit at 00:48 UTC read 4,610 metadata records and 4,605 results
without read failures. Evidence and the repeatable audit script are in
`experiments/transfer-profile/`. The recent cutoff is October 6, 22:04 UTC.
Quantiles use linear interpolation. Retention censors the historical slow runs.
Aggregate durations can include failed or retried work; they are not throughput.

| Repository and window | Capture samples | Capture p50 / p95 / max, seconds | Ship p50 / p95 / max, seconds |
|---|---:|---:|---:|
| Eichler, all retained | 4,277 | 0.92 / 2.02 / 17.00 | 4.18 / 5.47 / 30.86 |
| Eichler, after cutoff | 47 | 1.01 / 2.39 / 4.37 | 4.54 / 5.42 / 30.86 |
| Pandora, all retained | 20 | 0.36 / 0.51 / 0.61 | 2.79 / 3.89 / 3.93 |
| Pandora, after cutoff | 2 | 0.36 / 0.36 / 0.36 | 2.98 / 3.18 / 3.20 |

Eichler has 4,276 retained ship timings; one capture has no ship timing. Recent
paired Eichler runs spent 163.55 seconds shipping and 55.91 seconds capturing.
Shipping exceeded capture in 27 of those 47 runs. Saved records contain no
source-reuse indicator or rsync byte statistics, so cache hits and misses cannot
be separated from these fields.

There are 57 instrumented captures, 49 after the cutoff. None reached 60 seconds.
The slowest recent capture, `e22b10aefd83`, took 4.37 seconds: both passes' name
listing totaled 2.67 seconds and nested-worktree scanning 0.93 seconds. The
largest recent shipping outlier, `10e596e2699f`, took 30.86 seconds while capture
took 1.13 seconds. Neither identifies the historical capture-stall cause.

## Worker-local experiment

Pandora admitted the `transfer-profile` job as local run `9773dfe98ace`, worker
run `rfa57c3daea9f443`. It passed in 55.7 seconds with peak memory 1,615 MiB. The
worker was idle when the run was submitted. The job used a disposable container
and temporary source trees; it did not benchmark against production source caches.

The receiver environment was Linux 7.0.0-31-generic x86_64, Python 3.14.4,
and GNU rsync 3.4.1, protocol 32. The admitted CPU pin was four cores with eight
threads. Both sender and receiver rsync processes ran inside that container.

The synthetic fixture contained 5,000 regular files totaling 375 MiB and one
internal symlink. One hundred large files held nearly all bytes: half used
compressible text, half deterministic pseudorandom data. The other 4,900 files
were 128 bytes each. An executable and filenames with spaces and a newline
were included. The one-file edit changed 3,925,888 bytes; the divergent base
differed in 90 files totaling 353,329,920 bytes. Three poor bases were equivalent
hardlinked copies of that divergent tree. The fourth base matched the original.

One priming round was excluded. Ten measured rounds rotated case order. Flags
matched production's checksum, explicit NUL-delimited paths, no timestamp
preservation, and link-dest behavior. `--stats` recorded transfer counters and
`--no-whole-file` enabled the delta algorithm on local transport. No system
caches were dropped. Verification and inode accounting were outside measured
rsync wall time. Full per-round evidence is committed beside the harness.

| Case | Median / p95 seconds | Median literal bytes | Median transport bytes sent | Regular files shared with bases |
|---|---:|---:|---:|---:|
| Cold, no base | 0.258 / 0.275 | 393,216,000 | 393,691,980 | 0 |
| Warm unchanged | 0.162 / 0.168 | 0 | 165,719 | 5,000 |
| Warm 1% edit | 0.167 / 0.172 | 3,925,888 | 4,092,603 | 4,999 |
| Divergent base | 1.151 / 1.283 | 353,329,920 | 353,585,275 | 4,910 |
| Three poor bases, good fourth, 1% edit | 0.238 / 0.252 | 3,925,888 | 4,092,603 | 4,999 |
| Cold with compression | 0.314 / 0.381 | 393,216,000 | 196,961,148 | 0 |

All 66 receivers, including priming, matched expected paths, SHA256, modes,
and symlinks. Additional probes verified same-size edits with restored mtimes,
unchanged bytes with new mtimes, executable-mode changes, and old-base
immutability. All temporary trees were removed before the artifact returned.

## Interpretation and limits

A good fourth base recovered the same 99% byte reuse as a good first base.
Its median cost was 0.071 seconds higher in this fixture. Warm worker-local
processing was below 0.24 seconds, substantially below the production ship
median. This does not isolate checksum CPU or prove where the production
remainder goes: sender hardware, filesystem, transport, SSH helpers, and worker
contention differ.

Compression halved transport bytes on the deliberately half-compressible
fixture but increased local elapsed time. This is not evidence for enabling
compression on WAN transfers. The fixture and local pipe do not establish
Eichler compressibility or network throughput.

The September 20 experiment used a different capture path. Current
`snapshot.freeze` copies nothing and `transfer.send` receives the live worktree.
Do not use that older note's copied-freeze timing or admission guarantee as the
current baseline.

A CAS proposal also needs mode-aware immutable materialization: file SHA256
identifies bytes, while executable state is separate. Changing the mode of a
hash-only hardlink changes every path sharing the inode. Preserve read-only
cached sources and independent writable execution trees. New CAS feeds would
need gateway compatibility, verified atomic insertion, and GC coordination with
live and staged references. Those are design requirements, not implemented here.

Before a CAS POC, record real transfer substeps: cache probe, base discovery,
staging, rsync, publication, and cleanup. Separate cache hits, base counts,
source bytes, and rsync byte counts. Then measure the Mac sender or an isolated
network path if rsync dominates. Adoption still needs whole-request improvement
and correctness checks; this experiment provides neither a CAS implementation
nor an end-to-end comparison.
