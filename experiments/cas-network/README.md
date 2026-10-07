# Mac-to-worker CAS comparison

This experiment compares rsync and both CAS integrity policies across a real
SSH connection. It uses controlled synthetic files in private scratch
directories. It does not install a service or change Pandora's daemon,
gateway, source cache, or configuration.

Run this command from an authorized Mac with SSH shell access to the worker:

```sh
python3 experiments/cas-network/benchmark.py --host ubuntu@WORKER_IP \
  --rounds 3 --out artifacts/cas-network.json
python3 experiments/cas-network/summary.py \
  --records artifacts/cas-network.json --out /tmp/cas-network-summary.json
```

Use `--mib 1 --files 103 --rounds 1` for a small transport smoke test. The
default fixture contains 375 MiB across 5,000 regular files. One priming round
is excluded from summaries. The remote helper is uploaded only into the
new private scratch directory. Cleanup checks its owner marker before
removing it. The command also closes its own SSH master connection.

Add `--hybrid` to compare five methods: rsync, the two original whole-blob CAS
policies, and `cas_hybrid_trusted` / `cas_hybrid_rehash`. The default remains
the original three-method comparison. Select cases with repeated `--case`
options. `--warm-only` omits the default cold case; an explicit `--case cold`
conflicts with that option. Duplicate case selections are refused.

```sh
python3 experiments/cas-network/benchmark.py --host ubuntu@WORKER_IP \
  --hybrid --warm-only --rounds 3 --out artifacts/cas-network.json
python3 experiments/cas-network/benchmark.py --host ubuntu@WORKER_IP \
  --hybrid --case full_large_rewrite --case mode_change --case new_path \
  --rounds 3 --out artifacts/cas-network.json
```

The default cases remain `cold`, `warm_unchanged`, `small_delta`, and
`tiny_large_edit`. The optional cases rewrite the complete large file, change
one small file's executable mode without changing bytes, or introduce a new source path.
They run only when selected explicitly. Each saved report declares its cases
and methods. The offline summarizer requires every declared case, method, and
measured round to have exactly one verified sample. It accepts the original
three methods or all five; partial hybrid policy sets are refused.

The sender captures one manifest for each source variant before measuring
transfers. The CAS path sends that manifest to a scratch receiver. The receiver
links known blobs and returns missing paths. The sender uploads those paths
with rsync. A final control request verifies new blobs and publishes the
snapshot. The rehash policy also verifies reused blobs before publication.
The trusted policy assumes the private store still contains verified,
read-only insertions.

Hybrid CAS retains the same manifest planning and integrity policies. It
uploads only missing identities, using the read-only baseline as rsync's
same-path `--link-dest` basis. `--ignore-times` forces those selected files to
be transferred even when size and mtime match, and `--no-whole-file` permits
block-delta reuse. The stage is private; the receiver never uses `--inplace`
or append modes. Every reconstructed missing file still passes complete
SHA256 and canonical-mode verification before blob insertion. Rehashing also
checks reused blobs before publication. New paths without a same-path basis
can still require full payloads. Hybrid reuse does not establish protection
against live-worktree races, concurrent writers, or corrupted trusted storage.

The rsync path uses `--checksum`, `--link-dest`, and the same publication
boundary. Warm cases retain the same baseline content for both methods. Each measured sample
starts with its own store or stage, so an earlier sample cannot provide newly
uploaded content to a later sample.

Transfers share a dedicated, warmed SSH connection. Its setup is outside the
measured samples. The receiver uses disk-backed scratch space under its home
directory. Both sides record their rsync versions. Cases include an unchanged
tree, a small edit, and a 16-byte edit in a large file with unchanged size and
mtime. Case and method order rotate between rounds. Filesystem caches remain
warm and the worker may have other jobs running.

Here, cold means no retained source content for that sample. It does not mean
cold filesystem caches.

The transaction timer includes actual control requests and replies, payload
transport, verified insertion, and publication. Full output audits and
independent execution copies are separate. CPU and filesystem I/O counters
retain their producer and native units. Helper `/proc/self/io` counters describe
the helper only. They do not describe rsync's child processes. Application
control-message sizes are separate from rsync protocol counters; neither is a
complete encrypted network byte count.

`verification_seconds` measures the receiver's output audit.
`audit_transaction_seconds` measures the entire audit RPC, including output
verification, execution copying, execution verification, cleanup, and SSH.
Those child intervals are already inside the audit transaction. Do not add
them again. The persistent SSH master and SSH server CPU are outside the
reported CPU attribution. Receiver helper CPU excludes interpreter startup,
imports, and response serialization; transaction wall includes those costs.

This is a transport experiment. It does not measure Pandora's complete
submission path, source capture from a live repository, concurrent CAS writers,
garbage collection, or crash durability.
