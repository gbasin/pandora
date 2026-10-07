---
status: log
---
# Normal source-transfer observations, 2026-10-07 UTC

Rsync accounts for 70.11% of cache-miss shipping time in this sample, despite
small payloads. The owner chose an isolated CAS POC for a controlled comparison.
Mac sender and network costs still need measurement before production adoption.
These records do not isolate checksum CPU, storage reads, protocol latency, or
remote contention. Issue #182 remains open.

## Evidence and method

The read-only export at 14:50:55 UTC selected retained regular metadata files
with nonempty `transfer` evidence. It read no source files or logs and started
no jobs. The 72 records are all from Eichler. Starts span 14:31:27 through
14:50:48 UTC, after the live upgrade to v0.3.16. Evidence is preserved in
`experiments/transfer-profile/evidence/normal-transfers-2026-10-07.json`.

There are 29 source-cache hits and 43 misses. Every miss recorded rsync exit 0,
publication and cleanup timings, a subsequent engine-submission timing, and
four candidate bases. Submission evidence shows these uploads progressed past
publication. Timers alone do not prove helper success; cleanup failures can be
harmless and leave a recorded duration. Six jobs were still running,
but their source transfers were complete: four hits and two misses. Later job
failures do not invalidate successful transfer observations. Quantiles use
linear interpolation at index `(n - 1) * percentile`.

| Seconds, median / p95 / max | Hits, n=29 | Misses, n=43 |
|---|---:|---:|
| Capture | 0.980 / 1.214 / 1.550 | 1.090 / 1.348 / 1.590 |
| Ship | 0.260 / 0.296 / 0.320 | 5.010 / 5.898 / 7.320 |
| Submit | 0.410 / 0.530 / 0.570 | 0.420 / 0.603 / 1.220 |
| Rsync | Not invoked | 3.552 / 4.251 / 5.624 |

Misses spent 154.146 seconds in rsync out of 219.850 shipping seconds.
The aggregate rsync fraction is 70.11%; the per-run median is 70.43%.
Other shipping work totals 65.704 seconds, approximately 1.53 seconds per miss.
Those are summed durations across potentially overlapping transfers, not
elapsed wall-clock time or a prediction of whole-job savings.

The median miss source size is 294,481,028 bytes (280.84 MiB), with 5,386 paths.
Median rsync sent-plus-received bytes are 258,337; median literal bytes are
8,435. Twenty-five of 43 misses moved under 300,000 protocol bytes and under
0.1% of their source size. Rsync protocol counters exclude SSH encryption and
network framing. Matched bytes describe reconstruction of transferred files,
not byte reuse across all unchanged source files.

| Miss counter | Median | p95 | Maximum |
|---|---:|---:|---:|
| Sent bytes | 258,116 | 656,482.5 | 1,000,387 |
| Received bytes | 341 | 10,191.6 | 14,603 |
| Sent plus received | 258,337 | 665,629.1 | 1,010,586 |
| Literal bytes | 8,435 | 399,038.2 | 741,994 |
| Matched bytes | 20,639 | 988,769.8 | 1,473,707 |
| Transferred file bytes | 43,540 | 1,627,153.9 | 1,731,063 |
| Regular files transferred | 2 | 48.9 | 52 |

## Examples and limits

Run `69ca84d41cd7` moved 250,638 protocol bytes and 700 literal bytes, but spent
4.322 seconds in rsync and 5.700 seconds shipping. The slowest ship,
`5319057c34ab`, moved 415,332 protocol bytes and spent 5.624 seconds in rsync
within 7.320 seconds shipping. The largest payload, `f4ef6c1d8242`, moved
1,010,586 protocol bytes and spent 4.108 seconds in rsync within 5.630 seconds
shipping. Small payloads can be slower than larger ones in this sample.

Run `a34c228770bf` has approximately 0.973 seconds of ship time outside the
recorded transfer steps. The parent ship timer includes additional bookkeeping,
and observed durations can include scheduling delay. Do not attribute that gap
to one helper without further evidence.

This is one repository, one client, and a 19-minute normal-use window. Retention
and nonempty instrumentation select the sample. Source sizes and counter
values are observations, not independent frozen byte audits. The export does
not contain input IDs, tool versions, CPU or I/O measurements, or concurrency
context. All misses have four bases, so this sample cannot compare base counts,
cold sources, or divergent histories. No capture reached 60 seconds, so it
does not reproduce issue #75.

Compression has little payload to reduce on these warm transfers. CAS may
reduce comparison work, but this evidence cannot predict its whole-request
benefit or identify rsync's internal bottleneck. Compare controlled identical
inputs with sender/receiver CPU and I/O, recorded rsync versions, and network
latency. Include cold and divergent cases in the approved POC. Mode-aware
immutable materialization and verified atomic insertion remain requirements for
any later CAS design.
