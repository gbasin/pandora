---
status: log
---
# Manifest-directed transfer, 2026-09-20

Keep the existing local freeze before upload. Direct transfer from a live worktree
removed the local copy but did not improve median request time in this trial. It
also transmitted synthetic, non-admitted bytes when a source directory changed to
an external symlink. Remote verification prevented execution, but could not undo
the transfer. No production routing or capture code changed.

## Experiment

The base was Pandora `c7d65ba`. The target was the existing Eichler compiled-build
checkout: 4,810 manifest entries, approximately 380 MB. Both paths used the same
OVH VM, repository source cache, verified helper bundle, durable worker, and warm
BuildKit cache. The Mac used `/usr/bin/rsync`, which identified itself as
`openrsync`, protocol 29, rsync 2.6.9 compatible.

The experimental path read Git membership and hashed admitted entries without
copying them. It transferred a NUL-delimited explicit file list with
`rsync -lpcd --from0 --files-from=-`. Recursion was disabled so a selected file
that became a directory could not recursively admit its new children. The
receiver used a fresh attempt directory and a private hardlink seed, without
in-place writes. Before publishing the cache or launching a worker, the client
verified the remote snapshot and repeated local membership and content checks.

This was an operator POC, not an installed launcher adapter. A failed prelaunch
check raised an error. It did not implement the durable rejection receipt needed
for normal launcher recovery. Accepted builds used the existing durable worker;
all nine build attempts completed successfully, verified cleanup, and were released.

## Measurements

Six preparation-only samples alternated the order of three copy and three manifest
runs. Both included remote verification; the manifest path additionally rechecked
local inputs. Copy preparation took 9.66, 7.59, and 8.84 seconds. Manifest preparation
took 6.99, 7.72, and 7.71 seconds. Those measurements excluded worker submission,
execution, and evidence retrieval.

Six subsequent complete requests also alternated paths. These are the relevant
comparison through verified evidence retrieval:

| Path | Request times | Median | Initial capture | Transfer |
| --- | --- | ---: | --- | --- |
| Existing local freeze | 13.18, 14.03, 11.75 s | 13.18 s | 3.21–3.82 s | 2.10–2.56 s |
| Manifest-directed live transfer | 12.10, 13.91, 13.65 s | 13.65 s | 0.67–0.72 s | 3.21–4.30 s |

Worker execution took 1.66–2.02 seconds across these six requests. All returned the
same expected image digest, `sha256:6c16d2b325f08e5aa59204dfe882dc8128511c43441aae197004753cbbc6d7f1`.
The new path also spent 1.35–1.53 seconds on prelaunch remote verification and local
rechecking. Request timing excludes launcher-level local source checking and output
publication. No coding-agent or service-backed journey trial was repeated here.

The bundled rsync reported a 1.85 MB file list and 45,466 file entries for explicit
paths, versus approximately 0.42 MB and 5,257 entries for recursive frozen-source
transfer. Both snapshots verified against the same manifest; the larger count is
not evidence of additional admitted files. Investigating that metadata overhead
or testing another rsync implementation remains separate work. These small samples
do not establish latency percentiles or performance under twelve-agent contention.

Three earlier pilot builds took 18.64, 13.17, and 15.75 seconds. They contained an
unnecessary repeated set construction in the POC's exclusions calculation. That
was removed before the six comparison requests. Pilot evidence is retained and
is not pooled into the comparison.

## Mutation and admission probes

The unchanged fixture preserved executable bits, an internal symlink, and a filename
containing spaces and a newline. Ignored and explicitly excluded secret fixtures
were absent remotely. The following changes were rejected before execution:

- Same-size content replacement with the original mtime restored.
- An edit after transfer but before the local recheck.
- A newly added file or changed ignore rules.
- A deleted file, changed symlink target, or changed executable bit.
- An unexpected file inserted into the remote snapshot.
- A selected file replaced by a directory containing an unlisted child. The child
  did not transfer.

A separate throttled transfer probe observed the receiver writing a partial 4 MB
file before changing another source file. Rsync exited successfully, but snapshot
verification rejected the result. No worker was started. A previously verified
snapshot also retained its original bytes after later local edits.

One admission probe contradicted the proposed safety boundary. After manifest
capture, a selected file's parent directory was replaced with a symlink to a
synthetic directory outside the repository. That directory contained the same
filename with the marker `SYNTHETIC-NOT-ADMITTED`. Rsync uploaded the marker. Remote
verification rejected execution, but admission had already failed at transport.
Only synthetic data was used, and the remote probe directory was removed.

The unchanged local-freeze path finishes capture checks before uploading its staged
copy. This experiment does not establish that existing capture is secure against
all concurrent filesystem changes or malicious writers. It establishes that this
replacement introduces a live-source transfer boundary that the proposed post-copy
verification cannot protect. A second stat or hash check does not close a race
between that check and rsync opening the path.

## Proof table

| Claim | Impact if false | Evidence | Status | Consequence |
| --- | --- | --- | --- | --- |
| Omitting the copy improves whole-request latency | Added complexity without agent benefit | Three complete requests per path, interleaved | Unresolved benefit; not demonstrated | Retain current path |
| Final verification rejects ordinary source drift | Tests could run unintended inputs | Boundary mutations and observed in-flight edit | POC-confirmed for tested cases | Retain both remote and local checks |
| Explicit path selection prevents unintended bytes leaving the Mac | Admission exclusion fails | Ancestor-symlink probe uploaded synthetic marker | Contradicted | Do not adopt live-path rsync as implemented |
| Accepted remote source is independent of later edits | Results become ambiguous | Frozen fixture retained original bytes | POC-confirmed | Continue immutable execution inputs |
| The new path fits launcher retry semantics | Agent gets stuck with a never-started job | Prelaunch exception only; no rejection receipt | Unresolved | Integration needs a durable rejection contract |

## Disposition

The experiment does not justify adopting this capture path. Preserve the current
backend and snapshot boundary. Any future optimization should preserve the
pre-upload admission check and demonstrate a material whole-request improvement.
Filesystem-assisted local copying could be investigated separately; it has not
been measured or approved as the replacement by this trial.

The local POC tree and remote scratch directories were removed after evidence
capture. No containers were running at cleanup; the VM had 45 GiB free. Existing
warm caches and acknowledged build-attempt evidence remain. The scripts, transfer
statistics, worker reports, and structured summary are retained under
`experiments/manifest-transfer/evidence/2026-09-20`.
