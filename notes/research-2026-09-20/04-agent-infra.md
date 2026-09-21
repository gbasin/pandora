# Pandora research — slice 04: AI-coding-agent infrastructure landscape

Research date: **2026-09-20**. All claims carry a URL. Anything I could not confirm from a
primary source is marked **[unverified]**. Search budget for this session was exhausted partway
through, so some sub-topics were reached by direct fetch of known URLs rather than discovery;
gaps are flagged explicitly at the end.

---

## 0. Executive answer

Nobody ships Pandora today. But every *layer* of it exists separately:

| Pandora capability | Who has the closest thing | Gap |
| --- | --- | --- |
| Ship dirty working tree to remote compute, run command, stream back | **Crabbox** (MIT, openclaw/crabbox) — literally "warm a box, sync the diff, run the suite" | No speculation, no memoization, no triage, no cross-agent view |
| Structured test failures delivered to an agent pre-commit | **Buildkite Preflight** (experimental) | Commits to a temp branch; CI-shaped; not speculative; not local-tree-native |
| Pairwise conflict detection across parallel agent worktrees | **Clash** (MIT, clash-sh/clash) | Pure `git merge-tree` textual simulation — no test execution, no semantic conflicts |
| Memoized results keyed on content hash, on a dirty tree | **Nx Cloud, Bazel, BuildBuddy** — this already ships | Requires a hermetic declared-input build graph; useless in the messy long tail |
| Speculative fork-to-N of a warm environment | **Morph Infinibranch** — ships a 16-branch speculative demo | Pure infra; no repo/worktree/diff semantics |
| Speculative/parallel validation of candidate merges | **Merge queues** (Trunk, Graphite, GitHub) | Operate on *committed PRs*, never on uncommitted work |
| Best-of-N with automated validation | **Jules `--parallel` (5 attempts)**, Devin `parallel([...])` + retry-until-green | Human picks the winner; Devin's is user-scripted, not a product |
| Failure triage (mine/flaky/pre-existing) | Meticulous, Momentic, Antithesis (flaky); Datadog, Develocity (impact) | Product-specific test runners or CI dashboards, not your repo's suite in your agent's loop |
| Review evidence packs on uncommitted trees, agent-formatted | **CodeRabbit CLI** (`--uncommitted`, `--include-untracked`, `--agent`) | **Explicitly does not execute code or tests** |
| Unsolicited delivery into the agent loop | **Claude Code + Codex CLI hooks** (`additionalContext` on `PostToolUse`) | Open to everyone; not a moat |

The *unowned* space is the intersection: **a shared, content-addressed verification substrate that
sees many agents' uncommitted trees at once, actually executes the repo's own suite, and pushes
unsolicited results back into the agent loop.** Every individual piece is owned; the join is not.
Be aware that two premises are weaker than they look: memoization-on-dirty-trees already ships in
Nx/Bazel, and "free RAM" does not exist in rented compute (§10).

---

## 1. Agent sandbox / execution platforms

The compute layer Pandora would rent. Three things matter for Pandora specifically: (a) can you get
a dirty tree in fast, (b) can you fork a *warm, already-set-up* environment cheaply so speculation
isn't dominated by setup cost, (c) do you pay while idle.

### 1.1 The one platform architecturally built for speculation: Morph Cloud "Infinibranch"

<https://cloud.morph.so/docs/blog/developers>, <https://cloud.morph.so/web/product/devboxes>

- A runtime that **separates storage and compute for whole VMs**; branching creates lightweight
  branches with "near-zero overhead" rather than duplicating resources → **"unlimited parallel
  branches."**
- **Start / branch / restore all in < 250 ms** (vendor claim), vs "2–3 minutes for traditional VMs".
- Branches **preserve application and memory state**; can "branch to hundreds of parallel replicas
  or scale to zero after a period of inactivity."
- The docs contain a worked example that forks into **16 parallel branches, each speculatively
  testing a different value** — i.e. Morph is *already shipping the demo of Pandora's core
  mechanic*.
- "Branch-level security", analogous to row-level DB security.
- Morph EFS: mountable shared ephemeral filesystem across hosts.
- Pricing: **MCU model, $0.05/MCU-hour**, where 1 MCU = 1 vCPU-hr = 4 GB RAM-hr = 16 GB disk-hr =
  5 TB snapshot-hr (<https://cloud.morph.so/web/pricing>). Developer $40/mo for 1,000 MCUs; Team
  $250/mo for 7,500.
- Integrates explicitly with Codex, Claude Code and Gemini CLI, plus VS Code/Cursor and CI.
- **Infinibranch was early-access via signup form** in the fetched material — GA status
  **[unverified]**.

**Strategic read:** Morph is the single most dangerous company in this survey for Pandora. They own
the primitive, they already market it for "parallel agent execution with isolated environments", and
they already integrate with Claude Code. What they do *not* have is the local-side half: the dirty
tree capture, the digest identity, the memoization index, the triage, the hook delivery. Pandora
should probably treat Morph as a *backend*, not a competitor — and should assume Morph will keep
moving up the stack.

### 1.2 Others with true live-memory fork

- **Freestyle** (<https://www.freestyle.sh/>): full Linux VMs, nested virtualization, **"live cloning
  to duplicate running VMs with memory state preserved"**, **65 ms** boot claim. Pricing
  $0.04032/vCPU-hr + $0.0129/GiB-hr, tiers $0/$50/$500 per month
  (<https://www.freestyle.sh/pricing>). Ships a "coding agents: persistent workspace" guide and lists
  Claude Code in its toolchain. No documented idle discount **[unverified]**.
- **microsandbox** (<https://github.com/microsandbox/microsandbox>): OSS, libkrun (KVM/HVF),
  documented `msb snapshot create --from-sandbox app --full` and **live sandbox forking**, <100 ms
  boot claim. Ships an Agent Skills framework + MCP server. This is the self-host path.
- **mitos** (<https://github.com/mitos-run/mitos>): OSS, Firecracker + Kubernetes CRDs, explicitly
  "millisecond microVM sandbox forking for AI agents", fork a running VM into N copies. Surfaced
  during research, not independently vetted **[unverified]**.
- **CodeSandbox SDK**: secondary sources claim Firecracker + "memory snapshot, live VM cloning"
  (<https://blog.logrocket.com/comparing-ai-agent-sandbox-platforms-e2b-modal-daytona-and-more/>);
  codesandbox.io returned HTTP 403 to every fetch attempt, so **[unverified]**.

### 1.3 Comparison table

| Platform | Isolation | Live-memory fork-to-N? | Cold start (claim / measured) | Price anchor | Idle economics |
| --- | --- | --- | --- | --- | --- |
| **Morph Cloud** | VM (Firecracker-class, unnamed in own docs) | **Yes, headline feature** | <250 ms / — | $0.05/MCU-hr (≈$0.05 vCPU-hr) | auto-sleep / TTL **[unverified]** |
| **Freestyle** | Full Linux VM, nested virt | **Yes** | 65 ms / — | $0.0403/vCPU-hr | no idle discount found |
| **microsandbox** (OSS) | libkrun | **Yes** | <100 ms / — | self-host | n/a |
| **E2B** | Firecracker | pause/resume only | 150 ms / **717 ms create, 662 ms resume** | $0.000014/vCPU-s ≈ $0.050/vCPU-hr | billed while alive; auto-pause **[unverified]**; 1 hr Hobby / 24 hr Pro session cap |
| **Modal Sandboxes** | gVisor default; experimental VM mode | memory snapshots (Function-focused) | sub-sec / **2,437 ms create** | $0.0000394/core-s | scale-to-zero |
| **Daytona** | container default; VM/GPU/macOS options | hot snapshot (VM only, `includeMemory`) | <90 ms / **742 ms create, 1,254 ms resume** | $0.0504/vCPU-hr, per-second | auto-stop/TTL, warm pools |
| **Runloop** | VM + container | disk snapshot + branch ("Git for Agent State") | <2 s /10 GB image | $0.108/vCPU-hr | **$0 compute while suspended** |
| **Fly Sprites** | KVM VM, "inside-out" nested container | checkpoint as everyday op (metadata-only) | 1–2 s | pay only for blocks written | auto-suspend, "practically nothing while asleep" |
| **Fly Machines** | Firecracker | no | <1 s from stopped | $0.00000078/s (shared-1x/256MB) | stopped = rootfs storage only, $0.15/GB/30d |
| **Cloudflare Sandbox SDK** | Containers + Durable Objects | no | ~2 s **[secondary]** | $0.000020/vCPU-s | **true scale-to-zero**, `sleepAfter` |
| **Vercel Sandbox** | Firecracker | snapshot + persistent resume | "milliseconds" | $0.128/vCPU-hr **active CPU only** | **I/O wait not billed**; 24 hr session (Pro) |
| **Blaxel** | hardware-isolated microVM | FS snapshot on suspend | 25 ms resume claim / **2,824 ms create, 1,924 ms resume** | $0.0000115/GB RAM-s | **$0 compute suspended**, $0.20/GB-mo standby |
| **Northflank** | microVM (likely Firecracker) | **[unverified]** | <1 s | **[unverified]** | pause stops compute |
| **Depot agent sandboxes** | container | no | <5 s | **$0.01/min, per-second** | billed only while prompt processing |

Sources for the above rows: <https://e2b.dev/pricing>, <https://modal.com/docs/guide/vm-sandboxes>,
<https://modal.com/docs/guide/memory-snapshots>, <https://www.daytona.io/docs/en/snapshots/>,
<https://daytona.io/pricing>, <https://www.runloop.ai/pricing>,
<https://fly.io/docs/machines/overview/>, <https://fly.io/blog/design-and-implementation/>,
<https://developers.cloudflare.com/containers/pricing/>,
<https://developers.cloudflare.com/sandbox/>, <https://vercel.com/docs/vercel-sandbox>,
<https://vercel.com/docs/sandbox/pricing>, <https://www.blaxel.ai/pricing>,
<https://northflank.com/docs>, <https://depot.dev/docs/agents/overview>.

### 1.4 Three findings that matter for Pandora's economics

1. **Vendor cold-start claims are unreliable.** Every platform LogRocket independently measured came
   in several times slower than its own headline number (E2B 150 ms claimed vs 717 ms measured;
   Blaxel 25 ms resume claimed vs 1,924 ms measured; Modal sub-second claimed vs 2,437 ms measured)
   (<https://blog.logrocket.com/comparing-ai-agent-sandbox-platforms-e2b-modal-daytona-and-more/>).
   A design that assumes "<250 ms fork" needs its own benchmark before the plan depends on it.
2. **Idle is basically already free at several vendors.** Runloop, Blaxel and Cloudflare all
   explicitly zero out compute billing while suspended; Vercel doesn't bill I/O wait at all. This
   *undercuts* the "free RAM" framing if Pandora rents cloud compute — there is no free RAM to
   scavenge, you buy seconds. The "free RAM" thesis only literally holds if the workers are the
   developer's own idle machines or a fleet the org already pays for.
3. **The real cost is setup, not the test run.** Because branch-from-warm-snapshot is ~100× cheaper
   than build-from-scratch, the whole speculative model lives or dies on whether Pandora can keep a
   warm, dependency-installed snapshot per repo and fork it per speculative run. Morph/Freestyle/
   microsandbox make that cheap; E2B/Modal/Cloudflare make it a "boot N sandboxes from one snapshot"
   orchestration problem.

### 1.5 Open-source primitives

Firecracker: <125 ms boot, <5 MiB overhead, up to 150 microVMs/sec/host; adopters include AWS
Lambda, E2B, Fly.io, Kata, Koyeb, Northflank, Qovery
(<https://firecracker-microvm.github.io/>). gVisor is a userspace application kernel, explicitly not
a syscall filter or VM wrapper (<https://github.com/google/gvisor>). libkrun wraps KVM/HVF into a
minimal VMM (<https://github.com/containers/libkrun>). Apple `container` runs each container in a
lightweight VM on Apple Silicon / macOS 26+ (<https://github.com/apple/container>). bubblewrap gives
unprivileged user-namespace isolation, weaker than any of the above
(<https://github.com/containers/bubblewrap>) — and is what Claude Code itself uses on Linux
(see §7.1 / <https://code.claude.com/docs/en/sandboxing>).

---

## 2. Cloud / background coding agents and how they verify

### 2.1 The universal structural fact

**Every major cloud coding agent starts from a git remote, not from your working tree.** This is the
single most important competitive fact in the whole report.

- **GitHub Copilot coding agent**: runs in "its own ephemeral development environment, powered by
  GitHub Actions", where it can "explore your code, make changes, execute automated tests and
  linters and more." It **works exclusively on GitHub — not from uncommitted local state**. One
  branch at a time, exactly one PR per session, **hard 59-minute cap**. No parallel/best-of-N
  documented (<https://docs.github.com/en/copilot/concepts/agents/coding-agent/about-coding-agent>).
- **Cursor cloud agents**: "isolated VMs in the cloud with full development environments",
  configured via `.cursor/environment.json` using agent-led setup, **saved snapshots**, or a
  Dockerfile. Agents can "build, test, and interact with the changed software"; the docs say an
  environment without test capability leaves the agent unable to *"close the loop on its work."*
  **Clone from source control and work on separate branches** — uncommitted local state not
  addressed. Billed at model API pricing. "Run as many agents as you want in parallel", but no
  multiple-attempts-per-prompt mechanism documented
  (<https://cursor.com/docs/cloud-agents>).
- **OpenAI Codex cloud**: "Run tasks in isolated cloud environments", "give longer tasks dedicated
  environments", "start work in parallel and return as each task reaches a reviewable result."
  Environment dependencies/setup steps configurable per repo. **No documented best-of-N selection**
  and **no documented support for uncommitted local changes**
  (<https://learn.chatgpt.com/docs/cloud>). The `codex-1` launch post reported **72.1% pass@1 and
  83.8% pass@8** on internal evals, but pass@8 is a research number — production gives you one
  attempt per task (<https://openai.com/index/introducing-codex/>) **[the 72.1/83.8 figures are from
  a secondary summary of the launch post; re-verify against the post before quoting]**.
- **Google Jules**: "runs in a virtual machine where it clones your code, installs dependencies, and
  modifies files"; generates a plan you approve before changes. The docs do **not** state whether it
  runs tests automatically, whether it generates multiple candidates, or whether it can see
  uncommitted work (<https://jules.google/docs>) **[unverified]**.

### 2.2 GitHub Agent HQ — the platform-vendor land grab

Announced at GitHub Universe, **2025-10-28**
(<https://github.blog/news-insights/company-news/welcome-home-agents/>):

- **Mission Control**: a command centre across GitHub, VS Code, mobile and CLI for assigning,
  steering and tracking agent work.
- **Third-party agents from Anthropic, OpenAI, Google, Cognition and xAI** available through paid
  Copilot subscriptions.
- **Custom agents** defined with AGENTS.md rules and guardrails.
- **Agents review code before humans see it**; GitHub Code Quality in public preview.
- **Branch controls** giving "granular oversight over when to run CI and other checks for
  agent-created code."

The article does not detail automated test execution beyond CI gating. Read for Pandora: GitHub is
explicitly building the orchestration + review + CI-gating layer for multi-agent work, on the
**server side of the git remote**. That is both a threat (they will eventually want the whole loop)
and a boundary (their product surface begins where a branch is pushed).

### 2.3 Parallel-agent orchestrators

| Product | Isolation | Best-of-N? | Uncommitted tree? | Shared verification infra? | Delivery |
| --- | --- | --- | --- | --- | --- |
| **Sculptor** (Imbue) | per-agent **container** | manual only | no — "Pairing Mode" syncs container work back to local git | not stated | IDE sync, diff, conflict detection |
| **Conductor** | git worktrees only, runs on your Mac with your permissions | no | no — branch per workspace | not stated | diff viewer, "Checks" gate, PR |
| **Vibe Kanban** (BloopAI, sunsetting) | git worktrees | not stated | no | not stated | diff review, PR, browser preview |
| **claude-squad** | git worktrees | no | no | not stated | terminal diff tabs, git commit/push |
| **Crystal** (stravu) | git worktrees | manual compare | no | not stated | **deprecated Feb 2026 → Nimbalyst** |
| **Terragon** | — | — | — | — | **shut down**; site shows only a notice |
| **Charlie** (Charlie Labs) | "devboxes" (isolated Linux, tech unnamed) | no — task decomposition, not competing attempts | no | partial — one task tree can hand off multiple devboxes | results into GitHub/Linear/Slack/Sentry, "no dashboard required" |
| **Factory.ai droids** | "Droid Computers", persistent (4 CPU / 8 GB), BYO or managed | not stated | not stated | implied by persistence | App, Slack, VS Code Remote-SSH, CLI SSH |
| **Devin** | Linux VM booted from an **org-wide snapshot**; CLI sandbox uses bubblewrap | **yes, user-scripted** — `parallel([...])` primitive + documented "retry until tests pass 3× in a row" | no | **yes** — shared pre-baked org snapshot | draft PRs, embedded IDE/shell/browser |
| **Amp** (Sourcegraph) | "Orbs" — remote isolated machines, tech unnamed | no default; "Oracle" is single-pass LLM review | **unclear** — orbs use file sync/upload, `amp sync` mirrors back to local checkout | not stated | diff view, orb terminal, `amp sync` |
| **Cursor cloud agents** | isolated cloud VM, Dockerfile/snapshot configurable | parallel runs, no auto-pick | no | **yes** — background builds pre-warm each environment | PRs with screenshots/video/logs, remote desktop |
| **Jules** | VM per task, plus a "repoless" ephemeral env | **yes, explicit** — `--parallel` flag, up to 5 concurrent attempts on the same prompt, human picks | no | **yes** — per-repo env snapshot reused for future tasks | diff editor + "Publish PR" |

Sources: <https://imbue.com/blog/sculptor-announce>,
<https://conductor.build/docs/concepts/parallel-agents>,
<https://raw.githubusercontent.com/BloopAI/vibe-kanban/main/README.md>,
<https://raw.githubusercontent.com/smtg-ai/claude-squad/main/README.md>,
<https://raw.githubusercontent.com/stravu/crystal/main/README.md>,
<https://charlielabs.ai/how-charlie-works/>, <https://docs.factory.ai/droid-computers/overview>,
<https://docs.devin.ai/work-with-devin/dynamic-workflows.md>, <https://ampcode.com/docs/orbs>,
<https://cursor.com/docs/cloud-agents>, <https://jules.google/docs/environment>,
<https://jules.google/docs/changelog>.

### 2.4 Does anyone do best-of-N with automated validation?

**Almost, but nobody closes the loop automatically.** Corrected from an earlier assumption in this
report:

- **Jules ships an explicit N-parallel flag** (`--parallel`, up to 5 attempts on the *same* prompt,
  changelog 2025-11-10), plus a pre-execution "Planning Critic" claimed to cut task failures by
  9.5% and an adversarial "Critic Agent" reviewer
  (<https://jules.google/docs/changelog>, <https://jules.google/docs/code/>). **The human picks the
  winner** — there is no automated test-based selection.
- **Devin** exposes `parallel([...])` and a documented "retry until tests pass 3× in a row" pattern
  in Dynamic Workflows (<https://docs.devin.ai/work-with-devin/dynamic-workflows.md>) — automated
  validation, but user-scripted rather than a product feature.
- Everyone else's "parallel" means N *different tasks*, not N attempts at one.

So the primitive exists and the validation exists, but **no product joins them into "run N, score by
tests, hand back the winner."** That is a real open lane. Note it is a lane where "spend compute to
save tokens" runs backwards — best-of-N *spends* tokens to buy correctness — so position it
separately from the memoization story.

**Shared verification infrastructure across tasks is already normal**: Devin (org snapshot), Jules
(per-repo snapshot), Cursor (pre-warmed background builds) all amortise environment setup across
agent runs. What none of them share is *results*.

### 2.5 Bugbot — the muddiest claim

cursor.com/bugbot itself describes only diff analysis: "detects the hardest logic bugs with a low
false positive rate", "70%+ of flags get resolved before merge", "more than half of the bugs that we
find are ultimately fixed by engineers". **There is no mention on Cursor's own page of executing
code, running tests, or sandboxes** (<https://cursor.com/bugbot>). Secondary coverage claims Bugbot
"spins up a cloud agent, tests a fix, and proposes the fix directly on your PR", with ~90 s average
review time and 90% under three minutes
(<https://www.digitalapplied.com/blog/cursor-bugbot-90-second-reviews-june-2026-release>)
**[unverified — secondary only; the fix-proposal flow is plausible given Cursor owns cloud agents,
but the execution claim is not on the vendor page]**.

---

## 3. Coordinating parallel agents on one repo

### 3.1 The worktree-per-agent pattern is now the default

By 2026 the "one git worktree per agent" pattern is baseline rather than advanced practice
(<https://www.augmentcode.com/guides/git-worktrees-parallel-ai-agent-execution>). Claude Code itself
exposes worktree isolation natively (`isolation: worktree`), and both Cursor and Windsurf added
first-class worktree support in 2026 releases
(<https://nimbalyst.com/blog/best-git-worktree-tools-ai-coding-2026/>, dated 2026-04-23).

### 3.2 Orchestrators: many, and none of them validate

A 2026-04-23 comparison of worktree tooling covers Nimbalyst, Conductor, Vibe Kanban, Superset,
Claude Squad, Cline, Claude Code, Cursor 3, Windsurf Wave 13, Gastown, OpenClaw+Antfarm, Clash and
Worktrunk. Its own summary column is blunt: **none of them run tests natively**; test integration is
left to the user, external CI, or the agent's own shell
(<https://nimbalyst.com/blog/best-git-worktree-tools-ai-coding-2026/>).

That is the single most important structural fact for Pandora in this category. The orchestration
layer (which worktree, which agent, which kanban column) is crowded and commoditised. The
**validation** layer under it is empty.

### 3.3 Clash — the closest competitor for cross-worktree conflict detection

`clash-sh/clash` is an MIT-licensed Rust CLI explicitly built for "avoid[ing] merge conflicts across
git worktrees for parallel AI coding agents"
(<https://raw.githubusercontent.com/clash-sh/clash/main/README.md>). Mechanism:

- Discovers all worktrees (main + linked), finds the merge base for each **pair**, and runs
  `git merge-tree` three-way merges via the `gix` library — entirely read-only.
- **Analyses uncommitted work**: it detects conflicts across staged, unstaged *and* committed
  changes.
- Commands: `clash check <file>` (pre-edit), `clash status` (conflict matrix), `clash watch` (live
  TUI), `clash status --json` (machine-readable).
- **Agent delivery**: ships a Claude Code plugin (`claude plugin marketplace add clash-sh/clash`)
  that installs a **PreToolUse hook on Write/Edit/MultiEdit** to check for conflicts before the
  agent writes. For other agents it falls back to `.cursorrules` / instruction files.
- Show HN was ~6 months before 2026-09 (i.e. around 2026-03), author "matk9", low engagement
  (<https://news.ycombinator.com/item?id=47137470>).

**How Pandora differs.** Clash answers "will these two worktrees textually conflict?" Pandora's
pitch is the strictly harder question: "will these two worktrees *merge cleanly and still break the
build*?" That is a semantic conflict, and it can only be answered by actually merging the two dirty
trees and running the suite — which requires exactly the remote-execution substrate Pandora is
building. Clash also validates the *delivery channel* (PreToolUse hook into Claude Code) and proves
the demand, at a small scale. It is prior art, not a blocker; it is also free and local, which caps
what anyone can charge for the textual-conflict feature alone.

### 3.4 Merge queues and stacking

- **Graphite** now embeds Cursor Cloud Agents directly into the Graphite PR surface, so agents can
  "create, iterate, and merge without switching contexts", with CI verification in the loop
  (<https://graphite.com/features/agents>). Graphite's reviewer product is diff-reading, not
  code-executing **[unverified — the agents page does not describe Diamond's execution model;
  covered further in §4]**.
- Merge queues in general are the *only* mainstream product family that already does speculative
  parallel validation of candidate merge orders. But they are strictly post-commit, post-PR. The
  "merge-ahead testing" Pandora proposes is a merge queue moved left of the commit — which no queue
  vendor can do, because they have no access to your working tree.

---

## 4. AI code review and verification: who actually executes code

### 4.1 The split

| Product | Static or executes? | Sandbox | Validates generated tests by running? | Flaky triage | Uncommitted tree? | Feeds agents? |
| --- | --- | --- | --- | --- | --- | --- |
| **CodeRabbit CLI** | **static only — explicit**: "does not execute or run code or tests. It performs static analysis only" | n/a | n/a | not stated | **yes — `--uncommitted`, `--include-untracked`** | CLI + Claude Code/Codex plugins + `--agent` JSON output |
| **Sourcery** | **static only** | n/a | n/a | not stated | **yes** — IDE review runs on the working diff | API; CLI/MCP unconfirmed |
| **Greptile** | core review static; **TREX add-on executes** — "spins up services, dev servers, mocks, browser agents… runs your PR branch in a sandbox" | undisclosed | unclear | not stated | not confirmed — PR/branch oriented | **CLI, MCP, API** |
| **Cursor Bugbot** | static per its own page (see §2.5) | — | — | — | no | PR comments → cloud agent **[unverified]** |
| **Qodo Cover** | **executes** — subprocess `pytest`/`go test`, Docker for integration | Docker | **yes, explicit** generate→run→coverage loop | not stated | **yes** — local file paths, no VCS required | **CLI, MCP, Claude Code/Codex plugins** |
| **Diffblue Cover** | **executes** — tests "compile, run, and accurately validate current behavior" | local, "no cloud service required" | **yes, explicit** | not stated | unclear | CLI; no MCP found |
| **Ellipsis** | **executes** — "isolated cloud environments", VM-based | generic VM | one Playwright example; unclear | not stated | not stated | CLI + API; no MCP |
| **Meticulous** | **executes** — headless Chromium "heavily parallelized across a compute cluster" | cloud cluster | yes (session replay is the validation) | **yes, explicit** — "built to eliminate all flakes… deterministic scheduling engine" | partial — local replay/CLI, but `--commitSha` oriented | CLI, MCP (data layer only), API |
| **Momentic** | **executes** — hosted browsers, Android emulators, iOS simulators | unnamed | **yes, explicit** | **yes, explicit** — self-healing specs, root-cause analysis | **yes, explicit** — "same command locally and in CI" | CLI, MCP, API |
| **QA.tech** | **executes** — cloud-hosted vision-based agents | unnamed | **yes, explicit** | not stated | partial — local dev server via secure tunnel | MCP (16 tools) + API |
| **Antithesis** | **executes** — deterministic hypervisor, fault injection, time-travel debugger | own hypervisor | n/a | **yes — "never fight another flaky test"**, via instruction-level determinism | no | **agent-facing skills**: "your agent only declares victory once our platform has verified its output" |
| **Tusk** | **executes** — generated tests run "locally or in CI", not a proprietary sandbox | your CI/local | yes | not stated | partial — "run locally before merging" | CLI (`cli.usetusk.ai`) |
| **CodeBeaver** | **unverifiable** — domain 404 / TLS mismatch; June-2026 Wayback snapshot shows spam. Probably dead. | — | — | — | — | — |

Sources: <https://docs.coderabbit.ai/cli>, <https://docs.sourcery.ai/>,
<https://www.greptile.com/trex.md>, <https://github.com/qodo-ai/qodo-cover>,
<https://cover-docs.diffblue.com>, <https://ellipsis.dev/docs/environments>,
<https://app.meticulous.ai/docs>, <https://momentic.ai>, <https://qa.tech/product/mcp>,
<https://antithesis.com/product/what_is_antithesis/>, <https://www.usetusk.ai/>.

**Pattern:** pure *review* tools are static; execution lives in *test-generation* tools (Qodo Cover,
Diffblue) and *E2E/dynamic* tools (Meticulous, Momentic, QA.tech, Antithesis). Nobody executes a
repo's own existing test suite as a review artifact — which is precisely the "review evidence pack"
Pandora proposes.

### 4.2 The one that should worry you: CodeRabbit CLI

CodeRabbit's CLI has **`--uncommitted` and `--include-untracked` flags** and an **`--agent` JSON
output mode**, with Claude Code and Codex plugins (<https://docs.coderabbit.ai/cli>). That is:
uncommitted-tree input, agent-shaped output, agent-harness integration — the same three properties
Pandora claims. CodeRabbit also runs a cloud "Coding Agent" that turns a review finding or a failing
CI check into an implemented task (<https://docs.coderabbit.ai/guides/code-review-overview>).

The saving grace is that CodeRabbit **explicitly does not execute code**. They have the shape and
the distribution; Pandora's claim has to be that *running the tests* is what makes the evidence
worth reading. That is a defensible but narrow line, and it will be tested the moment CodeRabbit
adds a sandbox.

### 4.3 Memoized test results and test-impact analysis — the real prior art

This is where the strongest prior art for "memoized results for uncommitted trees" lives, and the
finding is more mixed than expected.

| System | Content-hash memoization? | Test-impact selection? | Works on a local uncommitted tree? | Published numbers |
| --- | --- | --- | --- | --- |
| **Bazel remote cache** | **yes, explicit** — action cache keyed on a hash of declared inputs/outputs/command; env vars only counted if allow-listed via `--action_env` | no | **yes, explicit** — documented `.bazelrc` local config; disk cache for branch/workspace switching | — |
| **BuildBuddy** | implied via Bazel REAPI action cache | no | **yes, explicit** — docs show a developer running `bazel build --remote_executor=grpcs://remote.buildbuddy.io` from their own machine, "not just CI systems" | — |
| **Nx Cloud** | **yes, explicit** — "Nx hashes those inputs to calculate the task hash"; local cache checked before remote | **yes** — `affected` via git diff + dependency graph | **yes, explicit** — `affected` defaults its baseline to **"your current file system"**, i.e. uncommitted changes | qualitative only |
| **Gradle Develocity PTS** | adjacent — reuses Build Cache input snapshots to feed an ML model | **yes** — ML trained on the org's Build Scan history | ambiguous — local activity tracked, PTS-on-dirty-tree not confirmed | **"51 days 11 hours" serial test time saved across 47.3K tasks** (63% enabled); one build 41% faster |
| **Datadog Test Optimization** | no — coverage graph + git diff + Bloom filter (~0.04% FP) | **yes** | not stated; CI-only setup documented | no % in text |
| **Trunk.io** | not stated | "predictive testing" = merge batching against a predicted merge state, not change-impact selection | **no** — CI/merge pipeline only | — |

Sources: <https://bazel.build/remote/caching>, <https://www.buildbuddy.io/docs/rbe-setup/>,
<https://nx.dev/ci/features/remote-cache>, <https://nx.dev/ci/features/affected>,
<https://develocity.ai/develocity/product/predictive-test-selection/>,
<https://docs.datadoghq.com/tests/test_impact_analysis>,
<https://docs.trunk.io/flaky-tests/overview>, <https://docs.trunk.io/merge-queue/merge-queue>.

**Two important corrections to the Pandora premise:**

1. **Content-hash memoization on an uncommitted tree already ships** — in Bazel, BuildBuddy and Nx
   Cloud. Nx's `affected` explicitly compares against the current filesystem, not a commit. So
   "memoized results for uncommitted trees" is **not novel**; it is novel only *outside the
   hermetic-build-graph world*. Pandora's actual claim has to be "we do this for repos that do not
   have a Bazel/Nx graph", i.e. the messy long tail where inputs cannot be declared. That is a
   much harder engineering problem and a much weaker cache-hit guarantee — be honest about it in the
   design doc.
2. **Nobody combines the two families.** Content-hash task memoization (Bazel, Nx, BuildBuddy) and
   coverage/ML test-impact analysis (Datadog, Develocity) are separate product families. A
   "test-result cache keyed on content hash, with impact-based selection, on a dirty tree" does not
   exist as a product. That combination is the genuinely open slot.

---

## 5. Remote dev/test offload for local agents

### 5.1 Crabbox — the closest direct competitor to Pandora's base layer

`crabbox.sh` / `openclaw/crabbox`, MIT, ~1.4k stars, 3,026 commits on main
(<https://crabbox.sh/>, <https://github.com/openclaw/crabbox>).

- Tagline: *"warm a box, sync the diff, run the suite."* Pipeline is **lease → sync → run →
  release**.
- **Syncs uncommitted changes and non-ignored files without pushing a commit** — the exact primitive
  Pandora is built on. Dependency/build dirs excluded by default; deps installed on the box.
- **Warm boxes**: `crabbox warmup` / `crabbox prewarm` create persistent leases reused across an
  edit-run loop, syncing only changed files on subsequent runs.
- Cache volumes and cache controls exist; providers like local containers support reusable caches.
- Backends: local containers (Docker/Podman), static SSH, AWS/Azure/GCP/DigitalOcean/Hetzner,
  Proxmox/Firecracker/KVM, Apple Silicon VMs, and managed sandboxes (E2B, Daytona, Modal,
  Cloudflare, Anthropic, GitHub Codespaces, Railway, RunPod, Vercel). The site claims **81
  registered providers**; the README claims 15+ — treat the larger number as marketing
  **[unverified]**.
- **Agent integration**: `crabbox init --detect` generates an Agent Skill for compatible coding
  agents; skills published to npm; Zed and Herdr plugins.
- Business model: *"Crabbox Software Is Free. Compute Isn't."* MIT CLI, you pay providers. Admins
  set TTL and spend caps; leases rejected above limits.
- Coordinator runs on Cloudflare or Node+Postgres, with direct and team-coordinator modes.

**What Crabbox does NOT do** (checked against both README and docs site): no digest-verified frozen
snapshot semantics described, no result memoization keyed on tree content, no speculative execution
(only `prewarm` of the *environment*, not of the *command*), no failure triage, no cross-agent
conflict/merge-ahead work, no review evidence packs. Its `prewarm` is warm-pool provisioning, not
speculative validation.

**Assessment.** Crabbox owns the plumbing layer Pandora needs and gives it away under MIT. That is
simultaneously the biggest threat (a well-funded team could bolt speculation on top) and the
strongest validation (someone built the hard, unglamorous dirty-tree sync and it got traction).
Pandora's differentiation has to live entirely above the transport: memoization, speculation,
triage, cross-agent. If Pandora's pitch is "we ship your dirty tree to a remote box", Crabbox has
already won that on price.

### 5.2 Buildkite Preflight — the closest competitor for agent-facing verification

Preflight is one of four "agentic primitives" Buildkite shipped (MCP Server, Model Providers,
Pipeline Triggers, Preflight), status **experimental**
(<https://buildkite.com/platform/agentic-workflows/>).

- *"Catches build failures the moment they happen. Agents instantly receive structured signals to
  act on, fix, and re-run."*
- Handles uncommitted work by **capturing changes in a temporary branch that is cleaned up
  afterwards** — i.e. it fakes a commit rather than shipping a content-addressed snapshot.
- Supports fail-fast (stop on first failure) and **test prioritisation using cached resources**.
- Results reach agents as *structured signals* rather than raw logs; the MCP Server is described as
  providing scoped pipeline access with **token optimisation via log caching**.

**How Pandora differs.** Preflight is the strongest evidence that a CI vendor sees the same problem
Pandora sees — "agent needs structured validation before commit, and raw logs waste tokens." But it
is (a) experimental, (b) pipeline-shaped (you must have a Buildkite pipeline), (c) commit-shaped
(temp branch), (d) reactive rather than speculative, and (e) single-agent — no cross-worktree view.
Notably Buildkite independently converged on the *token-cost* framing ("log caching for token
optimization"), which supports the Pandora thesis.

### 5.3 Depot remote agent sandboxes

<https://depot.dev/blog/now-available-remote-agent-sandboxes>, <https://depot.dev/docs/agents/overview>

- Per-agent isolated container, **2 vCPU / 4 GB**, typically **starts in under 5 s**, persistent
  filesystem mounted with the project codebase.
- **$0.01/minute, billed by the second with no one-minute minimum**, billed only while the agent is
  processing a prompt.
- **Clones from a git provider** (`--repository` / `--branch`) — it does *not* sync a local dirty
  tree. This is the standard failure mode of the whole "remote agent" category.
- The publish date given by the fetch was ambiguous **[unverified — Depot's X post
  <https://x.com/depotdev/status/1955715771964776701> announces it; treat as 2025-08]**.

The $0.01/min figure is a useful public anchor for Pandora's unit economics: a 2 vCPU box costs
$0.60/hour of *active* time, and Depot charges nothing while idle. If Pandora's speculative runs are
short and bursty, this is the price floor a competitor could match.

### 5.4 Ona (formerly Gitpod)

<https://ona.com/docs> — "the platform for background agents", ephemeral isolated environments,
kernel-level security, SSO/OIDC/SCIM, Dev Container support, triggered by PRs/issues/webhooks. The
docs do not state whether it runs tests as a built-in function or whether it can work from a local
uncommitted tree **[unverified]**. Like Depot, the entry point is a repo + branch, not your desktop.

### 5.5 Namespace

<https://namespace.so/docs/features/faster-builds> confirms remote Docker builders on dedicated
compute with NVMe and AMD64/ARM64. Devboxes, cross-invocation caching, microVM details and pricing
were not confirmed from the page fetched **[unverified — gap]**.

### 5.6 Coder, DevPod, Okteto, mirrord, Tilt, Garden

Not fetched (search budget exhausted) **[gap]**. Directionally: these are remote/ephemeral *dev
environment* tools whose unit of work is a long-lived workspace or a cluster-attached process, not a
per-command content-addressed snapshot. Coder announced Claude Code support in "Agent Relay" for
regulated enterprises on 2026-09-15
(<https://www.globenewswire.com/news-release/2026/09/15/3362210/0/en/coder-brings-claude-code-to-agent-relay-unlocking-agentic-development-for-the-world-s-most-regulated-enterprises.html>)
— evidence that the enterprise wedge here is *governance*, not speed.

---

## 6. Published data: where agents waste tokens and time

### 6.1 The strongest single number for Pandora

**"How Do AI Agents Spend Your Money? Analyzing and Predicting Token Consumption in Agentic Coding
Tasks"**, Bai, Huang, Wang, Sun, Mihalcea, Brynjolfsson, Pentland, Pei — arXiv 2604.22750, v2 dated
2026-04-30 (<https://arxiv.org/abs/2604.22750>, <https://arxiv.org/html/2604.22750v2>). Eight
frontier models on SWE-bench Verified. Findings:

- Trajectories decompose into five phases by share of rounds: **Explore 30.37%, Fix 33.53%,
  Validate 16.59%, Setup 9.98%, Closeout 9.53%.** → **~26.6% of agent rounds are Validate + Setup**,
  i.e. the exact work Pandora proposes to move off the agent's context.
- Agentic tasks consume **~1000× more tokens than code chat / code reasoning, with *input* tokens,
  not output tokens, driving cost.**
- **Cache-read tokens dominate both volume and dollar cost across all phases**, despite output
  tokens being priced ~80× higher — because accumulated input context is so large.
- **Runs on the same task differ by up to 30× in total tokens.**
- High-cost failing runs show **~50% repeated file actions on the same files** — "inefficient search
  dynamics that inflate context length and token usage without proportional progress."
- **More tokens ≠ more accuracy**: accuracy peaks at intermediate cost and saturates or declines.
- Models cannot predict their own token usage (correlation ≤ 0.39).

Read carefully, this is a **mixed** result for Pandora. It strongly supports "validation is a large,
addressable slice of agent work" and "test output inflates context permanently, and you pay for it
on every subsequent turn via cache reads." It weakly *undermines* the naive version of the thesis,
because the dominant single cost is context re-reading during *exploration and fixing*, not test
execution itself — so Pandora's savings come from (a) not putting raw logs in context and (b) not
needing the Setup phase, more than from the test run itself.

### 6.2 Trajectory failure analysis

A 2026 analysis of OpenHands, SWE-agent and Prometheus trajectory logs reports **failure
trajectories are 12–82% longer than successful ones**, and that **repository navigation dominates
agent activity over patch writing** **[secondary source: cited in
<https://arxiv.org/html/2604.20779v1> and related surveys; I did not reach the primary Majgaonkar et
al. paper — verify before quoting]**. Also reported: SWE-agent and OpenHands consume ~2× the tokens
of MiniSWE-Agent for modest success gains **[unverified]**.

### 6.3 Language-dependent waste

"The Best Programming Language for Tokenmaxxing", arXiv 2607.22807
(<https://arxiv.org/html/2607.22807v1>): after controlling for difficulty, agents spend **1.28–1.69×
more tokens on OCaml than Python**, 1.07–1.57× on Rust, 0.85–1.34× on Java. Smaller models generate
hundreds-to-thousands of tokens *after* reaching a working solution on cosmetic refactoring. Agents
frequently prototype in Python then translate. Relevant to Pandora only as colour: waste is real and
measurable, but it is concentrated in debugging loops, not build systems.

### 6.4 Human review is measurably the bottleneck — the strongest evidence FOR the review-pack idea

Faros AI longitudinal telemetry, **10,000+ developers across 1,255 teams, up to two years of data,
published 2025-07-28** (<https://www.faros.ai/blog/lab-vs-reality-ai-productivity-study-findings>):

| Metric | Change with AI adoption |
| --- | --- |
| Tasks handled per day | +9% |
| PRs per day | +47% |
| Task completion rate | +21% |
| **Merged PRs** | **+98%** |
| **PR size** | **+154%** |
| **Code review time** | **+91%** |
| Bugs per developer | +9% |

Doubling merged PRs while nearly doubling review time and 2.5×-ing diff size is the clearest public
quantification of the agent-era review bottleneck.

Supporting: METR's RCT (16 experienced OSS developers, 246 issues, published 2025-07-10) found
developers were **19% slower** with early-2025 AI tools while *believing* they were 20% faster
(<https://metr.org/blog/2025-07-10-early-2025-ai-experienced-os-dev-study/>). METR revised its
experimental design in 2026 because participants refused to work without AI
(<https://metr.org/blog/2026-02-24-uplift-update/>), and believes developers are more sped up in
early 2026 than in early 2025 — so do not over-cite the 19% figure as current.

Counter-signal: a Microsoft study of CLI agent rollout to **tens of thousands of engineers in early
2026** found adopters **merged ~24% more PRs**, sustained over a four-month window
(<https://arxiv.org/abs/2607.01418>). The authors caveat that "a merged PR is not the same as the
value it delivers."

### 6.5 What I could NOT find

No public study quantifying **flaky/pre-existing failure cost specifically to coding agents**, and
no published data on how many agent turns are spent re-running a suite the agent already ran. This
is the single biggest evidence gap under the Pandora thesis and would be the highest-value thing to
measure yourself from local Claude Code / Codex transcripts. **[gap]**

---

## 7. Agent-facing delivery channels: how to inject unsolicited context

This is the part of the idea with the *best* technical footing, and it is worth being precise
because the whole product depends on it.

### 7.1 Claude Code hooks (<https://code.claude.com/docs/en/hooks>)

The hook system explicitly supports injecting context into the model's transcript. Relevant fields:

- **`additionalContext`** — "extra information added to Claude's context." Available on
  **`PreToolUse`, `PostToolUse`, `PostToolUseFailure`**. Per the docs, it is *prepended to tool
  input/output*.
- **`systemMessage`** — a message shown to Claude in the transcript; available on most events.
- **`updatedInput`** — only on `PreToolUse`; replaces the entire `tool_input` object.
- **`permissionDecision` / `permissionDecisionReason`** — allow/deny/ask on `PreToolUse`,
  `PermissionRequest`, `PermissionDenied`, `UserPromptSubmit`, `UserPromptExpansion`.
- Exit code 2 **blocks** on `PreToolUse`, `UserPromptSubmit`, `UserPromptExpansion`, `Stop`,
  `WorktreeCreate`, `WorktreeRemove`, `PreModelSwitch`; it does **not** block on `PostToolUse` /
  `PostToolUseFailure` (the tool already ran) but the message still reaches the transcript.

**This is exactly the Pandora delivery mechanism, and it exists today.** The intended play is:

1. `PreToolUse` on the agent's `Bash(npm test)` call → if Pandora already has a memoized result for
   this tree digest, **deny with a reason** (or `updatedInput` to a no-op) and return the cached
   result, saving the whole run.
2. `PostToolUse` / `PostToolUseFailure` on a test command → attach `additionalContext` with the
   triage verdict ("3 of these 5 failures are pre-existing on main; 1 is a known flake; 1 is yours,
   at src/foo.ts:42").
3. `WorktreeCreate` / `FileChanged` / `SessionStart` → trigger speculative runs.

Note that Claude Code also exposes `FileChanged`, `TaskCreated`, `TaskCompleted`, `WorktreeCreate`
and `WorktreeRemove` events — a richer trigger surface than most people realise — but these are
**logging-only** for context purposes (no `additionalContext`), so they are useful as *triggers* but
not as *delivery*.

Risk to flag: `additionalContext` on `PostToolUse` is prepended to the tool output. Anything Pandora
injects is unsolicited text the model did not ask for, and models do ignore such text when it is
verbose or low-signal. The discipline has to be: one or two lines, imperative, with a file:line.

### 7.2 Codex CLI

Codex supports lifecycle hooks configurable at user/project/session level, with an admin control
`allow_managed_hooks_only = true` in `requirements.toml`
(<https://raw.githubusercontent.com/openai/codex/main/docs/config.md>).

**Codex hooks are near-identical in shape to Claude Code's**, per a source-grounded read of
`codex-rs/hooks/src/lib.rs` and `schema.rs`
(<https://deepwiki.com/openai/codex>, queried 2026-09-20):

- Events: `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`,
  `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `SubagentStart`, `SubagentStop`, `Stop`,
  `Interrupt` (constant `HOOK_EVENT_NAMES`).
- JSON over stdin/stdout with `hookSpecificOutput`. **`PostToolUse` supports `additionalContext`,
  and that context is recorded into the session history** (`PostToolUseOutcome.additional_contexts`).
- `PreToolUse` supports `permissionDecision`, `permissionDecisionReason` and `additionalContext`;
  `PreToolUseOutcome` has `should_block` and `updated_input`.
- **`updatedMCPToolOutput` on `PostToolUse` exists but is currently unsupported** and fails open,
  preserving the original tool output — so you can *append* context but not *replace* a tool result.

**This closes the portability worry.** The exact Pandora delivery mechanism — append triage to a
test command's tool result, or deny/rewrite a redundant test invocation before it runs — works
identically on Claude Code and Codex CLI today. The two dominant local agent harnesses converged on
the same contract. Cursor and Gemini CLI still need the MCP fallback **[unverified]**.

### 7.3 AGENTS.md

<https://agents.md/> — open format, **60,000+ open-source repos**, originated from OpenAI Codex,
Amp, Google Jules, Cursor and Factory, now stewarded by the **Agentic AI Foundation under the Linux
Foundation**. Read by Codex, Copilot, Jules, Gemini CLI, Cursor, VS Code, Zed, Warp, Aider, goose,
Factory, Devin and others. Agents read the nearest AGENTS.md up the directory tree.

For Pandora this is a *static* channel: good for "use `pandora test` instead of `npm test`",
useless for delivering a result. Treat it as the installation surface, not the delivery surface.

### 7.4 MCP

MCP is the portable channel but is fundamentally **pull, not push** — the agent must decide to call
a tool. Buildkite's MCP Server is a good example of the pattern in this space, notably with "token
optimization via log caching" (<https://buildkite.com/platform/agentic-workflows/>). For Pandora,
MCP is the right fallback for Codex/Cursor/Gemini, but it loses the key property: unsolicited
delivery. Hooks push; MCP waits.

**Design consequence:** Pandora's headline behaviour (results the agent didn't ask for) works
natively on **both** Claude Code and Codex CLI via hooks (§7.1, §7.2), and degrades to pull-based
MCP on Cursor, Gemini CLI and the rest. Since Claude Code + Codex CLI are the two harnesses named
in the product premise, this is a smaller constraint than it first appears.

### 7.5 PR comments consumed by agents

CodeRabbit explicitly closes this loop: it has a "Coding Agent" that "turns a user request, a review
finding, a coding plan or a failing CI check into a task that CodeRabbit's agent implements in the
cloud" (<https://docs.coderabbit.ai/guides/code-review-overview>). Cursor's Bugbot "spins up a cloud
agent, tests a fix, and proposes the fix directly on your PR"
**[unverified — from secondary source <https://www.digitalapplied.com/blog/cursor-bugbot-90-second-reviews-june-2026-release>;
cursor.com/bugbot itself says nothing about executing code]**. Antithesis markets agent-facing
skills where "your agent only declares victory once our platform has verified its output"
(<https://antithesis.com/product/what_is_antithesis/>).

The pattern is established: review findings → agent task. But it is all post-PR. Nobody is doing
findings → agent, pre-commit, in-loop.

---

## 8. Who owns which part of Pandora today

| Pandora feature | Closest owner(s) | Their unit of work | What they cannot do |
| --- | --- | --- | --- |
| Dirty-tree → remote execution | **Crabbox** (MIT) | uncommitted tree ✅ | no memo, no speculation, no triage |
| Digest-verified frozen snapshot | Morph / Freestyle / microsandbox (VM snapshot) | VM image | not keyed to *your* working tree content |
| Speculative validation before asked | **Morph Infinibranch** demo; merge queues | VM branch / PR | Morph has no repo semantics; queues need commits |
| Memoized results for uncommitted trees | **Nx Cloud** (`affected` vs current filesystem), Bazel, BuildBuddy | hashed declared build-graph inputs ✅ | needs a hermetic graph; no help for a plain `pytest` repo |
| Failure triage (mine/flaky/pre-existing) | Meticulous, Momentic (own runners); Datadog, Develocity (CI) | their own tests, or CI runs | not your repo's suite, not in the agent's loop |
| Hunk-level bisect of dirty diff | nobody found | — | — |
| Pairwise conflict detection across worktrees | **Clash** (MIT) | worktree pairs, incl. uncommitted ✅ | textual `git merge-tree` only; no test execution |
| Merge-ahead testing | merge queues (Trunk, Graphite, GitHub) | committed PRs | cannot see uncommitted work |
| Review evidence packs | **CodeRabbit CLI** (`--uncommitted`, `--agent`), Greptile TREX, Sourcery IDE | working diff ✅ (CodeRabbit, Sourcery) | CodeRabbit + Sourcery explicitly do not execute; TREX is PR-scoped |
| Unsolicited delivery into the agent loop | **Claude Code + Codex CLI hooks**, Buildkite Preflight, Clash plugin | tool call boundary ✅ | nobody has anything worth delivering |

Five things in this table already work on uncommitted state: **Crabbox** (transport), **Clash**
(textual conflicts), **Nx/Bazel/BuildBuddy** (hashed memoization, if you have the graph),
**CodeRabbit CLI** and **Sourcery** (static review). Crabbox, Clash and Bazel are free. The one
thing *nobody* does on an uncommitted tree is **execute the repo's own test suite and reason about
the result** — that is the actual hole.

---

## 9. Closest direct competitors, and how they differ

1. **Crabbox** — same transport, same audience, MIT, already integrated with agent skills. Differs
   by having no intelligence above the transport: no content-addressed result cache, no speculation,
   no triage, no cross-agent awareness. **This is the competitor to benchmark against, and the one
   that caps your pricing on the transport layer at $0.**
2. **Buildkite Preflight** — same insight (agents need structured pre-commit validation, raw logs
   waste tokens), from a CI vendor with distribution. Differs by being pipeline-shaped,
   temp-branch-shaped, reactive, single-agent and experimental. If Buildkite makes this GA and
   local-first, they are the most credible fast-follower with an existing enterprise channel.
3. **Morph Cloud** — owns the speculation primitive and already ships a 16-way parallel speculative
   branching demo, already integrates with Claude Code. Differs by having no repo/worktree/diff
   semantics: it is infrastructure, not a product for a developer with four dirty worktrees. Likely
   backend, possible acquirer, possible competitor if they move up-stack.
4. **Clash** — owns the conflict-detection feature outright, free, with the same hook-based delivery
   channel. Differs by being textual-only. Pandora's version is "did these two dirty trees merge
   cleanly *and pass*", which Clash structurally cannot answer.
5. **Merge queues (Trunk / Graphite / GitHub)** — own speculative parallel validation of candidate
   merge orders. Differ by requiring commits and PRs. "A merge queue that runs left of the commit"
   is a genuinely clean way to describe Pandora to someone technical.
6. **CodeRabbit CLI** — already has the three structural properties (uncommitted input, agent JSON
   output, harness plugins) and a large install base. Differs only in that it **explicitly does not
   execute** (<https://docs.coderabbit.ai/cli>). One sandbox away from being the direct competitor.
7. **Nx Cloud / Bazel + BuildBuddy** — already do content-hash memoization against the working
   filesystem. Differ by requiring a hermetic declared-input build graph. They own the easy half of
   the market (monorepos with a graph) and will never serve the messy half.

---

## 10. The "spend free RAM to save paid tokens" thesis

### Evidence FOR

- **~26.6% of agent rounds are Validate (16.59%) + Setup (9.98%)** on SWE-bench Verified across
  eight frontier models (<https://arxiv.org/html/2604.22750v2>). That is a large, well-defined slice
  to remove from the agent's loop.
- **Input tokens, not output tokens, drive agentic cost**, and **cache-read tokens dominate both
  volume and dollar cost across every phase** (same source). Test output pasted into context is
  charged again on every subsequent turn. Replacing 400 lines of pytest output with one line of
  triage compounds across the rest of the session. This is the sharpest mechanistic argument the
  thesis has.
- **Same task, same agent, up to 30× token variance between runs**; high-cost failure runs show
  ~50% repeated file actions (same source). Agents genuinely thrash, and validation-driven thrash is
  the subset Pandora can address.
- **More tokens does not mean more accuracy** — accuracy peaks at intermediate cost (same source).
  So token savings are not being traded against quality.
- **Failure trajectories run 12–82% longer than successful ones**
  **[secondary; verify Majgaonkar et al.]** — cutting the failure loop short is disproportionately
  valuable.
- Cursor's own docs concede the framing: an environment where the agent cannot test leaves it unable
  to "close the loop on its work" (<https://cursor.com/docs/cloud-agents>).
- Buildkite independently converged on token cost as the reason to give agents *structured* results
  and cached logs (<https://buildkite.com/platform/agentic-workflows/>).
- The delivery channel exists and is documented, not speculative: `additionalContext` on
  `PostToolUse` / `PostToolUseFailure`, and `updatedInput` / deny-with-reason on `PreToolUse`
  (<https://code.claude.com/docs/en/hooks>).
- Human review is measurably the bottleneck: **+98% merged PRs, +154% PR size, +91% review time**
  across 10,000+ developers (<https://www.faros.ai/blog/lab-vs-reality-ai-productivity-study-findings>).
  Review evidence packs address a quantified, worsening pain.

### Evidence AGAINST

- **"Free RAM" is mostly rhetorical if you rent cloud compute.** Runloop, Blaxel and Cloudflare all
  zero out compute billing on suspend; Vercel doesn't bill I/O wait; Depot charges $0.01/min only
  while active (<https://www.runloop.ai/pricing>, <https://www.blaxel.ai/pricing>,
  <https://developers.cloudflare.com/containers/pricing/>, <https://vercel.com/docs/sandbox/pricing>,
  <https://depot.dev/docs/agents/overview>). There is no idle capacity lying around to scavenge —
  speculative runs are *bought*, and a wrong speculation is money burned. The thesis only holds
  literally on self-hosted or already-paid-for fleets.
- **The dominant token cost is exploration and fixing, not validation.** Explore + Fix = 63.9% of
  rounds vs Validate + Setup 26.6% (<https://arxiv.org/html/2604.22750v2>). Pandora addresses the
  minority slice. A plausible ceiling is 10–20% of agent spend, not 50%.
- **Agents ignore unsolicited context.** There is no published evidence that hook-injected
  `additionalContext` changes agent behaviour, and plenty of folk evidence that agents skim
  injected text. **[gap — no study found]** The whole "delivered in the next tool result" mechanism
  is an untested assumption.
- **Speculation hit rate is unproven.** Predicting *which* command the agent will run next, on
  *which* tree state, before it asks, is the hard part. If the hit rate is 30%, you pay for three
  runs to save one, and the economics invert.
- **Memoization on a dirty tree is weaker than it sounds.** Agents edit constantly; each edit
  invalidates the digest. Cache hits concentrate on the narrow case of "agent re-runs the identical
  suite on an unchanged tree", which good agents already avoid.
- **Memoization on a dirty tree is also not novel.** Nx Cloud hashes task inputs and `affected`
  explicitly baselines against "your current file system"; Bazel's action cache is content-hashed
  and documented for local developer machines; BuildBuddy shows developers running remote execution
  from their own laptops (<https://nx.dev/ci/features/affected>, <https://bazel.build/remote/caching>,
  <https://www.buildbuddy.io/docs/rbe-setup/>). Pandora's version only adds value where there is no
  hermetic build graph — which is exactly where content-hashing is unreliable, because you cannot
  enumerate the inputs. That tension deserves an explicit answer in the design doc.
- **The transport is free.** Crabbox gives away the hardest engineering (dirty-tree sync, 15+
  providers, warm boxes) under MIT (<https://github.com/openclaw/crabbox>). The review-pack shape is
  free too: CodeRabbit CLI already reads uncommitted+untracked changes and emits agent JSON
  (<https://docs.coderabbit.ai/cli>).
- **The category has casualties.** Terragon has shut down and CodeBeaver's domain is dead (404/TLS
  mismatch, overtaken by spam per a June-2026 Wayback snapshot). Crystal deprecated itself in Feb
  2026; Vibe Kanban is sunsetting. Agent-tooling churn in 2026 is high, which cuts both ways for a
  new entrant.
- **Flaky-failure cost to agents is undocumented.** Nobody has published what fraction of agent turns
  are wasted on pre-existing or flaky failures. The triage feature's value is assumed, not measured.
  **[the highest-value gap to close, and you can close it yourself from local transcripts]**

### Net read

The mechanism (hooks) is real and documented. The waste (validate + setup ≈ 27% of rounds; cache
reads dominating cost) is real and measured. The *framing* is the weak part: "free RAM" implies
scavenged idle capacity, which does not exist in the rented-compute world, and the token savings
ceiling is nearer 10–20% than transformative. The strongest honest pitch is narrower and better
evidenced: **"your agent pastes 400 lines of test output into a context you pay to re-read every
turn; we replace it with one line of triage, and we already ran the test before it asked."**

---

## 11. Would a platform vendor do this natively?

**Short answer: GitHub and Cursor will build adjacent versions; neither will build the uncommitted
part; Anthropic and OpenAI have structural reasons not to.**

**GitHub — most likely, least dangerous.** Agent HQ (2025-10-28) is explicitly the multi-agent
mission control, with agent-before-human code review, GitHub Code Quality, and branch controls over
when CI runs for agent code (<https://github.blog/news-insights/company-news/welcome-home-agents/>).
They will absolutely build "test agent PRs speculatively and triage failures." But GitHub's entire
product surface starts at `git push`. GitHub has never had a product that reaches into your working
tree, and Copilot coding agent explicitly does not access local machine state
(<https://docs.github.com/en/copilot/concepts/agents/coding-agent/about-coding-agent>). The
uncommitted-tree wedge is structurally out of their reach.

**Cursor — most dangerous.** Cursor owns the editor (so it sees the dirty tree), owns cloud agents
with VM snapshots, owns Bugbot, and added worktree support in 2026. Cursor is the one company with
all the pieces and the motive. Counter-argument: Cursor's cloud agents currently clone from source
control onto branches (<https://cursor.com/docs/cloud-agents>), and Cursor's strategy is to make
*Cursor's* agents better, not to be neutral infrastructure for Claude Code and Codex running in
somebody's terminal. A tool that serves all agents equally is something Cursor structurally will not
build.

**Anthropic — will build the primitives, not the product.** Claude Code's sandboxing is deliberately
*local* OS-level: Seatbelt on macOS, bubblewrap + socat + optional seccomp on Linux, with an
out-of-sandbox proxy enforcing a domain allowlist
(<https://code.claude.com/docs/en/sandboxing>). For hosted execution, Anthropic partnered rather
than built: Claude Managed Agents run the agent loop on Anthropic's platform while **Cloudflare**
provides VM and V8-isolate sandboxes, announced ~2026-05-19
(<https://blog.cloudflare.com/claude-managed-agents/>) — "decoupling the brain from the hands."
Anthropic's revealed preference is to own the model and the harness, ship hooks so others build the
rest, and rent execution from partners. The hook system's richness (`additionalContext`,
`updatedInput`, `FileChanged`, `WorktreeCreate`, `TaskCompleted`) reads as a deliberate extension
point. That is good news for Pandora's *existence* and bad news for its *moat*: the API is open to
everyone, including Crabbox.

**OpenAI — least likely.** Codex cloud is repo-and-branch shaped
(<https://learn.chatgpt.com/docs/cloud>); Codex CLI has hooks but their context-injection contract is
unconfirmed (<https://raw.githubusercontent.com/openai/codex/main/docs/config.md>). OpenAI's
incentive is model quality and inference revenue; a product whose explicit pitch is "use fewer
tokens" is not one they will prioritise.

**The uncomfortable version.** The moat is not technology. Morph has the forking, Crabbox has the
sync, Clash has the conflict detection, Anthropic has the delivery channel, and everything is MIT or
documented. The defensible asset is the **index**: a content-addressed, cross-worktree, cross-agent
memory of what has been validated and what failed and why, accumulated over a team's repo. That is
the only asset that gets better with use and that no incumbent can bootstrap from their side of the
git remote. Whatever else the plan says, it should be built to accumulate that index from day one.

---

## 12. Gaps in this research

- **Search budget exhausted mid-task**; later topics were reached by guessing URLs. Coder, DevPod,
  Okteto, mirrord, Tilt, Garden, Namespace devboxes and Runloop's session limits were not covered
  from primary sources.
- **codesandbox.io blocks automated fetches (HTTP 403)**; its "live VM cloning" claim is secondary
  only. **openai.com returns 403** too, so Codex's pass@1/pass@8 numbers are secondary.
- Codex CLI hook semantics were resolved via DeepWiki's source-grounded index rather than official
  docs; confirm against `codex-rs/hooks/src/schema.rs` directly before building on
  `updatedMCPToolOutput` behaviour.
- **No data found** on: agent turns wasted on flaky/pre-existing failures; whether agents actually
  act on hook-injected context; Morph Infinibranch GA status or per-branch pricing.
- **Terragon** (terragonlabs.com) shows only a shutdown notice; **CodeBeaver** (codebeaver.ai)
  returns 404 with a TLS mismatch and a June-2026 Wayback snapshot shows spam on the domain. Neither
  could be researched. Treat both as dead.
- Amp's "Orbs" may or may not accept an uploaded working tree rather than a git clone — the docs
  mention file sync/upload and `amp sync` back to the local checkout but do not state the initial
  state's provenance (<https://ampcode.com/docs/orbs>). **Worth a direct check: if Amp uploads a
  working tree, Sourcegraph is closer to Pandora than this report credits.**
- The Majgaonkar et al. trajectory-failure numbers (12–82% longer failure trajectories) are from a
  secondary citation and should be verified before use.
