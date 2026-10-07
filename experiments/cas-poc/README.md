# Controlled CAS comparison

This experiment compares the current rsync checksum/link-dest mechanism with
manifest-based CAS materialization. It uses synthetic source trees and stores
inside a disposable worker job. It does not change production transfer code,
gateway feeds, cache GC, or the live daemon.

Run the declared job from this worktree root:

```sh
pandora run -- python3 experiments/cas-poc/benchmark.py
```

The remote job refuses local fallback. It returns `artifacts/cas-poc.json`.
Temporary source trees, stores, snapshots, and execution copies are removed at
job exit. The defaults are 375 MiB, 5,000 regular files, five measured rounds,
and one excluded priming round.

Run the small correctness and comparison tests directly:

```sh
python3 -m unittest discover -s experiments/cas-poc -p 'test_*.py'
```

## Policies and correctness

Each missing file is sent by rsync without a pre-transfer checksum scan, then
copied and SHA256-verified before exclusive atomic blob insertion. The blob
key contains SHA256 and canonical read-only mode. Cached snapshot paths share
those immutable inodes. No code chmods or truncates a shared blob inode.
Concurrent insertion validates the winning blob before accepting it.

Two CAS policies run as separate controlled samples:

- `trusted` skips hashes on reused blobs after verified insertion. It assumes
  protected storage whose only writers use that insertion path. Read-only
  permissions alone do not establish this guarantee. The fault test shows that
  trusted reuse can publish corrupted data if this assumption fails; the full
  output audit detects the mismatch afterward.
- `rehash` hashes every unique reused blob inside the transfer timer and rejects
  corruption before publishing the snapshot. Its phase cost remains included
  in total CAS transfer time.

The comparison uses canonical `0444` nonexecutable files and `0555` executable
files for both rsync and CAS. It models executable identity in Pandora's
manifest, not preservation of arbitrary original permission bits. Both paths
create independent execution copies with `0644`/`0755` modes. Output audits
check SHA256, exact canonical/execution modes, paths, symlinks, and independent
execution inodes. Every original source and rsync base is audited after all
rounds. Corruption, interrupted insertion, insertion collisions, changed source
bytes, invalid paths, and symlink escape have separate fault tests.

Rsync's checksum comparison and matching preserved attributes are documented
in the [official rsync manual](https://rsync.samba.org/ftp/rsync/rsync.1).
Permission differences can prevent link-dest sharing, so source and base modes
are kept identical across each controlled case.

## Measurement scope

The seven cases are an empty store, an unchanged warm tree, a one-small-file
edit, a one-large-file edit, divergent bases, retained history, and an
executable-mode change. Every case runs rsync, trusted CAS, and rehashing CAS.
Case and method order rotate. Fresh stores prevent a previous sample's newly
inserted blobs from lending a warm hit to the next policy or round.

`retained_history` deliberately gives CAS both the original and divergent
content while rsync has only four divergent bases. Seeded blob counts and bytes
are recorded. This demonstrates retained-content reach, not equal storage/GC
budgets. Other cases seed equivalent source content into both mechanisms.

Transfer timers exclude fixture construction, shared manifest capture, base or
store seeding, initial input validation and setup, and external full output
audits. CAS records planning, upload, verified insertion, cached verification,
and publication phases. Each sample also records identical independent-copy
cost and external audit cost. `transfer_and_copy_seconds` includes transfer
and copying; `verified_transfer_and_copy_seconds` additionally includes both
full audits. These are experiment intervals, not normal Pandora request time.

Both rsync processes run in one worker container with local transport and
`--no-whole-file`. Empty content stores still use warm filesystem caches.
Neither Mac sender reads, WAN/SSH latency, production helper calls, container
injection, nor concurrency is measured. CAS manifest/control-message transport
is absent. Serialized manifest sizes are reported so that cost is not silently
claimed to be zero. Method rotation cannot remove all cache warming from seed
verification or prior cases.

This POC is not a production protocol. It does not provide live-worktree race
protection, GC/reference coordination, fsync crash durability, authenticated
remote planning, or a rollout path. Adoption requires measurements of the real
sender/transport and these guarantees. A worker-local speedup alone is not
sufficient evidence for a production change.

Run the targeted 16-byte large-file edit probe through its separate scratch job:

```sh
pandora run -- python3 experiments/cas-poc/delta_probe.py
```

It returns `artifacts/cas-delta-probe.json`. This probe preserves file size and
mtime, and measures the whole-blob upload penalty against rsync block reuse.

Reproduce either evidence summary offline:

```sh
python3 experiments/cas-poc/summary.py \
  --records experiments/cas-poc/evidence/worker-local-2026-10-07.json \
  --out /tmp/cas-summary.json
```

Use `delta-probe-2026-10-07.json` for the tiny-edit summary. The summarizer
rejects missing policies/cases, missing or duplicate rounds, priming records,
and failed receiver audits. Missing counters remain unknown.
