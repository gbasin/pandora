# Rsync transfer diagnostics, October 8, 2026

The controlled comparison does not justify a production flag change or a CAS
implementation. Keep checksum verification and defer CAS. Stop this performance
investigation until a concrete workload or priority warrants more work.

## Method

Run from the isolated `experiment/rsync-diagnostic-182` worktree:

```sh
python3 experiments/rsync-diagnostic/probe.py --rounds 3 --out artifacts/rsync-diagnostic.json
```

The sender was macOS with rsync 3.5.1. The Ubuntu worker used rsync 3.4.1 and
home-directory ext4 scratch. The fixture contained 375 MiB in 5,000 regular files,
an internal symlink, an executable file, and paths with spaces and a newline.
Both compressible and incompressible large files were present.

Seed one immutable baseline outside the timers. Warm a private SSH connection.
Run five variants once for priming, then in three rotated measured rounds:

- `normal`: real checksum rsync into a fresh stage with the retained baseline as
  `--link-dest`.
- `dry_basis`: the same invocation with `--dry-run`.
- `dry_no_basis`: checksum dry run without the baseline.
- `empty`: real rsync from an empty source into a fresh empty stage.
- `ssh_true`: `true` over the same warmed SSH connection.

The wall timer covers each invocation. Source capture, remote planning, stage
creation, finalization, and audits are outside that interval. The persistent SSH
master and sshd are outside the reported CPU subsets. File-system block counters
retain native units. Unavailable logical child I/O counters remain unknown.

Normal samples audit the full source by SHA-256, path, mode, and symlink target.
Dry-run, empty, and SSH samples audit empty stages; they do not prove source
materialization. A separate edit proof and checksum tracing probe are excluded
from the measured distributions. No CAS store was initialized.

## Results

Each row has three measured observations. CPU totals sum user and system time
within each observation before taking the median. Receiver CPU includes the
receiver wrapper and its waited children; sender CPU includes the calling process
and its waited children.

| Variant | Median wall, seconds | Median file-list generation, seconds | Median sender CPU, seconds | Median receiver CPU, seconds |
| --- | ---: | ---: | ---: | ---: |
| normal | 2.422 | 1.434 | 0.731 | 0.070 |
| dry_basis | 2.657 | 1.473 | 0.724 | 0.041 |
| dry_no_basis | 2.506 | 1.426 | 0.689 | 0.004 |
| empty | 0.905 | 0.001 | 0.016 | 0.002 |
| ssh_true | 0.285 | unknown | 0.006 | unknown |

Normal wall times were 2.731, 2.422, and 2.271 seconds. Subtracting each normal
sample's own file-list generation time leaves 0.986, 0.988, and 1.022 seconds.
The sender file-list phase tracks much of the variable wall time, but this phase
includes scanning, reads, hashing, and scheduling. It does not isolate checksum
CPU. Rsync documents that sender checksum work happens while building the file
list: [official rsync manual](https://github.com/RsyncProject/rsync/blob/master/rsync.1.md).

Same-round wall contrasts have mixed signs:

| Contrast | Round 1 | Round 2 | Round 3 |
| --- | ---: | ---: | ---: |
| normal minus dry_basis, seconds | -0.186 | +0.093 | -0.385 |
| dry_basis minus dry_no_basis, seconds | +0.410 | -0.886 | +0.412 |

These comparisons do not establish a materialization or basis-checksum saving.
They are interventions, not an additive decomposition of network, checksum, and
CPU costs. The paired median empty-minus-SSH difference was 0.603 seconds. It
includes receiver helper, rsync protocol, and waiting costs, not pure SSH overhead.
Receiver active CPU was small relative to elapsed time.

The tracing probe confirmed `xxh128` was already negotiated. Production also
already sorts the file list and uses warmed, multiplexed SSH. The normal samples
transferred zero regular files. There is no demonstrated safe flag improvement
here; dropping checksums would weaken the same-size, same-mtime guarantee.

## Integrity and setup evidence

A separate edit changed 16 bytes in the middle of a 3,925,888-byte file while
preserving its size and nanosecond mtime. Rsync transferred one file with 1,976
literal bytes and 3,923,912 matched bytes. The complete published snapshot and an
independent writable copy passed their audits.

All 22 records passed their intended audits: 15 measured samples, five excluded
priming samples, one edit proof, and one additional tracing probe. Final checks
confirmed that both Mac source variants and the retained worker baseline were
unchanged, the owned scratch root was removed, and the owned SSH master closed.
The experiment did not modify production transfer code, daemon configuration, or
worker caches.

Baseline capture took 0.588 seconds and tiny-edit capture took 0.698 seconds.
Total source preparation took 5.277 seconds, including those captures; do not
add the capture intervals again. Baseline upload took 78.025 seconds outside the
probe timers. This synthetic pass did not reproduce #75's capture stalls.

Three rounds do not balance all five rotation positions. Shared worker load and
warm file-system caches were uncontrolled; the recorded one-minute worker load
ranged from 1.269 to 1.542. The reported percentiles describe these observations
and do not establish stable tails.

## Saved evidence

- [Raw records](../experiments/rsync-diagnostic/evidence/mac-worker-2026-10-08.json)
- [Offline summary](../experiments/rsync-diagnostic/evidence/mac-worker-summary-2026-10-08.json)
- [Provenance and executed source hashes](../experiments/rsync-diagnostic/evidence/provenance-2026-10-08.json)
- [Commands and timer contract](../experiments/rsync-diagnostic/README.md)

The offline summary was independently reproduced. Thirteen focused experiment
tests passed locally. Repository validation is recorded in the pull request.
