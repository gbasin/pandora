---
status: log
---
# Hybrid CAS transport comparison, 2026-10-07 UTC

The owner authorized a follow-up to the Mac-to-worker comparison. This pass
adds an opt-in hybrid: reuse known CAS identities, then rsync only missing
files against the same retained snapshot. It changes no production daemon,
gateway, cache, or configuration. Issue #182 remains open.

Hybrid preserves rsync's block reuse for the tiny large-file edit, removing
whole-blob amplification. It still sends the full manifest, so total observed
application traffic remains greater than rsync. Timing favors the hybrid for
that case in all three paired rounds, but this serial, partially balanced
study does not establish a stable production speedup.

## Setup and evidence

The source has 5,000 regular files totaling 375 MiB and one internal symlink.
The baseline matches the preceding transport study. Files have canonical
read-only modes `0444` and `0555`; paths include spaces and a newline. All
variants replace private inodes before changing bytes or modes.

Six warm cases cover unchanged content, a 128-byte small-file edit, a 16-byte
edit in the incompressible `large/075.bin`, a complete rewrite of that same
large file, an executable-to-nonexecutable change in `small/00000.txt`, and
a new 128-byte path with no retained same-path basis. Both large-file edits
preserve size and nanosecond mtime. The mode case preserves bytes and mtime.
Cold uploads are omitted: this follow-up targets the delta fallback rather
than repeating the preceding empty-store comparison.

Five methods run once per case in an excluded priming round and three measured
rounds: rsync, whole-blob trusted CAS, whole-blob rehashing CAS, and the two
matching hybrid policies. Every sample has an independent stage/store seeded
with the same logical baseline. Newly uploaded objects cannot carry into a
later sample. Three rotations only partially balance five method positions;
this is an exploratory serial comparison under shared worker load, with warm
filesystem caches and no dropped caches.

Hybrid uploads use `--ignore-times --no-whole-file` on missing paths only,
with the same read-only `--link-dest` baseline. No wrong basis inode is
prelinked into the destination. No `--delete`, `--inplace`, or append mode is
used for hybrid uploads. The receiver verifies the complete reconstructed
SHA256 and canonical mode before inserting each new blob. Rehashing policies
also read and verify cached blobs before publication. Trusted policies assume
previous verified read-only insertions remain intact.

Transaction wall time includes manifest control traffic, planning, missing
payload transfer, required verification, and publication. Complete output
verification, independent writable copying, execution verification, and
cleanup use a separate audit RPC. They are outside transfer wall time and
retain the preceding accounting boundaries. CPU covers only attributed
sender self/waited children and receiver helper/rsync subsets; SSH master and
server CPU are not attributed. Receiver logical child I/O remains unknown.
Physical block counters do not measure all reads.

All 90 measured samples and 30 excluded priming samples passed output and
independent execution audits. The Mac source variants and retained worker
baseline remained unchanged. Owned scratch removal and private SSH-master
closure succeeded. A preceding 1 MiB / 103-file smoke comparison passed its
30 measured and 30 priming samples with the same cleanup checks.

The sender used macOS 26.2 arm64, Python 3.14.7, and Homebrew rsync 3.5.1.
The worker used Python 3.14.4, rsync 3.4.1, and ext4 home-directory scratch.
Common manifest capture took 80.712 seconds across seven variant labels,
including the baseline captured twice. This is outside sample wall time and
is not a single production freeze measurement. It differs substantially from
the preceding study's 5.588-second capture across four variant labels. Worker one-minute load ranged
from 0.859 to 3.726 during measured samples. Repeated setup/audit/copy work
also changes filesystem state across serial samples. The causes of timing
variation were not isolated; do not attribute the difference between studies
to CAS, transport, or worker load alone. Issue #75 remains unreproduced.

The raw records, offline summary, and provenance are new files:

- [Raw records](../experiments/cas-network/evidence/hybrid-mac-worker-2026-10-07.json)
- [Offline summary](../experiments/cas-network/evidence/hybrid-mac-worker-summary-2026-10-07.json)
- [Measured code hashes and command](../experiments/cas-network/evidence/hybrid-provenance-2026-10-07.json)

The measured worktree was based on `f5518a0`; hashes identify the uncommitted
producer/receiver files actually run. Earlier evidence and dated logs remain
unchanged. The original three methods and four cases remain the CLI default;
hybrid methods and extra cases require explicit options. The offline summary
requires a complete verified declared matrix and reproduces historical
three-method summaries unchanged. Twenty-five targeted tests passed; Ruff
and shell checks passed.

## Observed transfer costs

Median transaction wall seconds, three samples per cell:

| Case | Rsync | Whole trusted | Whole rehash | Hybrid trusted | Hybrid rehash |
|---|---:|---:|---:|---:|---:|
| warm_unchanged | 7.293 | 1.057 | 1.282 | 1.108 | 1.221 |
| small_delta | 6.064 | 2.039 | 2.368 | 2.083 | 2.309 |
| tiny_large_edit | 9.285 | 2.746 | 2.984 | 2.188 | 2.355 |
| full_large_rewrite | 8.082 | 2.804 | 3.215 | 2.967 | 3.251 |
| mode_change | 6.510 | 2.071 | 2.191 | 1.951 | 2.210 |
| new_path | 9.491 | 2.219 | 2.452 | 2.274 | 2.429 |

The tiny-edit rsync samples were 4.498, 9.285, and 17.037 seconds. Keep that
spread visible when using the medians. Both hybrid policies beat rsync in
every paired warm sample here, but those observations are not a forecast
for a dedicated worker or a slower link.

Hybrid-minus-whole-CAS wall differences are paired by case and round, then
their median is taken. Negative values mean the hybrid took less time.
These are not differences between independently computed medians.

| Case | Trusted paired difference, seconds | Rehash paired difference, seconds |
|---|---:|---:|
| warm_unchanged | +0.051 | -0.065 |
| small_delta | +0.044 | -0.056 |
| tiny_large_edit | -0.775 | -0.607 |
| full_large_rewrite | +0.163 | +0.035 |
| mode_change | +0.062 | -0.224 |
| new_path | -0.033 | -0.077 |

For the tiny edit, hybrid improves on its matching whole-blob policy in all
three paired rounds. The full-rewrite control has mixed paired outcomes and
adds about 11.9 KB of basis/protocol traffic. The small, new-path, mode, and
unchanged timing differences are mixed; they do not establish a threshold
for deciding when to request a delta basis.

Tiny-edit byte counters (trusted policies shown):

| Observation | Rsync | Whole CAS | Hybrid CAS |
|---|---:|---:|---:|
| Literal payload bytes | 1,976 | 3,925,888 | 1,976 |
| Matched payload bytes | 3,923,912 | 0 | 3,923,912 |
| Observed application bytes | 187,698 | 4,498,503 | 593,507 |

Observed application bytes add each row's plan/finalization request and
response bytes to its rsync sent/received protocol counts, then take the
median. These disjoint application observations are not encrypted wire-byte
measurements. The hybrid reduces that total by about 7.58 times versus
whole-blob CAS, while remaining about 3.16 times rsync. Rehashing has the same
literal/matched behavior and a 593,552-byte application median. The roughly
570 KB manifest remains; delta reuse does not remove control overhead.

The full-rewrite and unseen-path controls send all 3,925,888 and 128 literal
bytes respectively, with no matched bytes. The mode-only hybrid reconstructs
128 matched bytes into an independent inode with the new mode; the baseline
stays executable and unchanged. All CAS unchanged samples skip rsync rather
than reporting an observed zero from an invoked payload process.

Reused-blob verification in hybrid rehashing has case medians of 0.199 to
0.246 seconds. That inner phase directly measures the rehash cost; differences
between total trusted and rehashing walls also contain timing variation and
must not be assigned entirely to integrity reads.

For the tiny edit, attributed CPU seconds are summed within each sample
before taking medians. Receiver totals include plan/finalization helper self
CPU and the receiver wrapper/waited rsync children. They exclude SSH and
interpreter startup/import work.

| CPU seconds | Rsync | Whole trusted | Whole rehash | Hybrid trusted | Hybrid rehash |
|---|---:|---:|---:|---:|---:|
| Sender self + waited children | 1.1826 | 0.0666 | 0.0686 | 0.0672 | 0.0613 |
| Receiver measured subset | 0.0649 | 0.1495 | 0.3616 | 0.1442 | 0.3451 |

Plan/finalization helper logical reads (`rchar`) are about 394.986 MB with
rehashing, versus 1.770 MB for unchanged trusted reuse and 5.696 MB for the
tiny-edit trusted cases. These exclude rsync-child logical reads, which are
unknown. Recorded helper physical `read_bytes` values are zero; this does
not mean there were no reads. Inner phase sums stay within transaction wall;
the largest residual bookkeeping interval is 90.446 ms and is included.

## Integrity and production boundary

The new reused-cache corruption probe replaces a sample blob with private
corrupted bytes while retaining its expected mode. It does not modify a
shared template or baseline inode. Hybrid rehashing refuses publication;
hybrid trusted mode publishes, and the independent output audit rejects the
result. The audit is experimental instrumentation outside transfer timing;
it is not a claim that trusted production CAS would detect the fault before
execution. Both hybrid policies also refuse corrupt newly reconstructed
payloads before insertion/publication.

The current production whole-input cache already bypasses shipping on a hit.
The unchanged warm case isolates a transfer mechanism; it does not establish
additional savings on that existing hit path. This experiment retains one
same-path baseline. It does not test divergent-history basis selection,
concurrent GC, retry/crash recovery, malicious peers, or disk-cold operation.
No production size threshold or default integrity policy is selected here.

Production review identified these requirements before integration:

- Use a repository-scoped object namespace unless cross-repository reuse is
  explicitly chosen. Key objects by SHA256 and canonical executable mode.
- Pin planned objects and register active stages under the existing admission
  lock before returning an inventory response. Add a stage lease covering
  planning, upload, verification, and publication. The existing 1,800-second
  orphan rule covers today's rsync path, not a new multiphase transaction.
- Publish and collect under the same lock. Retain current protection for
  latest, live attempts, the one-hour grace, and four eligible snapshots.
  Sweep only unreferenced objects; stage pins/leases must prevent a collector
  from invalidating an in-flight plan. Do not hold the lock across SSH,
  payload transfers, or full-file hash reads.
- Restore writable `0644`/`0755` modes on independent execution copies.
  Current Incus injection uses `rsync -a`, which preserves source mode, so
  simply substituting `0444`/`0555` CAS snapshots would change `/work` behavior.
- Protect object insertion from raw client rsync writes. Review gateway feed
  allowlisting and old-client compatibility before owner-controlled rollout.
- Choose the reused-object integrity guarantee explicitly. Always verify new
  objects; strict rehashing is a proposed initial default until trusted
  provenance and the protected mutation boundary are established.

These are design findings, not implemented production behavior. The next
piece should define the source-cache transaction and GC protocol with its
fault tests before connecting this experiment to live source transfer.
