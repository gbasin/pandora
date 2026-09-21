# 01 — Fast CI runners and remote execution: competitive landscape for Pandora

Research date: 2026-09-20. Sources are primary (vendor docs, engineering blogs, pricing pages,
GitHub APIs) unless marked. Vendor performance and savings claims are attributed, not asserted.
Gaps are marked **not found** rather than filled with inference.

Raw per-vendor notes: `raw-a.md` (Blacksmith/Depot/Namespace/WarpBuild/BuildJet), `raw-b.md`
(Ubicloud/RunsOn/Actuated/Buildkite/CircleCI), `raw-c.md` (Earthly/Dagger/crabbox/sandbox
scouting), `raw-d.md` (BuildBuddy/EngFlow/NativeLink + REAPI primer), `raw-e.md`
(Nx/Turborepo/Develocity/test-intelligence).

**Method caveat worth stating up front:** the session's web-search budget (200 queries) was
exhausted partway through. A handful of items rest on search-engine synthesis rather than a
directly fetched primary page; those are flagged inline. Nothing in the "lessons to steal"
section depends on an unflagged claim.

---

## Part 1 — Per-vendor

### Tier A: GitHub Actions replacement runners (own metal + microVMs)

#### Blacksmith (blacksmith.sh)

The most architecturally transparent competitor, and the only vendor in the entire survey that
published its own margin curve.

1. **Isolation / substrate.** Firecracker microVMs, one per job, KVM hardware isolation, cgroup
   hard limits on memory/CPU-quota/disk-IO per VM, nftables packet filtering per VM
   ([arpitbhayani.me writeup](https://arpitbhayani.me/videos/blacksmith-github-actions-internals-and-architecture)).
   Substrate is **owned bare metal**, not a hyperscaler: AMD Ryzen 7950X desktop/gaming CPUs
   chosen for single-core boost clock, local NVMe, "500+ hosts across several data centers"
   ([blacksmith.sh/blog/cache](https://www.blacksmith.sh/blog/cache),
   [economics post](https://www.blacksmith.sh/blog/the-economics-of-operating-a-ci-cloud)). The
   Aug 2026 us-west outage was a *facility cooling loss*, so it is colocation, not owned
   buildings.
2. **Cache.** "Sticky Disks": persistent ext4 NVMe volumes in a **self-hosted Ceph cluster on
   local NVMe**, shared org/repo-wide across runners. Every job gets a **copy-on-write clone**;
   writes are promoted to the new committed snapshot **only if the job exits 0**
   ([docs](https://docs.blacksmith.sh/blacksmith-caching/dependencies-sticky-disks),
   [github.com/useblacksmith/stickydisk](https://github.com/useblacksmith/stickydisk)).
   Separately they reverse-engineered the GitHub Actions cache protocol (which targets Azure Blob)
   with a host-level Go proxy translating to self-hosted **MinIO**, plus an in-VM nginx proxy, so
   the cache is physically colocated with compute.
   **Numbers:** GitHub's cache averaged 49.8 MB/s (peak 54.7) on a 114 MB cache, ~8s; Blacksmith's
   colocated cache hit 327.5 MB/s, <1s — a measured ~6.5x ([blog/cache](https://www.blacksmith.sh/blog/cache)).
   A second source quotes ~400 vs ~100 MB/s (4x). Treat as two approximate measurements, not one
   authoritative figure.
3. **Pricing / economics.** $0.004/min for x64 2vCPU, scaling with vCPU; ARM at 0.625x; 3,000 free
   min/month. The [economics post](https://www.blacksmith.sh/blog/the-economics-of-operating-a-ci-cloud)
   is the single most useful commercial document in this whole survey: **10% fleet utilization ≈
   35% gross margin; 20% ≈ 70%; 35%+ ≈ 85%+**. Their explanation is statistical multiplexing —
   many customers' bursty 5–40 minute jobs aggregate into a Poisson-smoothed curve — plus temporal
   arbitrage (weekend volume ~1/5 of weekday; early-UTC hours idle).
4. **Spot / oversubscription.** No spot tier published. Oversubscription ratio **not disclosed**;
   cgroup hard ceilings are described, which implies they are *not* oversubscribing memory.
5. **Beyond speed.** Test Analytics auto-parses JUnit XML and posts failing tests as PR comments
   ([docs](https://docs.blacksmith.sh/blacksmith-observability/test-analytics)) — but a
   Blacksmith-owned comparison property self-reports **no test splitting and no automated flaky
   detection/auto-rerun**. **Blacksmith Sandboxes** (full VMs for coding agents, explicitly
   pitched as "mirror your CI environment") are announced but **"coming soon," no pricing**
   ([blacksmith.sh/sandboxes](https://www.blacksmith.sh/sandboxes)). **[code]smith** is a
   background coding agent with a "Muscle Memory" cache that memoizes inferred tool-result
   *shapes* (field names/types, not values) across sessions to cut context/token cost
   ([blog](https://www.blacksmith.sh/blog/code-smith-code-mode)) — conceptually adjacent to our
   "save agent tokens" thesis but at the tool-call layer, not the test layer.
6. **Uncommitted source.** **Not found.** SSH-into-a-running-Actions-job exists but operates on
   the already-pushed commit.
7. **Lessons.** Two 2026 postmortems (July 21 control-plane degradation, including sticky-disk
   request failures; Aug 13 regional cooling loss). SOC2 Type 1. No cross-tenant or
   cache-poisoning incident found. Series B led by Peak XV, Aug 2026 (amount **not verified**).

#### Depot (depot.dev)

The most aggressive AI-agent positioning of any runner vendor, and the most conservative
isolation model.

1. **Isolation / substrate.** Classic Depot Docker builds run in **single-tenant EC2 instances**
   — "your build is running in your own project's EC2 instance" — destroyed after use, with a
   standby pool of warm stopped instances cutting provisioning from 40s–5min to **2–3s**
   ([depot magic explained](https://depot.dev/blog/depot-magic-explained)). **Depot Metal** adds
   bare-metal EC2 (5th-gen EPYC, 1:1 vCPU-to-core) running "a microVM hypervisor" — Depot wrote a
   whole post comparing QEMU microvm vs Cloud Hypervisor and then **declined to say which they
   chose** ([post](https://depot.dev/blog/differences-between-qemu-and-cloud-hypervisor)). Their
   hypervisor choice is genuinely **unverified**. Substrate is squarely AWS.
2. **Cache.** Docker layer cache on a **Ceph cluster over NVMe SSDs, colocated in the same AZ** as
   the builder; per-project, per-architecture volumes, 50 GB default, $0.20/GB/month beyond.
   **Numbers:** migrating EBS → Ceph-on-NVMe raised write throughput **140 MB/s → 900 MB/s (>6x)**
   ([same post](https://depot.dev/blog/depot-magic-explained)). Depot Metal layers a three-tier
   model: dedicated storage instances with local NVMe, **NVMe-oF/TCP** between compute and
   storage tiers, S3 as the durable base for VM images and snapshots, plus host-memory caching on
   both sides; claimed "~30% faster" ([announcement](https://depot.dev/blog/announcing-depot-metal)).
3. **Pricing.** Per-second: $0.0001/sec (2vCPU/8GB) to $0.0032/sec (64vCPU/256GB). Docker builds
   $0.04/min; GHA-compatible jobs $0.006/min; cache and registry storage both $0.20/GB/mo; agent
   sandboxes $0.01/min ([pricing](https://depot.dev/pricing)). No utilization/margin statements
   **found**. $10M Series A (Felicis/YC/Pioneer, ~Mar 2026).
4. **Spot / oversubscription.** **Not found**, and largely not applicable given one-EC2-per-build.
5. **Beyond speed.** No test analytics or flaky detection. But: **Depot Remote Agent Sandboxes**
   are shipped, not "coming soon" — isolated containers, 2vCPU/4GB, **~5s launch**, persistent
   filesystem so a resumed session picks up where it left off, native git (checkout/commit/PR),
   **Claude Code only** today, async-only
   ([announcement](https://depot.dev/blog/now-available-remote-agent-sandboxes)). `depot claude`
   launches a remote sandbox by default; `--local` keeps the session local but still uses Depot's
   cache. Plus **Depot Code**, a diskless git server storing packfiles in S3. CEO Kyle Galbraith
   has publicly argued GitHub's PR/review workflow **cannot scale to agent-generated code volume**
   and is pitching composable primitives to bypass it ([secondary source](https://runtimewire.com/article/depot-github-pull-request-model-coding-agents)).
6. **Uncommitted source.** Strongest of the runner vendors. `depot build` routes a **laptop's**
   Docker build to remote builders sharing the *same* team cache as CI
   ([docs](https://depot.dev/docs/container-builds/how-to-guides/local-development)), and **Depot
   Cache** is a generic remote cache backend for Bazel, Go, Gradle, Turborepo, sccache and Pants,
   usable from dev machines ([docs](https://depot.dev/docs/cache/overview)).
7. **Lessons.** No published incidents or security postmortems **found**. Single-tenant-EC2
   architecturally reduces cross-tenant compute risk at the cost of warm-pool density.

#### Namespace (namespace.so)

Best published thinking on cache *placement*, and the single most useful security-vs-warmth
postmortem in the survey.

1. **Isolation / substrate.** Proprietary in-house hypervisor (technology **not named** — do not
   assume Firecracker) plus their own orchestration; they explicitly **moved off AWS and Equinix
   Metal** to their own colocated bare metal, saying AWS unit economics "didn't quite work out"
   ([so you want to build your own datacenter](https://namespace.so/blog/so-you-want-to-build-your-own-datacenter)).
   Desktop-class high-clock EPYC chosen because "compilation has sequential critical paths," plus
   Apple Silicon. Custom Clos network, 25/50/100 Gbps, owned IP ranges. **No elasticity: capacity
   planned months ahead, racks planned around power budget.**
2. **Cache.** "Cache Volumes" are **topology-aware snapshots**: the scheduler tracks which cache
   revisions live on which physical node and **routes the job to the node that already has it**,
   so there is no download/extract step at all ([zero-latency caching](https://namespace.so/blog/zero-latency-caching)).
   Versioning is **last-write-wins with per-job forking**: each job gets a private clone of the
   last successfully-committed version and only promotes on exit 0
   ([docs](https://namespace.so/docs/architecture/storage/cache-volumes)). They will serve a
   *stale* version rather than pay a fetch — an explicit staleness-for-latency trade.
   **Hard numbers: none published.** Their own post defers mechanics to "a future blog post."
3. **Pricing.** Unit-minutes = vCPU × minutes × platform multiplier (Linux 1x, Windows 2x, macOS
   10x, Linux-on-Apple-Silicon 7x). Linux 4vCPU/8GB = $0.004/min prepaid. Cache billed on two
   axes: **snapshot usage $0.002/GB-hr while attached** plus **storage $0.0048/GB-day at rest**
   ([pricing](https://namespace.so/pricing)). Devboxes $0.004/Devbox-min × size multiplier. $23M
   seed+Series A, NEA-led. No margin/utilization statements **found**.
4. **Spot / oversubscription.** **Not found.** The "plan hardware months ahead" model argues
   against aggressive oversubscription, but this is inference.
5. **Beyond speed.** No flaky detection or test analytics **found**. **Devboxes** are persistent
   managed dev environments "for both developers and AI coding agents," auto-start on demand,
   auto-pause when idle, wired into Cache Volumes and remote builders
   ([blog](https://namespace.so/blog/introducing-devboxes)). Namespace is a **Cognition/Devin
   "Outposts" launch partner** — Devin's agent loop stays in Cognition's cloud, its *execution*
   runs on a Namespace Devbox; brain vs hands, split across companies
   ([blog](https://namespace.so/blog/devin-outposts-devboxes)). The **Warp.dev partnership
   writeup** is the best published multi-tenant-security-for-agents design found anywhere:
   one Namespace tenant per Warp team with zero shared resources or network, per-sandbox
   isolation within a tenant, **short-lived scoped credentials** and GitHub tokens limited to the
   triggering user's own permissions with changes attributed back to that user, a shared sidecar
   cache volume holding codebase-context indices across an agent's sandboxes, and Warp's own
   tooling mounted under `agent/` so it updates independently of the user's base image
   ([warp.dev/blog](https://www.warp.dev/blog/secure-cloud-sandboxes-for-ai-dev-with-namespace)).
6. **Uncommitted source.** Devboxes hold working (possibly uncommitted) state persistently. No
   separate laptop-reachable remote cache product **found**, unlike Depot.
7. **Lessons.** The Oct 22–23 postmortem ([slowdown-oct22](https://namespace.so/blog/slowdown-oct22),
   year ambiguous in the fetched page) documents: (a) a third-party bare-metal provider's vSwitch
   dropping packets for days before detection, (b) a K8s control-plane node going I/O-bound on
   older NVMe under pod-creation rate, cascading to high CPU, and (c) **the key one** — recovery
   required "repaving" hosts under an immutable-infrastructure model, rebuilding with new
   identities, new TLS certs and new encryption-at-rest keys, which sped recovery but
   **deliberately sacrificed regional cache warmth**. That is a named, first-party statement of
   the exact tension Pandora will live inside.

#### WarpBuild (warpbuild.com)

1–2. Docs say only "ephemeral VMs," no hypervisor named ([docs](https://www.warpbuild.com/docs/ci/cloud-runners)).
Linux ARM64 runs as a VM on Apple M4 Pro hardware via Apple's Virtualization framework; macOS/ARM
runners have no nested virt and **cannot run Docker**. BYOC mode deploys into the customer's own
AWS/GCP/Azure ([blog](https://www.warpbuild.com/blog/launch-byoc)). Cache architecture is
undisclosed; pricing implies the shape (Cache storage $0.20/GB-mo, cache ops $0.0001/op, **snapshot
restore $0.04/job**, snapshot storage $0.025/hr) ([pricing](https://www.warpbuild.com/pricing)).
3. $0.004–$0.064/min Linux x86; ARM ~25% cheaper; **BYOC flat $0.002/min** with caching included —
cheaper than their managed tier, because the customer's cloud bill absorbs the compute.
4. **The most useful negative signal in the survey: WarpBuild discontinued its managed spot runner
   tier entirely on June 8, 2026**, pushing spot capability into BYOC only. Somebody tried
   preemptible managed CI and pulled it.
5–7. No agent product, no test analytics, no local-source product, no postmortems — **all not
   found**. No funding round since a 2021 pre-seed **found**.

#### BuildJet (buildjet.com) — dead

**Shut down March 31, 2026** (announced Feb 6, 2026), no acquisition. Stated reason: GitHub's own
runner improvements (faster hardware, larger sizes, native ARM) closed the gap BuildJet existed to
fill ([shutdown post](https://buildjet.com/for-github-actions/blog/we-are-shutting-down)).
Historically KVM-based isolation (a third-party Firecracker claim is **unverified**), colocated
cache, 20 GB free cache per repo per week, $0.004–$0.064/min. No agent features, no analytics.
Its value to us is purely as evidence: **a pure price/perf runner play has a floor that the
platform owner can raise under you.** Note the adjacent facts — GitHub cut hosted-runner prices by
**up to 39% effective January 2026** ([GitHub changelog](https://github.blog/changelog/2025-12-16-coming-soon-simpler-pricing-and-a-better-experience-for-github-actions/)),
and announced then reversed a plan to bill self-hosted runner minutes
([The Register, Dec 2025](https://www.theregister.com/2025/12/17/github_charge_dev_own_hardware/)).

---

### Tier B: BYO-substrate and incumbent CI

#### Ubicloud (ubicloud.com)

1. **Cloud Hypervisor** (not Firecracker) VMs inside Linux namespaces, run as an unprivileged user
   under systemd sandboxing. Their own comparison post explains the choice — CH supports Windows
   guests, PCI passthrough, vhost-user and hugepages, which Firecracker deliberately omits — and
   **self-discloses that seccomp-bpf, though present in CH, is not actively filtered by Ubicloud**
   ([post](https://www.ubicloud.com/blog/cloud-virtualization-red-hat-aws-firecracker-and-ubicloud-internals)).
   Storage on **SPDK**, currently **non-replicated**. Substrate: leased **Hetzner** bare metal
   (plus Leaseweb and AWS bare metal); ARM runners on Hetzner RX220-series
   ([github.com/ubicloud/ubicloud](https://github.com/ubicloud/ubicloud)).
2. Transparent GitHub Actions cache proxy: 30 GB free standard, **100 GB premium** vs GitHub's 10.
   Claim: "**4x** download, **3x** upload" improvement, with uploads explicitly still lagging
   ([cache docs](https://www.ubicloud.com/docs/github-actions-integration/ubicloud-cache)).
   Fresh ephemeral VM per job; no host-level state reuse — which is *why* the proxy is needed.
3. Standard x64 2vCPU **$0.00125/min** vs GitHub's $0.006; 16vCPU $0.01/min vs $0.042
   ([pricing](https://www.ubicloud.com/docs/about/pricing)). A **26% price adjustment** rolled out
   through June 2026. No margin/utilization disclosure **found**.
4. No spot tier **found**. Relevant adjacent data point: Ubicloud's managed-Postgres work
   documents running **strict memory overcommit**, hitting a Linux kernel bug that produced
   spurious OOM errors from committed-memory miscalculation, and moving new databases to a "new
   strict memory overcommit" mode to eliminate them (URL not captured — **flagged**).
5–6. No test analytics, no flaky detection, no agent product, no local-source product — **not found**.
7. Ubicloud explicitly documents the **cross-branch cache-poisoning risk** and defaults to
   branch-scoped cache isolation, with a cautioned opt-out toggle. That is one of only two vendors
   in the entire survey with a first-party statement on cache poisoning.

#### RunsOn (runs-on.com)

Structurally the most different business model here: **it runs entirely in the customer's own AWS
account.** A CloudFormation stack creates VPC, S3 cache bucket, IAM roles and a **Fargate**
scheduler; control plane, runners, cache and GitHub credentials never leave the customer's account
([README](https://github.com/runs-on/runs-on)). One dedicated ephemeral EC2 instance per job;
isolation is stock AWS Nitro, not a RunsOn-operated VMM. Nested virt supported (KVM Linux, Hyper-V
Windows).

Cache: S3-backed `actions/cache` replacement in-VPC, Docker pull-through cache, EFS, or **EBS
sticky disks** — requested per-job via a label like `sticky=docker:20gb:gp3:750mbs:6000iops`;
RunsOn restores the latest compatible **EBS snapshot**, mounts it natively (no tar/untar), then
unmounts and re-snapshots ([sticky disks blog](https://runs-on.com/blog/introducing-sticky-disks/)).
v3.2 added telemetry putting EBS volume/snapshot **cost** next to queue time and Spot interruptions
— an explicit admission that warm caching is not free. No hard throughput numbers **found**.

**Pricing is a flat annual licence** — €300/€900/€1,800/€3,600 per year by tier, free for
non-commercial — with **compute billed by AWS at cost and no per-minute markup**. Headline claims:
7–12x cheaper than GitHub-hosted, ~$0.23/hr vs $2.52/hr for a 16-vCPU equivalent; self-reported
scale of "3 million jobs daily across 900 companies, ~2% of all GitHub Actions runs" (marketing,
uncorroborated). Runner start in "**less than 30s**."

**Spot is default-on with automatic on-demand fallback (a 2–3 second penalty), automatic retry on
interruption, and an optional circuit breaker** — the cleanest published preemptible design in the
survey, and it works precisely because the customer owns the capacity. No test analytics, no agent
features, no local-source features, no postmortems — **not found**.

#### Actuated (actuated.com, Alex Ellis)

Firecracker microVM per job, **1–2 second boot including the Docker daemon**, full root inside the
guest with no host access; **customer brings the hardware** (bare metal or nested-virt-capable
hosts; they recommend Hetzner RX at ~€200–250/mo, and note Equinix Metal **shut down June 30,
2026**). Flat-rate subscription, unlimited build minutes, from ~$250/month — no per-minute billing
at all. Docker layer cache local to the host plus optional S3-backed cache; no numbers published.

No agent, analytics or local-source features. But **Actuated has by far the best published
operational lessons** for anyone dispatching jobs from GitHub
([managing GitHub Actions and Firecracker](https://actuated.com/blog/managing-github-actions)),
based on 20,000+ VMs and 60,000+ webhook events:

- GitHub Actions **webhooks arrive out of order or are dropped**; jobs sit "queued" or
  "in_progress" with no corresponding event. Reconciliation logic is mandatory.
- **GitHub gives no way to bind a specific runner VM to a specific queued job**, so they built a
  "reaper" to GC idle VMs; GitHub sometimes reports an actively-running runner as idle.
- **Sub-second VM boot does not produce sub-second job start** — GitHub's own API latency and
  queuing dominates, especially during GitHub outages ("an outage almost every week").
- Replicating GitHub's hosted-runner base image is a long tail; they took an 80/20 approach and
  patched gaps surfaced by pilots (`unzip`, `libpq`).
- **Docker Hub rate limits** hit self-hosted runners because they lack GitHub's Docker Inc.
  exemption; mitigation is registry mirroring + host-local image cache.
- SSH into the microVM was essential for real debugging (port 53 blocked on Hetzner causing
  intermittent DNS failures; Chrome dependency issues).

#### Buildkite (buildkite.com)

1. Linux hosted agents: "multi-tenant architecture, where each job runs in a completely isolated
   virtualized environment," destroyed afterwards — **hypervisor not named**. macOS agents use
   Apple's Virtualization framework on Apple Silicon. Substrate: "multiple Tier 3+ data centers,"
   provider **not named** ([Linux hosted agents docs](https://buildkite.com/docs/pipelines/hosted-agents/linux)).
   12 shapes, 2vCPU/4GB to 64vCPU/256GB; jobs up to 8 hours (Linux) / 4 hours (macOS).
2. Persistent "cache volumes" survive job destruction; storage technology, limits and latency all
   **not disclosed**.
3. Pro $30/active user/mo; Linux hosted agents **$0.004/vCPU-min**, Mac $0.02/vCPU-min, metered to
   the second; self-hosted agents $3.50/agent/mo beyond 10. **Test Engine is priced separately at
   $15 per million test executions** over 1M/month included, with an unusual **P90 billing method
   that discards the top 10% of daily usage** ([pricing](https://buildkite.com/pricing/)).
4. No spot or oversubscription info **found**.
5. **Test Engine is the most complete test-intelligence product attached to a CI vendor here.**
   Flaky detection default: a test is flaky when it produces **both a pass and a fail on the same
   commit SHA**, within one build or across builds ([flaky tests docs](https://buildkite.com/docs/pipelines/configure/tests/flaky-tests)).
   Three monitor types: transition count (works without retries, resilient to infra-caused
   failures), passed-on-retry, and probabilistic. Remediation is **quarantine, not auto-rerun**:
   muted tests keep running and keep producing data so the system can detect recovery, but stop
   failing the build. **Workflows** automate detect → quarantine → notify Slack/webhook → file a
   Linear ticket ([blog](https://buildkite.com/resources/blog/introducing-test-engine-workflows/)).
   Test splitting uses historical timing with a documented 10-min → ~4-min rebalancing example,
   and Test Analytics auto-traces the slowest SQL queries and HTTP calls per test.
6. **Preflight** is the closest published analogue to part of Pandora's pitch, and it is
   **explicitly agent-facing, experimental**, and one of four "Agentic Workflows" primitives
   alongside the Buildkite **MCP Server**, **Model Providers** and **Pipeline Triggers**
   ([agentic workflows](https://buildkite.com/platform/agentic-workflows/)). It stops the pipeline
   **the instant a test fails** so an agent gets a structured single-failure signal instead of a
   whole-build wait; it prioritizes relevant tests by reusing caches; it delivers "structured
   results **before you commit**"; and it captures changes to a **temporary branch cleaned up
   afterward**. The MCP server converts and caches build logs into **Apache Parquet** so agents can
   query rather than ingest raw API payloads — an explicit token-reduction design.
7. Isolation claims only; no postmortem, cache-poisoning or OOM write-up **found**.

#### CircleCI (circleci.com)

1. Two cloud execution environments — Docker executor (shared kernel) and machine executor
   (dedicated VM) — an explicit customer-facing density-vs-isolation dial. Self-hosted: container
   runner on the customer's Kubernetes, or a **Machine Runner Orchestrator using KubeVirt** to
   give a full VM per job ([runner overview](https://circleci.com/docs/guides/execution-runner/runner-overview/)).
   CircleCI's own cloud substrate **not confirmed**.
2. Docker Layer Caching caches layers inside the remote-Docker VM. Two constraints matter:
   **sibling jobs in the same workflow run generally cannot read a cache another job just wrote**
   (reuse is across workflow runs), and enabling DLC makes remote-Docker VMs **exclusive** to the
   requesting job ([DLC docs](https://circleci.com/docs/guides/optimize/docker-layer-caching/)).
   Reported 15 GiB per org, 3-day unused expiry (support-article summary, **re-verify**). DLC is
   billed a **flat 200 credits per job regardless of hit rate**.
3. Credit-based, 1 credit = $0.0006; medium Docker ≈ 10 credits/min = $0.006/min — i.e. the same
   headline rate as GitHub. Network/storage overage at 420 credits/GB. No margin statements **found**.
4. No spot/oversubscription info **found**.
5. Test splitting: `circleci tests split --split-by=timings` uses real historical per-file
   durations. **Known distortion worth stealing the awareness of:** retried flaky tests multiply
   their recorded duration in JUnit XML, so a test retried 3x reports ~3x its real runtime and
   silently misweights the split toward flaky rather than genuinely slow tests
   ([writeup](https://dev.to/mukesh_13/smarter-test-splitting-in-circleci-balancing-parallel-containers-with-real-timing-data-instead-of-p7g)).
   Flaky detection: failed **and** passed on the same commit **within a 14-day window**
   ([Test Insights docs](https://circleci.com/docs/guides/insights/insights-tests/)).
   **Chunk** (launched Sept 2025) is an always-on agent at the pipeline layer that fixes flaky
   tests on a schedule and keeps dependencies current; since June 2026 it commits changes and
   opens PRs during a run and reruns pipelines that fail on transient errors.
   **Chunk Sidecars / "Microbuilds"** (~May–June 2026) are **remote Linux microVMs that let a
   coding agent run tests/lint/validation before code is committed or pushed**, firing
   automatically at the agent's natural pause points. CircleCI claims an average **27-second
   microbuild** and "78% faster feedback than a full pipeline"
   ([InfoQ](https://www.infoq.com/news/2026/06/circleci-chunk-sidecars/)), while chunk.ai claims
   "30x faster than your CI pipeline, 5x fewer tokens, 30x more compute-efficient"
   ([chunk.ai](https://chunk.ai/)). **Those two "faster" figures are mutually inconsistent; treat
   both as marketing.** Sidecars are currently **free on every plan**, with a note that they will
   say before charging. There is also a CircleCI MCP server whose `find_flaky_tests` tool chains
   into an in-IDE assistant ([May 2025](https://circleci.com/blog/fix-flaky-tests-with-ai/)).
6. `circleci local execute --skip-checkout` mounts the **actual working tree including uncommitted
   changes** into the executor — but the machine executor is unavailable locally and
   `save_cache`/`restore_cache` are **skipped with a warning**, i.e. no caching locally
   ([docs](https://circleci.com/docs/how-to-use-the-circleci-local-cli)).
7. No postmortems or cache-poisoning writeups **found**.

---

### Tier C: build tools that tried to sell remote execution

#### Earthly — three retreats, and the most important cautionary tale in this file

1. **Sept 12 → Oct 1, 2023: Earthly CI shut down.** Founder Vlad Ionescu's post-mortem
   ([we built the fastest CI in the world, it failed](https://earthly.dev/blog/shutting-down-earthly-ci/))
   gives four reasons: prospects treated CI as an interchangeable commodity and balked at
   migration cost; the installed base wanted *consistent* builds while Earthly CI sold *fast*
   builds; **their own Satellites product already delivered ~95% of the value, cannibalizing the
   new offering**; and an A/B test where replacing "CI" with "build" on the marketing site
   doubled conversions. Quote: *"Failing to create enough meaningful initial traction with an
   MVP… there would be a group of people tolerating the absence of features for the benefits.
   But that's not happening."*
2. **Apr 16 → Jul 16, 2025: Earthly Cloud and all Satellites shut down**
   ([a message about Earthly](https://earthly.dev/blog/shutting-down-earthfiles-cloud/)). Cloud,
   self-hosted and BYOC satellites, cloud secrets and cloud logs all discontinued. Stated reason
   one: *"translating that adoption into revenue has been much harder than we anticipated"* —
   grassroots bottom-up adoption in large orgs did not convert into company-wide paid deployments.
   Stated reason two, **again**: the free OSS tool cannibalized the paid tier, because prospects
   compared Earthly's paid offering **against Earthly's own free offering** rather than against
   their existing CI spend. OSS `earthly` went maintenance-only, no PRs accepted; GitHub API
   confirms last push **2025-10-23**, 12,048 stars, not archived. The community fork **EarthBuild**
   (created 2025-06-11, still active) has **178 stars** — ~1.5% of the original's traction.
   Departing customers were pointed at **Dagger**, who offered a free year of Dagger Cloud Team.
3. **2025–2026: pivot to Earthly Lunar**, an AI-era *guardrails/governance* product that converts
   wikis, AGENTS.md and postmortem action items into automated PR/deploy checks
   ([earthly.dev](https://earthly.dev/)). Note the direction: not agent *runtime*, agent
   *governance*. No dated launch post or explicit pivot rationale **found**.

Satellites' architecture while alive: managed or self-hosted BuildKit runners, isolated
containers, **cache persisted on the satellite instance itself** — "the same cache is used between
runs on the same satellite." **No published mechanism for moving warm cache between satellite
hosts was found**, which is a meaningful architectural contrast with Namespace/Blacksmith/Depot.
Pricing was $49/mo per satellite, ~$100–200/mo typical, $500+/mo enterprise, 6,000 free
build-minutes/mo (historical, from a cached pricing page). No margin or utilization disclosure, no
engineering postmortems — **not found**.

#### Dagger (dagger.io)

Custom BuildKit-based engine, containerized typed-function DAG, content-addressed caching with
two primitives (layer cache and named cache volumes). **Dagger Cloud** claims to sync "layers and
volumes across every Dagger Engine connected to your organization," such that *"a build on one
runner benefits from the cache populated by a completely different runner, even on a different
branch"* ([docs](https://docs.dagger.io/reference/configuration/cloud/)) — **vendor claim, no
published benchmarks found**. Pricing moved from usage-based to **flat $50/month for teams up to
10 users**, free for individuals, custom enterprise
([pricing post](https://dagger.io/blog/new-dagger-cloud-pricing/)), explicitly because customers
wanted predictability. A managed-compute "Dagger Cloud Checks" early-access tier is referenced in
secondary sources but **could not be confirmed against a primary page this session**.
Firecracker-for-multi-tenant-Dagger exists only as a community exploration, not as a confirmed
Dagger Cloud production mechanism.

The agent pivot is concrete and dated. **Container Use** (repo created 2025-05-23, blogged
2025-06-14, **4,047 stars** as of 2026-09-20, Apache-2.0) is an open-source **MCP server that
gives each coding agent a fresh container backed by its own git branch**, so parallel agents do not
collide and their work stays inspectable with plain `git`; ships `cu watch` and `cu terminal`
([blog](https://dagger.io/blog/agent-container-use/), [repo](https://github.com/dagger/container-use)).
Their framing of the problem is almost exactly ours: *"One AI coding agent is magic… So, naturally,
you think: what if I had ten? Chaos ensues."* Dagger also added a native `LLM` type in v0.18
(experimental) letting an LLM auto-discover and call Dagger Functions as tools, plus Dagger Shell
prompt mode ([blog](https://dagger.io/blog/llm/)). Solomon Hykes: *"We didn't plan for it, but our
community showed us the way… Agents blur the line between development and delivery. They need
programmable environments."* Notably, **Dagger never framed the pivot as a monetization failure**,
unlike Earthly.

#### crabbox (crabbox.sh) — the closest thing to Pandora's shape, and it is 4.5 months old

Built by **Peter Steinberger** (PSPDFKit founder, now lead of OpenClaw) under
[github.com/openclaw/crabbox](https://github.com/openclaw/crabbox). GitHub API, live 2026-09-20:
repo **created 2026-04-30**, **1,408 stars, 183 forks, 45 open issues**, near-daily releases
(v0.63.0 dated 2026-09-20). Tagline: *"warm a box, sync the diff, run the suite."*

- **Problem framing matches ours almost exactly**: at 10–15 parallel agents the bottleneck moves
  from code generation to **merge verification**; each agent needs its own sandbox (own DB, dev
  server, ports, Docker daemon) instead of colliding on one shared local machine
  ([third-party recap, 2026-06-29](https://www.aibuilderclub.com/blog/crabbox-parallel-agent-sandboxes)).
- **Uncommitted source is the design center, not an add-on**: the box is seeded from git
  (clone/fetch of origin + base ref) to get the tree cheaply, then the **dirty diff is shipped via
  rsync** using a NUL-delimited manifest of changed/deleted files computed from a local git
  manifest, with fingerprinting to skip no-op syncs and a guard against "suspicious mass deletion"
  of tracked files. Windows targets get a tar manifest instead. Re-sync happens before every run.
  Alternate hydration paths exist for a GitHub PR (`--fresh-pr`) or from CI Actions history
  ([how it works](https://crabbox.sh/how-it-works.html), [architecture](https://crabbox.sh/architecture.html)).
- **Provider-agnostic, never operates compute**: 81 registered providers (AWS/Azure/GCP/Hetzner,
  Docker, Kubernetes via KubeVirt, E2B, Daytona, Blacksmith "Testbox", or any static SSH host).
  Two-plane split: control plane (CLI ↔ coordinator over HTTPS+Bearer) handles lease lifecycle and
  billing guardrails; **data plane is direct SSH/rsync bypassing the coordinator entirely**, so
  files and secrets never transit crabbox's backend. Four modes: Brokered, Direct SSH, Registered
  Direct, and Delegated (provider owns sync+execution end-to-end). Coordinator self-hostable on
  Cloudflare Workers + Durable Objects or Node + Postgres.
- **Explicit commercial stance: "Crabbox Software Is Free. Compute Isn't."** MIT-licensed; you pay
  your provider directly. Cost tracking per lease (`estimatedUSD` from elapsed runtime,
  `reservedUSD` as worst-case-TTL ceiling), priced from an override → live provider pricing API →
  fallback table, with `CRABBOX_MAX_*` spend caps. **No managed compute offering found.**
- Cache volumes are provider-specific persistent storage reusable across leases, plus prebaked
  warm images and explicit warm-box reuse by slug/ID. **No published cache-hit or boot-time
  numbers.** No postmortems — consistent with its age.
- Our own prior assessment (`notes/crabbox.md`, 2026-09-19) adds limitations worth carrying
  forward: static SSH is direct-only and outside the coordinator, so it does not turn one host
  into a memory-admitted queue of isolated jobs; `attach` follows coordinator events and cannot
  resume execution after the originating CLI dies; losing SSH does not prove the remote process
  stopped; explicit artifact downloads are not general conflict-safe automatic source write-back.

---

### Tier D: Bazel remote execution and caching (memoization, done properly)

#### The shared protocol, because it *is* our memoization design

The [Remote Execution API](https://github.com/bazelbuild/remote-apis) defines content-addressable
storage plus an Action Cache. The action key is
**hash(argv + lexicographically-sorted environment variables + Merkle root of the entire input
tree + declared output paths + timeout + platform properties)**
([remote_execution.proto](https://github.com/bazelbuild/remote-apis/blob/main/build/bazel/remote/execution/v2/remote_execution.proto)).
Two actions with different timeouts are different actions. `do_not_cache` suppresses both caching
and in-flight request merging. A hit returns an `ActionResult` of output digests without
re-execution. This is exactly command-result memoization, with ~8 years of production hardening.

What breaks it, per [bazel.build/remote/caching](https://bazel.build/remote/caching) and
[Trusting builds with Bazel remote execution](https://blogsystem5.substack.com/p/bazel-remote-execution):

- **Undeclared inputs.** "Two users with different compilers installed will wrongly share cache
  hits because the outputs are different but they have the same action hash." Bazel only includes
  env vars explicitly allowlisted via `--action_env`, so unlisted host env that *does* affect
  output silently does not affect the key.
- **Files changing mid-build** — Bazel "might upload invalid results to the remote cache";
  mitigated by `--experimental_guard_against_concurrent_changes`.
- **Non-determinism inside actions** (network fetches, embedded timestamps/hostnames) poisons the
  cache for everyone who later hits it.
- **A documented real incident**: `--remote_local_fallback` let a failed remote build fall back to
  *unsandboxed local* execution, whose outputs differed from the sandboxed path — and those got
  uploaded to the action cache.

#### BuildBuddy (buildbuddy.io)

Isolation types are a configurable property: `oci`, `docker`, `podman`, `firecracker`, `sandbox`,
`none` self-hosted; **only `oci` and `firecracker` on managed cloud**
([RBE platforms](https://www.buildbuddy.io/docs/rbe-platforms/)). Firecracker was chosen
specifically to allow Docker-in-Docker ([microVM docs](https://www.buildbuddy.io/docs/rbe-microvms/)).
Cloud substrate **not disclosed**; Enterprise is available on-prem via Helm.

**Their snapshotting work is the most directly transferable engineering in the survey**
([Snapshot, Chunk, Clone](https://www.buildbuddy.io/blog/fast-runners-at-scale/)):

- They snapshot the **entire Firecracker VM — disk and memory — including a live Bazel server
  process with Bazel's in-process Skyframe Analysis Cache warm inside the JVM.** Restoring resumes
  a warm server rather than cold-starting one.
- Snapshots are **chunked** (memory and disk independently), each chunk content-addressed,
  compressed, and **stored in the normal CAS**, so warm state is shareable across machines rather
  than pinned to the originating executor.
- Memory pages are **lazily faulted in via Linux `userfaultfd`**, so a restore does not require
  loading the whole memory image.
- Writes from a cloned VM go to new `.dirty`-suffixed chunks, so **many clones share one base
  snapshot copy-on-write**.
- Result: **median CI run ~30 seconds** vs ~3.5 minutes for GitHub Actions initialization alone;
  small-repo tests "as little as 6 seconds"; marketed as "8x or more." Typical Bazel workloads
  cited at 20 GB memory / 80 GB disk. **An exact ms-to-restore figure was not found** — the docs
  only say Firecracker boot is "hundreds of milliseconds."
- `recycle-runner` puts a VM to sleep instead of destroying it; `test.runner-recycling-key`
  versions the recycling key so dependency changes force a clean runner. BuildBuddy's own docs
  state plainly that **runner recycling reduces action hermeticity** — a first-party
  acknowledgement that warm state is a correctness trade, not a free lunch.
- **Content-defined chunking** for cache transfer has harder numbers
  ([blog](https://www.buildbuddy.io/blog/content-defined-chunking/)): 40% less data uploaded, 40%
  smaller disk cache, **~300 TiB of duplicate chunk data skipped in one two-week production
  window**, ~85% dedup on files >2 MiB, 20–40% overall traffic savings, via new `SplitBlob` /
  `SpliceBlob` RPCs where chunks are themselves ordinary CAS entries.
- A **failure write-up worth reading** ([Unusual Builds with Bytes](https://www.buildbuddy.io/blog/unusual-builds-w-bytes/)):
  when local artifact-TTL tracking broke, execution-root disk usage went from 1.8 MB to 640 MB
  (355x), microVMs downloaded "hundreds of gigabytes" and ran out of disk. **The metadata about
  what is still valid in cache is itself fragile state.**

Pricing: Personal free (100 GB cache transfer, up to 80 cores, Mac cores billed at **$45/core**
even on free); Team pay-as-you-go at an **undisclosed $/GB** over 100 GB, up to 800 cores;
Enterprise custom ([pricing](https://www.buildbuddy.io/pricing/)). No spot/oversubscription info
and no margin statements **found**. Ships a **first-party MCP server**
([docs](https://www.buildbuddy.io/docs/enterprise-mcp/)) exposing build/test metadata explicitly so
"a local coding agent can check failing CI tests on BuildBuddy and then fix issues automatically,"
with documented security guidance: run an **isolated MCP relay that injects the auth header** so
the raw API key never reaches the agent, and use a **dedicated Reader-role, narrowly scoped key**.

#### EngFlow (engflow.com)

Containers via Docker or Sysbox by default, **gVisor** for hermetic builds, configurable isolation
levels, separate VPCs, shielded VMs, customer-managed keys. Substrate confirmed **AWS** via an
[AWS Startups case study](https://startups.aws.com/learn/fast-reliable-and-cost-efficient-builds-tests-at-scale-with-engflow-remote-execution-on-aws):
EC2 On-Demand **and Spot** for schedulers and workers, EBS for instance storage, **S3 for durable
CAS**, NLB, auto-scaling, three AZs, Graviton3 + gp3.

EngFlow does BuildBuddy's trick at the **container** level and publishes **better latency numbers**
([CI Runners docs](https://docs.engflow.com/ci-runners/ci-runners-bazel-overview.html)): on first
pass, a snapshot of the container including the filesystem **and the running Bazel server process**
is taken and stored in the CAS; later jobs fetch it instead of cold-starting. "We can reuse the
snapshot any number of times on any worker."

- Buildkite CI startup **~3 min → ~30s**
- GitHub Actions CI startup **~3m10s → ~40s**
- **Snapshot revival ~0.5 GB/second** (scales with heap size)
- Machine boot overhead on top: **~12s on GCP, ~25s on AWS**
- **Constraint stated in their own docs (~Jan 2026): warm-Bazel CI runners are Linux-only and have
  NO multi-tenancy support.** That is an explicit admission that cross-tenant warm-snapshot sharing
  is unsolved for them.

Also: `experimental_mnemonic_based_invocation_affinity` reuses the same executor within an
invocation for actions sharing a mnemonic, and a "randomized executor reuse probability" knob keeps
workers alive for reuse to raise utilization — effectively a utilization dial. Spot claimed to save
**70% on average, up to 90%** of *EngFlow's own* compute cost; **nothing published about whether
that passes through to customers**. Pricing is free tier (single machine, Linux+Bazel only) vs
Enterprise custom quote — **no dollar figures published anywhere**. No flaky detection beyond
surfacing Bazel's own `--runs_per_test`, and **no agent/MCP story at all** — a real gap versus the
other two.

#### NativeLink (nativelink.com / TraceMachina)

One Rust binary serving as cache, scheduler or worker by configuration; Apache-2.0 core with
**FSL/BUSL modules** (metrics and remote persistent workers require a licence). Runs on
AWS/GCP/Azure/own hardware. $4.7M seed (Wellington-led, Samsung Next participating).

**Local Remote Execution (LRE)** is their distinctive idea and the clearest published treatment of
laptop↔CI cache sharing ([docs](https://docs.nativelink.com/explanations/lre)): toolchains are
pinned to **Nix store paths** (`/nix/store/<hash>-clang-…/bin/clang`) whose hash encodes the
toolchain's own build inputs, and Bazel references those absolute content-hashed paths instead of
resolving via `PATH`. Because the toolchain bytes are identical locally and remotely, locally
computed action digests match remotely computed ones — they claim "virtually perfect" cache hit
rate across repositories, developers and CI. Stated limits: only within the same system
architecture; **toolchains only, not all inputs**; C++ toolchain is x86_64-linux only.

They brand explicitly for agents — "build infrastructure for the agentic era", `llms.txt` /
`llms-small.txt` / `llms-full.txt` served without auth or JS, and a **5-tool MCP server for Claude
Code / Cursor / Codex, gated behind the Enterprise licence**
([nativelink.com/agents](https://nativelink.com/agents)).

**Notable gaps:** no snapshot/warm-restore feature found at all (unlike BuildBuddy and EngFlow);
**the isolation technology used for untrusted worker execution is never named**; and their LRE doc
explicitly puts sandboxing and poisoning **out of scope** — so they publish the cache-sharing
mechanism in detail without a companion mitigation policy. Open issues suggest real production
rough edges (worker-pool deadlock under sustained load requiring manual restart,
[#2672](https://github.com/TraceMachina/nativelink/issues/2672)).

Open-source alternatives per [bazel.build's own list](https://bazel.build/community/remote-execution-services):
**Buildbarn** (Go; pluggable stores, now favouring a "circular" fixed-size index + ring buffer over
the earlier Redis+S3), **Buildfarm** (Java), **BuildGrid** (Python, RWAPI — widely considered too
heavyweight, which is why most implementations rolled their own scheduler↔worker protocol).

---

### Tier E: monorepo task caches and test intelligence

#### Nx Cloud / Nx Agents

Agents run on Nx Cloud infrastructure; hypervisor and provider **not published**. Artifacts move
between agents through the **same content-addressed remote cache** — when a task depends on output
produced on another agent, Nx restores that output before running the dependent task
([DTE docs](https://nx.dev/docs/features/ci-features/distribute-task-execution)). Pricing: Hobby
free with 50,000 credits/month; Team $29/mo with $29 included credit, $5.50 per extra 10,000
credits, $19/contributor, $2.25/concurrent CI connection ([pricing](https://nx.dev/pricing)). The
credits-per-agent-minute table lives on a doc page I could not fetch — **gap**.

Nx is the furthest along on generated background work:

- **Flaky detection mechanics are the cleanest published design anywhere**
  ([docs](https://nx.dev/docs/features/ci-features/flaky-tasks)): *"If Nx ever encounters a task
  that fails with a particular set of inputs and then succeeds with those same inputs, it marks
  that task as flaky."* The **input hash is the identity key**, so a failure after a code change is
  never mistaken for a flake, and evidence aggregates across machines and CI runs. Retry is capped
  at 2 attempts total and is deliberately **sent to a different agent** to rule out
  agent-local causes. The flaky label **auto-expires after 2 weeks** without incidents.
- **Reliable CI** ([2024-03-21](https://nx.dev/blog/reliable-ci-a-new-execution-model-fixing-both-flakiness-and-slowness)):
  they moved CI from "a DAG of VMs" to "a DAG of tasks." Their modelled (not measured) arithmetic:
  a 0.1% per-test failure rate over 500 tests gives a traditional pipeline a **39%** chance of a
  flake-caused failure, which two retries drive to 0.000005%; and 50 agents with a 1% chance of a
  5-minute npm-install stall means **40% of runs affected**, 20→25 min, amortized to ~6 seconds
  under their model.
- **Self-Healing CI** (early access [2025-06-23](https://nx.dev/blog/nx-self-healing-ci)): on task
  failure, **Nx Cloud starts an AI agent on Nx Cloud infrastructure** that reads the logs, uses the
  project graph for structure, proposes a fix, **validates it by re-running the originally failed
  tasks with the proposed changes**, then surfaces it as a PR comment or in the editor for human
  approval. `--fix-tasks` scopes eligibility. Available on all plans; credit consumption
  **unconfirmed**. This is the closest shipped product to "spend capacity on generated work that
  saves human review time." Nx frames the two features as complementary: *"flaky tests get retried
  transparently, genuine bugs get fixed intelligently."*
- Local dev machines read and write the same remote cache, but **Nx Agents themselves are driven
  from a CI pipeline — no "run my dirty tree on agents" feature found.**

#### Turborepo remote cache (Vercel)

Storage only, no execution. Content-addressed task outputs, stored locally in `.turbo/cache` and
uploaded remotely; the wire protocol is a **documented open HTTP API**
([openapi](https://turborepo.dev/docs/openapi)), which is why community servers exist
(`ducktors/turborepo-remote-cache`, `brunojppb/turbo-cache-server`). "Remote Caching is free… on
all plans." **Developer laptops both read and write the shared cache by default** after
`turbo login` + `turbo link` ([docs](https://turborepo.dev/docs/core-concepts/remote-caching)).
Integrity: **HMAC-SHA256 artifact signing** via `remoteCache.signature: true` plus
`TURBO_REMOTE_CACHE_SIGNATURE_KEY` (minimum 32 bytes, shared by client and server, tag carried in
an `x-artifact-tag` header); artifacts failing verification are **ignored and treated as a cache
miss**. The existence of this feature is itself the lesson: a cache writable from laptops is a
code-execution vector, and the shipped mitigation is integrity signing, not access control.

#### Gradle Develocity

Test Distribution agents ship as a Docker image or executable JAR and connect out to the Develocity
server — so execution runs on **customer-supplied capacity**, not a vendor fleet
([agent manual](https://docs.develocity.ai/test-distribution/3.8/test-distribution-agent/)).
Build cache uses a local node plus remote cache nodes; note that **Build Cache Node support is
discontinued at end of 2026** (Develocity 2027.1 drops it, migrate to "Develocity Edge")
([build acceleration docs](https://docs.develocity.ai/2026.2/administration/build-acceleration/)).

**Pricing is per committer, per year, contact-sales** — SaaS bundles a typical build/test volume
with "sustained usage beyond that billed by consumption"; self-hosted has no usage charge
([pricing](https://develocity.ai/pricing/)). The most mature product in this whole category
monetizes **per seat, not per minute.**

**Predictive Test Selection** is the deepest "generated background work" feature shipped by anyone
([docs](https://docs.develocity.ai/2026.2/using-develocity/predictive-test-selection/)):

- The model is trained on the customer's own Build Scan data (code changes and test outcomes)
  combined with "training from millions of test executions across many projects," and updates with
  every new Build Scan.
- Three **selection profiles — Conservative / Standard / Fast** — explicitly trading confidence
  (percent of failures correctly predicted) against savings (percent of test time avoided).
- Two modes: **"relevant tests"** for pre-merge and *local* checks, and **"remaining tests"** for
  post-merge verification — i.e. the skipped tests are run later, asynchronously. That is
  structurally a background-work pattern.
- A **PTS Simulator "replays your historical build data to estimate what enabling PTS would have
  saved, and what it would have missed,"** reporting predicted failure-detection rates.
- Build Scans record, per test, **why it was selected or skipped**.
- Marketing claims are inconsistent: "up to 90%" on one Gradle page, "up to 70%" on another.
  Treat both as marketing.

#### Adjacent test intelligence

**Launchable**, the closest independent ML test-selection company, was **acquired by CloudBees in
August 2024** (secondary sourcing; date lightly verified) — the standalone ML-test-selection
business did not stay standalone. **Trunk.io Flaky Tests** auto-detects, tracks and **quarantines**
flaky tests so they stop breaking pipelines while still running and collecting data; quarantine is
toggled from a dashboard without touching source or the merge queue; webhooks fire on
quarantine/failure/resolution ([trunk.io/flaky-tests](https://trunk.io/flaky-tests)). Third-party
reporting puts Trunk at $15–40/developer/month (**unverified**) — again per seat. **Datadog Test
Optimization, BuildPulse, moonrepo, Bitrise: not researched** (search budget) — **gap**.

---

## Part 2 — Comparison table

| Vendor | Isolation | Substrate | Warm-state mechanism | Published numbers | Pricing model | Spot/batch | Beyond-speed features | Uncommitted source |
|---|---|---|---|---|---|---|---|---|
| Blacksmith | Firecracker + cgroups + nftables | Own metal (Ryzen 7950X, 500+ hosts, colo) | Sticky Disks: Ceph-on-NVMe, CoW clone per job, promote on exit 0; MinIO GHA-cache proxy | 49.8→327.5 MB/s cache (6.5x); margins 10%util=35%, 20%=70%, 35%+=85%+ | $0.004/min 2vCPU x64; 3k free min | No | Test Analytics (JUnit→PR comment) only; Sandboxes *coming soon*; [code]smith tool-shape memoization | Not found |
| Depot | Single-tenant EC2/build; unnamed microVM hypervisor on Metal | AWS (incl. bare-metal EC2) | Ceph-on-NVMe per-AZ layer cache; Metal: NVMe-oF/TCP + S3 + host-mem tiering | EBS→Ceph 140→900 MB/s (6x); warm pool 40s–5min→2–3s; Metal "~30% faster"; sandbox launch ~5s | Per-second $0.0001–$0.0032/s; Docker $0.04/min; cache $0.20/GB-mo; sandboxes $0.01/min | No | Remote Agent Sandboxes (shipped, Claude Code); Depot Code (git-on-S3) | **Yes — `depot build` from laptop shares CI cache; Depot Cache for Bazel/Go/Gradle/Turbo** |
| Namespace | Proprietary hypervisor (unnamed) | Own colo metal (left AWS + Equinix) | Cache Volumes: topology-aware, **schedule job to the node holding the cache**; per-job fork, last-write-wins | **None published** (deferred to "future post") | vCPU×min×platform multiplier; cache $0.002/GB-hr attached + $0.0048/GB-day | No | Devboxes for humans+agents; Devin Outposts partner; Warp sandbox architecture | Devboxes hold working state; no laptop cache product found |
| WarpBuild | "Ephemeral VMs" (unnamed); Apple Virtualization for ARM | Likely hyperscaler; BYOC into customer cloud | Undisclosed; snapshot restore priced at $0.04/job | "4x cache" (marketing only) | $0.004–$0.064/min; **BYOC flat $0.002/min** | **Discontinued managed spot 2026-06-08** | None | Not found |
| BuildJet | KVM | Undisclosed | Colocated cache, 20 GB/repo/week free | None | $0.004–$0.048/min | No | None | **Shut down 2026-03-31** |
| Ubicloud | **Cloud Hypervisor** + namespaces + systemd sandbox (seccomp *not* enforced) | Leased Hetzner / Leaseweb / AWS metal; SPDK storage, non-replicated | GHA cache proxy; fresh VM per job, no host state reuse | 4x download / 3x upload (vendor) | $0.00125/min std 2vCPU vs GH $0.006 | No | None | Not found |
| RunsOn | Stock EC2/Nitro, 1 instance per job | **Customer's own AWS account** | S3 in-VPC cache, EBS **sticky disks** mounted from snapshot (no tar/untar), re-snapshot after | "<30s" start; no throughput numbers | **€300–€3,600/yr licence; compute at AWS cost, no markup** | **Spot by default, on-demand fallback (2–3s), auto-retry, circuit breaker** | None | Not found |
| Actuated | Firecracker, single-tenant, immutable rootfs | **Customer's own metal / nested-virt hosts** | Host-local Docker layer cache + optional S3 | **1–2s VM boot incl. Docker** | **Flat ~$250+/mo, unlimited minutes** | No | None | Not found |
| Buildkite | Unnamed hypervisor; Apple Virtualization on macOS | "Tier 3+ DCs", provider undisclosed | Persistent cache volumes; tech undisclosed | None | $30/user/mo + $0.004/vCPU-min Linux; **Test Engine $15/M executions, P90 billing** | No | **Test Engine: pass+fail on same SHA ⇒ flaky; 3 monitor types; quarantine (mute/skip) not auto-rerun; timing-based splitting; workflows→Slack/Linear** | **Preflight (experimental): structured results before you commit, temp branch, agent-facing; MCP server with Parquet log cache** |
| CircleCI | Docker executor (shared kernel) or machine executor (VM); KubeVirt VM-per-job self-hosted | Undisclosed | DLC inside remote-Docker VM; **siblings in one workflow can't share**; 15 GiB/org, 3-day expiry | None; DLC flat 200 credits/job regardless of hit | Credits, 1cr=$0.0006; medium ≈$0.006/min | No | Test Insights (fail+pass same commit, 14-day window); timings split; **Chunk** agent commits fixes + opens PRs; MCP `find_flaky_tests` | **Chunk Sidecars: remote microVMs validating uncommitted agent work; claims 27s avg**; `circleci local execute --skip-checkout` (no cache) |
| Earthly Satellites | Containers, BuildKit | SaaS / self-hosted / BYOC | Cache pinned to the satellite instance; **no cross-host mechanism found** | None | $49/mo/satellite | No | None | Build tool runs on local source by design |
| Dagger | BuildKit engine, OCI runtimes | Self-hosted engines + Dagger Cloud | Content-addressed layer cache + cache volumes; Cloud claims cross-runner sync | None published | **Flat $50/mo ≤10 users** | No | **Container Use: MCP, container per agent backed by a git branch (4,047 stars); native LLM type in v0.18** | Build tool runs on local source by design |
| crabbox | Whatever the provider gives (81 providers) | **None — BYO compute always** | Provider cache volumes, prebaked warm images, warm-box reuse by slug | None | **MIT, free; you pay your provider**; per-lease cost estimate + spend caps | Provider-dependent | Parallel-agent isolation is the whole product | **Core design: git seed + rsync of the dirty diff with manifest, fingerprint skip, mass-deletion guard; re-sync before every run** |
| BuildBuddy | oci/docker/podman/**firecracker**/sandbox (cloud: oci+firecracker) | Managed cloud (undisclosed) + on-prem Helm | **Full VM disk+memory snapshot incl. warm Bazel server; chunked into CAS; UFFD lazy paging; CoW clones** | **median CI ~30s**; CDC: 40% less upload, ~300 TiB dedup/2wk, 85% on >2 MiB | Free 100 GB/80 cores; Team undisclosed $/GB; Mac $45/core | Not found | MCP server (broad); flaky dashboard; Build-without-Bytes | Documents **read-only cache for dev machines** (`--remote_upload_local_results=false`) |
| EngFlow | Docker/Sysbox containers, gVisor option | **AWS (EC2 On-Demand + Spot, S3 CAS, 3 AZ)** | **Container snapshot incl. running Bazel server, stored in CAS, reusable on any worker** | **3min→30s (Buildkite), 3m10s→40s (GHA), ~0.5 GB/s revival, +12s GCP / +25s AWS boot** | Contact sales; **no $ published** | **Yes — Spot, stateless workers, custom termination policies; 70–90% on own compute** | `--runs_per_test` surfacing only; **no agent/MCP story** | Not found |
| NativeLink | **Not named** | AWS/GCP/Azure/own hardware | Tiered composable stores (memory→S3/Redis); **no snapshot feature found** | None | OSS free; BUSL modules licensed; Enterprise contact-sales | Not found | "Agentic era" branding, `llms.txt`, **5-tool MCP (Enterprise-gated)** | **LRE: Nix-pinned toolchain paths make laptop digests match CI digests**; poisoning explicitly out of scope |
| Nx Cloud | Undisclosed | Nx Cloud infra (AWS marketplace for enterprise) | Task-level content-addressed cache; agents exchange outputs through it | Modelled only: 39%→0.000005% flake-failure; 20→25min→+6s | Hobby free 50k credits; Team $29/mo + $19/contributor | Not found | **Flaky = same input hash fails then passes; retry on a *different* agent, max 2; label expires in 2 weeks. Self-Healing CI: AI agent on their infra proposes a fix and validates by re-running the failed tasks** | Laptops share the cache; agents are CI-driven only |
| Turborepo | N/A (storage only) | Vercel or self-hosted | Content-addressed task outputs over an **open HTTP API** | None | Free on all plans | N/A | None | **Laptops read *and write* the shared cache by default; HMAC-SHA256 signing, failed verify = cache miss** |
| Develocity | Agents as Docker image / JAR on **customer capacity** | SaaS or self-hosted | Local node + remote cache nodes (**node component EOL end-2026**) | None captured | **Per committer, per year, contact sales** | Not found | **PTS: ML test selection, Conservative/Standard/Fast profiles, "relevant" vs "remaining tests" modes, replay Simulator, per-test select/skip audit in Build Scan** | PTS "relevant tests" documented for local checks; cache shared laptop↔CI |

---

## Part 3 — What is already commodity

Do not build a business on any of these. They are table stakes, several vendors deep, and in
some cases free.

1. **Fast GitHub Actions runners at roughly $0.004/2vCPU-min.** Blacksmith, WarpBuild, BuildJet
   (dead), Ubicloud ($0.00125) and Namespace all converged on the same price band within a factor
   of ~3. GitHub itself cut hosted prices by up to 39% in January 2026, and BuildJet shut down
   explicitly because GitHub closed the gap. Price/perf alone has a platform-owner risk floor.
2. **Colocated NVMe cache beating a WAN object store.** Blacksmith 6.5x, Depot 6x, Ubicloud 4x,
   RunsOn EBS sticky disks, WarpBuild snapshots. The *idea* that you put the cache next to the
   compute is universal; the numbers only differ by a factor of ~2.
3. **Copy-on-write per-job cache clones promoted only on success.** Blacksmith Sticky Disks and
   Namespace Cache Volumes implement the identical pattern independently. It is the obvious
   correct answer, not a differentiator.
4. **Content-addressed memoization of command results.** The Bazel Remote Execution API has
   specified this precisely since ~2018 — argv + sorted env + input-tree Merkle root + declared
   outputs + timeout + platform. Nx, Turborepo, Gradle and Dagger all re-implement the same idea
   at task granularity. "We memoize command results" is a description of a decade-old commodity.
5. **Sharing a cache between developer laptops and CI.** Turborepo does it by default, Nx does it,
   Gradle does it, Depot Cache does it for six build systems, NativeLink's LRE is an entire
   engineering effort to make it hit reliably. Reading warm state from a dirty local tree is
   normal, not novel.
6. **Flaky-test detection by "failed and passed on the same input."** Buildkite (same commit SHA),
   CircleCI (same commit, 14-day window), Nx (same input hash), Trunk, Develocity, BuildBuddy all
   ship it. The *mechanism* is commodity; only Nx's "retry on a different agent" and the input-hash
   key are notably better-designed than the median.
7. **Test splitting by historical timing.** CircleCI, Buildkite, Nx all ship it.
8. **An MCP server exposing CI data to coding agents.** BuildBuddy, NativeLink, Buildkite,
   CircleCI, Dagger all shipped one within about 18 months. This is now the minimum, not a moat.
9. **A remote sandbox for a coding agent.** Depot (shipped), Blacksmith (announced), Namespace
   Devboxes, E2B, Daytona, Modal, Morph, Vercel Sandbox, Fly Sprites, Northflank, Blaxel, Runloop,
   Coder, Codespaces, plus every agent vendor's own cloud. This is the most crowded square on the
   board and is being commoditized from both ends — by infra vendors moving up and by model
   vendors moving down.
10. **Validating uncommitted agent work in a remote microVM before commit.** CircleCI Chunk
    Sidecars ship this today, free on every plan. Buildkite Preflight does a branch-snapshot
    variant. crabbox does the rsync-the-dirty-diff variant, MIT-licensed. **This specific idea is
    no longer unclaimed territory** — it is where three separate vendors landed in 2026.

---

## Part 4 — What none of them do

Nothing here should be read as "therefore it is a good business" — several of these are unclaimed
because they are hard, not because they are valuable. But they are genuinely unclaimed.

1. **Nobody sells idle capacity as generated verification work.** Blacksmith published the
   utilization→margin curve and treats off-peak capacity as *margin*, not as *inventory to spend on
   the customer's behalf*. No vendor in this survey runs speculative work in the trough and gives
   the result away. The one partial exception is Develocity's "remaining tests" mode, which defers
   skipped tests to post-merge — and even that is scheduled, not opportunistic.
2. **Nobody does speculative execution on uncommitted snapshots.** CircleCI Chunk Sidecars run
   uncommitted work **when the agent asks**. Buildkite Preflight runs **when you invoke it**.
   crabbox runs **when you run it**. No vendor watches a working tree and pre-computes the verdict
   before the request arrives.
3. **No published per-commit "baseline known-failing set."** Every flaky-detection product answers
   "is this test flaky?" Nobody publishes "here is the set of failures this commit inherited, so
   the agent should ignore them." This is the single most concrete gap: the *primitive* everyone
   has (result memoization keyed on input hash) plus the *data* everyone has (historical pass/fail
   per test) trivially yields a baseline diff, and nobody ships it as a product.
4. **No auto-bisect.** BuildBuddy, EngFlow, NativeLink, Nx, Buildkite, CircleCI — **not one**
   published an automated bisect feature, despite all of them holding both the commit history and a
   warm memoized execution layer that would make bisect nearly free.
5. **Nobody does merge-ahead / cross-agent conflict detection.** Merge queues exist (Trunk,
   Aviator) and sandboxes-per-agent exist (Dagger Container Use gives each agent a git branch), but
   nothing tests *the combination* of several agents' in-flight, uncommitted work before any of it
   lands. Dagger's own framing — "what if I had ten? Chaos ensues" — names the problem and then
   solves only the isolation half.
6. **No mutation testing at any vendor.** Zero hits across all 20+ vendors.
7. **Nobody meters or reports agent tokens saved.** Two vendors are adjacent: Blacksmith's "Muscle
   Memory" caches tool-result shapes to shrink context, and Buildkite's MCP server caches logs as
   Parquet so agents query instead of ingest. Both are token-reduction *mechanisms*; neither is
   sold as a measured saving.
8. **Preemptible/batch tiers are conspicuously absent from managed multi-tenant runners.**
   Spot exists where the customer owns the capacity (RunsOn, EngFlow on their own AWS bill,
   WarpBuild BYOC) and **nowhere** in a managed own-metal fleet. WarpBuild actively withdrew its
   managed spot tier in June 2026. **Pandora's "background work on idle capacity" is, in effect,
   the internal-batch-tier that this market tried in customer-visible form and retreated from** —
   which is either the insight or the warning, and the distinction matters.
9. **Memory oversubscription is undisclosed everywhere.** Blacksmith describes cgroup hard ceilings
   (implying no overcommit). Ubicloud's only published overcommit experience is a Postgres kernel
   bug producing spurious OOMs. Nobody publishes a ratio.
10. **Warm-state sharing across tenants is an admitted open problem.** EngFlow's own docs state the
    warm-Bazel runner feature has **no multi-tenancy support**. BuildBuddy states runner recycling
    **reduces hermeticity**. Namespace's postmortem documents deliberately destroying cache warmth
    to repave hosts securely. Three independent vendors, three admissions that warm + multi-tenant
    + safe is unsolved.
11. **Cache poisoning is barely addressed by the runner tier.** Only **Ubicloud** (branch-scoped
    cache with a cautioned opt-out) and **Turborepo** (HMAC-SHA256 artifact signing) ship a
    first-party mitigation. The Bazel world has the mature answer — read-only cache for untrusted
    writers — and NativeLink, the vendor most aggressively enabling laptop→shared-cache hits,
    explicitly puts it out of scope.

---

## Part 5 — Architecture lessons to steal

**Cache and warm state**

1. **Route the job to the cache, do not route the cache to the job.** Namespace's topology-aware
   scheduling tracks which cache revision sits on which physical node and schedules there, so the
   fetch cost is *zero*, not merely fast. They will even serve a **stale** version rather than pay
   a fetch. This dominates every "make the download faster" approach in the survey and is the
   single best architectural idea found.
2. **Snapshot the process, not just the artifacts.** BuildBuddy snapshots the whole Firecracker VM
   including a **live warm Bazel server with its in-memory analysis cache**; EngFlow snapshots the
   container including the running Bazel server. Both restore into a *warm* process. Concrete
   numbers to design against: **EngFlow 3min→30s (Buildkite), 3m10s→40s (GHA), ~0.5 GB/s revival,
   +12s GCP / +25s AWS boot overhead**; **BuildBuddy median CI run ~30s**.
3. **Chunk snapshots into the CAS and lazily page memory with `userfaultfd`, with copy-on-write
   `.dirty` chunks.** This is what makes a snapshot shareable across hosts instead of pinned to
   one, and lets many clones share one base. Earthly Satellites' failure to do this — cache lived
   on the satellite instance — is the counterexample.
4. **Content-defined chunking pays measurably.** BuildBuddy: **40% less data uploaded, 40% smaller
   disk cache, ~300 TiB of duplicate chunks skipped in a two-week window, ~85% dedup on files
   >2 MiB, 20–40% overall traffic savings.** Chunks are ordinary CAS entries; negotiation is
   `SplitBlob`/`SpliceBlob`.
5. **Copy-on-write per job, promote only on exit 0.** Blacksmith and Namespace arrived at this
   independently. It is the cheapest possible defense against one job poisoning the next tenant's
   warm state.
6. **Colocate and measure.** Blacksmith 49.8 → 327.5 MB/s; Depot EBS → Ceph-on-NVMe 140 → 900 MB/s.
   Both are ~6x, both from moving the cache next to the compute. Depot's Metal design adds the
   tier worth copying: **local NVMe on dedicated storage hosts, NVMe-oF/TCP between compute and
   storage, S3 as the durable base, host memory caching on both ends.**
7. **Cache-validity metadata is itself fragile state.** BuildBuddy's TTL-tracking bug turned 1.8 MB
   of execution root into 640 MB (355x) and made microVMs download hundreds of gigabytes until they
   ran out of disk. Whatever tracks "what is still warm" needs the same rigour as the cache.

**Correctness of generated work**

8. **Key flakiness on the input hash, not the commit.** Nx: *fails then succeeds with the same
   input hash ⇒ flaky.* This is strictly better than Buildkite's and CircleCI's same-commit-SHA
   rule because it survives rebases, works across machines and CI runs, and never mistakes a
   post-change failure for a flake. Pandora already hashes inputs for memoization — the flaky
   signal is free.
9. **Retry on a *different* machine, and time-decay the label.** Nx sends the retry to a different
   agent specifically to rule out agent-local causes, caps at 2 attempts, and **drops the flaky
   label after 2 weeks without incident.** Both details are cheap and both matter.
10. **Quarantine beats auto-rerun for the human-facing surface.** Buildkite mutes a flaky test —
    it keeps running and keeps producing data so the system can detect recovery — but stops failing
    the build. Trunk does the same. Silent retry hides the signal; quarantine preserves it.
11. **Timing-based splitting is poisoned by retries.** A test retried 3x reports ~3x its runtime in
    JUnit XML, so timing-based splitting progressively over-allocates capacity to flaky rather than
    slow tests. If we split by timing, use per-attempt durations, not the sum.
12. **Give the speculative feature a confidence/savings dial and a replay simulator.** Develocity
    ships **Conservative / Standard / Fast** profiles trading failure-detection rate against time
    saved, and a **PTS Simulator that replays historical build data to estimate what enabling it
    would have saved and what it would have missed**. Every speculative feature Pandora ships
    should be provable on replayed history before anyone trusts it in the path.
13. **Record why each item was selected or skipped.** Develocity puts per-test select/skip
    rationale in the Build Scan. A speculative system that cannot explain an omission will not be
    trusted after its first miss.
14. **Validate the generated fix by re-running exactly the tasks that failed.** Nx Self-Healing CI
    does this before surfacing anything to a human. The validation step, not the generation step,
    is what makes generated work worth a reviewer's attention.

**Multi-tenancy and security**

15. **Warm + multi-tenant + safe is unsolved, and the vendors say so.** EngFlow: warm CI runners
    have **no multi-tenancy support**. BuildBuddy: runner recycling **reduces action hermeticity**;
    mitigate with a versioned `runner-recycling-key`. Namespace: recovery required repaving hosts
    with new identities, certs and encryption keys, **deliberately destroying cache warmth**. Plan
    for the repave path *and* for the warmth it costs, rather than discovering the conflict during
    an incident.
16. **Make untrusted writers read-only.** The Bazel ecosystem's settled answer is
    `--remote_upload_local_results=false` for dev machines with a single trusted CI write
    credential, plus disabling network inside workers and enforcing strategy flags via
    `InvocationPolicy` so users cannot override them. A documented real incident:
    `--remote_local_fallback` let a failed remote build fall back to unsandboxed local execution
    whose divergent outputs were then uploaded and poisoned the cache.
17. **Sign artifacts if anything writable is reachable from a laptop.** Turborepo's HMAC-SHA256
    signing with a ≥32-byte shared key, failing verification as a **cache miss** rather than an
    error, is the minimum viable design.
18. **Know the GitHub Actions cache-poisoning class before building a cache proxy.** Caches are
    branch-scoped, not workflow-scoped; child branches read parent caches; the 10 GB per-repo limit
    with LRU eviction lets an attacker flush a legitimate entry with 30+ GB of junk and write a
    poisoned entry under the same key for a later trusted workflow to restore. The
    `ACTIONS_RUNTIME_TOKEN` stays valid **6 hours after the workflow completes** and cannot be
    revoked by the maintainer ([Adnan Khan, May 2024](https://adnanthekhan.com/2024/05/06/the-monsters-in-your-build-cache-github-actions-cache-poisoning/),
    [Cacheract, Dec 2024](https://adnanthekhan.com/2024/12/21/cacheract-the-monster-in-your-build-cache/)).
19. **Copy Namespace/Warp's credential model for agent sandboxes**: one tenant per customer team
    with zero shared resources or network, per-sandbox isolation inside a tenant, **short-lived
    scoped credentials**, GitHub tokens limited to the triggering user's own permissions with
    changes attributed back to that user, a shared sidecar volume for codebase-context indices, and
    vendor tooling mounted under a separate path so it updates independently of the user's image.
20. **Keep the agent's API key out of the agent.** BuildBuddy's documented pattern: an isolated MCP
    relay that injects the auth header, plus a dedicated Reader-role, narrowly scoped key.
21. **Do not name a hypervisor you have not hardened.** Ubicloud publicly discloses that
    seccomp-bpf exists in Cloud Hypervisor but **is not actively filtered** by them — honest, and a
    reminder that "we use microVMs" is not a security posture by itself. Firecracker's jailer
    (chroot + pid/net namespaces + seccomp) is the baseline to match. Note also Ubicloud chose
    Cloud Hypervisor over Firecracker deliberately for Windows guests, PCI passthrough, vhost-user
    and hugepages.

**Operations**

22. **Sub-second VM boot does not buy sub-second job start.** Actuated, with 1–2s Firecracker
    boots, reports total job-start latency still mirroring GitHub-hosted because GitHub's own
    queuing and API dominate. If Pandora dispatches from a platform's events, that platform's
    latency is the floor.
23. **GitHub's webhooks arrive out of order or are dropped**, jobs sit queued with no event, there
    is **no way to bind a runner to a specific queued job**, and GitHub sometimes reports a busy
    runner as idle. Actuated built reconciliation plus a "reaper" after observing 60,000+ events.
    Budget for this rather than discovering it.
24. **Docker Hub rate limits hit self-hosted runners** because they lack GitHub's exemption.
    Registry mirroring plus host-local image cache is the mitigation.
25. **Colocation is still someone else's operations.** Blacksmith's Aug 2026 outage was a facility
    cooling loss; Namespace's was a provider vSwitch dropping packets for days before detection.
    Owning the servers does not mean owning the failure domain. Also note **Equinix Metal shut down
    June 30, 2026** — a substrate that several of these vendors once relied on simply vanished.
26. **Price the cache explicitly.** RunsOn v3.2 surfaces EBS volume and snapshot cost next to queue
    time and spot interruptions. Namespace bills cache on two axes (**$0.002/GB-hr while attached**
    plus **$0.0048/GB-day at rest**). Warm state is a real COGS line; if Pandora's differentiator
    is background work on warm state, it needs a cost model from day one.

**Commercial**

27. **Blacksmith's margin curve is the model to reason against**: **10% utilization ≈ 35% gross
    margin, 20% ≈ 70%, 35%+ ≈ 85%+**, driven by Poisson smoothing across many bursty tenants plus
    weekend (~1/5 of weekday) and early-UTC troughs. Pandora's thesis is to *convert that trough
    into product* rather than banking it as margin, which means the background work must be worth
    more to the customer than the ~50 margin points it consumes between 10% and 20% utilization.
28. **Earthly's two post-mortems are the sharpest warning available.** (a) Buyers treat CI as a
    commodity and will not pay migration cost. (b) Bottom-up adoption in large orgs did not convert
    to company-wide paid deployment: *"translating that adoption into revenue has been much harder
    than we anticipated."* (c) **Their own free/cheaper product cannibalized the paid one — twice.**
    If Pandora ships an open CLI plus a hosted fleet, prospects will price the hosted fleet against
    our own free tier, not against their CI bill. (d) Positioning matters materially: swapping
    "CI" for "build" doubled conversions.
29. **The mature end of this market monetizes per seat, not per minute.** Develocity (per
    committer/year), Buildkite (per active user + separate test-execution metering), Trunk
    (per developer), RunsOn (flat annual licence, compute at cost), Actuated (flat monthly,
    unlimited minutes), Dagger ($50/mo flat, after moving *off* usage-based pricing because
    customers wanted predictability). Per-minute pricing is where the commodity fight is.
30. **Vendor mortality in this category is real and recent**: BuildJet dead (Mar 2026), Earthly CI
    dead (2023) and Earthly Cloud/Satellites dead (Jul 2025), Equinix Metal dead (Jun 2026),
    WarpBuild's managed spot tier withdrawn (Jun 2026), Launchable absorbed by CloudBees (Aug 2024).
    Buyers in this space have been burned and will ask about continuity.
31. **crabbox defines the credible free floor.** MIT-licensed, 1,408 stars in 4.5 months,
    near-daily releases, 81 providers, explicit stance *"Crabbox Software Is Free. Compute Isn't."*
    Any Pandora pitch has to answer why it is worth paying for something crabbox gives away — and
    the honest answer has to be the generated background work, not the remote box.

---

## Appendix — gaps and unverified items

- **Not researched** (search budget): Datadog Test Optimization / CI Visibility, BuildPulse,
  moonrepo, Bitrise, Aspect Build, Hermetiq, Google RBE pricing.
- **Unfetched / unresolved**: Nx Cloud's credits-per-agent-minute table (two URL guesses 404'd);
  BuildBuddy's Team tier $/GB (undisclosed by BuildBuddy); EngFlow and NativeLink dollar pricing
  (contact-sales); Develocity per-committer price (contact-sales); Blacksmith Series B amount.
- **Unverified claims flagged in the body**: Depot's actual hypervisor choice; Blacksmith's "5ms
  Firecracker boot" (AWS's own published figure is ~125ms); BuildJet's Firecracker usage; the
  bex.co "40x" Docker cache claim; the CircleCI Chunk "27s / 78% faster" vs chunk.ai "30x faster"
  contradiction; Trunk's $15–40/dev/month; Launchable's CloudBees acquisition date; RunsOn's
  "3M jobs daily / 900 companies"; Develocity's inconsistent "90%" vs "70%" PTS savings claims;
  a reported Feb 2026 AI-driven cache-poisoning campaign against Microsoft/Datadog/CNCF repos
  (secondary synthesis only — **verify before citing**).
- **Ambiguous dates**: Namespace's `slowdown-oct22` postmortem does not state its year clearly in
  the fetched content.
