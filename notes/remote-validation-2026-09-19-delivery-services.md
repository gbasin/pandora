# Output delivery, dependency caching, and service-backed validation

Dated evidence, 2026-09-19 UTC. The user authorized continued use of the existing
OVH VM. These are evaluator-controlled POCs, not an implementation of the v0.1
routing contract or an uncoached coding-agent evaluation.

## Output delivery

The atomic directory-exchange probe ran on macOS and Linux. It retained the old
directory and verified edits made before exchange and writes through an already
open file handle after exchange. Both versions survived, including the state
immediately after exchange where a process could stop. However, the local edits
were now in the retained directory, outside the original path. This preserves
bytes but does not satisfy the stronger promise that arbitrary concurrent local
edits remain in place. An advisory lock cannot exclude ordinary editors.

A separate artifact-publication probe verified content before an atomic
no-clobber directory rename. A child process exited immediately before or after
publication. Retry either published the staged result or verified the existing
result. Both platforms rejected a conflicting existing artifact directory and a
symlink artifact. Existing conflicting bytes were unchanged.

This is evidence for publishing results into unique run directories. It does
not prove workspace writeback, power-loss durability, path safety against a
hostile concurrent process, or a complete delivery implementation. The fixture
has flat regular-file artifacts and a trusted manifest in memory.

The proposed scope refinement is automatic replacement only for declared
generated-output directories, with one writer during execution and retained
old generations. Mixed source/output directories would return separate artifacts.
The user asked whether this would confuse agents; they have not approved that
refinement. Do not treat it as settled. Normal build output delivery and source-
changing workflows need separate agent UX evidence. A returned source diff may
be easy to apply, but that is an extra action whose reliability is unproven.

## Real dependencies and compilation

A separate fixture uses pnpm 12.3.4, TypeScript 5.6.3, and Zod. It compiles and
executes TypeScript, changes source, then changes Zod from 3.23.8 to 3.24.2 with
an updated lockfile. Installs inside builds use --frozen-lockfile. Lockfile
preparation is measured separately. The BuildKit builder has 2 CPUs, 2 GiB RAM,
and no extra swap allowance. It uses a pinned Node base and a persistent pnpm
store cache mount. Each runtime check uses a fresh container.

| Build | Seconds | Evidence |
| --- | ---: | --- |
| Cold | 9.931 | pnpm reused 0 packages and downloaded 2; compiled program printed original |
| Identical | 1.105 | All fixture steps cached; runtime output remained original |
| Source edit | 1.780 | Installation cached; compiled program printed edited |
| Dependency edit | 3.564 | pnpm reused 1 package and downloaded 1; runtime output remained edited |

Lockfile preparation took 2.944 and 2.977 seconds, outside those build timings.
The fixture is intentionally small. These results establish real package reuse
and source invalidation; they do not estimate Eichler install times or long
compiled-build performance. They do not test compiler incremental state, a
shared Turbo server, or cross-worker caches.

## Eichler service-backed loop

The service probe reuses the frozen source from pilot attempt
3fc43c6bbb14422bb7bbab010bcf0111 and its matching dependency image. It does not
modify that snapshot or any target-repository checkout. The installed dependency
gate passes in every service attempt.

A fresh execution container receives source while retaining the installed
inputs and their timestamps. Three external service containers provide Postgres,
PgBouncer, and the Neon WebSocket proxy in a per-run network namespace. No host
ports are published. No execution container receives a Docker socket. The
existing startInstance({external:true}) helper migrates the disposable database
and starts the real local API Worker. The runner invokes S0-01 and checks its
recorded write routes. All fixtures and data are simulated.

The runner is limited to 2 CPUs and 6 GiB RAM. Postgres has 768 MiB, PgBouncer
256 MiB, and the proxy 128 MiB, each capped at half a CPU. The worker has 4 CPUs
and roughly 16 GiB RAM. One heavy probe runs at a time. Source and services are
fresh for each attempt. Generated Worker secrets are not collected in logs.

| Attempt | Journey phase seconds | Outcome |
| --- | ---: | --- |
| Initial baseline | 0.089 | Evaluator setup failure: tsx resolved from repository root rather than scenarios package |
| Corrected baseline | 43.208 | S0-01 passed; services removed |
| Locally seeded failure | 4.633 | S0-01 failed with the exact seeded error; services removed |
| Local repair, fresh run | 42.810 | S0-01 passed; services removed |
| Interrupt after 8 seconds of journey execution | Interrupted | Probe failed visibly; all owned containers and network removed |

The evaluator copied S0-01 to a local temporary directory, inserted a throw at
its run entry, transferred it as an explicit override, inspected the returned
failure, removed that throw locally, and transferred the fixed file. The source
override digests are recorded in the broken and fixed result files. This is a
scripted repair of a known seeded fault, not autonomous diagnosis of a product
bug. It proves service-backed execution and changed-input behavior underneath
the future profile. It does not prove transparent pnpm journey routing or
artifact usability for Codex or Claude Opus.

Cancellation here is a caught alarm in the evaluator process followed by teardown.
It is not SIGKILL, host loss, SSH-loss recovery, or the production cancellation
protocol. The interrupted subprocess's buffered journey logs were not preserved,
which is a collection limitation of this probe. Cleanup steps were recorded.

## Evidence and implications

The scripts live under experiments/delivery, experiments/dependencies, and
experiments/services. Selected raw logs, reports, source-override digests, and
container resource summaries live under
[experiments/evidence/2026-09-19-v01](../experiments/evidence/2026-09-19-v01).
The initial failed baseline is retained alongside successful attempts.

All probe containers, dedicated builders, and private networks were removed.
Downloaded images, Buildx, and evidence remain on the authorized VM. The original
surface pilot images and evidence were preserved. No production routing changed.

The service-backed workflow is feasible with external lifecycle management.
BuildKit's package cache works for actual dependency changes. Unique artifact
publication has a viable recovery mechanism. Automatic replacement of editable
workspace paths remains a contract and agent-UX question. Before enabling it,
evaluate a generated-output build and a source-changing snapshot/codegen workflow
with both coding agents, including visible conflicts and returned diffs. The
existing four-agent validation trial does not answer that question.
