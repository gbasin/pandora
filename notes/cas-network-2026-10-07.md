---
status: log
---
# Mac-to-worker CAS comparison, 2026-10-07 UTC

The owner authorized a real transport comparison after the worker-local CAS
POC. This experiment runs the sender on the Mac and sends control messages and
payloads to private disk-backed scratch space on the existing worker. It
changes no production daemon, gateway, cache, or configuration.

CAS improves the small-edit transfer on this route under both integrity
policies. Rehashing still beats rsync here, unlike the earlier worker-local
comparison. The whole-blob penalty remains real: the tiny large-file edit
needs much more application traffic for a small timing gain. Keep CAS
experimental while designing a large-file delta fallback and the production
insertion/GC boundary. Issue #182 remains open.

## Setup and evidence

The synthetic source has 5,000 regular files totaling 375 MiB and one internal
symlink. Files have canonical read-only modes `0444` and `0555`. Paths include
spaces and a newline. Source variants replace private inodes before changing
bytes or metadata.

The four cases are empty retained content, unchanged warm content, one
small-file edit, and a 16-byte edit in `large/075.bin`. The last case keeps file
size and mtime unchanged. That file is incompressible, unlike `large/000.bin`
in the earlier worker-local delta probe. Differences between those studies
cannot be attributed solely to transport.

Rsync, trusted CAS, and rehashing CAS each run once per case in an excluded
priming round and three measured rounds. Each case rotates method order across
the measured rounds. Warm samples start with the same logical baseline
content; cold samples start without retained content. New sample stores
prevent cross-sample reuse of newly uploaded blobs. The shared fixture setup
keeps both a baseline snapshot and a CAS template, so this is equal retained
content per mechanism, not an equal whole-experiment disk-allocation claim.

One dedicated SSH master is warmed before samples. Its setup is reported
separately. Application control messages carry the actual manifest, missing
paths, and finalization response. The transaction timer includes those
messages, rsync payload transport, required verification, and publication.
Rsync uses checksum matching against its baseline. CAS verifies every new
blob before insertion; rehashing also verifies reused blobs inside the timer.

Full output verification, independent writable execution copying, execution
verification, and cleanup run in a separate audit RPC. The copy uses distinct
inodes. The audit transaction includes these operations and SSH; do not add
its nested intervals again. The Mac source variants and worker baseline are
verified again after all samples. Cleanup checks the scratch owner marker and
closes only the benchmark's SSH master.

All 36 measured samples and 12 excluded priming samples passed their output
and independent execution audits. The Mac source variants and worker baseline
remained unchanged. Scratch cleanup and SSH-master closure both succeeded.
Raw records, their offline summary, and measured code hashes are preserved in
`experiments/cas-network/evidence/`. The run used an uncommitted worktree based
on `fccfe5f`; hashes identify the actual measured files independently of that
base commit. A preceding 1 MiB / 103-file transport smoke test passed all
24 samples, including priming, and completed the same cleanup checks.

The sender was macOS 26.2 on arm64 with Python 3.14.7 and Homebrew rsync 3.5.1,
protocol 33. The worker used Python 3.14.4, GNU rsync 3.4.1, protocol 32, and
ext4 storage. Common manifest capture took 5.588 seconds across the source
variants; it is outside transfer timing. The one-minute worker load average
ranged from 1.67 to 11.20 across measured samples.

## Results

Median / p95 transaction seconds, including actual control messages and
publication, excluding the external audit RPC:

| Case | Rsync | Trusted CAS | Rehashing CAS |
|---|---:|---:|---:|
| Empty retained content | 57.152 / 65.951 | 61.656 / 73.740 | 60.933 / 70.380 |
| Warm unchanged | 3.143 / 3.349 | 0.983 / 1.020 | 1.222 / 1.301 |
| One small-file edit | 3.104 / 4.014 | 1.866 / 1.920 | 2.143 / 2.420 |
| 16-byte large-file edit | 2.970 / 3.187 | 2.702 / 2.721 | 2.729 / 2.785 |

Trusted CAS reduces the small-edit median by 1.238 seconds, about 40%.
Rehashing reduces it by 0.961 seconds, about 31%. Both CAS policies beat rsync
in all three paired small-edit rounds. Cold uploads dominate elapsed time;
both CAS policies verify new blobs identically, so their cold timing
difference does not isolate an integrity-policy effect.

Every CAS plan sends a 569,875-byte serialized manifest. For the small edit,
rsync sends 164,552 protocol bytes and CAS sends 287 payload-protocol bytes.
The CAS transaction additionally sends approximately 570 KB of control
requests. Adding the disjoint application control and rsync sent/received
counters per sample gives median observed application traffic of 166,093
bytes for rsync and 571,804 bytes for trusted CAS. The faster CAS path still
moves more application bytes in this case.

The tiny edit changes 16 bytes in a 3,925,888-byte file. Rsync sends 1,976
literal bytes and reuses 3,923,912 matched bytes. CAS uploads all 3,925,888
literal bytes. Median sent protocol bytes are 174,343 for rsync and 3,926,986
for CAS. With control requests and responses added per sample, median observed
application traffic is 187,796 versus 4,498,502 bytes, about 24 times as much.
Trusted CAS's median timing advantage is only 0.268 seconds. This run does
not establish the same advantage on slower or contended links.

## CPU and I/O observations

The following sender values are medians of per-sample coordinator plus waited
child user/system CPU sums. They do not add separately computed medians.
They exclude the persistent SSH master.

| Case | Rsync sender CPU seconds | Trusted CAS | Rehashing CAS |
|---|---:|---:|---:|
| Warm unchanged | 0.658 | 0.020 | 0.021 |
| One small-file edit | 0.680 | 0.039 | 0.037 |
| 16-byte large-file edit | 0.705 | 0.042 | 0.043 |

For the small edit, receiver plan/finalization helper CPU has medians of
0.001, 0.145, and 0.375 seconds. The separate waited rsync receiver CPU medians
are 0.076, 0.004, and 0.004 seconds. Those subsets exclude sshd and helper
startup, so they are not complete receiver CPU totals.

The small-edit finalization helper records a median 1,200,474 logical read
bytes (`rchar`) for trusted CAS and 394,416,350 for rehashing CAS. The latter
includes reading almost the entire retained source. The measured reused-blob
verification phase has a 0.251-second median. Rehashing's small-edit transfer
median is 0.277 seconds above trusted reuse while remaining faster than rsync
on this Mac-to-worker route. The corresponding filesystem block
counters must be read separately; logical cached reads do not imply physical
disk reads.

The earlier worker-local fault probes showed why the reuse policy matters:
trusted reuse can publish a corrupted retained blob if store protection fails,
whereas rehashing rejects it before publication. Read-only permissions alone
do not protect against every process able to change the store. This transport
comparison did not inject corruption. Its results measure the cost of both
policies without selecting a production guarantee.

## Measurement limits

The worker remains shared and filesystem caches are not dropped. Load is
recorded for each sample. Three measured observations per case and method are
insufficient for stable tail estimates. Quantiles use linear interpolation.

Sender CPU separates the coordinator and waited child processes. Receiver
CPU separates control-helper input/action work and waited rsync processes.
Helper startup/imports, response serialization, SSH master, and sshd CPU are
outside the stated CPU attribution. Transaction wall includes those costs.
Helper `/proc/self/io` counters describe that process only; child logical I/O
bytes are unknown. Filesystem block counters remain in their native units.
Zero disk-input blocks can reflect cached reads and do not mean no reads.

Control-message sizes and rsync protocol counters are application observations,
not encrypted network byte totals. Source creation, manifest capture, verified
template insertion, and SSH setup are outside transaction timing. The common
manifest-capture duration is reported separately.

An unchanged source in this experiment deliberately exercises both transfer
algorithms. A production whole-input cache hit already skips rsync, so this
case does not represent a new production savings opportunity by itself.
The edited cases are more relevant to production cache misses.

This is a controlled scratch protocol, not Pandora's whole submission path.
Production gateway feeds, hostile live-worktree races, concurrent publishers,
GC, crash durability, and rollout remain outside this experiment.
