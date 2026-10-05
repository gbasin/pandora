# The worker

How to provision, prove, share and maintain the Linux worker that runs remote
jobs. To replace a worker, follow [worker-rebuild.md](worker-rebuild.md). The
Mac side is in [operations.md](operations.md).

A worker is a disposable Linux machine. Pandora can repopulate the source
cache from worktrees, and each golden rebuilds from its toolchain description.
The ledger, the learned size classes and the attempt directories
are history. A rebuild starts them empty. Rebuild a worker rather than
repair it. [`docs/worker-rebuild.md`](worker-rebuild.md) is the full
procedure, with cut-over and upgrade cadence.

## Requirements

* x86_64 Ubuntu 26.04, at least 4 vCPU, 15 GiB of memory and 96 GB of disk.
* A spare block device of at least 40 GB for the Incus storage pool, sized for
  about three goldens of 4 to 5 GiB each plus concurrent runs. 40 GB is an
  estimate, not a measurement. Without one, a loop file works, but it is slower
  and adds a boot dependency.
* A `ubuntu` user with your SSH key and passwordless sudo. Check with
  `ssh ubuntu@WORKER_IP sudo -n true`.

Do not install Incus or Docker on the host. `provision` installs Incus at the
pinned version. Docker runs only inside each run's instance.

## Provision

Copy [`scripts/versions.toml`](../scripts/versions.toml) to
`deploy/workers/<name>.toml` and commit it: the manifest is the one file that
reproduces the worker, and this repository is public, so keep no IPs or
hostnames in it. Live example: `deploy/workers/pandora-rbx.toml`. Set `device` to the spare block device. Pin `incus` and `incus-client`
to exact dpkg versions. Set `run_disk_gib`, the per-run root quota. The quota
counts bytes the run shares with its golden, so a 12 GiB quota over a 4 GiB
golden leaves the run about 8 GiB of its own writes.

Run `provision` from the Mac. `--host` belongs to `worker`, before the verb.
It defaults to `[worker] host`.

```sh
pandora worker --host ubuntu@WORKER_IP provision --versions ./versions.toml --no-canary
```

`provision` installs the declared packages, disables unattended upgrades,
creates or adopts the pool, creates the bridge, its forwarding rules, the
`pandora` project and the `runner` profile, installs the boot units, and
writes the manifest. Every step reports `present`, `created`, `changed` or
`skipped`. Run it again. The second run must report `0 changed`.

Every run's `eth0` is a port on the one bridge, `pandorabr0`. The profile's
`eth0` carries `security.port_isolation=true`, and the executor sets the same
key on every clone before it starts. An isolated port cannot reach another
isolated port, so a service one run binds on `0.0.0.0` does not answer a
concurrent run. Runs still reach the bridge address, where the turbo cache
listens, and the internet through NAT. `harden` reads the bridge port flag on
the host and records the answer in the run's evidence as
`cgroup.eth0.port_isolation`. If the port is not isolated, the run does not
execute. It fails as `clone-failed`, which is retryable. A worker provisioned
before the profile carried the key is protected by the executor alone. It
needs no re-provision.

Use `--loop-file 18G` instead of `device` only when there is no spare device.

## Canary

The canary is the worker's health check: about 35 checks per enrolled golden.
The older canary took about 100 s. The budget is 240 s per golden. It reads
every enrolled repository's `pandora.toml` through the daemon's loader and
proves each distinct `[worker]` golden: it builds or reuses the golden, runs a
real journey with its compose stack in one clone, runs the surface job's
`validate` step in another, then proves two concurrent clones cannot reach each
other on the bridge, checks the disk quota and drives a memory hog until the
watchdog kills it as `oom`. The isolation check opens a listener on `0.0.0.0`
in one clone and confirms the host reaches it. It then confirms the other
clone reaches the bridge address: the turbo cache when it is serving,
otherwise the bridge DNS on port 53. Only then does it expect a connect from
the second clone to the first to fail. The hog writes its own 1.5 GiB of
random files under `/work/.pandora-hog` in the first proven golden's clone,
then reads them in a loop under a 512 MiB ceiling. It does not depend on what
the repository put in `/work`, so any golden proves the watchdog.

```sh
pandora worker canary --mark
```

* What runs comes from `[worker.canary]` in the repository's `pandora.toml`:
  `journey = "S0-01"` is spliced into the `journey` job's `run` argv, with that
  job's environment. `compose = "tools/stack/compose.yml"` is brought up and
  down first. `surface = "web"` is given to the `surface` job's
  `validate`, or to its shard `plan` with one shard when it has no `validate`.
  `journey_job` and `surface_job` name different jobs. The table is not part of
  the golden's fingerprint. A key left out is a check not run, and the verdict
  says so.
* The golden is built from `<engine_root>/src/<repo>/latest` on the worker. The
  client writes that tree after each transfer, so on a fresh worker it exists
  only after the first routed run. If it is absent and the golden is not built,
  the canary fails with that reason. Run one claimed command from an enrolled
  worktree first, or pass `--source <a tree on the worker>`.
* `--journey F` and `--surfaces F` are overrides for a worker no repository is
  enrolled against yet. Each names a toolchain JSON file with the `[worker]`
  keys. A path that exists on the Mac is shipped. Any other path is read on the
  worker.
* `--mark` writes the ready state from the verdict. Without it the canary only
  reports.

`provision` without `--no-canary` runs the same canary and marks the verdict.
It takes the same `--journey`, `--surfaces` and `--source` flags.

A worker that fails the canary is never marked `ready`. The ready state is a
label for people: `pandora worker status` and the daemon's health notices
report it, and submission does not check it. Do not send work to a worker that
is not `ready`. Reboot a new worker once before it takes work. Then confirm the state again.

```sh
pandora worker status
```

`status` prints the ready state, the host and kernel, installed versions against
the manifest, pool use, the admission gate, the goldens, the last canary and the
[verdict signer](#verdicts). The admission gate is `CLOSED` while the pool is
below `disk_floor_gib`. Then new runs are refused, and queued runs and shards
wait. A package or setting that differs from the manifest, or a kernel that
differs from the one the canary passed on, reads `drifted`, not `ready`, and
stops verdict signing ([Verdicts](#verdicts)). So does a worker with no stored
manifest at `<root>/worker/versions.toml`: the drift list says
`object manifest: ... (not stored)`. Re-run
the canary with `--mark` after any change to the machine.

## Verdicts

A whole run that passes on a `ready` worker signs a verdict: a statement that
this job, with this argv, passed over this exact git tree on this golden. A
CI job for the same tree can verify the signature and skip the work.

* The tree comes from the synthetic repository. Only a job with
  `git = "synthetic"` has one. The tree covers every file the run saw, tracked
  or untracked. Secret-filtered files never reach the worker, so a tree with
  one of them never equals a commit's tree.
* The payload is canonical JSON with `kind`, `v`, `run_id`, `repo`, `job`,
  `argv`, `cwd`, `env_digest`, `input_id`, `tree`, `golden`, `engine`,
  `outcome` and `finished`. `engine` is the digest of the engine bundle that
  ran it. `golden` is the toolchain fingerprint in `golden-<fingerprint>`.
  `cwd` is the job's working directory as the plan carries it. `env_digest`
  is the sha256 hex of the run's environment mapping as the plan carries it,
  serialized as `json.dumps(env, sort_keys=True, separators=(',', ':'))`. The
  payload binds the digest, not the values, so no environment value is
  published.
* The conditions, in order: the outcome is `passed` (`not_passed`); the
  attempt is a whole run, not a shard or a fan-out parent (`not_whole`); the
  worker's ready state is `ready` (`worker_not_ready`); the run has a tree
  (`no_synthetic_git`); the worker has not drifted (`worker_drifted`).
  `result.json` names the first that failed in `verdict_skipped`
  ([Run results](operations.md#run-results)). The tree comes before drift so
  a job without synthetic git never pays for the drift check.
* Drift blocks signing. Before it signs, the engine compares the host with
  the manifest stored at `<root>/worker/versions.toml`, using the same
  comparison as `pandora worker status`: each manifest package's installed
  version from one `dpkg-query`, the `[worker]` settings read off the host,
  and the kernel against the one the last canary passed on. Any difference
  skips the verdict as `worker_drifted`, and the run log gets one line that
  names each difference:
  `pandora: verdict not signed: worker_drifted: package incus: want 6.0.5-8, have 6.0.6-1 (version differs)`.
  A missing or invalid manifest, or a `dpkg-query` that cannot run, is drift
  too, with a detail that says which (`manifest unreadable: ...`,
  `dpkg unreadable: ...`). `dpkg-query` and `systemctl` each get 5 seconds; one
  that times out is unreadable, and a setting `systemctl` cannot read is
  drift. The answer, a failed check included, is cached for 60 seconds in
  `<engine_root>/keys/drift.json`, shared by every run's supervisor, so a hung
  tool costs one run its timeout, not every run. A new `canary --mark`, a
  changed manifest or another worker root starts a fresh check. A package that
  dpkg holds in any state but installed counts as not installed, so a host in
  the middle of an `apt` run reads as drifted, and can stay drifted for up to
  a minute after `apt` finishes. That is expected: the next check clears it.
  After a deliberate package change, re-run the canary with `--mark`.
* The key is an Ed25519 OpenSSH key at `<engine_root>/keys/verdict` (0600) in
  a 0700 directory. The first run that signs creates it. It never leaves the
  engine root and is never injected into an instance. `submit` refuses a
  `source_path` that does not resolve, symlinks included, to a directory below
  `<engine_root>/src`, as `source-outside`: the source is what the instance
  mounts. The gateway refuses any rsync path in `keys` or above it, any
  `--link-dest`, `--copy-dest` or `--compare-dest` that is relative or reaches
  `keys`, and every option that makes the server follow a symlink: `-L`, `-k`,
  `--copy-links`, `--copy-unsafe-links` and `--copy-dirlinks`, alone or in a
  short-option cluster such as `-rlptgoDL`. A teammate who can ship a bundle can still
  run code as the worker user ([The gateway](#the-gateway)), so trust the
  signer only as far as every key that reaches the worker.
* Signing runs `ssh-keygen -Y sign -n pandora-verdict`. A missing
  `ssh-keygen` or a failed signature leaves `verdict` null with
  `verdict_skipped` set to `sign_failed:<reason>`. The run's outcome and exit
  code do not change.

Read the public key with `pandora worker status`. It prints
`verdict signer: <key line>`, or `verdict signer: none yet (created on the
first signed run)`. `pandora worker --json status` carries it as
`verdict_signer`. Put that line in the verifying repository's allowed-signers
file. Rebuilding a worker makes a new key, so update that file after a
rebuild ([docs/verdicts.md](verdicts.md#rotate-the-key-after-a-worker-rebuild)).

What a verdict does not check:

* The drift check before signing skips the objects `status` checks with
  `sudo incus` and `systemctl`: the storage pool, project and bridge, the
  pool, network and engine units, linger and the forward rules. `status`
  reports one of those as `drifted` while runs still sign. A run could not
  have started without most of them.
* A change within the 60-second cache is not seen until the cache expires.
* The engine reads the ready state from `PANDORA_WORKER_ROOT`, or `~/pandora`
  when that is unset. A worker kept under a custom `pandora worker --root`
  writes its state file there, so the engine finds none and nothing signs:
  each result reads `verdict_skipped: worker_not_ready` while `status` reads
  `ready`. That is fail-safe but silent. Set `PANDORA_WORKER_ROOT` for the
  engine to the same root.
* The tree is git's view of the bytes, not the bytes. A file that git
  normalizes stores differently from what the run saw: a CRLF file under
  `* text=auto` is stored with LF, and an LFS pointer is stored in place of
  the content it names. A tree can therefore match a checkout whose bytes
  differ. This is known and rare. `test_git` pins the normalization.

## Sharing a worker

Several Macs, each with its own client daemon and its own user, can share one
worker and one `engine_root` ([#156](https://github.com/gbasin/pandora/issues/156)).
Every client logs in as the same worker user; what a key may do is decided by
the key, not by whoever is holding it.

### Users in the manifest

`versions.toml` gains a `[[users]]` entry per teammate's key:

```toml
[[users]]
name = "sterling"               # the client name this key speaks as
role = "user"                   # or "admin": a plain shell, no gateway
key = "ssh-ed25519 AAAA... sterling@laptop"
```

`provision` renders them as a managed block in the worker user's
`authorized_keys`, between `# >>> pandora users >>>` markers. A `user` line
carries `restrict` and the forced command
`<root>/bin/gateway --name <name> --engine-root <er> --worker-root <wr>`; an
`admin` line is the plain key. Lines outside the markers are never touched, so
the account's own key survives. Removing an entry and re-running `provision`
revokes the access. `name` is validated like `[client] name`; `role` defaults
to `user`.

### The gateway

`provision` installs `pandora/worker/gateway.py` as `<root>/bin/gateway`. Pinned
as a key's forced command, it admits exactly the wire shapes a client sends and
refuses everything else with `pandora-gateway: refused as <name>: <reason>`:

* the home probe and the bundle presence check (`sh -c`, two fixed forms);
* `python3 -c <script>` for the fixed feed scripts only, allowlisted by sha256
  in `<engine_root>/feeds.allow` and in each installed bundle's
  `pandora/.feeds`, with every path argument inside the engine root;
* `python3 -m pandora.engine.service` under an installed bundle, for the
  lifecycle verbs: `submit`, `resubmit`, `lookup`, `status`, `result`,
  `cancel`, `wait`, `logs`, `ps`, `stats`, `health`, `cache-stats`,
  `reconcile`;
* `pandora.worker.service` under an installed bundle, read-only: `status`,
  `capacity`, `goldens`, `pins`;
* `rsync --server` with every path operand inside the engine root, never in
  or above `<engine_root>/keys`, and no option that follows symlinks
  ([Verdicts](#verdicts)).

`gc`, `canary`, `ready`, `retain`, `cache-clear` and a bare shell are refused
on a `user` key. An admitted command runs with `PANDORA_GATEWAY_CLIENT` set to
the key's name, and the engine trusts that pin over whatever the request
claimed — so a gatewayed client cannot speak as another client, whatever code
it runs.

The gateway bounds command *shape*, not content: a bundle is client code
running as the worker user, so a teammate who can ship a bundle can run
anything as that user. Identity, revocation and verb scoping are what it buys;
isolation between users stays out of scope.

The e2e workflow exercises it against the live worker:
`scripts/e2e-gateway.sh` generates a teammate keypair, installs `bin/gateway`,
`feeds.allow` and a managed `authorized_keys` block over the admin key
(the e2e worker keeps no manifest to re-provision), refuses a shell, a
`python3 -c` stranger and an escaping rsync, runs `worker status` and a
selftest through the pinned key, checks the ledger names the pin, and lifts
the block to prove revocation.

### The engine floor

`provision` writes `<engine_root>/min_engine_version`, defaulting to the
provisioner's own engine version (`[worker] min_engine_version` overrides it).
`submit`, `resubmit` and a `lookup --fence` refuse a bundle older than the
floor as `engine-version`, which never falls back: the caller is told to run
`pandora upgrade`. One ledger, one budget and one scheduler are shared by
every bundle, so an old engine writing new rows is the failure this exists to
prevent.

### What holds between clients

* Every attempt has its own row, directory, log, result and instance, even
  when two Macs submit the same tree at the same moment. The
  source cache and the turbo cache are content-addressed and written by
  temporary file and rename, so two writers of one entry leave one whole entry.
  The caches are shared on purpose: a second client inherits warm turbo
  entries, source dedup and the repository's golden.
* A request id held by one client is never attached to by another. The worker
  refuses the second submission as `request-collision`, and nothing starts on
  the worker. The client treats it as `engine-error`, so the
  [Fallback](pandora-toml.md#fallback) table decides.
* One memory budget covers every client's runs. Admission counts them all.
* A run cannot reach another run's ports on the bridge, its own client's or
  another's. Each instance's `eth0` is an isolated bridge port. The turbo cache
  on the bridge address stays reachable and still requires its bearer token.
* `cancel`, `lookup`, `wait`, the retry, and now the reads — `status`, `logs`,
  `result` — act only on the calling client's runs. Another client's run is
  refused as `not-yours`. A run submitted before attribution existed has no
  client, and any client may act on it.
* Reconcile and retention act on what a row records (live, finished,
  orphaned), never on who submitted it.

What does not hold yet: there is no fair share between clients, so one Mac can
fill the budget. And the guarantees above assume the worker's keys were
provisioned through `[[users]]`: a client whose key is a plain shell — admin
or pre-gateway — can still name anyone, and a Mac running old code sends no
name at all. The engine floor keeps bundled code honest, not bare keys.
Change `[client] name` only while `pandora ps` shows nothing live: runs
submitted under the old name answer cancel and lookup only to that name.

Where the name shows:

* `pandora ps` ends its first line with `as <name>`, and says how many runs each
  other client has live on the worker. `--json` has `client` on each row.
* `pandora result <id>` prints `client <name>`. `--json` has `client`.
* `pandora stats` names this client on its first line and lists every client the
  worker has seen, with runs and live runs.

## Maintenance

Sweep leaked instances, leaked volumes and old goldens once a week. Read the
dry run first.

```sh
pandora worker gc --dry-run
pandora worker gc
```

`gc` keeps the `--keep N` most recently used goldens per toolchain family. A
family is one repository's `[worker] source_id`, so a rebuilt toolchain pushes
out its own older goldens and never another toolchain's
([#81](https://github.com/gbasin/pandora/issues/81)). A toolchain with no
`source_id` is its own family and is never pruned by `--keep 1` or higher. Goldens no
recorded attempt explains share one `(unknown)` family. The default for `N`
comes from `golden_keep` in the versions manifest, which is 2.

`gc` never removes these goldens, whatever `--keep` says:

* One a live attempt needs.
* One whose fingerprint an enrolled repository's `pandora.toml` names. The
  client computes these from `[[repos]]` and passes each as `--protect`. The
  receipt says `kept ... named by <repo> pandora.toml`. An enrolled
  configuration that does not load stops `gc` before it asks the worker.
* One named by `--protect FINGERPRINT` on the command line.
* A pinned one. Remove a pinned golden by hand.

A `gc` without `--dry-run` writes a receipt under `~/pandora/worker/receipts/`
on the worker.

The other worker verbs:

| Verb | What it does |
|---|---|
| `pandora worker goldens` | Each golden: repository, referenced and exclusive bytes, pinned or not, last use. |
| `pandora worker pins --toolchain F [--source D]` | Resolve a toolchain to its base-image fingerprint, lockfile digest and registry manifest digests. |
| `pandora worker reconcile` | Adopt or fail runs whose supervisor is gone after the worker service restarts. |
| `pandora worker retain` | Delete old attempt directories and unreferenced source snapshots. |
| `pandora worker stats` | The worker's scheduler picture and outcome counts. |
| `pandora cache stats`, `pandora cache clear [--repo R]` | The worker's turbo remote cache, served on the runs' bridge. |

`--json`, like `--host`, goes before the verb: `pandora worker --json status`.
