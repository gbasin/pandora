# Frozen suites and isolated shard evidence

Normal `pnpm journeys [--keep-going]` now uses a remotely owned parent with
concurrent shard dispatch bounded by the parent's accepted `max_parallel` value
and the worker scheduler's resource capacity. See the [agent command instructions](../routing/README.md#sharded-suites).
The private interfaces below also support bounded operator evaluations.

A plan captures one source digest, the selected catalog, the global replay cover,
and exact shard membership. Each shard uses the same source digest and recomputes
the plan before starting the Workers runtime. Eichler's own suite CLI executes the
shard, including its database clones, clean runs, selected replays, and reports.
Each request has fresh containers, services, and writable source. Dependency
images and package caches remain reusable.

## Parent evaluation

Use a private request with an exact selected catalog for a bounded parent trial:

```json
{"action":"run","shard_count":2,"selection":["S0-01","S0-02"],"keep_going":false}
```

Submit it with `warm.py --workflow suite-run --suite-request REQUEST`, plus the
same host, repo, and output arguments shown below. Set `selection` to `null` for
the full catalog. Recover the same parent with `transport.py HOST OUTPUT`. Normal
routed commands instead print and support `pandora wait <attempt-id>`. The
parent freezes input once, reserves all attempt identities before dispatch, and
returns one verified summary. Its queue budget is cumulative across planning
and shards. A failure stops new dispatch unless `keep_going` is true.

## Independent attempt evaluation

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
dispatcher instead persists and checks the accepted attempt identity for every shard.

These are independent private requests. Each receives the configured scheduler's
admission, queue deadline, and execution limit. They do not themselves establish
one parent invocation, fair scheduling between suites, or a cumulative waiting
budget across independently submitted requests. They do not publish fixture
changes. The suite CLI runs without `--update`, and the adapter checks that
expectations stayed unchanged. Full-suite update proposals require a later central
publication step.

The independent API remains useful for operator probes. Agents use the normal
command and should not be taught to coordinate these requests themselves.
