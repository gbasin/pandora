# Boat overflow

Overflow is opt-in on the primary OVH worker. Installing this code does not
change the installed daemon, worker configuration or production fleet. Ordinary
remote jobs still use OVH unless the operator explicitly enables an allowlist.

When OVH remains full past `after_seconds`, a queued eligible job can atomically
claim Boat as its destination. If OVH has room by then, it admits the job locally
and avoids a Boat start. The original run/request/input IDs, source manifest,
client identity, learned reservation, hard ceiling and CPU hint travel together.
A Boat-owned attempt never reenters OVH admission, including after a lost reply
or relay restart.

The initial adapter supports single remote jobs on an exclusive non-SMT eight-core,
16 GiB VM. It excludes fan-out, sessions, singleton servers and write-back. The
hard ceiling must be an existing size class at most 12,288 MiB and at least the
job's declared floor. A smaller reservation does not make a larger ceiling fit.
Commands over 195 minutes are excluded to leave room inside the provider TTL.

## Operator configuration

The worker reads `<engine-root>/overflow.toml`, not a client or repository
setting. It does not read the owner's local daemon configuration for overflow.

```toml
enabled = false
url = "https://compute.example.com"
token_file = "/run/secrets/pandora_token"
allow = ["eichler:native-unit"]
after_seconds = 60
max_claims = 2
# signer = "ssh-ed25519 ... pandora-overflow"
```

The installed primary worker maintenance code and clients must first be upgraded
and old processes drained. The primary's `min_engine_version` must be at least
7 before overflow can be enabled. Client bundles include frozen manifests and
an engine v7 settlement fence prevents stale reconcilers from closing Boat jobs
as generic supervisor-gone failures. Owner-operated installation remains separate
from development in this worktree.

The broker also has an explicit repository/job allowlist, shared concurrency cap,
CI priority policy, HTTPS endpoint and dedicated authentication token. The token
stays on the primary and controller; VMs receive generated per-attempt capabilities.
The primary freezes its endpoint and policy with each destination claim. Changes
to defaults do not redirect an existing attempt.

## Worker and relay contracts

`pandora.engine.overflow_worker` validates the entire source manifest, inventory,
contents, executable modes, symlink containment and Git marks, then invokes the
existing engine with the chosen limits. It drops client-selected worker pins.
Finished exact duplicates return persisted JSON; partial or unfinished attempts
are uncertain and never execute again. It is an internal module, not a new
public `pandora` command.

`pandora.engine.overflow` owns primary routing and collection. Every broker call
binds the immutable packet identity, so a run-ID collision cannot attach to or
cancel somebody else's packet. The relay preserves logs and artifacts, checks
original result IDs, outcome/exit consistency, cleanup receipt and any expected
signature, then fsyncs files before publishing completion and acknowledging
custody. Missing or invalid evidence never fabricates a pass. Cancel requests
survive relay failure and are routed to the same durable lease.

The broker keeps source until primary acknowledgement and keeps acknowledged
results/logs for seven days. Identity tombstones remain. Primary retention keeps
unacknowledged custody records and reconciliation repairs a missing local result
from retained broker evidence without repeating execution.

## Cleanup and trust

The initial integrated policy retires the whole Pandora VM after every lease.
The patched scheduler marks it terminating while releasing the task's occupied
blocks, avoiding a window in which CI could reuse an incompletely cleaned host.
Normal successful engine cleanup still supplies its receipt. Forced kills can
leave empty Incus cgroups; retirement removes the entire host in that case.
This is more conservative and costs more starts than normal VM reuse.

Nested Incus work uses sibling VM cgroups outside Docker's task accounting while
retaining the private task PID namespace. Exclusive placement and Incus limits
are required. The worker canary runs after cold golden preparation. Source and
result archives are capped at 256 MiB compressed/1 GiB expanded. Streamed logs
preserve offsets with a fixed 64 MiB prefix; final collection includes the full
log within the archive limit.

CI and Pandora share the trusted internal host boundary selected by the owner.
Signing is optional and uses a dedicated controller-held overflow key delivered
to the outer worker only after the canary passes. It is never mounted into the
job's source. The OVH signing key is not copied or used to re-sign results.
Pin the expected public signer on the primary and enroll it separately for CI
reuse. Without signing readiness, an ordinary result remains honestly unsigned.

See Eichler Compute's `docs/pandora-overflow.md` and `deploy/pandora*.example.toml`
for the broker/runtime setup, remote feature guard, limits and staged rollout.
