# Source-transfer measurements

This experiment measures the current rsync checksum and link-dest processing
with synthetic data. It does not implement CAS or change source-cache feeds.

Run the representative experiment through Pandora from this worktree's root:

```sh
pandora run -- python3 experiments/transfer-profile/benchmark.py
```

The declared `transfer-profile` job runs remotely and refuses local fallback.
It generates 375 MiB of synthetic regular files plus an internal symlink under
one temporary directory. It does not read production source caches or source
content. The JSON artifact returns as `artifacts/transfer-profile.json`.
The temporary source and receiver trees are removed before the job exits.

One priming round is excluded. Ten measured rounds rotate the six cases:
cold, warm unchanged, warm 1% edit, divergent base, three poor bases before a
good fourth base, and cold compression. Each receiver is verified against its
expected paths, SHA256 digests, modes, and symlinks. Additional probes cover
new mtimes with unchanged content, same-size edits with restored mtimes, and
changed executable mode. The old bases must remain unchanged.

Both rsync processes run inside the worker container. `--no-whole-file`
enables the delta algorithm for this local transport. Record the exact rsync
version from the result; this experiment cannot reproduce the Mac sender,
network conditions, SSH setup, or concurrent transfer contention. No system
caches are dropped. Treat the observations as warm filesystem-cache results.
Compression timing here does not predict WAN compression benefit. Verification
and inode accounting happen outside the measured rsync interval.

Run the small harness tests directly; these use a 1 MiB scratch fixture:

```sh
python3 -m unittest discover -s experiments/transfer-profile -p 'test_*.py'
```

The saved-run audit reads metadata and results without contacting a daemon or
worker. Supply a retained client state directory and a timezone-qualified cutoff:

```sh
python3 experiments/transfer-profile/saved_timings.py \
  --state <client-state> --since 2026-10-06T22:04:00+00:00 \
  --out /tmp/saved-timings.json
```

Repeat `--repo <name>` to filter repositories. The default includes all recorded
repository names. Missing and malformed timing fields are counted separately.
Retained runs are a censored sample. Overall `ship` includes cache lookup, SSH,
staging, rsync, publication, and cleanup. It does not measure wire throughput
or isolate checksum time. Saved same-input history is not a cache-hit indicator.

Committed evidence records the experiment's date and environment. Keep old
measurements intact; add a new evidence file for another run. The decision note
states which conclusions the measurement supports and which remain untested.

The normal-transfer snapshot from v0.3.16 includes cache and phase observations.
Summarize this immutable export without accessing live state:

```sh
python3 experiments/transfer-profile/observations.py \
  --records experiments/transfer-profile/evidence/normal-transfers-2026-10-07.json \
  --out /tmp/normal-transfer-summary.json
```

The export contains selected metadata fields only. It excludes source contents,
logs, commands, credentials, and worktree paths. The summary separates cache
hits, completed misses, and incomplete or unknown observations. A completed miss
requires successful rsync plus a subsequent submission timing; publication and
cleanup timers alone do not prove callback success. Later job outcomes do not
change the transfer cohort. Each statistic reports its own sample count.
