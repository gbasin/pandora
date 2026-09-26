---
status: log
---
# Repository source caches, 2026-09-20

The previous global `latest` pointer alternated between unrelated repositories.
After a Docker fixture build, Acme uploaded its source again despite having
an older complete snapshot on the worker. The recorded transfer took 69.1 seconds.

The cache now uses SHA-256 of the client's canonical Git common-directory path.
Worktrees, including nested worktrees, share that identity. Separate clones have
separate caches. No remote URL or credentials enter the cache key.

The concurrency trace that informed the implementation:

- Initial state: repository A points at attempt 1; repository B points at attempt 2.
- Client A prepares attempt 3. Under the source-cache lock, it hardlinks attempt 1's
  source into attempt 3's private seed directory.
- Another client publishes attempt 4 as repository A's latest snapshot.
- Retention takes the same lock and removes acknowledged attempt 1 if eligible.
  Attempt 3's seed still owns references to those files.
- Client A transfers against its seed, uploads the manifest and worker scripts,
  then atomically publishes attempt 3's complete source. It removes its seed.

Rsync never modifies the seed. Writable container mounts remain private copies.
A disconnected transfer can leave an unresolved attempt and seed for operator
reconciliation; it cannot publish an incomplete snapshot. The pointer retains
one latest source per repository without an automatic inactive-repository expiry.
This is a reuse policy, not a disk quota.

Deploy with older submissions drained: older uploaded workers do not take the
new source-cache lock. The legacy pointer is still protected for compatibility,
but new clients neither use nor replace it. Existing repositories incur one cold
upload to establish their keyed cache.

VM validation passed both service-backed S0-01 runs with an unrelated fixture
build between them. The first Acme transfer took 63.9 seconds; the second took
2.7 seconds. All three requests exited zero with cleanup verified. Local tests
passed: 13 warm-worker tests, 13 routing tests, and 7 publication tests.
