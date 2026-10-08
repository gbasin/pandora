# Rsync intervention diagnostics

This experiment compares five rsync/SSH interventions in private synthetic
scratch trees on an authorized Mac and worker. It changes no production
daemon, gateway, cache, or configuration. It does not implement production CAS.

Run from an authorized Mac with SSH shell access to the worker:

```sh
python3 experiments/rsync-diagnostic/probe.py --host ubuntu@WORKER_IP \
  --rounds 3 --out artifacts/rsync-diagnostic.json
```

Use `--mib 1 --files 103 --rounds 1` for a small transport smoke test. The
default fixture contains 375 MiB and 5,000 regular files. The measured matrix
uses unchanged source content matching the retained baseline. A separate
edit-proof transfer changes 16 bytes in an incompressible large file while
preserving size and mtime, then audits the complete edited source. The probe
removes its private scratch directories and closes its own SSH master.

Each measured round contains these five variants:

| Variant | Timed operation | Audited output |
|---|---|---|
| `normal` | Unchanged-source checksum transfer against the retained basis | Complete source tree |
| `dry_basis` | Dry run with the same source and retained basis | Empty stage |
| `dry_no_basis` | Dry run with the same source and no retained basis | Empty stage |
| `empty` | Rsync with an empty fixture | Empty stage |
| `ssh_true` | SSH `true` on the warmed connection | Empty stage |

Source capture and fixture preparation happen before measurement. Source
capture here builds a controlled fixture manifest; it is not Pandora's normal
live-worktree freeze. Argument and NUL-delimited file-list preparation also
happen before that timer. Setup, planning, finalization, audits, and the separate
edit-proof and checksum-negotiation tracing probes are outside measured rounds. The
tracing probe retains NSTR/CMD output to inspect the negotiated algorithm;
its duration is not mixed into measured rounds.

The raw report preserves samples, excluded priming samples, rsync counters,
stdout, CPU subsets, receiver observations, and separate audit scopes. Dry
runs do not transfer or verify the complete source. Their audit only checks
that no files appeared in the stage. Normal output receives a full source
audit. Missing counters and unavailable child logical-I/O bytes remain unknown.

The offline summary requires one audited sample for every variant and
measured round. It reports each variant's wall, CPU subsets, native filesystem
block counters, and supported rsync statistics with their own observation
counts. It also reports signed, same-round differences for normal minus
dry-with-basis and dry-with-basis minus dry-without-basis. Scheduling variation
can make a difference negative. These are intervention comparisons, not an
additive decomposition of checksum CPU, metadata work, disk reads, or WAN
time. Do not add or subtract unrelated medians to infer those components.

Sender coordinator and waited-child CPU exclude the persistent SSH master.
Receiver helper and waited-rsync CPU exclude sshd and unattributed startup
work. Filesystem blocks retain native units; zero disk blocks can coexist with
cached reads. Application rsync counters are not encrypted network byte totals.
The worker can have other jobs running, filesystem caches remain warm, and
small samples do not establish stable tail estimates. When the measured round
count is not a multiple of five, rotation only partially balances variant
order and does not give each variant equal exposure to every position.

Summarize a saved report offline:

```sh
python3 experiments/rsync-diagnostic/summary.py \
  --records artifacts/rsync-diagnostic.json --out /tmp/rsync-diagnostic-summary.json
```

The summary validates the separate edit proof's full-source audit when that
probe is present. It excludes edit-proof, tracing, and priming records from
variant distributions. It reads only the saved JSON file, opens no SSH
connection, and reads no source tree or production state.
