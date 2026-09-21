# Resource admission experiment

This is the admission core used by configured Pandora workers. A worker with a
valid `worker-config.json` uses the uploaded scheduler and can admit overlapping
work within its declared resource limits. Legacy workers without that configuration
retain one FIFO worker slot.

The implementation uses Python's standard SQLite library and process-held file
locks. One transaction reserves CPU, RAM, and a slot together. Each invocation
also has a parallelism cap. Callers must enforce the declared limits on the actual
containers, builders, and supporting services.

## Policy

The scheduler section of the [complete worker configuration](../warm/worker-config.example.json)
has this shape:

```json
{"version":1,"cpu_millis":3500,"memory_mib":13000,"max_running":2,"policy":"fair","disk_mib":24576,"disk_floor_mib":10240}
```

`fair` alternates admission between waiting invocations. A newly waiting focused
invocation can precede a suite's next shard. New arrivals cannot continually jump
an older waiting invocation. `fifo` admits strictly by request ticket instead.
Neither policy interrupts running work.

The selected request waits until its CPU and RAM fit. The policy does not fill
spare capacity with later small requests while that selected request waits. This
can leave capacity idle, but avoids starving larger requests. Fair scheduling
skips an invocation that already reached its parallelism cap; strict FIFO does
not bypass its head for that reason.

## Queue clock and recovery

Each invocation owns one cumulative queue budget. Time counts once while it has
waiting work and no admitted task. A running sibling pauses that invocation's
queue clock. Another invocation's running task does not pause it. Re-registering
the same invocation preserves its budget, spent time, and settings.

Admission includes preparation through verified cleanup. The caller must publish
its cleanup receipt and immediately call `settle(attempt)` before dropping its
attempt lock. Waiting claimants also reconcile completed reservations. The clock
charges each interval using its state before the transition; completing a task
does not retroactively bill its execution time as waiting. Reconciliation delay
remains part of the admitted interval. The ledger does not estimate actual CPU
activity or reconstruct an unreported completion time.

The same boot identity and configuration must be supplied when reopening the
ledger. A reboot, configuration change, corrupt clock, or corrupt database stops
new admission. Use the [operator recovery runbook](../warm/notes/operator-recovery.md)
for an acknowledged recovery or configuration migration. Do not delete the ledger
to bypass unresolved work.

A dead running owner retains its resource reservation and blocks new admission.
An explicit verified cleanup receipt can release capacity without inventing a
test result. Fail-fast withdraws waiting requests but leaves running siblings
alone. Parent result collection remains a separate responsibility.

New leases hold a shared `worker.lock`; an old exclusive worker or maintenance
lock prevents admission. This is a resource interlock, not a fairness guarantee
between the old and new schedulers. Do not use mixed scheduling modes as a rollout.

## VM probe

The bounded probe needs Linux, Docker, passwordless `sudo docker`, and an existing
image containing Node. Copy `probe.py`, `resource_admission.py`,
`scheduling_policy.py`, and `admission.py` with their relative directory layout.
Run from the copied tree:

```sh
python3 -B experiments/scheduler/probe.py \
  --root /home/ubuntu/pandora-resource-probe/unique-trial \
  --image EXISTING_NODE_IMAGE
```

Use a new root for each trial. The probe uses two slots with a combined ceiling of
one CPU and 256 MiB. It checks the actual Docker limits. It proves overlapping
containers, a focused request's fair turn, and a dead-owner cleanup barrier.
Containers have unique probe names and a 30-second process lifetime. The probe
removes its containers on exit. It writes `result.json` with identities and events.
This is not a journey benchmark or a coding-agent concurrency trial.

## Legacy and safety boundary

Configured admission protects shared BuildKit operations with explicit ownership,
protects dependency-image use from retention, accounts for disk capacity, and
distinguishes other live attempts from abandoned resources. The suite parent
collects out-of-order results, preserves exact identities, and stops new dispatch
without interrupting already-running shards. These protections do not retrofit a
legacy worker, and unknown or corrupt state still stops admission for operator
recovery.

Legacy journey limits sum to 3.5 CPU and 7.125 GiB, including database, pooler,
and proxy. Legacy surface validation and dependency preparation each cap their main
container at 2 CPU and 6 GiB. Host processes and Docker overhead require headroom.
A worker's configured slot count never overrides its CPU or RAM budget.
