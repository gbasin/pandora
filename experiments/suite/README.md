# Frozen suite plan and shard foundation

This private evaluation interface runs Eichler suite shards on the existing SSH
worker. It does not enable `pnpm journeys` in agent sessions. Normal suite routing
still rejects that command until parent-request recovery and scheduling exist.

A plan captures one source digest, the selected catalog, the global replay cover,
and exact shard membership. Each shard uses the same source digest and recomputes
the plan before starting the Workers runtime. Eichler's own suite CLI executes the
shard, including its database clones, clean runs, selected replays, and reports.
Each request has fresh containers, services, and writable source. Dependency
images and package caches remain reusable.

## Operator evaluation

Create request JSON outside the target repository. A full plan uses:

```json
{"action":"plan","shard_count":4,"selection":null}
```

For a bounded evaluation, set `selection` to exact IDs such as
`["S0-01","S0-02"]` and use two shards. This selection is recorded in the plan
and cannot be reported as full catalog coverage.

Submit the plan through the private warm client:

```sh
python3 experiments/warm/warm.py --host ubuntu@HOST --repo /path/to/worktree \
  --output /path/to/state/plan --workflow suite --suite-request /path/to/plan-request.json
```

Build each shard request from the returned `results/suite-plan.json`:

```json
{"action":"shard","plan":{},"shard":1}
```

Replace `{}` with the entire returned plan object. Submit each request with the
same warm command and a distinct output directory. Keep source fixed throughout
this evaluation. A changed source digest stops submission before remote execution.
Do not copy an old result into a new request or alter the plan to retry a shard.
Use `experiments/warm/transport.py HOST OUTPUT_DIRECTORY` to recover the existing
accepted attempt after a disconnected client.

Aggregate downloaded attempts:

```sh
python3 experiments/suite/aggregate.py --plan-attempt /path/to/state/plan \
  /path/to/state/shard-1 /path/to/state/shard-2
```

The command verifies artifact hashes, terminal cleanup, request identity, plan
identity, and exact result membership. It prints JSON and exits zero for a passing
suite, one for complete failure evidence, or 75 for incomplete or invalid evidence.
Missing shards, duplicate shards, duplicate journey results, and mixed plans are
errors. Coverage summaries retain Eichler's informational semantics; uncovered
routes do not silently become a new test gate.

## Limits

The aggregator uses the independently verified attempts explicitly supplied by
the operator. A content-identical plan can be reused across executions. It does
not establish that the attempts belong to one coordinated invocation. The parent
dispatcher must persist and check the accepted attempt identity for every shard.

These are independent private requests. Each retains the existing FIFO admission,
queue deadline, and 20-minute execution limit. They do not yet implement one
parent invocation, fair scheduling between suites, or the agreed cumulative
waiting budget across dispatches. They do not publish fixture changes. The suite
CLI runs without `--update`, and the adapter checks that expectations stayed
unchanged. Full-suite update proposals require a later central publication step.

The API is a foundation for normal-command routing. Agents should not be taught
to coordinate these requests themselves.
