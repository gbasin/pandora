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

Copy [`scripts/versions.toml`](../scripts/versions.toml) to a file for this
worker. Set `device` to the spare block device. Pin `incus` and `incus-client`
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

Use `--loop-file 18G` instead of `device` only when there is no spare device.

## Canary

The canary is the worker's health check: about 26 checks per enrolled golden.
The older canary took about 100 s. The budget is 240 s per golden. It reads
every enrolled repository's `pandora.toml` through the daemon's loader and
proves each distinct `[worker]` golden: it builds or reuses the golden, runs a
real journey with its compose stack in one clone, runs the surface job's
`validate` step in another, then checks the disk quota and drives a memory hog
until the watchdog kills it as `oom`.

```sh
pandora worker canary --mark
```

* What runs comes from `[worker.canary]` in the repository's `pandora.toml`:
  `journey = "S0-01"` is spliced into the `journey` job's `run` argv, with that
  job's environment. `compose = "tools/stack/compose.yml"` is brought up and
  down first. `surface = "borrower-web"` is given to the `surface` job's
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
the manifest, pool use, the admission gate, the goldens and the last canary. A
package or setting that differs from the manifest, or a kernel that differs from
the one the canary passed on, reads `drifted`, not `ready`. Re-run the canary
with `--mark` after any change to the machine.

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
* `rsync --server` with every path operand inside the engine root.

`gc`, `canary`, `ready`, `retain`, `cache-clear` and a bare shell are refused
on a `user` key. An admitted command runs with `PANDORA_GATEWAY_CLIENT` set to
the key's name, and the engine trusts that pin over whatever the request
claimed — so a gatewayed client cannot speak as another client, whatever code
it runs.

The gateway bounds command *shape*, not content: a bundle is client code
running as the worker user, so a teammate who can ship a bundle can run
anything as that user. Identity, revocation and verb scoping are what it buys;
isolation between users stays out of scope.

The e2e workflow exercises it against the live worker once the runner carries
a teammate key: `scripts/e2e-gateway.sh` refuses a shell, a `python3 -c`
stranger and an escaping rsync, runs `worker status` and a selftest through
the pinned key, and checks the ledger names the pin. Its header lists the four
files the runner needs; until they exist the job reports a skipped gate.

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
