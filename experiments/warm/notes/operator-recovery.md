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

`acknowledge-missing-result` requires a dead owner, no valid terminal evidence,
no pending cleanup markers, no exact owned resources, and a valid
`admission-cleanup.json`. It writes one immutable `operator-result.json` with
`state: infrastructure-failed`. The receipt binds the attempt, submitted source
and workflow, and any retained unusable terminal hash. It never writes a
terminal, exit code, or test outcome. A client reports this receipt as an
infrastructure failure and permits the next deliberate command as a new
invocation.

`acknowledge-suite-parent` first rejects a live parent. It validates the complete
`children.json` registry and each staged child submission. Each child must have
full terminal evidence or its own bound operator result after verified cleanup.
The copied suite cleanup then accepts only those bound child results, verifies
the parent cleanup, and removes `suite-cleanup.pending`. The utility writes a
distinct immutable `operator-cleanup.json` before it acknowledges the parent.
Retries validate each receipt again. The command never writes child terminals or
claims test success.

`migrate` takes the exclusive worker lock without waiting. It refuses live
attempts, pending cleanup, unresolved managed resources, and a running row that
has neither full terminal evidence nor a bound operator acknowledgement after
verified cleanup. It archives the SQLite database with SQLite's backup API. It
then rebuilds the current schema and clock in the same SQLite transaction. This
keeps existing connections on the same inode and removes old tickets before they
can claim. It writes a new durable ledger-generation marker and installs the
validated config after the transaction. An interruption fails closed until the
operator resumes recovery.
