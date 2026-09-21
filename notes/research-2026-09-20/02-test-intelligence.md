# Test Intelligence, Flaky-Test, Test-Selection, Merge-Queue and Auto-Bisect Landscape

**Research date:** 2026-09-20. Prepared for the Pandora evaluation (remote execution service that spends idle capacity on generated background verification work for teams running many parallel AI coding agents).

**Sourcing rules used:** primary sources preferred (vendor docs, pricing pages, papers, GitHub repos). Every factual claim carries a URL. Publication dates noted where available. Claims that could not be confirmed against a primary source are marked **unverified**.

**Known gap in this research:** the session's web-search budget was exhausted partway through. Vendor documentation coverage is strong; community criticism (HN/Reddit threads, practitioner blog posts) is materially under-covered. Absence of criticism in this report is not evidence of absence of criticism.

---

## 0. Executive summary — the seven answers that matter

1. **Almost nobody spends compute to *classify*.** The flaky-test market is overwhelmingly passive: it analyses results customers already produced. The exceptions are Datadog (Early Flake Detection reruns new tests up to 10×), Buildkite (opt-in `bktec` retries of the failed subset), Nx Cloud (retries a known-flaky task on a different agent), Mergify (Auto-Retry of failed jobs), and Google/Chromium internal culprit finders (which rerun aggressively). Trunk, BuildPulse, Currents, CircleCI and GitLab spend zero.
2. **Uncommitted working-tree state is mostly, but not entirely, unserved — and the exceptions matter.** Every *flaky-test* and *merge-queue* product is anchored to a commit SHA or a pushed ref; IDE plugins from Datadog and Currents only read CI results backwards into the editor. But **test selection is different**: Gradle Develocity PTS keys on task input-file fingerprints rather than VCS commits and is explicitly recommended for "local and pre-merge/pull-request builds"; Meta's PTS runs at **diff-time** on uncommitted review diffs; and the academic RTS tools (Ekstazi, STARTS) key on file checksums and work on arbitrary working-tree state. The genuinely unserved combination is **remote, shared, multi-tenant verification of a dirty tree** — and the only prior art there is a handful of tiny OSS tools (§5.7).
3. **Memoizing a command by a content digest of the source tree is thoroughly solved — inside a build system.** Bazel, Gradle, Nx, Turborepo and Pants all do it. Doing it for *arbitrary* commands against a *dirty* tree is the part that is barely built.
4. **MCP is already table stakes.** Trunk, Datadog, Buildkite, BuildPulse, Currents and Nx all ship MCP servers. Datadog's and Currents' can write (quarantine/disable, cancel runs). Nx Cloud ships a full agentic loop: an AI fix for a red CI job, verified by re-running the task, applied via MCP.
5. **The best published effectiveness numbers are old and come from Meta, Google and academia**, not from vendors. Vendors publish essentially no precision/recall data for flake detection.
6. **Auto-bisect on red main is not a commercial product.** Merge-queue vendors bisect a *failed batch of candidate PRs*; that is a different problem from searching already-landed history. Flake-aware culprit finding exists only in a Google paper.
7. **Hunk-level delta debugging of an uncommitted diff has been a published technique since 1999 and has no maintained tool.**

---

## 1. Flaky-test management and test-result analytics

### 1.1 Trunk.io — Flaky Tests

**Mechanism.** Ingest-and-analyse plus runtime exit-code manipulation. Independent **monitors** each watch for one pattern; a test's health is the highest-priority active monitor: **Broken > Flaky > Healthy**. "A test stays in its detected state until every health classification monitor that flagged it has independently resolved" ([detection docs](https://docs.trunk.io/flaky-tests/detection)).

**Input.** `trunk-analytics-cli` accepts **JUnit XML** (`--junit-paths`), **Bazel Build Event Protocol JSON** (`--bazel-bep-path`) and **Xcode xcresult** (`--xcresult-path`); git metadata auto-detected ([uploader docs](https://docs.trunk.io/flaky-tests/uploader)). No SDK, no OTel, no coverage.

**Flaky vs broken — published algorithm** ([detection](https://docs.trunk.io/flaky-tests/detection)):
- **Pass-on-retry** (the only default-ON monitor): a test fails then passes on the same commit. Branch-agnostic.
- **Failure-rate monitor** (default OFF): failure rate over a configured % in a window. Can be typed flaky *or* broken.
- **Failure-count monitor** (default OFF): N failures in a rolling window.

Out of the box, **Trunk detects nothing unless your CI already retries.** That is the key architectural dependency.

**Compute spend: none.** `trunk-analytics-cli test <COMMAND>` wraps your existing command; the CLI does not execute tests itself. Quarantine is **exit-code override, not skipping** — "Tests continue running normally and uploading results… changes the exit code from failure to success" ([quarantining](https://docs.trunk.io/flaky-tests/quarantining)). Requires `continue-on-error: true`. Only Flaky-status tests are auto-quarantine candidates; broken tests are not.

**Pricing** ([trunk.io/pricing](https://trunk.io/pricing), [billing docs](https://docs.trunk.io/setup-and-administration/billing)): Free $0/committer/mo, ≤5 private-repo committers, 5M test spans/mo. Team $0/committer/mo, unlimited committers, **1M test spans per committer/mo included, $3 per additional 1M spans**. Enterprise custom. A *committer* = non-bot user with a commit to an enabled private repo in the last 30 days.

**Effectiveness.** No precision/recall published. GA blog (2025-10-02) names Zillow, Metabase, BetterUp, Brex "reclaim[ing] hundreds of engineering hours every quarter" ([blog](https://trunk.io/blog/trunk-flaky-tests-is-out-of-beta)). The [20.2M CI jobs analysis](https://trunk.io/blog/what-we-learned-from-analyzing-20-2-million-ci-jobs-in-trunk-flaky-tests-part-1) (2024-11-12) is industry statistics, not product efficacy — it cites Google's 0.5%/1.6%/14% flake rates for small/medium/large tests and models 1,000 tests at 0.1% flake rate producing ~63% of PRs hitting a flake.

**Limitations/criticism.** Detection is inert without CI retries; the two statistical monitors are off by default. Quarantined flakes still burn full CI time. The 2024 launch HN thread ([42117789](https://news.ycombinator.com/item?id=42117789)) surfaced the obvious objection — "What's the point here over like just hitting rerun?" — and the counter that rerunning destroys the test's value as a gate.

**Uncommitted state:** no. CI-only, PR/stable/merge-queue branches ([get-started](https://docs.trunk.io/flaky-tests/get-started)).

**AI-agent integration — the most purpose-built in the cohort.** Remote MCP at `https://mcp.trunk.io/mcp`, OAuth 2.0 + OIDC, tools **`fix-flaky-test`** and **`setup-trunk-uploads`**; supports Cursor, Claude Code, GitHub Copilot, Gemini CLI ([repo](https://github.com/trunk-io/mcp-server)). `fix-flaky-test` returns pass rate, failure-mode consistency %, duration min/avg/max, git blame, first-seen commit, cross-branch trend. Trunk's stated strategy is ["Don't build agents, build context enrichment"](https://trunk.io/blog/don-t-build-agents-build-context-enrichment) (2026-02-11): ship the proprietary CI+git context rather than a competing coding agent. Their controlled experiment claims context-fed agents correctly diagnosed a nondeterministic-ordering flake where context-free agents hallucinated rendering errors; no accuracy % published.

### 1.2 BuildPulse

**Mechanism.** Pure passive correlation over "test results and various metadata such as git commit and tree SHAs"; "does not require access to source code or any PII" ([docs](https://docs.buildpulse.io/flaky-tests/overview)). Also now sells hosted CI **runners** and an AI code-review beta as separate SKUs — the closest existing shape to Pandora's "runners + analytics" bundle.

**Input.** **JUnit XML only** — "no annotations, no SDK" ([product page](https://buildpulse.io/products/flaky-tests)), via Go CLI `buildpulse-test-reporter submit` ([repo](https://github.com/buildpulse/test-reporter), MIT, pushed 2026-09-19), a [GitHub Action](https://github.com/buildpulse/buildpulse-action), or REST. Optional `--coverage-file`.

**Flaky vs broken — published thresholds** ([quarantining docs](https://docs.buildpulse.io/flaky-tests/guides/Test%20Quarantining)):
- **Git-based**: pass + fail on the same **git *tree* SHA** (not commit SHA — survives rebase/amend). Gated by a "Quarantine Minimum Count," default **10, not configurable**, plus a configurable disruption percentage.
- **Statistical**: Detection Minimum Count (default 10) + Detection Disruption Percentage, default **30%** failure rate.
- Auto-unquarantine after **7 clean days**. Ranking by engineering hours lost, CI minutes wasted, PRs blocked.

**Compute spend: none.** Quarantine is advisory — you poll `GET https://api.buildpulse.io/v1/flaky/tests` and enforce yourself.

**Pricing** ([buildpulse.io/pricing](https://buildpulse.io/pricing)): Startup **$99/mo** (3M test executions, 5 seats); Team **$249/mo** (10M, 30 seats, REST API + MCP); Growth **$499/mo** (30M, unlimited seats); Enterprise custom. Runners **$0.004/min** Linux 2vCPU (3,000 free min/mo) up to $0.128/min at 64 vCPU. AI Code Review beta **$29/seat/mo**.

**Effectiveness.** Vendor-only. Runner claim: "at least 31 percent faster and cost at least 54 percent less" than GitHub Actions ([buildpulse.io](https://buildpulse.io/)). Case studies for [AuditBoard](https://buildpulse.io/blog/auditboard-case-study) and [Replit](https://buildpulse.io/blog/replit-case-study). A "553 hours saved / 74% of devs reported better main-branch stability" figure circulates in their content but could not be traced to a customer result — **unverified**.

**Limitations.** Minimum-count of 10 hard-coded for git-based quarantine; a 30% failure-rate trigger also catches genuinely broken tests; enforcement is DIY; tree-SHA correlation misses environment-dependent flakes that never recur on the same tree.

⚠️ **Name collision**: the "Buildpulse" acquired by CopperTree Analytics in 2018 is a different, building-energy company ([Crunchbase](https://www.crunchbase.com/acquisition/coppertree-analytics-acquires-buildpulse--02f23b85)).

**AI integration.** `@buildpulse/mcp` ([repo](https://github.com/BuildPulseLLC/buildpulse-mcp), created 2026-05-17). Read-only tools: `find_flaky_tests`, `get_test_history`, `get_recent_failures`, `get_repo_flakiness`, `get_repo_coverage`, `get_submission_test_results`. Marketing copy mentions a "Claude-powered agent [that] opens fix PRs" — **unverified / early access**.

### 1.3 Buildkite Test Engine (formerly Test Analytics)

**Mechanism.** Results warehouse + a **monitors → actions** workflow engine + an active in-runner client. Execution data retained **120 days** ([docs](https://buildkite.com/docs/test-engine)).

**Input.** Three mutually exclusive paths ("Configure only one method… from each test run" or you get duplicate executions, [test-collection](https://buildkite.com/docs/test-engine/test-collection)): framework collectors (RSpec, minitest, Jest, Vitest, Cypress, Playwright, Mocha, Jasmine, pytest, gotestsum, xUnit/.NET, ExUnit, Cargo, XCTest, Android, Java/JUnit XML); the Test Collector plugin (JUnit XML/JSON); or `bktec`. Accepts results from non-Buildkite CI.

**Flaky vs broken — the most sophisticated published detector set** ([monitors](https://buildkite.com/docs/pipelines/configure/tests/workflows/monitors)):
- **Transition count**: "a change from passing to failing, or failing to passing, in a sequence of results." At window = 5: `FFFFF` = 0, `PPFFF` = 0.2, `PFPFF` = 0.4. A consistently-failing test scores 0, so broken ≠ flaky by construction. **This is the only detector in the entire cohort that works without any retries.**
- **Passed on retry**: passes and fails on the same commit SHA; recovers after "seven days or 100 executions," whichever first.
- **Probabilistic flakiness score (PFS)**: "a Bayesian statistical model to derive the probability that a test will become flaky on its next execution." **Enterprise only.** Lineage is Meta's PFS work ([engineering.fb.com, 2020-12-10](https://engineering.fb.com/2020/12/10/developer-tools/probabilistic-flakiness/)).
- **New test** (beta); **duration threshold** (p50 over ≤100 executions).

**Compute spend: yes, opt-in.** `bktec` re-runs the failed subset; "Retries and built-in result uploads are off by default. Retry eligibility depends on the runner, remaining retry budget, and muted-test retry settings" ([test-engine-client](https://github.com/buildkite/test-engine-client)). Supported for RSpec, Jest, Vitest, Playwright, pytest, gotest, Cucumber — **not Cypress**. These retries are *remediation* that incidentally feed the passed-on-retry monitor; there is **no dedicated "run it 50× to classify it" job**. `bktec` also does dynamic test splitting across parallel jobs.

**Quarantine is enforced at runtime**: states enabled / **muted** / **skipped**. "Muted tests continue running, but their failures do not affect the build result"; skipped tests are excluded and therefore "don't produce execution data for reliability detection" ([test-state-and-quarantine](https://buildkite.com/docs/pipelines/configure/tests/test-suites/test-state-and-quarantine)). Mute works across the seven `bktec` frameworks; **skip only for RSpec, pytest, Cucumber**. `bktec` only runs inside Buildkite Pipelines, so on GitHub Actions quarantine is display-only.

**Pricing** ([buildkite.com/pricing](https://buildkite.com/pricing)): Free $0 (≤5 users, 250k executions/mo); **Pro $30/active user/mo** (≤50 users, 1M executions/mo included, **$15 per additional 1M**, 1 workflow); Enterprise custom, 30-user minimum, PFS monitor. ([Workflows launch blog, 2025-10-02](https://buildkite.com/resources/blog/introducing-test-engine-workflows/).)

**Effectiveness.** The product page's "89.73% reliability" and "10m → 4m" are demo data ([platform/test-engine](https://buildkite.com/platform/test-engine/)). No precision/recall for any monitor — **treat all effectiveness claims as unverified**.

**AI integration.** [buildkite-mcp-server](https://github.com/buildkite/buildkite-mcp-server) exposes `list_tests`, `get_test`, `list_test_runs`, `get_test_run`, `get_failed_executions`, plus an investigations toolset with `compare_builds` ("What changed since this build last worked on main?") and `get_build_failure_summary` ([tools](https://buildkite.com/docs/apis/mcp-server/tools)). **Read-only — no MCP tool to mute or skip.**

### 1.4 Datadog Test Optimization / CI Visibility

**Mechanism.** Native tracer instrumentation emitting session → module → suite → test spans ([docs](https://docs.datadoghq.com/tests/)) for .NET, Java/JVM, JS, Python, Ruby, Swift, Go, plus Bazel rules. E.g. `pip install -U ddtrace` then `pytest --ddtrace`. **JUnit XML** upload via `datadog-ci junit upload` exists, but **XML gives reporting only** — Test Impact Analysis, Early Flake Detection, Auto Test Retries and quarantine all need the native SDK for runtime control. Git data uploaded as **packfiles** (commit/tree objects), not working-tree contents.

**Flaky definition.** "A test that exhibits both a passing and failing status across multiple test runs **for the same commit**" ([flaky_test_management](https://docs.datadoghq.com/tests/flaky_test_management/)); tags `is_flaky`, `is_new_flaky`, `is_known_flaky`. Exits flaky state after 30 days without failures; moves to **Fixed** after 30 days of consistent passes. Failure-rate policies evaluate over a rolling 7 days.

**Early Flake Detection — the canonical compute-spender.** A test not in the known-tests list is "new" and is **retried up to ten times**; "if any of the test attempts fail" it is flaky. Tests inactive >14 days are re-classified new. Tests slower than **five minutes are excluded** to avoid delaying pipelines ([EFD docs](https://docs.datadoghq.com/tests/early_flake_detection/)). Retry counts scale inversely with duration ([ATR docs](https://docs.datadoghq.com/tests/auto_test_retries/)):

| Initial duration | Retries |
|---|---|
| ≤5s | 10 |
| >5–10s | 5 |
| >10–30s | 3 |
| >30s–5m | 2 |
| >5m | 1 |

Guardrail: dd-trace-js disables EFD when **>30% of test suites are new** (`faultyThreshold = 30`), preventing a first-ever run from retrying everything 10× (DeepWiki over [dd-trace-js](https://github.com/DataDog/dd-trace-js)).

**Auto Test Retries.** Retries **up to 5 times per failing test**, capped by `DD_CIVISIBILITY_TOTAL_FLAKY_RETRY_COUNT` at **1000 per session**. Java-only `..._FLAKY_RETRY_ONLY_KNOWN_FLAKES=true` restricts retries to tests already known flaky. Python ≥4.15.0 adds Dynamic ATR, budgeting retries by first-attempt duration. Retries execute in the customer's CI via the tracer library, not on Datadog infrastructure (implied by the env-var configuration surface; **the docs do not state this explicitly**).

**Test Impact Analysis (formerly Intelligent Test Runner).** SDK collects **per-test code coverage**; the backend marks a test skippable if it passed at an earlier commit where its covered files and the repo's tracked files were identical. Matching uses **a Bloom filter with an approximate 0.04% false-positive rate**, biased so tests are never wrongly skipped — some skippable tests run anyway ([TIA](https://docs.datadoghq.com/tests/test_impact_analysis/)). **Documented blind spots**: library-dependency changes, compiler-option changes, external-service config changes, and data-file changes in data-driven tests are not detected. Escape hatches: `ITR:NoSkip` in the commit message or as a PR label, "tracked files," unskippable tests, default branch excluded. Scale cutoffs: analyses ≤100 recent commits; no skipping for commits touching ≥**5,000 files**; won't skip a test covering ≥**16,000 files**. **No headline % saved is published** — the [launch blog](https://www.datadoghq.com/blog/streamline-ci-testing-with-datadog-intelligent-test-runner/) (2022-10-19) gives none.

**Quarantine/disable.** `@test.test_management.is_quarantined` / `is_disabled`. **Quarantined tests still execute** but don't affect CI status; **disabled tests are skipped entirely.** Policies trigger on N days Active (checked daily at 12:15 UTC quarantine / 12:30 UTC disable), flaking in specified branches, or a 7-day failure rate threshold (checked every 15 min). **Attempt to Fix** retries a marked test, default 3 retries (4 total runs) in dd-trace-js.

**Compute-spend ledger.** Spends: EFD (≤10× per new test), ATR (≤5/test, ≤1000/session), Attempt to Fix (3×), Dynamic ATR. Saves: TIA, Disable. Neutral: detection, quarantine, dashboards. **The cost cliff: every rerun is a billable span.**

**Pricing** ([datadoghq.com/pricing/list](https://www.datadoghq.com/pricing/list/)): **Test Optimization $20/active Git committer/mo** annual ($24 monthly, $29 on-demand); Pipeline Visibility $8; Code Coverage $8. A committer is counted by author email and billable only at **≥3 commits that month** ([pricing docs](https://docs.datadoghq.com/account_management/billing/pricing/)). Allotment **1M test spans per committer/mo**, pooled; overage **$3 per million spans** ([CI Visibility billing](https://docs.datadoghq.com/account_management/billing/ci_visibility/)).

**Uncommitted state:** no, and structurally impossible for TIA — uncommitted edits have no SHA and no packfile to match against. The [IDE plugins](https://docs.datadoghq.com/ide_plugins/) (JetBrains, VS Code & Cursor) are read-only overlays: flaky tests as inlays above test declarations, CI history linked from source, VS Code "Fix in Chat" ([blog](https://www.datadoghq.com/blog/datadog-ide-plugins/)).

**AI integration — the only write-capable one.** MCP at `https://mcp.<SITE>/v1/mcp?toolsets=core,software-delivery` for Cursor / Claude Code / OpenAI Codex / VS Code ([docs](https://docs.datadoghq.com/getting_started/software_delivery_mcp_tools/)). Tools: `get_datadog_flaky_tests`, **`update_datadog_flaky_test_states`** (quarantine, disable, mark fixed, restore), `get_datadog_test_optimization_settings`, `get_datadog_flaky_tests_management_policies`, `search_datadog_test_events`, `aggregate_datadog_test_events`.

### 1.5 Currents.dev, Sorry Cypress, and Playwright-focused dashboards

**Currents.dev.** Cypress/Playwright results dashboard + **test orchestration**: the Currents server "keeps a live queue of pending tests and dynamically assigns them to machines as they become available," learning from historical durations and adapting to "runner spin-up delays or unresponsive machines" ([test-orchestration](https://docs.currents.dev/guides/test-orchestration.md)). Crucially, "**the tests are running on your existing environment**" — Currents coordinates but does not execute. Claims "up to 50% faster compared to Playwright native sharding" ([currents.dev](https://currents.dev/)).

Input is reporter/CLI wrappers (`@currents/playwright`, `cypress-cloud`, `pwc-p`), not JUnit XML.

**Flaky detection is the weakest heuristic in this report.** "Detection is automatically activated **when Playwright retries are enabled**. When a test has retries enabled and it doesn't pass on the first attempt, it is marked as flaky" ([flaky-tests](https://docs.currents.dev/dashboard/tests/flaky-tests.md)). This is Playwright's own `flaky` status surfaced in a dashboard — ["tests that failed on the first run, but passed when retried"](https://playwright.dev/docs/test-retries) — not independent cross-run analysis. Flakiness *rate* is computed differently on three different pages (flaky ÷ passed; flaky results ÷ selected results; flaky executions ÷ overall executions). **Compute spend: none** — "Currents doesn't automatically rerun failed tests itself."

**Pricing** ([currents.dev/pricing](https://currents.dev/pricing)): **Scale $49/mo** (10K test results/mo, **$4.90 per additional 1K**, 50 users); **Business $99/mo** (10K results, **$9.90 per additional 1K**, + SSO/SCIM); Enterprise custom, self-hosting available. Note the unit economics: $4.90/1K = **$4,900 per million test results**, versus Trunk's and Datadog's $3/million. Currents is priced for small, slow E2E suites, not unit-test volume.

**AI integration — surprisingly deep.** `@currents/mcp` (local stdio via npx, `CURRENTS_API_KEY` with read-only or read/write scoping) exposing `currents-cancel-run`, `currents-reset-run`, `currents-delete-run` plus retrieval of projects/runs/results/perf metrics ([mcp-server](https://docs.currents.dev/ai/mcp-server.md)). Plus an **IDE extension** for VS Code/Cursor that surfaces CI failures and auto-registers the MCP server; a dashboard **"Fix with AI"** button that generates a prompt containing "the processed error, code frame, a link to the error-context snapshot, and the MCP identifiers"; and an **n8n integration** for "workflows that run without a human in the loop" that can generate "a PR that heals the failing test" ([ai/overview](https://docs.currents.dev/ai/overview.md)).

**Sorry Cypress.** OSS MIT self-hosted "drop-in replacement for Cypress Dashboard" — parallelisation, video/screenshot storage, integrations ([repo](https://github.com/sorry-cypress/sorry-cypress)). Currents.dev is the managed descendant. **No flaky detection, no Playwright support.** 2,808 stars, last pushed 2025-09-14 — roughly a year stale as of this report (GitHub API, 2026-09-20). Treat as maintenance mode.

**Adjacent.** **Azure Playwright Workspaces** (renamed from Microsoft Playwright Testing, under Azure App Testing; doc updated 2026-09-16) is a **cloud browser execution** service billing on browser minutes, with session recordings, traces, Live View and "Take Control" interactive debugging ([overview](https://learn.microsoft.com/en-us/azure/app-testing/playwright-workspaces/overview-what-is-microsoft-playwright-workspaces)). It spends compute heavily — that is the product — but has **no flaky-test analytics**. **ReportPortal** (OSS) does ML defect *triage*, not flake detection: "an XGBoost model which features (about 30 features) represent different statistics about the test item, log message texts, launch info," assigning a defect type only at ≥50% probability ([auto-analysis docs](https://reportportal.io/docs/analysis/AutoAnalysisOfLaunches)). **Allure TestOps** advertises flaky detection with no published mechanism or pricing — unverified.

### 1.6 CI-provider built-ins (the free baseline you are competing against)

- **CircleCI Test Insights**: flaky = "failed and passed on the same commit in a **14-day window**" ([docs](https://circleci.com/docs/guides/insights/insights-tests/)). Purely passive; no reruns. CircleCI also does timing-based test splitting.
- **GitLab**: unit test reports parse JUnit XML and show "Failed {n} time(s) in {default_branch} in the last 14 days" — historical tracking, not flake detection; no test selection ([docs](https://docs.gitlab.com/ci/testing/unit_test_reports/)).
- **Playwright**: `--retries=N` labels a test `flaky` natively, free ([docs](https://playwright.dev/docs/test-retries)).
- **Bazel**: `--flaky_test_attempts` retries a failing test (default 3 for targets with the `flaky` attribute) and reports it as FLAKY; `--runs_per_test` plus **`--runs_per_test_detects_flakes`** actively classifies — "If one or more runs for a single shard fail and one or more runs for the same shard pass, the target will be considered flaky" ([user manual](https://bazel.build/docs/user-manual)). This is free, local, compute-spending flake classification.

---

## 2. Predictive test selection and test-impact analysis

### 2.1 CloudBees Smart Tests (formerly Launchable)

**Status.** CloudBees acquired Launchable, announced **2024-08-07** ([CloudBees newsroom](https://www.cloudbees.com/newsroom/cloudbees-acquires-launchable-to-boost-genai-efforts-across-devsecops); [SiliconANGLE](https://siliconangle.com/2024/08/07/cloudbees-acquires-ai-based-software-testing-startup-launchable/)), terms undisclosed; founders Kohsuke Kawaguchi and Harpreet Singh returned to CloudBees. Now **CloudBees Smart Tests** ([docs](https://docs.cloudbees.com/docs/cloudbees-smart-tests/latest/)); the CLI renamed `launchable` → `smart-tests` ([cloudbees-oss/smart-tests-cli](https://github.com/cloudbees-oss/smart-tests-cli)). Alive and actively documented.

**Mechanism — changed since the acquisition.** Current docs say PTS "uses **large language models (LLMs)** to understand your code changes… Using commit information, it calculates the **similarity between changed files and test files**" ([predictive-test-selection.adoc](https://github.com/cloudbees-oss/smart-tests-cli/blob/main/smart_tests/docs/modules/features/pages/predictive-test-selection.adoc)). No coverage instrumentation, no build-graph integration — a notable retreat from Launchable's original gradient-boosted-tree approach.

**Inputs.** `smart-tests record build` (walks git history), `record session`, `subset`, `record tests` (JUnit XML; a generic "file" profile covers unsupported runners) ([CLI reference](https://github.com/cloudbees-oss/smart-tests-cli/blob/main/smart_tests/docs/modules/references/pages/cli-reference.adoc)).

**The distinctive feature — selection targets expressed as confidence.** `--confidence 90%`, `--time 10m`, or `--target 20%`. Confidence is defined as "the probability of correctly catching a failing session"; the worked example: "if we set our optimization target to **90% confidence**, we should expect only to run **10 minutes** of tests and expect to catch **90% of failing sessions**… if we set our optimization target to 25 minutes, we should expect to catch **95% of failing sessions**" ([subset target docs](https://github.com/cloudbees-oss/smart-tests-cli/blob/main/smart_tests/docs/modules/send-data-to-smart-tests/pages/subset/choose-a-subset-optimization-target.adoc)). Caveat in the same doc: confidence/time targets can return *all* tests when the input list is shorter than the target.

**Compute spend.** Pure analysis, plus an explicit **observation mode** (`record session --observation`) that runs the *full* suite while recording what the subset *would* have done — a deliberate paid-verification phase ([observe-subset-behavior.adoc](https://github.com/cloudbees-oss/smart-tests-cli/blob/main/smart_tests/docs/modules/send-data-to-smart-tests/pages/subset/observe-subset-behavior.adoc)). Ramp-up: "typical duration: **1–2 weeks** worth of test run data" ([onboarding guide](https://github.com/cloudbees-oss/smart-tests-cli/blob/main/smart_tests/docs/modules/references/pages/onboarding-guide.adoc)).

**Uncommitted diffs:** no — selection keys off recorded commits/builds. **Pricing:** not public. **Effectiveness:** CloudBees marketing cites "50% reduction in machine hours, 90% reduction in test execution times, 40% reduction in build times" for early customers including BMW and GoCardless, but the cited blog contains no named customers or figures — **unverified**. **AI agents:** CloudBees ships a Unify MCP server ([devops-agent-kit](https://github.com/cloudbees-oss/devops-agent-kit)) covering CI runs, logs, security, flags — **no Smart Tests tools in it**.

### 2.2 Gradle Develocity — the most relevant competitor to several Pandora ideas

**Predictive Test Selection.** ML trained on your Build Scan history plus "millions of test executions across many projects"; before each run "the model cross-references a **snapshot of the test suite and the code under test** with change and outcome history" ([docs 2026.2](https://docs.develocity.ai/2026.2/using-develocity/predictive-test-selection/)). Signals include code-change sensitivity, test flakiness history, recent failures, and **whether tests have already passed against identical inputs** — i.e. memoization is folded into the selection model ([PTS docs](https://docs.develocity.ai/predictive-test-selection/)). Safety rails: "always selects tests that were recently added, changed, failed, or found flaky"; "the model **errs toward selection when uncertain**"; every select/skip reason appears in the Build Scan.

**Inputs.** Gradle 5.0+ / Maven Surefire+Failsafe, Develocity 2022.2+, **task/goal input-file capturing enabled**, Build Scans published. JUnit Platform (JUnit 3/4/5, Spock, Kotest ≥5.6, TestNG, jqwik, ArchUnit, Cucumber with config). **No coverage instrumentation, no git.**

**Uncommitted diffs: YES — the key differentiator in this whole report.** Selection is driven by **task input-file fingerprints, not VCS commits**, and the docs explicitly recommend PTS for "local and pre-merge/pull-request builds" ([guide](https://docs.develocity.ai/2026.2/guides/predictive-test-selection/)).

**Knobs.** Modes `RELEVANT_TESTS` (default) and `REMAINING_TESTS` (runs the previously-skipped complement — a built-in nightly safety net). Profiles `CONSERVATIVE` / `STANDARD` / `FAST`. `mustRun { includeClasses / includeAnnotationClasses }` pins tests. A **Simulator** replays historical builds to estimate "what enabling PTS would have saved, and what it would have missed," visualising % task failures predicted, savings potential, avoidable tests, and risk trends — **copy this; it is how you sell a probabilistically-unsafe feature.**

**Data requirement:** "at least **50 executions over a 14-day period**" and "at least **14 days** of code and dependency changes"; below that, all tests are selected. Calibration claim: **"calibrated to catch over 99% of non-flaky test task/goal failures."**

**Documented limitations.** Tests must declare all file inputs; **not supported** for `buildSrc`, included builds, or Android device tests; some JUnit Platform engines (Spek) unsupported; coverage measurement is recommended only with PTS **disabled**.

**Published numbers.** Dashboard example: "**51 days 11 hours of serial test time saved**" across 47.3K test tasks (63% of total), and a task at mean duration 43m07s, "**41% faster** with test selection enabled" ([product page](https://develocity.ai/product/predictive-test-selection/)) — illustrative rather than an attributed case study. The trial pitch claims averages of "50% reduction in build times, 40% decrease in test time" ([pricing](https://develocity.ai/pricing/)).

**Flaky detection — two mechanisms, one of which needs no retries** ([docs](https://docs.develocity.ai/2026.2/using-develocity/flaky-test-detection/)): (a) *within-build*, **requires retries** (Gradle/Maven Surefire/sbt/Bazel retry, junit-pioneer, Jest) — fail-then-pass in the same task execution records `FLAKY`; (b) *cross-build*, **no retries needed** — compares outcomes across builds with the **same input fingerprint**. Mechanism (b) is the same trick as Nx's, and is the single cheapest flake signal available to anyone who already memoizes by input hash. PTS "selects recently flaky tests by default."

**Build Cache** keyed on task inputs, local + distributed, for Gradle/Maven/sbt/Bazel ([docs](https://docs.develocity.ai/2026.2/using-develocity/build-cache/)); note the interaction — cache entries store only when **all** tests were selected and successful. **Test Distribution** spreads existing suites across remote agent pools ([docs](https://docs.develocity.ai/test-distribution/3.8/)).

**Pricing:** "Priced **per committer, per year**," no dollar figures published; SaaS includes a usage allowance with "sustained usage beyond that billed by consumption," self-hosted has "no usage charge" ([develocity.ai/pricing](https://develocity.ai/pricing/)).

**AI agents: first-class.** Develocity MCP Servers (2025.3+) expose Build Scans, test results, failure analysis, Policy Scan results and org-wide analytics; documented clients include Claude Code, GitHub Copilot and Gemini ([MCP docs](https://docs.develocity.ai/2026.2/integrations/agentic-ai/mcp-servers/)). There is also a "Build Caching Optimizer Agent."

### 2.3 SeaLights (now Tricentis)

**Status.** Tricentis acquired SeaLights on **2024-07-17** ([Wikipedia: Tricentis](https://en.wikipedia.org/wiki/Tricentis)); `sealights.io/test-impact-analysis/` now 301s to `tricentis.com/products/quality-intelligence-sealights` (observed 2026-09-20). Docs live at [docs.sealights.io](https://docs.sealights.io/).

**Mechanism — the most coverage-centric here.** Three ways to associate code with tests ([docs](https://docs.sealights.io/knowledgebase/test-optimization/how-it-works/associating-code-with-tests.md)): **statistical modelling** ("machine learning to analyze each test's impact on code during parallel testing"), **one-to-one mapping** (**OpenTelemetry instrumentation** capturing every application operation), and **calibration** (sequential test runs simulating 1:1 mapping — accurate immediately, slow).

**Change detection at method granularity.** A **Build Scanner** agent "scans all binaries and artifacts during each build, mapping the code and detecting changes at the **method/function level**"; it "ignores branch names and instead compares the **hash** of the method/function" ([docs](https://docs.sealights.io/knowledgebase/test-optimization/how-it-works/detecting-modified-code.md)). **Uncommitted diffs: no** — it operates on compiled artifacts.

**The safety rule worth stealing.** "If 60% of your tests are not enough to cover 100% of the methods you changed, SeaLights will **prioritize safety and add tests until every code change is exercised at least once**" ([docs](https://docs.sealights.io/knowledgebase/test-optimization/how-it-works/generating-test-recommendations.md)). Recommendation inputs: modified-code coverage, tests that failed in the last run of that stage, pinned must-run tests, new/unmapped tests, dependent tests, previously-blocked tests.

**Enforcement is in-runner.** "SeaLights agents instrument the code, **query the SeaLights API for the exclusion list, and skip those tests for you dynamically**" ([docs](https://docs.sealights.io/knowledgebase/test-optimization/how-it-works/integrations.md)). Frameworks: JUnit, TestNG, Cucumber, xUnit, nUnit, Mocha, Cypress, Jest, Robot, plus a public API. Quality gates "block untested code changes from reaching production." Pricing not public; no MCP/AI integration found (**unverified**).

### 2.4 Harness Test Intelligence

A "**Test Graph**" correlating class methods to test methods, maintained by "**smart instrumentation**… we instrument the **byte code on the fly**" — "this doesn't require any change in the source code, build, or test process." Three selection signals: changed code (git-driven), changed tests, new tests; the graph syncs on merge to main ([harness.io/blog/test-intelligence](https://www.harness.io/blog/test-intelligence), 2021-10-04, updated 2026-08-31).

Safety posture: "We are running **all the tests whenever we are not certain** about the changes in the PR." At publication it "only supports Java"; broader language support is claimed in marketing but **unverified** (the TI documentation paths returned 404 throughout this research; the docs appear restructured).

**Numbers:** Harness's own backend repo saw "**20–60% in savings**," cycle time ~60 min → 24–48 min (blog above); marketing claims "up to 80%" acceleration ([products/continuous-integration](https://www.harness.io/products/continuous-integration)). **Uncommitted diffs:** no.

### 2.5 CircleCI — parallelisation, not selection

`circleci tests split` divides a known test list across parallel executors by name (default), `--split-by=filesize`, or `--split-by=timings`. Timing data comes from **JUnit XML uploaded via `store_test_results`**, requiring `file` attributes on `<testsuite>`/`<testcase>` and `time` on `<testcase>`; "the first time the tests are run there will be no timing data." `circleci tests split` has been **superseded by `circleci tests run`**, which adds rerun-failed-tests ([docs](https://circleci.com/docs/guides/optimize/parallelism-faster-jobs/)). **It never reduces the test set based on code changes.**

Flaky detection: pass+fail on the same commit in a **14-day window**; insights over the latest 100 runs; OAuth orgs only ([docs](https://circleci.com/docs/guides/insights/insights-tests/)). Pricing is credit-based: Free $0 (30,000 credits/mo, 5 users); Performance from **$15/mo** (30,000 credits, **$15 per additional 25,000**, $15/mo per extra user); Scale custom ([pricing](https://circleci.com/pricing/)). Flaky detection is limited to 5 tests on Free.

### 2.6 Microsoft Test Impact Analysis (Azure DevOps / VSTest)

A **code-coverage-style data collector** (`datacollector://microsoft/TestImpact/1.0`) profiles instrumented runs to build per-test dependency maps at `<ImpactLevel>file</ImpactLevel>`; on a commit, tests whose dependency list includes a changed `.cs`/`.vb` file are selected, plus "existing impacted tests, **previously failing tests, and newly added tests**" ([learn.microsoft.com](https://learn.microsoft.com/en-us/azure/devops/pipelines/test/test-impact-analysis), ms.date 2018-12-07, page updated 2026-05-07).

**Safety by fallback:** "For commits and scenarios that TIA can't understand, it **falls back to running all tests**… if the code commit contains changes to HTML or CSS files, it can't reason about them and falls back." Overrides: periodic run-all (recommended), `DisableTestImpactAnalysis=true`, `TIA_IncludePathFilters`, and a user-supplied `TIA.UserMapFile`.

**Hard limits still in current docs:** managed .NET only, **single-machine topology**; not supported for multi-machine, **data-driven tests**, adapter parallel execution, **.NET Core**, or UWP. No numeric effectiveness claims on the page; the 2017 MSDN series is linked but dead. Maintained but visibly stale, with no deprecation notice as of the 2026-05-07 revision. Microsoft's own verification advice — run impacted and all tests in sequence and compare — is itself the criticism.

### 2.7 Meta Predictive Test Selection (Machalica et al., 2019) — the best-documented system

[arXiv:1810.05286](https://arxiv.org/abs/1810.05286), ICSE-SEIP 2019; blog [2018-11-21](https://engineering.fb.com/2018/11/21/developer-tools/predictive-test-selection/).

**Abstract numbers, verbatim:** the strategy "reduces the total infrastructure cost of testing code changes by a **factor of two**, while guaranteeing that **over 95% of individual test failures** and **over 99.9% of faulty changes** are still reported back to developers." It "also accounts for the non-determinism of test outcomes, also known as test flakiness."

**Method.** Binary classifier over (change, test-target) pairs, **gradient boosted decision trees**. Candidates are always `DependentTests(d)` from the **Buck build graph** — the model only *prunes* a safe set. Selection = `LikelyFailing ∪ HighlyRanked` with a `ScoreCutoff` and `CountCutoff`.

**Features.** Change-level: file/target cardinality, change counts over 3/14/56 days, file-extension bit vector, distinct authors. Target-level: historical failure rates over 7/14/28/56 days, project name, test count. Cross: **minimal distance from changed file to target in the dependency graph**, common path tokens. Feature selection dropped common-tokens and distinct-authors as regressions.

**Training data is bought with compute.** Labels need full `DependentTests(d)` runs, so Meta schedules **"learning test runs" on roughly a quarter of submitted code changes**, deferred off-peak. Retrained **weekly**, auto-promoted only if e.g. `SelectionRate < 0.3` at `TestRecall = 0.9`. **This is the single closest published precedent for Pandora's "use idle capacity to generate background verification work" thesis** — Meta pays a ~25% compute tax specifically to manufacture training labels.

**Flakiness.** Failing targets are rerun **up to ten times**; `|FlakedTests|` is **~4× |FailedTests|**. Without de-flaking, the model learns to predict flakes.

**Calibration and results.** `TestRecall > 0.95`, `ChangeRecall > 0.999`, `SelectionRate < 0.33`; deployment cut **total test executions 3× and machine cost 2×** versus the build-dependency baseline. Baseline context: "almost **99.9%** of test targets selected by build-dependency-based selection pass"; average `|DependentTests(d)| ≫ 1000`. Selecting just the **two** top-scoring targets catches ≥1 failure on **70%** of faulty changes.

**Scope — runs on uncommitted diffs.** PTS runs at **diff-time (uncommitted review diffs rebased on known-good master) and land-time**. It is explicitly **not safe** ("needs not be conservative"); the net is a **stabilization stage** running all tests on master every few hours, with release candidates cut only from stabilized revisions.

**Later Meta work:** [Diff Risk Score, 2025-08-06](https://engineering.fb.com/2025/08/06/developer-tools/diff-risk-score-drs-ai-risk-aware-software-development-meta/) — a fine-tuned Llama scoring diffs for risk, powering 19+ use cases including "optimizing build and test selection"; no precision/recall published (**unverified**).

### 2.8 Meta Probabilistic Flakiness Score (PFS)

[engineering.fb.com, 2020-12-10](https://engineering.fb.com/2020/12/10/developer-tools/probabilistic-flakiness/) — **a blog post, not a paper.** PFS = "how likely the test is to fail, provided it could have passed on the same version of code and in the same state of the world, had it been retried." A two-parameter per-test model — P(bad state) and P(fail | good state) — with **Bayesian inference implemented in Stan**, producing posteriors rather than point estimates. Inputs are existing CI outcomes: a failing first attempt is retried **once**; there is no dedicated rerun fleet. Deployed **mid-2018** across millions of tests. Thresholds are framework-dependent: "**well below 1% for unit tests, ~10% for some end-to-end frameworks**." Tests above threshold are **made ineligible for change-based testing** — flakiness gates the test out of selection entirely. No precision/recall published. Buildkite's Enterprise-only PFS monitor is the only commercial implementation of this lineage.

**Distinct from Apple:** Kowalczyk et al., [*Modeling and Ranking Flaky Tests at Apple*, ICSE-SEIP 2020](https://conf.researchr.org/details/icse-2020/icse-2020-Software-Engineering-in-Practice/2/Modeling-and-Ranking-Flaky-Tests-at-Apple) uses **entropy + flipRate** (frequentist), reporting **flakiness reduced 44% with <1% loss in fault detection** — the best published precision/recall-style number in the flake-management literature.

### 2.9 Google TAP and flaky-test statistics

**[Taming Google-Scale Continuous Testing, ICSE-SEIP 2017](https://huang.isis.vanderbilt.edu/cs8395/paper/google-testing-icse-seip-17.pdf).** TAP computes **AFFECTED targets as the reverse-dependency closure over the Blaze/Bazel BUILD graph** — pure static analysis, no ML, safe by construction at *target* granularity. Because cost grew quadratically, TAP **batches commits into "milestones" cut every ~45 minutes at peak**.

Numbers: 2B LOC; ~1 commit/second; **>13K projects, 800K builds, 150M test runs per day**; milestones up to **4.2M tests**, AFFECTED sets up to **1.6M**; delays up to 9 hours. Over Feb 11 – Mar 11 2016, of **5,562,881 affected targets** (4B+ outcomes) only **~63K ever failed**. Per average CL: roughly half PASSED, **43% AFFECTED-but-unexecuted**, 7.4% SKIPPED, **<0.5% FAILED**. Only **1.23% of test executions found a breakage or fix**. And the empirical case for pruning a safe graph: "**targets more than 10 dependency edges from the change hardly ever break**."

**Flakiness statistics** ([Micco, 2016-05-27](https://testing.googleblog.com/2016/05/flaky-tests-at-google-and-how-we.html)): "**about 1.5% of all test runs reporting a 'flaky' result**"; "**almost 16% of our tests have some level of flakiness**"; "**about 84% of the transitions we observe are from flaky tests**." Corroborated by [Leong et al., ICSE-SEIP 2019](https://mpapad.github.io/publications/pdfs/ICSE-SEIP2019.pdf) (flaky executions inflate the transition rate from 1.4% to 3.8%). [Listfield, 2017-04-17](https://testing.googleblog.com/2017/04/where-do-our-flaky-tests-come-from.html): 4.2M tests; flakiness correlates **linearly with binary size**; when a code change made a stable test flaky, the root cause was a production bug **about 1 in 6 times**. The widely-repeated "2–16% of compute spent on reruns" figure could **not be verified** from a primary Google source — **unverified**.

### 2.10 Academic regression test selection, briefly

- **Ekstazi** (Gligoric, Eloussi, Marinov, ISSTA 2015, [PDF](https://users.ece.utexas.edu/~gligoric/papers/GligoricETAL15Ekstazi.pdf)): **dynamic, file-level**. A Java agent records the file set each test class touches, with checksums; re-runs a test iff a dependency's checksum changed. **No VCS integration, so it works on arbitrary working-tree state including uncommitted edits.** 615 revisions / 32 projects / ~5M LOC: **end-to-end time −32% on average, −54% for longer suites**. Cost: collection overhead per run; for five projects with sub-5-second suites it was *slower* than retest-all.
- **STARTS** (Legunsen, Shi, Marinov, ASE 2017, [PDF](https://www.cs.cornell.edu/~legunsen/pubs/LegunsenETAL17STARTS.pdf), [GitHub](https://github.com/TestingResearchIllinois/starts)): **static, class-level firewall** over a bytecode type-dependency graph; no instrumentation. Selects **35.2% of tests**, end-to-end **81.0%** of retest-all.
- **Static vs dynamic safety** (Legunsen et al., FSE 2016, [PDF](https://www.cs.cornell.edu/~legunsen/pubs/LegunsenETAL16StaticRTSStudy.pdf)): 985 revisions / 22 projects. Against Ekstazi as reference, **class-level static RTS violated safety on 0.2% of revisions**; **method-level static RTS on 10.6%**. End-to-end 62.5% vs Ekstazi's 64.0% of retest-all. Their warning is the field's: "any RTS technique can be simply made faster by not selecting to run some tests, but then it risks missing regressions."
- **Shared failure modes:** reflection and dynamic class loading break static graphs (the concrete FSE 2016 violation is a `getClass().getName()` + `newInstance()` chain in Commons Math); non-code dependencies (config, resources, fixtures) are tracked as files by Ekstazi but invisible to pure type-graph RTS; test-order dependence breaks the independence assumption; dynamic RTS is unsafe across a revision boundary because dependencies were collected on the *old* code; **none of them address flakiness.**

### 2.11 Test-selection cross-cutting table

| | Signal | Needs coverage/instrumentation | Uncommitted diffs | Spends compute | Safe? |
|---|---|---|---|---|---|
| CloudBees Smart Tests | LLM file/test similarity + history | No | No (commit-based) | Observation mode (full runs) | No — probabilistic confidence target |
| Develocity PTS | ML on task input fingerprints + outcome history | No (build inputs) | **Yes** | No (retries optional) | No — profiles + must-run pins + `REMAINING_TESTS` |
| SeaLights | Coverage footprint (OTel/statistical) + method hashes | **Yes** | No (artifact-based) | Calibration runs | Near-safe: adds tests until every changed method is covered |
| Harness TI | Bytecode-instrumented Test Graph + git | Yes (on the fly) | No | Instrumentation overhead | Falls back to all tests when uncertain |
| CircleCI | Timing data only | No | N/A | `--rerun-failed` | N/A — not selection |
| MS TIA | File-level coverage dependency map | **Yes** | No | No | Falls back to all tests on unknown file types |
| Datadog TIA | Per-test coverage + Bloom filter vs changed files | **Yes** | No | No | Biased so tests are never wrongly skipped (~0.04% FP) |
| Meta PTS | GBDT over change/target/graph features | No (build graph) | **Yes (diff-time)** | **Yes** (≈25% learning runs + 10× deflake) | No — stabilization stage is the net |
| Google TAP | Bazel reverse-dependency closure | No | Presubmit on pending CLs | Yes (FACF reruns) | **Yes**, at target granularity |
| Ekstazi / STARTS | File / type dependency checksums | Ekstazi yes, STARTS no | **Yes** | No | STARTS class-level: 0.2% safety violations |

**The two durable criticisms of this whole category.** (1) Everything ML-based is *statistically* unsafe and needs a second line of defence — Meta's stabilization stage, Google's postsubmit, Develocity's `REMAINING_TESTS`, Microsoft's periodic run-all. A vendor that sells selection without also selling the safety net is selling half a product. (2) All ML approaches need weeks of history before they work (Develocity: 50 executions / 14 days; CloudBees: 1–2 weeks; Meta: 3 months plus a ~25% compute tax to generate labels), and models go stale as the codebase moves. Meta's weekly retrain with an automated promotion gate is the only published example of staleness being handled explicitly.

---

## 3. Merge queues and speculative merge testing

### 3.0 The shared mechanism

All of these implement Graydon Hoare's "Not Rocket Science Rule" — automatically maintain a repository of code that always passes all the tests ([graydon/bors](https://github.com/graydon/bors)). Serial queues are safe but slow: a repo landing 40 changes/day with 15-minute CI tops out around 32/day ([Graphite's history writeup](https://graphite.com/blog/bors-google-tap-merge-queue)). Speculation is the fix: test PR *N* against the *predicted* future main (main + PRs 1..N−1), in parallel, and throw away the wrong guesses.

**Universal compute model: every commercial vendor orchestrates; the customer's CI runs and pays for every speculative build.** Nobody in this set sells the compute. Speculation is a CI-spend multiplier traded for latency; batching is the counter-lever that can make it net-cheaper than one-PR-per-run.

**Universal: none operate on uncommitted working-tree state.** Confirmed across bors-ng, rust-lang/bors, Mergify, marge-bot, GitLab trains, Tide, GitHub (`gh-readonly-queue/...` branches), Aviator (draft PRs) and Zuul (Gerrit changes / GitHub PRs). Every system requires a pushed ref before it will speculate.

### 3.1 Trunk.io Merge Queue

"Test each PR against the head of `main` plus every PR ahead of it" ([docs](https://docs.trunk.io/merge-queue)). Four mechanics: predictive testing, **parallel queues** (lanes inferred dynamically from impacted build-graph targets — Bazel/Nx/custom — with no configuration), anti-flake protection, and batching.

**Batching + genuine auto-bisect.** Defaults: max wait 5 min, target batch size 4 PRs. On batch failure Trunk splits the batch in half and recurses (logarithmic), **with test caching so already-passing combinations aren't re-run**; passing PRs still merge ([batching docs](https://docs.trunk.io/merge-queue/concepts-and-optimizations/batching)).

**Optimistic merging.** A PR that failed can still merge if a *later* PR stacked on it passes — evidence the failure was flaky or fixed downstream. Trunk explicitly warns "flaky tests cause more retests with optimistic merging" and says not to enable it above a **5% flake rate** ([optimistic merging docs](https://docs.trunk.io/merge-queue/concepts-and-optimizations/optimistic-merging)).

**Claimed savings** (vendor): batching "50–70% reduction in CI minutes consumed," "3–5× more PRs per hour," "60–80%" total test-time cut; optimistic merging adds "20–30% reduction in average PR wait times, 1.5–2× higher throughput" (batching docs). Marketing cites Caseware (900-project Nx monorepo) going **6 hours → 90 minutes** time-to-merge and "cut CI costs by 60–90 percent" ([trunk.io/merge-queue](https://trunk.io/merge-queue)).

**Pricing:** Merge Queue is unlimited PRs; charging triggers above 5 private-repo committers; test-span overage $3/million ([billing docs](https://docs.trunk.io/administration/billing)). The literal per-seat dollar figure did not render on the pricing page — **unverified**.

**Baseline knowledge:** yes, via the Flaky Tests product's quarantine list plus its "broken tests" classification. **AI agents:** no MCP for the queue. Trunk positions *against* agent-driven churn: "Agents open more PRs and churn code more aggressively… logical conflicts, where two PRs merge cleanly but still break main."

### 3.2 Mergify

**Speculative checks.** Stacked temporary batch PRs representing cumulative merges — (PR1), (PR1+PR2), (PR1+PR2+PR3) — tested concurrently; "Batch 2 is tested on top of Batch 1 while Batch 1 is still validating" ([docs](https://docs.mergify.com/merge-queue/speculative-checks/)). Concurrency = `max_parallel_checks`, **up to 128**.

**Batching + bisect.** `batch_size` fixed or dynamic (`{min: 1, max: 10}`, auto-scaled by queue depth). On failure "all subsequent batches are deemed to fail as well, are canceled and put back into the queue"; the failed batch is **split into `max_parallel_checks` parts tested simultaneously**, recursing until the culprit is isolated, capped by `batch_max_failure_resolution_attempts` ([batches docs](https://docs.mergify.com/merge-queue/batches/)). Explicit cost framing: "Small batches keep latency low and make failures cheap to isolate, but spend more CI."

**Monorepo scopes** derived from the build graph (Bazel, Nx, Turborepo, Pants) let unaffected PRs **merge directly with no queue CI run**.

**CI Insights / Test Insights — the clearest "baseline oracle" in the market.** A flaky job is defined as "the same job, in the same pipeline, runs more than once on the same commit (SHA1) and the runs end with different conclusions, even though the code did not change" ([CI Insights docs](https://docs.mergify.com/ci-insights/)). **Auto-Retry** "automatically retr[ies] failed CI jobs caused by flaky tests or transient failures" — so Mergify does spend compute. And critically, the plugins "automatically rerun your tests to catch new flaky tests before they merge and **surface existing flaky or broken tests on your default branch** — all within a CI budget you control" ([changelog 2026-07-24](https://docs.mergify.com/changelog/2026-07-24-automatic-flaky-test-detection-and-prevention/)). That is a baseline oracle, shipped, with an explicit budget control.

**Effectiveness (vendor):** batching cuts CI runs 50–80%; speculative checks give "3–5× faster merge throughput" ([docs](https://docs.mergify.com/merge-queue/)). **Pricing:** OSS free; free for private teams ≤5 active contributors; **Max $21/seat/month** (15% off annual); Enterprise custom; bots free ([pricing](https://mergify.com/pricing)). CI Insights and Test Insights are bundled in the same $21 plan.

### 3.3 Aviator MergeQueue

**Parallel mode** creates **Draft PRs** combining queued PRs cumulatively; "a new branch with changes from both PR#1 and PR#2… a Draft PR is created from that new branch to trigger the CI runs," capped by "Max bot builds in parallel," beyond which queuing pauses ([docs](https://docs.aviator.co/mergequeue/concepts/parallel-mode)). On a later draft failing, "the bot closes all subsequent Draft PRs and restarts the queue after removing the failing PR."

**Batching + bisect.** `batch_size` groups PRs per CI run: 10 queued PRs need 10 extra pipelines at `batch_size=1`, **4 at 3, 2 at 5**; on batch failure Aviator requeues into halves and recurses ([batching docs](https://docs.aviator.co/mergequeue/concepts/batching)). Parallel queues derive from independent code paths in a monorepo — "thousands of parallel queues."

**Optimistic validation** (`use_optimistic_validation`, default true) with `optimistic_validation_failure_depth` capped at **3**: if the top draft's CI is still running or failed but a subsequent draft passes, the success validates it. A **beta flaky-test management** feature "reads the failure, reruns the ones that would pass on a retry, and stops those failures counting against the depth" ([docs](https://docs.aviator.co/mergequeue/concepts/managing-flaky-tests-in-mergequeue)). Acknowledged tradeoff: real failures linger in the queue longer.

**FlexReview** is a CODEOWNERS drop-in replacement using **domain expertise scoring** from historical authorship and review data, recursive ownership via distributed YAML, and reviewer minimisation; strategies include expertise, load balancing and oncall rotation ([docs](https://docs.aviator.co/flexreview)).

**Pricing** ([aviator.co/pricing](https://www.aviator.co/pricing)): Free $0 (no MergeQueue; 10 PR Verification runs/mo, up to 3 repos); **Team $20/dev/mo** (MergeQueue, ≤10 repos); **Scale $50/dev/mo** (full Verify, 50 verification runs/user/mo then **$1/run**, monorepo-aware features); Enterprise custom, self-hosted. Enterprise offers "Bring your own AI keys (Claude, OpenAI, Bedrock) — no credit limits"; the MergeQueue tagline is "Ship AI-generated code at velocity you trust," but no auto-fix-on-failure agent is documented.

### 3.4 GitHub native merge queue

"When a pull request is added to the merge queue, the changes in the pull request are grouped into a `merge_group` with the latest version of the `base_branch` as well as changes from pull requests ahead of it in the queue" ([docs](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue)). Validation runs on temporary branches prefixed `gh-readonly-queue/{base_branch}` — different SHAs from the PR, so third-party CI must watch those refs, and Actions workflows need `on: merge_group`.

**Limits:** build concurrency is "the maximum number of `merge_group` webhooks to dispatch (**between 1 and 100**)"; min/max group size 1–100 PRs; plus a wait-time setting and a `checks_timeout`.

**No bisect.** On CI failure "the merge queue automatically removes pull request #1 from the merge queue" and **recreates the temporary branches with only the remaining PRs' changes** — a queue reset, not a binary search. Trunk's competitive page characterises this as re-testing survivors from scratch, validating up to 100 PRs concurrently but merging strictly FIFO so one slow check blocks everything behind it, with only an opt-in ejection delay for flakes and "no detection or quarantine" ([trunk.io/trunk-vs-github-merge-queue](https://trunk.io/trunk-vs-github-merge-queue)) — vendor-sourced, treat as advocacy.

**Availability:** docs frontmatter versions the feature for Free/Pro/Team, Enterprise Cloud and Enterprise Server ([source markdown](https://raw.githubusercontent.com/github/docs/main/content/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue.md)); the exact private-repo plan restriction is **unverified**. No separate charge; cost is Actions minutes ([github.com/pricing](https://github.com/pricing)). GA'd mid-2023.

### 3.5 Graphite — still shipping, and the most agent-integrated

Not deprecated: docs carry Merge Queue Overview, Setup, Use, Optimizations, and External Merge Queue Integration (beta) ([graphite.com/docs](https://graphite.com/docs)). Domain moved graphite.dev → graphite.com.

Three optimizations ([docs](https://graphite.com/docs/merge-queue-optimizations)): **fast-forward merge** (all PRs in a stack processed concurrently), **parallel CI** ("speculative execution, similar to branch prediction, to run CI for multiple enqueued stacks at the same time"), and **batching (beta)** with two recovery strategies on batch failure — **full parallel isolation** (test every stack concurrently) or **bisection** (binary search, "fewer CI runs"). Passing stacks merge even when others fail.

**Numbers (vendor):** parallel CI gives "**1.5× faster merges**… 33% decrease for p95, 26% for p75"; heavy stacked-PR users see "up to **2.5× faster merges** (60% decrease for p95, 34% for p75)."

**Pricing** ([graphite.com/pricing](https://graphite.com/pricing)): Hobby free; **Starter $20/user/mo**; **Team $40/user/mo** (merge queue included); Enterprise custom. **AI:** "AI Reviews" / Graphite Chat for logic bugs, security, performance and edge cases with one-click fixes ([graphite.com/diamond](https://graphite.com/diamond)); docs include an **Agents** section and a **GT MCP** server; the pricing page advertises "Cursor Cloud Agents are now in Graphite."

### 3.6 bors-ng and successors

**bors-ng** batches `r+`-approved PRs into a merge commit on a `staging` branch; only on green does it fast-forward master. **On batch failure it splits into two smaller batches and recurses** until the culprit is isolated — O(E log N) CI runs versus O(N) serial ([README](https://github.com/bors-ng/bors-ng/blob/master/README.md)). `rollup`/`batch` comments set priority −1 to deliberately group trivial PRs. **Status: deprecated around May 2023, repo archived 2024-04-04**, read-only; the maintainer recommends GitHub's built-in merge queue ([bors.tech](https://bors.tech/)).

Lineage: Graydon's original bors (Rust, ~2013) → **Homu** ([rust-lang/homu](https://github.com/rust-lang/homu/blob/master/README.md)). Rust rollups are **manually curated**, not auto-bisected ([Rust Forge](https://xampprocky.github.io/rust-forge/release/rollups.html)). **rust-lang/bors** (Rust rewrite, check-suite webhooks) runs auto builds **one at a time** — serial, not speculative ([design.md](https://github.com/rust-lang/bors/blob/main/docs/design.md)); Homu migration completed Q4 2025 ([recap](https://blog.rust-lang.org/inside-rust/2026/01/13/infrastructure-team-q4-2025-recap-and-q1-2026-plan)).

Adjacent OSS: **marge-bot** (GitLab) batches already-green MRs and pushes directly to the target ([repo](https://github.com/smarkets/marge-bot)). **GitLab merge trains** — native, **Premium/Ultimate only** ([docs](https://docs.gitlab.com/ci/pipelines/merge_trains/)). **Prow/Tide** (Kubernetes) "automatically runs batch tests and merges multiple PRs together whenever possible" ([docs](https://docs.prow.k8s.io/docs/components/core/tide/)) — but batch-failure handling has been buggy in practice: a batch retested **270+ times** without shrinking ([test-infra #26148](https://github.com/kubernetes/test-infra/issues/26148)), and one flaky batch-mate discards an otherwise-green batch ([#13149](https://github.com/kubernetes/test-infra/issues/13149)).

### 3.7 Zuul — the deepest speculation model, and the best answer to "how much speculation should I buy?"

**Dependent pipeline.** "It assumes that all jobs will succeed and tests them in parallel accordingly" ([gating docs](https://zuul-ci.org/docs/zuul/latest/gating.html)). For queue A,B,C,D,E: jobs for B merge A+B then test; jobs for E merge A,B,C,D,E then test. Every change is tested in the exact merged state it would land in. Cross-project `Depends-On:` URLs form a DAG spanning repos, serialised naturally in a dependent pipeline — **speculative state across multiple repositories, including speculative artifacts, is a feature no vendor queue matches.**

**Gate reset.** C fails → C is removed, D is re-tested against tip without C, E re-tested assuming D passes. Everything behind the failure is discarded and re-run. That is the dominant cost of the model.

**Window = TCP congestion control.** `window` default **20**, `window-floor` **3**, `window-ceiling` null (unbounded), `window-increase-type` **linear** with factor **1**, `window-decrease-type` **exponential** with factor **2** ([pipeline config reference](https://zuul-ci.org/docs/zuul/latest/config/pipeline.html)). Success grows the window by one; failure halves it. **This is the most principled published answer to sizing speculation: adaptive, flake-responsive, self-throttling under instability.** Apache-2.0, self-hosted; all compute is yours. No auto-bisect (it ejects and resets) and no AI-agent integration found (**unverified**).

### 3.8 Uber SubmitQueue

**Venue correction:** *Keeping Master Green at Scale* is **EuroSys 2019**, not ICSE-SEIP — Ananthanarayanan, Saeida Ardekani, Haenikel, Varadarajan, Soriano, Patel, Adl-Tabatabai, DOI [10.1145/3302424.3303970](https://dl.acm.org/doi/10.1145/3302424.3303970) (verified via the Semantic Scholar API).

**Verifiable:** SubmitQueue "ensures all build steps (compilation, unit tests, UI tests) successfully execute for every commit point" in large monolithic repos, had been in production over a year, and **scales to thousands of commits per day** (abstract).

**Unverified in this session** (ACM returned 403 on the DOI and PDF; the Uber blog 404/503'd): the binary speculation tree (each pending change lands or doesn't, giving a naive 2^n builds), the **probabilistic model** ranking which speculative branches to build first, the conflict-analysis optimisation letting changes with disjoint build targets skip speculation, and all headline build-reduction and latency percentages. **Do not cite SubmitQueue numbers without pulling the PDF.** Not open source (**unverified**).

Related, not fetched this session: Google TAP uses two pointers — latest commit and last-known-green — batches the gap, parallelises, and **auto-bisects to find the culprit and roll it back** ([summary](https://graphite.com/blog/bors-google-tap-merge-queue)); primary source is Memon et al., ICSE-SEIP 2017 (covered in §2.9).

### 3.9 Merge-queue cross-cutting answers

**Who auto-bisects a failed batch?** Trunk (split in half + test caching), Mergify (split into `max_parallel_checks` parts, recursing, capped), Aviator (requeue into halves), Graphite (bisection *or* full parallel isolation), bors-ng (split in two, recurse), Google TAP (bisect + auto-rollback). **Who does not:** GitHub native (eject head PR, rebuild remaining from scratch), Zuul (eject + gate reset), rust-lang/bors (serial), Rust rollups (manual).

**Who has baseline knowledge of already-failing tests on main?** Mergify most explicitly (Test Insights surfaces "existing flaky or broken tests on your default branch" and auto-quarantines). Trunk quarantines flakes and separately flags "broken tests." Aviator has it in beta. GitHub offers only an opt-in ejection delay.

**Recurring limitations.** (a) *Flaky tests poison batches* — the universal failure mode; Trunk says don't use optimistic merging above a 5% flake rate; Tide has documented bugs here. (b) *Cost blowup* — naive full speculation is exponential (the problem SubmitQueue's probabilistic model exists to solve); Zuul's TCP window is the only adaptive answer. (c) *Head-of-line blocking* — GitHub validates 100 concurrently but merges strictly FIFO; Aviator pauses at the parallel cap; Zuul resets everything behind a failure.

**AI agents.** Graphite leads (Agents section, GT MCP server, Cursor Cloud Agents in-product). Aviator Enterprise supports bring-your-own Claude/OpenAI/Bedrock keys for Verify. **No vendor documents agent-driven auto-fix of a failed queue build** — Nx Cloud's Self-Healing CI (§5.2) is the only product in this entire report that does that, and it is not a merge queue.

---

## 4. Auto-bisect / culprit finding

### 4.1 Chromium: Findit → LUCI Bisection

**Findit (legacy, Python 2)** targeted "compile failures, test failures, and test flakes on Chromium Waterfall & Commit Queue" ([Chrome Analysis Tooling — Findit](https://sites.google.com/chromium.org/cat/findit)). Two stages:
- **Heuristic analysis** — correlates CLs in the regression range against error strings in the failure log (file/path/symbol overlap), scoring suspects. Latency **~1–2 minutes**, but "potentially generating false positives requiring manual verification."
- **Try-job analysis** — reruns the failing compile/test at revisions inside the regression range (first red build since last known green). Reported medians: **14 min for compile failures, 24 min for swarmed gtest failures**.
- **Flaky failures** — reuses post-submit build artifacts to skip compiling, then reruns the test via Swarming **up to 400 times per revision**, using *exponential* (not binary) search to narrow the regression range before try-jobs.
- Coverage gaps: non-swarmed tests, Telemetry-based tests, JUnit tests unsupported.

**LUCI Bisection** is the Go rewrite: "the culprit finding service for compile and test failures for Chrome Browser… the rewrite in Golang of the Python2 version of Findit" ([luci-go/bisection README](https://chromium.googlesource.com/infra/luci/luci-go/+/main/bisection/README.md)).

- **Nth-section search** over the blamelist ([pkg.go.dev](https://pkg.go.dev/go.chromium.org/luci/bisection/testfailureanalysis/bisection/nthsection)). `maxRerun` sets parallelism: **1 for compile (plain bisection), 2 for test failures (trisection)** — n-ary, not strictly binary.
- **Culprit verification** — a dedicated rerun pair (culprit and its parent) sets `VerificationStatus = ConfirmedCulprit`.
- **Inputs**: Buildbucket build results (reruns triggered via Buildbucket) plus LUCI Analysis test-result data from BigQuery.
- Statuses distinguish `FOUND` (verified) from `SUSPECTFOUND` (heuristic/nth-section, unverified) ([bisectionpb](https://pkg.go.dev/go.chromium.org/luci/bisection/proto/v1)).

**It lands reverts.** Actions are `create_revert`, `submit_revert`, `comment_culprit`, `comment_revert` ([revertculprit](https://pkg.go.dev/go.chromium.org/luci/bisection/culpritaction/revertculprit)). Blast-radius controls ([configpb](https://pkg.go.dev/go.chromium.org/luci/bisection/proto/config)): separate **daily limits** for creating vs submitting reverts (exceeding downgrades to a comment); `MaxRevertibleCulpritAge` (stale culprits get a comment); and **auto-commit of reverts is not supported for test failures — compile only**, matching legacy Findit policy.

**No published accuracy or false-positive rates for either system.** The daily-limit + age-limit + verification-rerun design is the de-facto blast-radius control in place of a published confidence threshold. Internal Chromium infra, though LUCI Bisection's code is open source. Notably, the Findit App Engine app now serves **code-coverage APIs explicitly for "automated tools and AI agents"** ([findit README](https://chromium.googlesource.com/infra/infra/+/main/appengine/findit/README.md)).

### 4.2 Flake Aware Culprit Finding (Google, ICST 2023) — the single most copyable algorithm in this report

Henderson, Dorward, Nickell, Johnston, Kondareddy ([paper PDF](https://hackthology.com/pdfs/icst-2023.pdf); [research.google](https://research.google/pubs/flake-aware-culprit-finding/)).

**Setting.** Google's TAP runs tests only at *milestone* versions, so a breakage's search range spans many CLs. The baseline `Bisect(t, S, k)` is binary search with `k` deflaking reruns per version. The authors identify bisect's core flaw: **"when using k-reruns to deflake the search, accuracy plateaus while build cost continues to increase linearly."**

**Algorithm.** Bayesian noisy binary search (Rényi–Ulam "half-lie" game, building on Ben-Or & Hassidim 2008). Maintain `Pr[Cᵢ]` over n+1 hypotheses — each suspect plus **"no culprit / it was a flake."** A FAIL at version i is possible at any i; a PASS at i is impossible if the culprit is at j ≤ i — an **asymmetric oracle** (the oracle never answers PASS when it should FAIL). Update rules are closed-form given an estimated flake rate `f̂ₜ`. `NextRuns` converts the posterior to a CDF and picks the suspect where cumulative probability ≥ ½, generalised to k parallel runs and r-ary search. An optional refinement maximises expected information gain, `E[I(sᵢ)] = (1 − f̂ₜ)(n + i·f̂ₜ − 1)`.

**Priors.** (a) Minimum **build-graph distance** between files touched by each suspect and the test under culprit-finding, computed from a *stale* build-graph snapshot; (b) historical likelihood that a breakage is real vs flaky, seeding `Pr[C_{n+1}]`.

**Inputs.** Test id, ordered suspect list, prior distribution, estimated flake rate `f̂ₜ` from historical failure rate, build flags.

**Self-verification without human labels.** A conclusion is INCORRECT if, e.g., a pass at sₙ proves it was a flake, or there is a pass at the identified culprit sₖ, or no pass at sₖ₋₁. Verification uses confidence C = 0.99999999, a flakiness floor of 0.1, and **≥8 reruns per check, sampled at 1% of conclusions**. This produced **144,130 verified conclusions in 60 days**; evaluation used **13,600 test breakages**.

**The key empirical fact for Pandora:** **~60% of ranges had a real culprit; ~40% were caused by a flaky failure.** Flaky ranges had on average **8 failing tests vs 224** for real ones.

**Results** (Figs. 2–4):
- Overall accuracy: all FACF variants beat all Bisect variants, p < 10⁻¹² against an adjusted α = 10⁻⁶. Bisect(1) ≈ **0.80**; FACF(all) ≈ **0.97+**.
- Ranges **with** a culprit: Bisect(1) ≈ **96.1%**, FACF(all) ≈ **97.5%** — a modest gap.
- Ranges **without** a culprit (flaky): Bisect(1) ≈ **0.50**, FACF(all) approaches **1.00**. This is the whole win.
- **Cost**: Bisect(1) is cheapest in builds; **FACF(all) is the next most efficient while also the most accurate.** FACF(priors) "does not improve accuracy but does improve efficiency… many more culprits were found with just two builds." The info-gain optimisation showed **no significant effect**.
- Economic argument: a production culprit finder cannot know a priori that a breakage was a flake; establishing that by deflaking costs ⌈log p / log f̂⌉ runs — **~6 extra runs at p = 10⁻⁴, f̂ = 0.2.**

**Limitations.** Assumes a linear version history (Piper); "only trivial changes to the mathematics" would be needed for a git DAG. Flake-rate estimation is sensitive to recent code changes — a single change "can completely change the flakiness characteristics of a test in unpredictable ways." Single-test formulation; multiple independent bugs in one range are not handled. Internal to Google; algorithm published, no OSS release.

### 4.3 Mozilla

**mozregression** ([repo](https://github.com/mozilla/mozregression), [docs](https://mozilla.github.io/mozregression/documentation/usage.html)) — OSS interactive regression-range finder over **prebuilt** Firefox binaries. Two phases: bisect **nightlies** by date (`--good 2014-12-25 --bad 2015-01-07`, or by release number), then "once reaching a one day range… the most recent commit tested is used for the end of a new range to bisect mozilla-inbound" — coarse date-level binary search, then changeset-level binary search over integration builds.

Automation: `mozregression --command 'test-command {binary}'`; "A return value of 0 will indicate a good build, and any other return code will indicate a bad build" ([automatic bisection](https://mozilla.github.io/mozregression/documentation/automatic-bisection.html)). Placeholders/env vars expose `{binary}`, changeset, repo, build date. **Compute cost is download-bound, not build-bound** — it never compiles. Limitation: only revisions with published builds are testable.

**Backouts are human.** Sheriffs "watch the autoland tree and back out bustage/regressions as necessary" ([Sheriffing/How:To:Autoland](https://wiki.mozilla.org/Sheriffing/How:To:Autoland)); the documented workflow is "use retriggers until finding the culprit then backout the revision which started the issue" ([Sheriffing/How:To:Backouts](https://wiki.mozilla.org/Sheriffing/How:To:Backouts)). **No automated backout bot is documented.** `bugbug` is adjacent but different — it "gets called by the Gecko decision task to determine which tasks shall run for a push," i.e. ML *test selection* ([Sheriffing/How To/Bugbug](https://wiki.mozilla.org/Sheriffing/How_To/Bugbug)).

### 4.4 Bazel / bazelisk bisect

**There is no `bazel bisect` command.** The feature lives in **bazelisk**: `--bisect=<GOOD>..<BAD>`, added in v1.17.0 ([bazelisk README](https://github.com/bazelbuild/bazelisk/blob/master/README.md)). It bisects **Bazel's own versions/commits**, not your code. Critical limitation, verbatim: "Bazelisk uses prebuilt Bazel binaries at commits on the main and release branches, **therefore you cannot bisect your local commits**." `BAZELISK_SHUTDOWN` / `BAZELISK_CLEAN` reset build state between probes — a concession to Bazel's incremental-state non-determinism. For bisecting *your* repo, the documented approach is plain `git bisect run` plus remote-cache reuse ([BuildBuddy](https://www.buildbuddy.io/blog/bisect-bazel/)).

### 4.5 `git bisect` and the SaaS gap

`git bisect run <cmd>` ([git docs](https://git-scm.com/docs/git-bisect)): binary search over the commit DAG; exit 0 = good, 1–127 = bad, **125 = untestable → auto-skip**, ≥128 aborts. `--first-parent` blames the merge commit instead of descending into a branch. `--no-checkout` updates `BISECT_HEAD` without touching the worktree. Cost ⌈log₂ N⌉ full build+test cycles, and **no flake handling whatsoever** — one flaky FAIL silently corrupts the entire search. Research improvement: [arXiv:1708.06623](https://arxiv.org/abs/1708.06623) decreases the number of validity queries and finds the *latest* regression point when a bug was introduced and accidentally re-fixed, "a feature that is missing in the state-of-the-art algorithms."

**The commercial category that exists is merge-queue batch bisection, not "main went red, find the commit."** Trunk "bisects with test caching — prior passing results are reused to cut CI cost during failure isolation," at higher concurrency to isolate faster; Mergify's "bisect-on-failure automatically identifies the culprit PR within a failed batch" (both per [trunk.io/trunk-vs-mergify](https://trunk.io/trunk-vs-mergify) — a competitor-authored page, so treat the Mergify feature name as verified-by-competitor). **No mainstream SaaS verifiably bisects already-landed history.**

### 4.6 Delta debugging, and delta debugging over a diff

**ddmin** — Zeller & Hildebrandt, *Simplifying and Isolating Failure-Inducing Input*, TSE 28(2):183–200, 2002 ([PDF](https://www.cs.purdue.edu/homes/xyzhang/fall07/Papers/delta-debugging.pdf)). Greedy binary-ish partitioning: split into n chunks, test each subset, then each complement; on failure halve granularity; terminate at **1-minimal**. **Worst case O(n²) tests**, best case ~log n. Two variants: `ddmin` (simplify one input) and `dd` (isolate the difference between a passing and failing input).

**The original delta-debugging paper is itself a diff-bisector.** Zeller, *"Yesterday, my program worked. Today, it does not. Why?"*, ESEC/FSE 1999 ([PDF](https://www.cs.columbia.edu/~junfeng/18sp-e6121/papers/delta-debug.pdf)) applied dd to **178,000 changed lines between GDB 4.16 and 4.17**, isolating a single failure-inducing change "within a few hours." **"Which part of my change broke it" was solved in 1999.**

**HDD** — Misherghi & Su, ICSE 2006 ([ACM](https://dl.acm.org/doi/10.1145/1134285.1134307)): ddmin level-by-level over an AST instead of flat lines; "orders of magnitude fewer test cases." Extensions include hoisting ([arXiv:2104.03637](https://arxiv.org/pdf/2104.03637)) and ProbDD's probabilistic monotonicity assessment ([arXiv:2506.11614](https://arxiv.org/pdf/2506.11614)).

**picire / picireny** — OSS Python, BSD-3, Renáta Hodován ([picire](https://github.com/renatahodovan/picire), [picireny](https://github.com/renatahodovan/picireny)). picire implements ddmin over line- or character-chunks with subset *and* complement reduction, caching, configurable iterators and `--parallel -j N`; picireny layers HDD on top via ANTLRv4 grammars. Interface: an interestingness script taking a path, exit 0 = interesting. Because picire is format-agnostic and line-based, **pointing it at a unified diff is directly feasible** — but that is inference, not a documented use case.

**C-Reduce** — Regehr, Chen, Cuoq, Eide, Ellison, Yang, PLDI 2012 ([preprint](https://users.cs.utah.edu/~regehr/papers/pldi12-preprint.pdf)). Not ddmin: a **fixpoint loop over ~50+ pluggable modular transformations** (peephole token edits, balanced-delimiter removal, a line-removal pass, pretty-printers, and 30 Clang-based source-to-source passes). Outputs "more than 25 times smaller" than other reducers on average. Hard numbers on a 98-test corpus (mean original 81 KB): wrong-code mean 58,208 bytes → Berkeley delta+Frama-C **7,256 bytes / 4 min** → **C-Reduce+Frama-C 258 bytes / 12 min**; crash bugs mean 108,608 → **C-Reduce 151 bytes / 2 min**. **Validity problem**: 12 of 41 (**29%**) Berkeley-delta-reduced wrong-code outputs "dynamically executed an undefined behavior," making the bug report invalid — hence the added validity checkers. Documented failure mode: "**Non-deterministic execution of the system under test can cause test-case reduction to fail**" (ASLR, memory limits, timeouts).

**cvise** — "super-parallel Python port of C-Reduce," runs **16 interestingness tests in parallel** by default; on real GCC PRs: PR92516 **35 min vs 77 min**, PR94523 **15 vs 33**, PR94937 **242 vs 303** ([README](https://github.com/marxin/cvise/blob/master/README.md)).

**Delta debugging over hunks — the closest prior art.** Artho, *Iterative Delta Debugging* ([PDF](https://people.kth.se/~artho/papers/idd-full.pdf)) explicitly models the patch hierarchy: "Change sets consist of changes to individual files, which are in turn broken up into so-called 'hunks', which contain a number of line-based changes… Extending DD to include the hierarchy of the generated patches can therefore improve precision of DD." The search "proceeds hierarchically, across files, hunks, and lines." Its limitations section is the sharpest statement of the interacting-hunks problem: "Incomplete functions will not compile, resulting in invalid code subsets whenever the syntactic structure of the target language is violated"; and for a signature change, "its definition and all instances of its usage have to be changed simultaneously. **A bisection-based algorithm such as DD cannot isolate such changes and produces overly large change sets.**"

**No dedicated, maintained tool bisects an uncommitted working-tree diff hunk-by-hunk.** Searches for `git-hunk-bisect`, "hunk bisect", "bisect hunks" returned only generic `git bisect` tutorials. Treat this as **a negative result at moderate confidence** (search budget was exhausted before GitHub-code-search-style queries). The build-it-yourself stack today: `git diff > p.diff`, split with `patchutils`/`splitpatch`, drive **picire** with an interestingness script that `git apply`s the surviving subset and runs the test. Expect the IDD failure modes — map non-compiling subsets to "untestable" (analogous to `git bisect`'s exit 125) rather than "good."

### 4.7 "Which line of my change broke the test?" — other approaches

- **Orca** — Bhagwan et al., OSDI 2018, Microsoft ([PDF](https://www.usenix.org/system/files/osdi18-bhagwan.pdf)). Not bisection: differential *code search*. Ranks commits by token overlap between the failure symptom and the commit diff, weighted by a **build provenance graph** that expands the search into ancestor builds. **Correctly localises 77% of bugs** for which on-call engineers used it. Runs no tests, so it is cheap.
- **Coverage-accelerated bisection**: An & Yoo (2021) use coverage to speed up both bisect and SZZ; Saha & Gligoric (2017) accelerate bisect via coverage-based test selection (both cited in FACF §VI). FACF's build-distance prior is the static, coarse-grained cousin.
- **Multiple bugs in one range**: Keenan's *multisection* (Perl Conference 2019) is the only named approach found.
- **AI-agent integration**: **none found.** No auto-bisect tool inside Claude Code, Devin or Cursor; agents invoke `git bisect run` ad hoc as a shell command.

---

## 5. Command-level result caching / memoization by content digest

### 5.1 Bazel

Action keys hash the command line, input file contents and environment; remote cache and remote execution share that key space. For tests specifically ([user manual](https://bazel.build/docs/user-manual)):

- **`--cache_test_results`** (default `auto`): Bazel reruns a test only if "Bazel detects changes in the test or its dependencies, the test is marked as `external`, multiple test runs were requested with `--runs_per_test`, [or] the test failed." Setting `yes` additionally "may cache test failures and `--runs_per_test` executions." **Failures are not cached by default** — the industry's implicit answer to "is a red result trustworthy enough to memoize?"
- **`--runs_per_test`**: run each test N times, all treated as separate tests.
- **`--runs_per_test_detects_flakes`**: "If one or more runs for a single shard fail and one or more runs for the same shard pass, the target will be considered flaky." This is a free, local, compute-spending flake classifier.
- **`--flaky_test_attempts`**: retry up to N; "a test that initially fails but eventually succeeds is reported as FLAKY." Default 1, or 3 for targets with the `flaky` attribute.

**Determinism diagnosis.** Bazel's documented workflow is to build twice with `--execution_log_json_file` and diff: "Execution logs contain records of all actions executed during the build. For each action there is a SpawnExec element containing all of the information from the action key" ([remote-execution-caching-debug](https://docs.bazel.build/versions/main/remote-execution-caching-debug.html)). Julio Merino's ["Bazel and action (non-)determinism"](https://blogsystem5.substack.com/p/bazel-action-determinism) (2025-07-21) gives the practical recipe — `bazel clean; bazel build --noremote_accept_cached --execution_log_json_file=log //:target` on two runs, diff the logs — and catalogues the causes (timestamps, PIDs/UIDs, unsorted hash-table output, network access, hidden tool dependencies, dynamic execution mismatch, nested build systems). Key nuance: "a single non-deterministic action does not necessarily poison the whole build" — deterministic downstream actions halt propagation.

**Cache poisoning.** Actions are judged successful by exit code even when the declared outputs are wrong or missing ([bazelbuild/bazel#14543](https://github.com/bazelbuild/bazel/issues/14543)); a compiler that crashes but exits 0 can write a bad entry that every subsequent build pulls. Mitigation is trust-boundary based: only trusted writers may populate the cache.

**BuildBuddy** sells hosted Bazel remote cache + RBE + build/test UI ([docs](https://www.buildbuddy.io/docs/rbe-setup/)). Bazel-only. Pricing tiers: Personal free (100 GB cache transfer, up to 80 cores), Team pay-as-you-go (up to 800 cores), Enterprise custom ([pricing](https://www.buildbuddy.io/pricing/)) — per-GB rate not published on the page.

### 5.2 Nx / Nx Cloud — the closest existing product to several Pandora ideas

**Caching.** Nx hashes all inputs for a task; on a hit it "restores the declared outputs and replays the recorded terminal output" ([caching docs](https://nx.dev/docs/features/ci-features)).

**Flaky detection *derived from the cache key*** — the single most directly relevant precedent. "Nx creates a hash of all the inputs for a task whenever it is run… if Nx encounters a task that fails with a particular set of inputs and then succeeds with those same inputs, it marks that task as flaky" ([flaky-tasks docs](https://nx.dev/docs/features/ci-features/flaky-tasks)). The hash pins the inputs, so "code changes don't trigger false positives, and results across different machines and CI runs are treated as equivalent evidence." Detection is at **task** granularity, not test granularity — "a task can run a single Playwright spec file, a Jest project, or a whole suite." On detection Nx "automatically send[s] that task to a different agent" (max 2 attempts) to isolate agent-caused flakiness. **The flaky flag is removed after 2 weeks with no incidents.**

**Compute spend: yes** — retries of known-flaky tasks, on a different agent.

**Self-Healing CI — a shipped agentic verification loop.** Triggered when tasks fail in PR CI, via `npx nx fix-ci` run with an always-run condition. "An AI agent powered by Claude" analyses failures using the workspace project graph and build configuration, reading optional guidance from `.nx/SELF_HEALING.md` and `CLAUDE.md`. **The AI runs on Nx Cloud's infrastructure, not the customer's runner.** Fixes are delivered as PR/MR comments with diff views and apply/reject buttons, as editor notifications in VS Code/Cursor/WebStorm, and in the Nx Cloud UI; `nx apply-locally <fix-identifier>` pulls a fix down. Critically, **the fix is verified by re-running the task before auto-application**: "The suggestion has been explicitly verified to fix the failing task" ([self-healing-ci docs](https://nx.dev/docs/features/ci-features/self-healing-ci)). Bring-your-own-compute requires the Enterprise plan.

**MCP server.** `nx-mcp` exposes `nx_current_running_tasks_details`, `nx_current_running_task_output`, `ci_information` (Nx Cloud pipeline data), `ci_task_output` (CI terminal logs), and **`update_self_healing_fix`** (apply or decline a Nx Cloud fix) ([nx-mcp reference](https://nx.dev/docs/reference/nx-mcp)). Supported agents: claude, codex, copilot, cursor, gemini, opencode; `npx nx configure-ai-agents` writes `AGENTS.md`/`CLAUDE.md`, MCP config and agent skills ([ai-setup](https://nx.dev/docs/getting-started/ai-setup)).

**Pricing** ([nx.dev/pricing](https://nx.dev/pricing)): Hobby free — 50,000 credits/mo, 5 contributors, 10 concurrent CI connections. Team **$29/mo** including $29 of usage credit; extra credits **$5.50 per 10,000**; additional contributors **$19 each**; extra concurrent CI connections **$2.25 each**; "Dedicated Compute Cluster + Sandboxing" **$99**. Enterprise custom.

### 5.3 Turborepo / Vercel Remote Cache

Two hashes decide a cache hit ([caching docs](https://turborepo.dev/docs/crafting-your-repository/caching)): a **global hash** (task definitions in `turbo.json`, root lockfile, root-dependency source files, `globalDependencies`, `globalEnv`, runtime flags and passthrough args) and a **package hash** (package `turbo.json`, package lockfile, `package.json`, source-controlled files configurable via `inputs`). On a hit, Turborepo restores both declared file outputs and the captured terminal logs. Notably, **"Turborepo automatically shares the local filesystem cache between the main worktree and any linked worktrees"** — relevant if Pandora runs agents in worktrees.

Documented caveat: "Turborepo **assumes that your tasks are deterministic**. If a task is able to produce different outputs given the set of inputs that Turborepo is aware of, caching may not work as expected." There is **no test-level granularity and no flaky-task concept** — Turborepo is a package-task cache, so a single flaky test poisons or invalidates a whole package's task result. The docs do not state whether failed tasks are cached (**unverified**).

### 5.4 Pants

Local process cache at `$HOME/.cache/pants/lmdb_store` keyed by input digests, plus remote caching "using gRPC and the open-source Remote Execution API for low-latency and fine-grained caching," so "all machines and CI jobs share the same cache" and it "downloads precisely what is needed by your run — when it's needed" ([CI docs](https://www.pantsbuild.org/stable/docs/using-pants/using-pants-in-ci)). Flakiness handled by retry: `[test] attempts_default = 3`.

### 5.5 Gradle build cache / Develocity

The build cache key covers "the task type and its classpath," output property names, input property names and values, DSL-added inputs, "the classpath of the Gradle distribution, buildSrc and plugins," and "the content of the build script when it affects execution of the task." **The task path is not an input**, so tasks at different paths can reuse each other's outputs. The `Test` task is cacheable ([build cache docs](https://docs.gradle.org/current/userguide/build_cache.html)).

Documented requirements read as a checklist of failure modes worth copying: complete input/output declarations ("**Missing task inputs can cause incorrect cache hits**"), relocatability (loadable into a different build directory), relative-path sensitivity for cross-machine sharing, and input normalisation for volatile inputs.

### 5.6 Earthly — dead

Earthly Cloud, including Satellites, **stopped working on 2025-07-16**; announced 2025-04-16. The company phased out Cloud Satellites, Self-Hosted Satellites, BYOC Satellites and their free tiers, plus Earthly Cloud Secrets and Logs, and **ended active maintenance of the Earthly open-source project**, supporting a community fork instead. Migration advice was "roll out your own remote Buildkit" ([shutdown post](https://earthly.dev/blog/shutting-down-earthfiles-cloud/)). A cautionary data point for a remote-execution-plus-cache business.

### 5.7 Memoizing arbitrary commands by tree hash — the thin, real prior art

This is the one place where the Pandora idea has close, explicit precedents, and they are all tiny.

- **`git-test`** (mhagger, 80 stars, created 2017-01-06, last pushed 2025-03-13 per GitHub API) — "runs automated tests against commits in a Git repository," keying results **by tree hash rather than commit**, so "old test results remain valid even across some kinds of commit rewriting" (message changes, squashing, reordering). Cache key is (tree, test name), so unit and integration suites don't collide. Integrates with `git worktree` for parallel runs. **On a dirty working tree, tests still execute but results are not recorded** ([repo](https://github.com/mhagger/git-test)). A Spotify fork exists ([spotify/git-test](https://github.com/spotify/git-test), [blog 2015-03-19](https://labs.spotify.com/2015/03/19/git-test/)).
- **greentree** (Reachpad, Apache-2.0, created 2026-08-12, **7 stars**, last pushed 2026-08-13 per GitHub API) — this is essentially one of Pandora's candidate jobs shipped as a CLI: "Test the tree, not every commit: verify a dirty working tree continuously, publish only verified trees." It content-addresses the dirty working tree via `git write-tree` (tracked plus untracked non-ignored files), keys verdicts on **(tree hash, check name, command hash, environment fingerprint)**, and "refuses to create a commit from any tree that has not passed" — "The published commit is built with `git commit-tree` from the exact tree object the checks passed." It explicitly frames itself for agents: "One idempotent verb for agents. `greentree gate` runs whatever is not cached, then publishes if green." Design detail worth copying: **only pass and fail enter the cache; timeouts, infrastructure errors and runs during which the tree changed are never cached** ([repo](https://github.com/Reachpad/greentree)). Its [Show HN](https://news.ycombinator.com/item?id=49280079) got 5 points and 1 comment — and that comment is exactly the right objection: "How do you handle non-deterministic tests or environment variables that aren't captured in the Git tree object?"
- **The tree-hash-as-cache-key argument** is independently made in ["Git tree hashes make better cache keys"](https://lunnova.dev/articles/git-tree-hashes-are-better-cache-keys/) (2026-03-13): commit hashes are invalidated by reword/reorder/squash while content is unchanged; subdirectory tree hashes let you scope invalidation further.

**Conclusion for this sub-area:** the *technique* is established and obvious to the people who have thought about it; the *product* does not exist. Nothing sells remote, shared, multi-tenant memoization of arbitrary test commands keyed on a dirty-tree digest.

### 5.8 Determinism certification

Nobody sells it. The tooling is OSS and rooted in the reproducible-builds community ([tools](https://reproducible-builds.org/tools/)):
- **reprotest** builds the same source twice under **deliberately varied conditions** (time, timezone, locale, file ordering, umask, hostname, user IDs, kernel version) and diffs the binaries — it tests determinism *and* environment-independence in one pass. A recent study used it across 4,000 packages from six ecosystems ([reproducible-builds.org](https://reproducible-builds.org/tools/)).
- **diffoscope** is the "in-depth and content-aware diff utility" that explains *why* two artifacts differ, recursing into archives and binary formats.
- Bazel's `--execution_log_json_file` diff (§5.1) is the build-system-native equivalent.

The gap: all of this targets **build artifacts**, not **test outcomes**. "Is this test deterministic?" is answered today only by `--runs_per_test_detects_flakes`-style repetition, not by a certification service.

---

## 6. Capability matrix against Pandora's candidate background jobs

Legend: **P** = exists as a shipping product · **I** = exists as internal infrastructure at a large company · **R** = exists in research only · **O** = exists only as small/unmaintained OSS · **—** = does not exist.

| Candidate job | Status | Who already does it | What is actually missing |
|---|---|---|---|
| **Baseline oracle** — known-failing/flaky tests per base commit | **P** (crowded) | Mergify Test Insights surfaces "existing flaky or broken tests on your default branch" and auto-quarantines within a CI budget you control ([changelog](https://docs.mergify.com/changelog/2026-07-24-automatic-flaky-test-detection-and-prevention/)); Trunk quarantine list + "broken tests"; Datadog quarantine/disable states; Buildkite mute/skip; BuildPulse quarantine API; Google TAP's last-known-green pointer | Nobody indexes the baseline **per base commit for an arbitrary dirty tree**. Every product's baseline is "tests currently known bad on main," not "what failed at the exact tree your agent branched from." |
| **Automatic failure triage** — yours / flaky / pre-existing | **P** for *flaky vs not*; **— for the three-way split against a base commit** | Datadog EFD + same-commit rule; Buildkite transition-count monitor; Trunk monitors; Nx input-hash rule; Mergify same-SHA rule; Aviator's in-queue "reruns the ones that would pass on retry"; BuildPulse tree-SHA rule | The "pre-existing" arm is the gap. Products classify *flaky vs broken*; they do not routinely answer "this red was already red before your change." Google's FACF shows why it matters: **~40% of red test ranges have no culprit at all.** |
| **Memoized command-level results keyed by a content digest of the source tree** | **P inside a build system**; **O for arbitrary commands**; **— as a remote multi-tenant service for dirty trees** | Bazel action cache + `--cache_test_results`; Gradle build cache (Test task cacheable); Nx task hash + remote cache; Turborepo global+package hash; Pants LMDB store + REAPI remote cache; BuildBuddy/Develocity as hosted caches. For arbitrary commands: [`git-test`](https://github.com/mhagger/git-test) (80 stars, tree-hash keyed, **does not record results for a dirty tree**) and [greentree](https://github.com/Reachpad/greentree) (7 stars, created 2026-08-12, keys on `git write-tree` of the dirty tree + command hash + environment fingerprint, explicitly agent-facing) | A **shared, remote, multi-tenant** cache for arbitrary test commands keyed on a dirty-tree digest, with a defensible environment fingerprint. Both OSS precedents are single-user and tiny; the Show HN comment on greentree — "How do you handle non-deterministic tests or environment variables that aren't captured in the Git tree object?" — is the unanswered question. |
| **Determinism certification** | **O/R only; nobody sells it** | `reprotest` (builds twice under deliberately varied time/timezone/locale/file-ordering/umask/hostname/UID/kernel) + `diffoscope` ([reproducible-builds.org/tools](https://reproducible-builds.org/tools/)); Bazel's twice-build `--execution_log_json_file` diff; Bazel `--runs_per_test_detects_flakes` | All existing tooling certifies **build artifacts**, not **test outcomes**. "Is this test deterministic, and under which environmental perturbations?" is answered today only by naive repetition. `reprotest`'s *variation matrix* applied to tests is a genuinely novel product surface. |
| **Learned fail-first test ordering** | **P (weakly)** | CloudBees Smart Tests `--confidence` targets rank before cutting; Meta PTS's `HighlyRanked` set — "selecting just the two top-scoring targets catches ≥1 failure on **70%** of faulty changes"; BuildPulse/Trunk rank flakes by impact; CircleCI `--split-by=timings` orders by duration only | Nobody sells *ordering as a product* — it is an internal step inside selection. This is a feature, not a company. |
| **Predictive test selection / test-impact maps** | **P (very crowded)** | Develocity PTS (works on uncommitted trees), CloudBees Smart Tests, Sealights/Tricentis, Harness TI, Datadog TIA, Microsoft TIA, Meta PTS, Google TAP, Ekstazi/STARTS | Nothing meaningful. This is the most thoroughly commoditised item on the list, from four directions (ML, coverage, build graph, static analysis) with 8+ vendors. |
| **Merge-ahead / speculative merge testing** | **P (very crowded)** | Trunk, Mergify (128-wide), Aviator, GitHub native, Graphite, bors-ng (archived), GitLab trains, Prow/Tide, Zuul (multi-repo speculative DAG), Uber SubmitQueue | Nothing, except *who pays for the compute* — every vendor orchestrates and bills the customer's CI. A vendor that owns the compute could price speculation differently. Also: nobody speculates on an **unpushed** change. |
| **Auto-bisect when main goes red** | **I / R; no mainstream product** | Chromium LUCI Bisection (nth-section + heuristics + verification reruns, **auto-reverts compile culprits only**, daily revert rate limits, `MaxRevertibleCulpritAge`); Google TAP bisect + rollback; Google FACF (Bayesian noisy binary search). Merge-queue vendors bisect *failed batches of candidate PRs*, which is a different problem | A commercial "main went red, here is the culprit commit, verified" service. Trunk/Mergify/Aviator/Graphite all bisect *forward-looking batches*, not landed history. Note LUCI Bisection publishes **no accuracy or false-positive rates** — nor does anyone else. |
| **Hunk-level bisect of an uncommitted diff (delta debugging)** | **R (since 1999); no maintained tool** | Zeller ESEC/FSE 1999 applied `dd` to **178,000 changed lines between GDB 4.16 and 4.17**, isolating the failure-inducing change "within a few hours"; Artho's *Iterative Delta Debugging* explicitly searches "across files, hunks, and lines"; `picire` (format-agnostic ddmin, `--parallel -j N`) can be pointed at a diff; C-Reduce/cvise for input reduction | **The clearest open space in this report.** No maintained tool bisects an uncommitted working-tree diff hunk-by-hunk. The technique is 27 years old and its failure modes are already catalogued (§4.6). Treat "no such tool exists" as a strong-ish negative result — search budget prevented exhaustive GitHub-code-search queries. |

---

## 7. Best published effectiveness numbers (the honest scoreboard)

Ranked by credibility. Note the pattern: **the good numbers are from Meta, Google, Apple and academia; the vendors publish almost nothing measurable.**

| Claim | Number | Source | Credibility |
|---|---|---|---|
| Predictive test selection, production | **2× infrastructure cost reduction**, >95% of individual test failures and >99.9% of faulty changes still reported; 3× fewer test executions; selection rate <0.33 | [Machalica et al., arXiv:1810.05286](https://arxiv.org/abs/1810.05286), ICSE-SEIP 2019 | Highest — peer-reviewed, production-deployed, explicit recall targets |
| Two top-ranked targets catch a failure | **70% of faulty changes** | same | Highest |
| Flaky-test ranking | **flakiness reduced 44% with <1% loss in fault detection** | [Kowalczyk et al., ICSE-SEIP 2020 (Apple)](https://conf.researchr.org/details/icse-2020/icse-2020-Software-Engineering-in-Practice/2/Modeling-and-Ranking-Flaky-Tests-at-Apple) | Highest — the only real precision/recall trade-off published in flake management |
| Flake-aware culprit finding | Bisect(1) ≈0.80 overall accuracy vs **FACF ≈0.97+**; on flaky (no-culprit) ranges **0.50 → ~1.00**; p<10⁻¹² over 13,600 breakages | [Henderson et al., ICST 2023 (Google)](https://hackthology.com/pdfs/icst-2023.pdf) | Highest — with a self-verifying evaluation harness (144,130 verified conclusions in 60 days) |
| Share of red test ranges with no real culprit | **~40%** (flaky ranges averaged 8 failing tests vs 224 for real breakages) | same | Highest — and the most load-bearing single fact for Pandora's triage job |
| Test executions that find anything | **1.23%** of executions found a breakage or fix; <0.5% of per-CL outcomes FAILED; "targets >10 dependency edges from the change hardly ever break" | [Memon et al., ICSE-SEIP 2017 (Google TAP)](https://huang.isis.vanderbilt.edu/cs8395/paper/google-testing-icse-seip-17.pdf) | Highest |
| Flakiness base rates | **1.5% of all test runs** flaky; **~16% of tests** have some flakiness; **84% of observed transitions** are from flaky tests | [Micco, Google Testing Blog, 2016-05-27](https://testing.googleblog.com/2016/05/flaky-tests-at-google-and-how-we.html) | High — old but universally cited |
| Dynamic RTS, open-source projects | Ekstazi: end-to-end **−32% average, −54% for longer suites** over 615 revisions / 32 projects | [Gligoric et al., ISSTA 2015](https://users.ece.utexas.edu/~gligoric/papers/GligoricETAL15Ekstazi.pdf) | High |
| Static RTS safety cost | class-level static RTS **violated safety on 0.2% of revisions**; method-level on **10.6%** | [Legunsen et al., FSE 2016](https://www.cs.cornell.edu/~legunsen/pubs/LegunsenETAL16StaticRTSStudy.pdf) | High — the number to quote when anyone claims selection is "safe" |
| Test-case reduction | C-Reduce outputs **>25× smaller** than other reducers; crash bugs 108,608 bytes → **151 bytes in 2 min**; **29% of Berkeley-delta outputs were invalid** (undefined behaviour) | [Regehr et al., PLDI 2012](https://users.cs.utah.edu/~regehr/papers/pldi12-preprint.pdf) | High |
| Failure localisation without running tests | **77% of bugs correctly localised** | [Orca, OSDI 2018 (Microsoft)](https://www.usenix.org/system/files/osdi18-bhagwan.pdf) | High |
| TIA skip-decision false positives | Bloom filter, **~0.04% false-positive rate**, biased so tests are never wrongly skipped | [Datadog TIA docs](https://docs.datadoghq.com/tests/test_impact_analysis/) | Medium-high — a vendor spec, but a checkable one |
| Predictive test selection calibration | "calibrated to catch **over 99% of non-flaky test task/goal failures**" | [Develocity PTS docs](https://docs.develocity.ai/predictive-test-selection/) | Medium — vendor, but stated as a calibration target with a simulator to check it |
| Merge-queue throughput | Graphite parallel CI **1.5× faster merges** (p95 −33%), stacked-PR users up to **2.5×** | [Graphite docs](https://graphite.com/docs/merge-queue-optimizations) | Medium — vendor, but specific percentiles |
| Merge-queue CI savings | Trunk batching "50–70% reduction in CI minutes," "3–5× more PRs per hour"; Mergify "50–80%" fewer CI runs, "3–5× faster merge throughput" | [Trunk](https://docs.trunk.io/merge-queue/concepts-and-optimizations/batching), [Mergify](https://docs.mergify.com/merge-queue/) | Low-medium — unattributed vendor ranges |
| PTS time saved | "51 days 11 hours of serial test time saved" across 47.3K tasks; one task 41% faster | [Develocity product page](https://develocity.ai/product/predictive-test-selection/) | Low — illustrative dashboard, not an attributed case study |
| Harness TI | **20–60% savings**, ~60 min → 24–48 min on Harness's own repo | [Harness blog](https://www.harness.io/blog/test-intelligence) | Low-medium — vendor dogfooding, but specific |
| Flake-detection precision/recall, any vendor | **none published by anyone** | — | This is the single most striking absence in the market |

---

## 8. Techniques worth copying

**Algorithms**

1. **FACF's Bayesian noisy binary search with a "no culprit" hypothesis** ([paper](https://hackthology.com/pdfs/icst-2023.pdf)). Maintain a posterior over n+1 hypotheses — each suspect plus "it was a flake." Exploit the **asymmetric oracle**: a FAIL is possible at any position, but a PASS at i is impossible if the culprit is at j ≤ i. Pick the next probe where the posterior CDF crosses ½. This directly outperforms deflaked bisection precisely where bisection collapses (0.50 → ~1.00 on flaky ranges) while costing less than k-rerun bisection. It also generalises to k parallel probes — i.e. it is *designed* to spend surplus capacity well.
2. **Develocity's cross-build flake signal, and Nx's input-hash rule.** If you already memoize a command by a content digest, flake detection is free: the same input hash producing both a pass and a fail is a flake, with no retries and no same-commit requirement. Nx: "if Nx encounters a task that fails with a particular set of inputs and then succeeds with those same inputs, it marks that task as flaky." For a dirty-tree digest this is strictly stronger than the commit-SHA rule everyone else uses, because it survives rebases, amends and cherry-picks.
3. **Zuul's TCP-congestion-control window** (default 20, floor 3, linear increase by 1, exponential decrease by 2). The principled answer to "how much speculative work should I launch?" Applies directly to sizing Pandora's background job queue against observed flake/failure rates.
4. **Datadog's Bloom filter for skip decisions**, biased toward false-positive *runs* rather than false-negative *skips*, at ~0.04% FP. A cheap, explainable, one-sided-error data structure beats an ML model you cannot audit.
5. **SeaLights' coverage-completion rule**: keep adding tests until every changed method is exercised at least once. A selection policy with a stated invariant is sellable in a way that "the model is 95% confident" is not.
6. **Duration-bucketed retry budgets** (Datadog: ≤5s → 10 retries, >5m → 1; plus the `faultyThreshold = 30%` guard that disables Early Flake Detection when too many tests look new). Any compute-spending classifier needs both a per-item budget scaled by cost and a global circuit breaker.
7. **Trunk's transition-count / Buildkite's transition-count monitor**: `PFPFF` scores 0.4, `FFFFF` scores 0. Flakiness as a *sequence* statistic separates flaky from broken with no retries and no same-commit requirement.
8. **Batch bisection with result caching** (Trunk): when splitting a failed batch, reuse already-passing sub-combinations. Obvious once stated, and it converts the split-and-recurse cost from O(E·log N) fresh runs to something much smaller.
9. **Hierarchical delta debugging over the patch structure** (Artho IDD): files → hunks → lines, with non-compiling subsets mapped to *untestable* (`git bisect` exit 125) rather than *good*. This is the correct skeleton for hunk-level bisect of an uncommitted diff.
10. **`reprotest`'s variation matrix** (time, timezone, locale, file ordering, umask, hostname, UID, kernel version) applied to test outcomes rather than build artifacts. This turns "determinism certification" from a coin flip into a structured report: *which* perturbation breaks this test.

**Data you must have**

- Test-level outcome history with **stable test identity** across renames and parameterisation — the unglamorous precondition every one of these systems depends on.
- An **input digest per unit of work** (Nx/Bazel/Gradle/Turborepo all prove this out). Without it you cannot memoize, and you cannot get free flake detection.
- A **dependency or coverage map** — either build-graph reverse closure (Google/Meta), per-test coverage (Datadog/Sealights/MS TIA), or file checksums (Ekstazi).
- **Weeks of history before predictions work**: Develocity needs 50 executions over 14 days; CloudBees 1–2 weeks; Meta 3 months plus a ~25% compute tax for labels. Plan the cold-start story or the product is unusable on day one.
- **Labels bought with compute.** Meta's "learning test runs on ~a quarter of submitted changes, deferred off-peak" is the closest published precedent for Pandora's core thesis, and it validates the economics: they judged full-suite runs on a sampled quarter of diffs worth paying for, purely to keep a model fresh.

**Failure modes to design against**

- **Flakes poison everything.** They corrupt `git bisect` silently (one bad FAIL ruins the search), poison merge batches (Trunk: don't use optimistic merging above 5% flake rate), destroy delta debugging (C-Reduce: "non-deterministic execution of the system under test can cause test-case reduction to fail"), and make models learn to predict flakes rather than failures (Meta). **Every compute-spending job needs a flake model underneath it, not beside it.**
- **Non-compiling intermediate states.** Interacting hunks (a signature change plus its call sites) cannot be isolated by bisection and produce "overly large change sets" (Artho). Budget for a third outcome — untestable — everywhere.
- **Cache poisoning.** Bazel judges actions by exit code even when outputs are wrong ([#14543](https://github.com/bazelbuild/bazel/issues/14543)); Gradle warns "missing task inputs can cause incorrect cache hits." A memoization service is a trust boundary.
- **Environment not captured by the digest.** The single comment on greentree's Show HN is the whole risk: environment variables, network state, system clock, and machine class are all outside `git write-tree`. Bazel's answer is `--strict_action_env` plus sandboxing; Nx's is "retry on a *different* agent" to isolate agent-caused flakiness.
- **Never cache ambiguous verdicts.** greentree's rule is right: "only pass and fail enter the cache; timeouts, infrastructure errors, and runs during which the tree changed are never cached."
- **Selection without a safety net is half a product.** Meta's stabilization stage, Google's postsubmit, Develocity's `REMAINING_TESTS`, Microsoft's periodic run-all. Idle capacity is an unusually good place to put that net — this is the strongest argument for the whole Pandora shape.
- **Auto-action needs blast-radius controls, not confidence scores.** LUCI Bisection ships daily rate limits on creating *and* submitting reverts, a max culprit age, and a hard rule that auto-commit of reverts is compile-only, never test failures. Nobody publishes an accuracy number; everybody ships rate limits.

---

## 9. Candid assessment: which Pandora ideas are already well served

**Do not rebuild these.**

- **Predictive test selection / test-impact maps.** This is the most commoditised item on the list. Eight-plus vendors attack it from four independent technical directions, the best-published result is seven years old, and the honest ceiling is known (Meta: 2× cost reduction at 95% test recall; Google: safe selection only at target granularity via a build graph you probably don't have). Worse, the cold-start tax is brutal and the sellable version requires shipping a safety net too. Entering here means competing with Gradle's PTS, which already works on uncommitted trees — the one property you would have hoped to differentiate on.
- **Merge-ahead / speculative merge testing.** Six commercial vendors plus GitHub native plus two mature OSS systems, with the algorithm design space fully explored (batching, optimistic validation, parallel queues, TCP-style windows, multi-repo speculative DAGs). The only unexploited angle is *owning the compute* rather than orchestrating the customer's CI — which is a pricing play, not a technical one, and is a very hard wedge against GitHub's free-tier native queue.
- **Flaky-vs-broken classification as a standalone capability.** Trunk, BuildPulse, Buildkite, Datadog, Mergify, CircleCI, Nx and Develocity all ship it; CircleCI and Playwright give it away free; Bazel gives it away free *and* spends compute on it (`--runs_per_test_detects_flakes`). Four of these vendors already ship MCP servers, so "flaky test context for your coding agent" is taken — Trunk in particular has publicly staked its entire strategy on exactly that.
- **Generic command memoization for teams already on Bazel/Gradle/Nx/Turborepo/Pants.** Those teams have it. Selling them a second cache is a hard conversation.

**Genuinely underserved — in rough order of defensibility.**

1. **Hunk-level bisect of an uncommitted diff.** Published technique since 1999, catalogued failure modes, zero maintained tooling, and an unusually good fit for the agent use case (an agent produces a large diff and one test goes red; "which of your 40 hunks did it" is exactly the question). It is also embarrassingly parallel and compute-hungry — i.e. it *wants* idle capacity. The honest caveat: ddmin is worst-case O(n²), interacting hunks defeat it, and partial patches often don't compile, so the product must degrade gracefully to "these 6 hunks, jointly."
2. **Three-way failure triage against the base commit (yours / flaky / pre-existing).** The market ships two of the three arms. The "pre-existing" arm requires exactly what Pandora has and others don't: the willingness to *go run the base commit* to find out. Google's ~40%-of-red-ranges-have-no-culprit figure is the size of the prize. This composes naturally with the baseline oracle.
3. **Auto-bisect on red main, flake-aware.** No commercial product does it; the best algorithm is published and unimplemented outside Google; the compute profile fits idle capacity. Copy FACF rather than `git bisect`. Ship LUCI Bisection's blast-radius controls (rate limits, max age, comment-don't-revert defaults), not a confidence score.
4. **Determinism certification for tests.** Nobody sells it. `reprotest`'s perturbation matrix is the right model and has never been applied to test outcomes. It is also the honest prerequisite for the memoization product — you cannot responsibly cache a verdict for a command you have not shown to be deterministic.
5. **Remote, shared memoization of arbitrary commands keyed on a dirty-tree digest.** The technique is proven (git-test, greentree, and every build system's hash); the *service* does not exist. The differentiator over Bazel/Nx is working for teams with no hermetic build system, on uncommitted state, shared across an agent fleet — which is precisely the situation "many parallel AI coding agents" creates. Risk: the greentree HN objection is unanswered, and answering it is what item 4 is for.

**The strategic read.** The flaky/selection/merge-queue market has converged on *analysis of results the customer already produced*, monetised per seat or per span, with MCP as the agent surface. Almost nobody sells compute, and the ones who spend it (Datadog EFD, Buildkite `bktec`, Mergify Auto-Retry) spend it in the *customer's* CI and bill them for the spans. Pandora's distinctive asset — owned idle capacity plus the dirty working tree — maps onto exactly the jobs that require running something the customer did not ask for, at a base commit or a partial patch they never pushed. That is items 1–5 above, and it is a genuinely different axis from everything in §1–§3.

**The one competitor to watch closely** is **Nx Cloud**. It already combines input-hash memoization, hash-derived flake detection, retry-on-a-different-agent, its own compute (Nx Agents), an MCP server with CI tools, and **Self-Healing CI — a Claude-powered agent that proposes a fix for a red CI job, verifies it by re-running the task, and delivers it through PR buttons, the editor, or `update_self_healing_fix` over MCP** ([docs](https://nx.dev/docs/features/ci-features/self-healing-ci)). That is the closest thing shipping to Pandora's end state. Its limits are the wedge: it is Nx-monorepo-only, task-granular rather than test-granular, and CI-triggered rather than working-tree-triggered.

