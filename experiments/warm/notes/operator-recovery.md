---
status: log
---
# Operator recovery foundation, 2026-09-20

`operator_recovery.py` is an operator-only, one-shot utility. It never starts,
retries, or supplies a test result. Run it from the worker bundle directory:

```sh
python3 operator_recovery.py --root /home/ubuntu/pandora-warm inspect
python3 operator_recovery.py --root /home/ubuntu/pandora-warm cleanup --attempt <attempt>
python3 operator_recovery.py --root /home/ubuntu/pandora-warm \
  acknowledge-missing-result --attempt <attempt> --reason host-reboot
python3 operator_recovery.py --root /home/ubuntu/pandora-warm \
  migrate --config /secure/operator/worker-config.json
```

`inspect` opens `resources.sqlite3` with SQLite `mode=ro`. It reports ledger
metadata, rows, attempt locks, terminal and cleanup receipts, and the existing
read-only managed-resource ownership check. It does not instantiate `Scheduler`,
so it cannot create tables or advance the scheduler tick.

`cleanup` requires the exact 32-hex attempt directory, a dead attempt lock, an
exclusive worker lock, and the copied `service_cleanup.py` inside that attempt.
Before it invokes that bundled entrypoint, it inventories Docker and rejects any
foreign or unlabelled managed container or network. The existing entrypoint then
removes the exact reserved names and writes `admission-cleanup.json` only after
its checks succeed.

`acknowledge-missing-result` requires a dead owner, no `terminal.json`, no
pending cleanup markers, and a valid `admission-cleanup.json`. It writes one
immutable `operator-result.json` with `state: infrastructure-failed`. It never
writes `terminal.json` or an exit code. A client can use this receipt to report
the lost result as infrastructure failure and clear its active recovery record;
the next deliberate command is a new invocation.

For a suite parent, do not acknowledge the parent after child cleanup alone.
`suite_parent_cleanup` intentionally leaves `suite-cleanup.pending` when any
staged child lacks a terminal result. The smallest safe follow-up is an explicit
operator parent action: validate `children.json`, require every staged child
owner dead and its resource cleanup verified, acknowledge each missing child
result separately, verify no child resource marker remains, then write a distinct
parent cleanup receipt before removing `suite-cleanup.pending`. It must not write
child terminals or claim test success. A parent that already has an unusable
terminal with missing cleanup or structured suite evidence needs a separate,
explicit acknowledgement design; this foundation does not override terminals.

`migrate` takes the exclusive worker lock without waiting. It refuses live
attempts, pending cleanup, unresolved managed resources, and a running row that
has neither a verified terminal nor a verified cleanup plus acknowledgement.
Stale terminal rows are accepted without running `Scheduler`. It archives the
SQLite file with SQLite's backup API, then replaces it with a fresh current
schema and clock. It writes a new durable ledger-generation marker before
releasing the worker lock. A claimant holding an unlinked old SQLite inode checks
that marker after commit and fails before it can return a lease. The config file
is installed last, so interruption leaves a configuration or generation mismatch
that fails closed until the operator resumes recovery.
