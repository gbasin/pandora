---
status: log
---

# Eichler workload inventory, 2026-09-21 UTC

This inventory informs the next Pandora adapters. It does not change routing or
expand the v0.1 contract. Eichler source was inspected at
`5fddeb6b081a72af4d690a171c9e00cc007c3c01`. Pandora currently routes journey commands,
borrower/Desk surface commands, and its supported Docker grammar. The [v0.1 contract](v0.1-contract.md) and linked VM evidence establish those
Pandora capabilities; they are not inferred merely from Eichler CI. Generic
`npm test`, `pnpm test`, `pnpm test:unit`, direct Vitest/Jest, and the other command
families below remain local unless a separate supported workflow invokes them.

## Evidence and limits

The [command catalog](eichler-command-catalog-2026-09-21.json) contains every script
from 19 tracked active package manifests: 139 script entries, including aliases
and hooks. The scan also covered the validation planner, CI workflows, native
runners, stack lifecycle, and representative capture/render tools. Legacy package
scripts and arbitrary ad hoc shell programs are outside that catalog.

Local validation receipts cover requests created from September 16 through
September 20 UTC. There were 1,064 retained requests: 1,063 classified as ordinary
Eichler work and one Pandora-named trial, which is separated. Root classification
uses paths, not a trustworthy workload tag. This cannot guarantee that every
experiment or intentional failure is excluded. Direct runner invocations, CI,
Pandora remote results, installs, and native builds bypassing this queue are not
counted here. The [sanitized aggregate](eichler-validation-receipts-2026-09-21.json)
contains counts and timing definitions without commands or environment values.

CASS was healthy and current when searched for recent pnpm, Vitest, Jest, Expo,
Xcode, Playwright, and GitHub run activity. Selected original agent logs confirm
those command families also occur outside the managed interface. The sample is
query-biased and includes diagnostic commands inspecting other commands; its
match counts are not execution counts. Raw history stays local under
`~/.local/state/pandora/workload-inventory/`.

Execution timing below is successful-receipt `finished - started`. Initial queue
wait is reported separately. Execution wall time still includes shared-machine
contention, process startup, service preparation, caches, and cleanup. These are
not uncontended benchmarks or CPU/RAM measurements. A preceding runaway process
can inflate a later command's duration. Failure counts do not establish that
resource starvation caused a failure.

## Recorded local validation durations

Ordinary Eichler requests recorded 682 successful outcomes (664 passes and 18
expectation updates), 341 failures, 39 cancellations and one request without a
result. These include development failures and preflight rejection; they are not
a reliability benchmark. Median initial queue wait was about one second, but the
90th percentile was 139 seconds and the maximum was about 69 minutes. Missing
start timestamps are excluded from queue statistics.

Only successful receipts contribute to the following durations. The count column
shows successful samples, not all submissions. Median uses the conventional
middle-pair average; p90 uses nearest rank.

| Suite | Requests | Successful samples | Median execution | P90 execution |
| --- | ---: | ---: | ---: | ---: |
| `agent-web` | 44 | 26 | 2.0 min | 2.8 min |
| `browser-integration` | 14 | 8 | 38.2 s | 54.0 s |
| `build` | 3 | 3 | 10.2 s | 11.9 s |
| `check` | 199 | 135 | 41.1 s | 2.6 min |
| `code` | 20 | 17 | 16.8 s | 2.3 min |
| `docs` | 47 | 42 | 26.1 s | 64.2 s |
| `employee-browser` | 16 | 8 | 92.5 s | 97.9 s |
| `full` | 28 | 24 | 2.9 min | 4.0 min |
| `journey` | 72 | 55 | 28.9 s | 2.8 min |
| `journeys` | 36 | 20 | 20.4 min | 28.1 min |
| `native` | 142 | 47 | 2.6 min | 5.6 min |
| `native-unit` | 129 | 99 | 22.3 s | 92.9 s |
| `node` | 17 | 11 | 5.2 s | 10.6 s |
| `postgres` | 44 | 16 | 43.5 s | 2.3 min |
| `races` | 3 | 3 | 85.1 s | 95.7 s |
| `surface` | 59 | 28 | 4.5 min | 14.4 min |
| `tools` | 19 | 18 | 8.5 s | 24.5 s |
| `typecheck` | 30 | 26 | 27.6 s | 71.5 s |
| `unit` | 141 | 96 | 43.2 s | 6.5 min |

Full versus selected tests matter more than the package manager name:

| Suite/selection | Successful samples | Median | P90 |
| --- | ---: | ---: | ---: |
| `unit`, no selectors | 20 | 6.0 min | 10.1 min |
| `unit`, nonempty selectors | 76 | 38.6 s | 2.3 min |
| `native-unit`, no selectors | 55 | 32.0 s | 109.8 s |
| `native-unit`, nonempty selectors | 44 | 4.6 s | 18.9 s |

A nonempty selector list is not proof of a small test: a project selector can
still select an entire package. The three build samples are filtered builds,
not full cold-build evidence. Cache state, revisions, scope and contention differ
between samples; the shorter `full` median than unfiltered `unit` is not a claim
that adding more tests makes a given run faster. Native failure counts likewise
do not identify simulator contention as the cause.

## Command families and routing boundaries

| Family | Entry points | Requirements and results | Placement assessment |
| --- | --- | --- | --- |
| Broad tests | `pnpm test`, `pnpm validate full`, unfiltered `pnpm test:unit` | Node tooling, Vitest, Turbo/package tests including Jest; reports and generated prerequisites | Strong next Linux adapter candidate; not covered by v0.1 |
| Focused logic/tool tests | `test:unit <selectors>`, `validate node <files>`, `validate tools`, `test:tools`, report-tool tests | Node/Vitest; package and file selection vary substantially | Keep cheap focused work local; allow explicit profile routing for known expensive subsets |
| Native JavaScript tests | `test:native-unit [agent|borrower] [selectors]`, package Jest | React Native/Jest mocks; no Xcode or simulator | Linux-capable; keep short focused tests local, evaluate full-suite cost separately |
| React race tests | `test:races [selectors]` | Vitest with scheduler delay across web-ui, Desk, borrower | Linux-capable candidate; preserve delay environment and selectors |
| Static checks | `check`, `check:docs`, `check:code`, lint, format checks, clock/parity/copy/catalog checks | Composite checks include actual tests, generated catalogs and Turbo typechecking; formatting may write files | Cheap individual checks local; broad check candidate after test adapter; formatting/source-mutating commands stay local |
| Builds and typecheck | `build --filter=...`, `typecheck`, `validate typecheck --filter=...` | Turbo, Vite, Expo exports, Worker bundles; generated catalogs, validators and dist outputs | Linux-capable except explicitly native compile; current small cached sample does not prove all builds are cheap |
| API journeys | `journey <id>`, `journeys`, optional `--update` | Isolated Postgres/PgBouncer/proxy/API, replay, reports and declared expectation return | Already supported remotely, including catalog sharding |
| Database tests | `test:postgres api [--foundation-only]`, `test:postgres scenarios` | Isolated DB stack, migrations, API; owned service teardown | Good Linux candidate for resource isolation despite some short tests; needs an adapter |
| Fixture surfaces | `test:surface borrower-web|desk [selectors]` | Production/fixture builds, Playwright/Chromium, screenshots and builds | Already supported remotely with internal sharding |
| Live browser integration | `test:browser-integration` | DB/API plus built borrower and Desk pages, private servers, browser evidence | Linux-capable, needs owned service lifecycle and artifact adapter |
| Realtor/employee browser | `validate agent-web`, `test:employee-browser` | Expo web export, Playwright, configured origins; employee runner has API bridge and no Docker | Linux-capable; separate adapters, not aliases for borrower/Desk fixture surfaces |
| Mockup browser/capture | `test:mockup-browser`, mockup `shots`, browser walkthroughs | Chromium, catalogs, before/after images and reports | Linux-capable; declare output paths and isolate servers |
| Progress capture | progress `capture`, `assets`, `build`, `test:e2e`; agent `capture:spine` | Many fixture screenshots, generated assets, dashboard build/browser checks | Expensive Linux candidate; preserve complete inventory/artifacts; not normal unit testing |
| PDF/site generation | static-docs `build`/`print`, deck print/build scripts, web/brand-tour builds | Browser print, font assets; CI deck pipeline adds Poppler/WebP | Linux-capable; explicit generated output return; review remains local |
| Film generation | film `prep`, `capture`, `render`, `site` | Playwright capture against API, Remotion video render, timestamped cuts; renderer reads Git commit metadata | Likely expensive Linux candidate, not benchmarked here; warm dependencies, source metadata and large artifacts need a profile |
| Interactive servers | `dev:stack`, Expo Metro/dev client, Vite previews, Remotion studio | Long-lived ports, hot reload, interactive clients and service leases | Keep managed locally for now; remote previews/tunnels are a different lifecycle from finite validation |
| Dependency preparation | `pnpm install --frozen-lockfile`, browser installation, Expo doctor; native CocoaPods preparation | Per-worktree dependencies plus machine caches; installs write node_modules and machine/cache state | Local bootstrap remains necessary for local agents; serialize/bound it separately; remote warm images do not populate local node_modules |
| Native iOS build | app `ios`, `agent:sim`, `expo run:ios`, Xcode/EAS iOS builds | Xcode/CocoaPods, simulator/device binaries, signing where needed | macOS worker; not the current Linux backend |
| Native iOS UI tests | `test:ios --device ... --bundle-sha256 ... <flows>` | Reviewed installed build, device and heavy leases, Maestro/Java, simctl, screenshots and crash diagnostics | macOS worker; keep current managed local runner or implement a separate remote Mac backend |
| Deploy/publish/migrate | app deploy scripts, `publish-cuts`, staging/production workflows, DB commands | Credentials and external mutations; some commands combine build and publish | Keep explicit operations; never silently treat them as replayable validation |
| Agent sessions and local infrastructure | Codex/Claude, Agentboard/tmux controllers, Pueue, CASS indexing, Docker Desktop | Long-lived local processes; twelve agents and simultaneous dependency installs still consume RAM even when tests run remotely | Track separately from test duration; remote validation does not eliminate this baseline or fix runaway shell polling |
| Repository automation | `gh` checks/logs, project/report/audit/reconciler tools and scheduled workflows | GitHub/API/LLM calls, sometimes writes/issues/deployments | Mostly I/O and authority-sensitive, not the main Mac compute-offload target |

Package scripts are not the full resource contract. For example, `pnpm test` is
broad unit/tooling validation and excludes journeys, PostgreSQL, race mode,
browser integration/surfaces and simulator flows. `pnpm check` includes tests and
code generation even though its name sounds static. Direct package commands can
bypass Pueue and Pandora.

## Existing scheduling and CI

The current validation planner uses Pueue light slots (two) for units, static
checks, builds and typechecking; heavy (one) for database/live-browser/native
work; surfaces (two) for fixture browsers; and a separate development-stack slot.
Those limits add together and are not a machine-wide RAM budget. Worker count
limits also multiply within jobs. The prose local-validation note has a stale
sentence saying `check` uses heavy; the inspected planner places it in light.

The merge regression has four journey shards and four surface matrix jobs
(two Desk plus two borrower), alongside independent code, PostgreSQL, race,
browser integration, employee-browser, mockup and documentation jobs. All runners in the core `ci.yml` regression workflow are Ubuntu. Separate audit
isolation workflows also include a self-hosted runner. There is no actual iOS simulator
CI job; Linux unit coverage of native source is not simulator evidence.

Progress fixture capture is now nightly. Workflow comments record a historical
33.9-minute capture phase within a 36-minute run, and the current timeout is
90 minutes. That is historical workflow evidence, not a fresh benchmark. The
source describes 1,328 fixture states. Video renders, full cold builds and many
unmanaged captures have no representative duration in these queue receipts.

Other scheduled work includes ten-minute progress refresh, reports/healing,
project audits, PR model reviews and weekly sweeps. Deploy workflows print PDFs,
build sites, migrate staging and publish Workers. Moving those GitHub jobs to
owned runners is a separate execution/credential decision; adding Pandora agent
adapters does not automatically reduce their CI bills.

## Expo and the Mac boundary

Eichler's agent `build` script runs `expo export --platform ios` and an export for
web. Those bundle JavaScript/assets; they are not Xcode compilation. Jest tests,
TypeScript checks, Metro bundling/exports and Expo web browser tests can run on
Linux. The repository already uses Ubuntu for its Expo web/browser work.
[Expo CLI explains export semantics](https://docs.expo.dev/more/expo-cli/#exporting).

`expo run:ios`, native iOS builds and iOS simulator execution require macOS/Xcode.
The native validation runner explicitly rejects non-Darwin hosts. It tests a
reviewed build already installed on a named simulator; it does not build or
install that app. A future remote adapter must move build identity, simulator
ownership, test evidence and cleanup together. Copying the current Linux
container wrapper to a Mac is not sufficient.
[Expo documents the macOS-only simulator](https://docs.expo.dev/workflow/ios-simulator/).

A cloud Mac is optional. The existing dedicated Mac or a second physical Mac
could perform this role. EAS can compile an iOS build remotely, but a completed
build alone does not execute this repository's Maestro suite. Hosted iOS builders
also use macOS; Android builders use Linux. Android native support is not present
in the inspected Eichler apps: their configurations specify iOS, and the borrower
pilot uses web for Android households.
[Expo build infrastructure](https://docs.expo.dev/build-reference/infrastructure/),
[local/custom-infrastructure builds](https://docs.expo.dev/build-reference/local-builds/).

## Adapter priorities

First add a finite Node-test profile for broad `test:unit`, `test`/`validate full`,
and selected expensive suites. Preserve file/test selectors, report absence and
exit status, cap test workers, and keep tests awaiting one result. Make routing
predictable from the command/profile; do not require the agent to guess current
RAM or select a server. The command may also invoke generated prerequisites:
validate those outputs instead of assuming every unit suite is read-only.

Next add realtor/employee browser and PostgreSQL/live-browser profiles. These
need explicit owned services, browser dependencies and artifact declarations.
The existing borrower/Desk adapter does not establish compatibility for them.
Progress/film/PDF capture profiles can follow according to actual usage and
artifact sizes. Separate render-only work from deploy/publish operations.

Keep cheap focused Jest/Node checks local. Broad static checks are worth measuring
on the remote worker, but their current median does not alone prove a latency
win after capture, transfer and queueing. Retained elapsed times do not justify
an automatic universal time threshold.

Continue using the current Mac's managed native runner while Linux offload removes
other load. A second Mac worker becomes useful if measured native contention
remains. It is a new execution backend and multi-worker scheduling scope, not a
missing toggle in v0.1. No new worker or routing rule was installed by this audit.

## Source references

- Eichler `package.json`, package manifests, and `turbo.json`.
- `tools/validation/plan.mjs`, `heavy.mjs`, `surface.mjs`, and `tools/notes/local-validation.md`.
- `tools/stack/instance.mjs`, `tools/browser-integration/run.mjs`, and `tools/employee-browser/run.mjs`.
- `.github/workflows/ci.yml`, `progress-nightly.yml`, deploy and automation workflows.
- `apps/agent/package.json`, `app.config.ts`, `tools/test-ios.mjs`, native evidence notes and README.
- `apps/borrower/package.json`, `app.config.ts`, README and EAS configuration.
- `apps/film/tools/render.mjs`, `capture/run.ts`, and static-docs/print tools.

All repository references above bind to the inspected Eichler commit, not a claim
that later main revisions have unchanged command semantics.
