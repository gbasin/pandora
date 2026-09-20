---
status: log
---
# Acknowledged worker loss and a fresh validation

The evaluator started ordinary `pnpm journey S0-01` through the launcher on the
authorized Linux worker. Attempt `b656e3ffaceb4d84955c3bcb0f003e6d` reached Workers
runtime readiness with its isolated supporting services. The evaluator then sent
SIGKILL to that attempt's systemd main process. No other unit received a signal.

The stop hook removed its owned resources. The explicit operator cleanup command
then verified a dead owner, no pending cleanup markers, and a matching cleanup
receipt. No verified terminal test result existed. An explicit
`acknowledge-missing-result` command recorded reason
`controlled-worker-loss-trial` and bound the acknowledgement to the exact
submission bytes and source digest.

The still-waiting local command retrieved the acknowledgement and exited 70. It
printed that the request had no test result and instructed the caller to run the
command again to start a new request. The local request closed as
`infrastructurefailure`. Neither the worker nor client fabricated a passing
terminal. Submission bytes remained immutable; local timing observations went
to `client-result.json`.

The next deliberate `pnpm journey S0-01` allocated a distinct attempt,
`80eeca9e578045fe98334a7e046742bc`. It passed with exit 0 and verified cleanup.
Both attempts used source digest
`bc95ddd2173511c3fbc4c507cc5ba1d528c38dab12fa0d5d358ff0955dcf48c1`.

Repeated acknowledgement of the lost attempt returned its original immutable
receipt. Attempting to acknowledge the successful fresh run was refused with
operator exit 70 and an explanation that a valid terminal result cannot be
overridden. This refusal is distinct from a test failure.

Evidence is retained under `~/.local/state/pandora/v01-operator-trial/`: the
fault receipt, cleanup receipt, acknowledgement, guard results, and both attempt
directories. This experiment covers a focused service-backed journey. Suite
parent reconciliation has separate tests and is not implied by this result.
