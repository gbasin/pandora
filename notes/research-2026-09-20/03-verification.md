# Pandora research slice 03 — Continuous, compute-hungry verification techniques

**Question:** what continuous verification techniques (a) are elastic and killable, (b) produce a trustworthy signal about whether AI-written code and AI-written tests can be trusted, and (c) have evidence of practical value?

**Date of research:** 2026-09-20. Source dates noted inline. Claims without a URL are marked *(unverified)*.

**Target codebase shape assumed throughout:** TypeScript / Playwright / Postgres web monorepo.

---

## 0. The short answer

Only one technique in this space has a large-scale industrial deployment, a published developer-acceptance number, *and* published evidence that its signal correlates with real bugs: **diff-scoped mutation testing surfaced as a code-review comment**, as built by Google. Everything else is either (a) genuinely elastic but produces a signal that does not apply to TypeScript web code (coverage-guided fuzzing, DBMS logic fuzzing), (b) produces a great signal but is not elastic or not killable (deterministic simulation, record/replay), or (c) is a product category with strong marketing and thin published evidence (autonomous AI QA agents).

Three findings should shape the product more than anything else in this document:

1. **The industry's default test-acceptance criterion is nearly worthless.** Meta measured LLM-generated, coverage-optimised tests at a **2.4% mutant kill rate**, and found that **49% of genuinely valuable tests added no line coverage at all** ([ACH, FSE 2025](https://arxiv.org/abs/2501.12862)). "Passes and increases coverage" — the bar used by every AI test-generation product — does not measure whether a test can detect a fault.
2. **Free compute does not buy proportional bugs.** Böhme & Falk's empirical law: linearly more bugs requires exponentially more machines ([FSE 2020](https://mboehme.github.io/paper/FSE20.EmpiricalLaw.pdf)). Pandora's compute advantage must be spent on *breadth* — many small jobs across many PRs and repos — not endurance on one target. Every economic claim should be framed that way.
3. **Noise control, not compute, is the hard part.** Google needed 100+ arid-node rules and 200+ function-name families to move mutant usefulness from 15% to 89%, and Tricorder enforces a hard **<10% "not useful" rate** with a probation-and-shutdown mechanism for analyzers that exceed it. The suppression ruleset is an operating cost, not a design-time artifact.

The commercial gap is real but narrow: **QA Wolf sells browser verification at ~$9/browser-hour with humans absorbing flakes, while Replay QA sells autonomous exploration for $200/month flat.** There is no hosted mutation-testing compute service at all — which is either an unserved market or a graveyard (§9).

### 0.1 The claims Pandora can defend, and the ones it cannot

**Defensible, with numbers:**

| Claim | Evidence |
|---|---|
| Coverage is not verification | 100% coverage / **4% mutation score** observed ([MutGen, TSE 2025](https://arxiv.org/abs/2506.02954)); GPT-4o-generated tests **18.8%** mutation score ([TestGenEval](https://arxiv.org/html/2410.00752v2)); **40.2%** on decontaminated real functions ([ULT](https://arxiv.org/abs/2508.00408)); Meta's coverage-optimised LLM tests at **2.4%** ([ACH](https://arxiv.org/abs/2501.12862)) |
| AI-written JS tests often assert nothing meaningful | **~38.6%** of TestPilot's generated JS tests are "trivial" — assertions that don't depend on package behaviour ([TestPilot](https://ar5iv.labs.arxiv.org/html/2302.06527)) |
| The agent that writes the bug writes the test that passes it | Fault detection **25% → 14%** when tests are generated after faulty code vs independently ([arXiv:2607.05139](https://arxiv.org/abs/2607.05139)) |
| "Tests pass" accepts wrong patches about a third of the time | **31.08%** weak tests in SWE-bench ([SWE-bench+](https://arxiv.org/abs/2410.06992)); **29.6%** of plausible patches behave differently from ground truth ([arXiv:2503.15223](https://arxiv.org/abs/2503.15223)) |
| Independent generated tests measurably improve patch trust | Using generated tests as a filter **doubled SWE-Agent precision to 47.8%** ([SWT-Bench](https://arxiv.org/abs/2406.12952)) |
| AI adoption degrades delivery stability | **−7.2%** per 25% adoption increase (DORA 2024); still negative in DORA 2025; **30–39%** of developers distrust AI code |
| Reviewers are drowning | **518.7M PRs merged, +29% YoY**, 1M+ from GitHub's own agent in 5 months (Octoverse 2025); **66%** cite "almost right, but not quite" as top frustration (SO 2025) |
| Seeded faults change developer behaviour at scale | Google: 24,000+ developers, ~15M mutants, statistically significant increase in tests written ([ICSE 2021](https://arxiv.org/abs/2103.07189)) |

**Not defensible — do not use:**

- *"AI code is 40–45% vulnerable."* Lab benchmarks say that; repo-scale CodeQL scanning of 7,703 real AI-generated files says **87.9% have no CWE-mapped vulnerability, and TypeScript is the least-affected language at 2.5–7.1%** ([arXiv:2510.26103](https://arxiv.org/abs/2510.26103)). Security is a weak lead for this stack.
- *"We measure your defect-escape rate."* Synthetic-to-real F1 collapses **0.847 → 0.066** ([arXiv:2606.15689](https://arxiv.org/abs/2606.15689)), and mutation-score correlations vanish once test-suite size is controlled ([Papadakis ICSE 2018](https://coinse.github.io/publications/pdfs/Papadakis2018hi.pdf)). Any escape-rate number will be off by an order of magnitude.
- *"More compute finds more bugs."* Exponential cost law (§2.2).

---

## 1. Mutation testing at scale

### 1.1 Google — the reference implementation

Three papers, same team (Goran Petrović, Marko Ivanković, with Gordon Fraser and René Just):

- *State of Mutation Testing at Google*, ICSE-SEIP 2018 — <https://research.google/pubs/state-of-mutation-testing-at-google/>
- *Practical Mutation Testing at Scale: A View from Google*, IEEE TSE 2021 — <https://research.google/pubs/practical-mutation-testing-at-scale-a-view-from-google/>, full text at <https://homes.cs.washington.edu/~rjust/publ/practical_mutation_testing_tse_2021.pdf>
- *Does mutation testing improve testing practices?*, ICSE 2021 — <https://arxiv.org/abs/2103.07189>, full text at <https://homes.cs.washington.edu/~rjust/publ/mutation_testing_practices_icse_2021.pdf>

**Scale.** The Mutation Testing Service serves ">24,000 developers on more than 1,000 projects" in a ~2-billion-line monorepo running 150M+ tests daily (TSE 2021, §1). The ICSE 2021 study analysed "almost 15 million mutants."

**What made it tractable — four mechanisms, in order of importance:**

1. **Diff-scoped.** Mutants are generated only for lines touched by the changelist under review, and only for lines with statement coverage. This is the single largest reduction.
2. **At most one mutant per line.** Justified by the observation that "the vast majority of mutants for a given line share the same fate — either all or none of them survive" (TSE 2021, §2.2). If the one mutant on a line is killed, no further mutants are generated for that line.
3. **Arid-node suppression.** An AST node is *arid* if mutating it produces an unproductive mutant. A compound node is arid iff all children are arid; a hand-curated `expert` function flags simple nodes. They have **"more than one hundred rules for arid node detection"** and **"name suppression rules for more than 200 function families"** (e.g. anything whose function name starts with `log`). Categories: uncompilable, equivalent, unproductive-killable, redundant.
4. **Context-based probabilistic operator selection.** Operator choice is weighted by historical per-context productivity. This "improves the probability that a generated mutant survives by more than 40% and the probability that a generated mutant is productive by almost 50%" over random selection (TSE 2021, RQ4).

**The numbers that matter for Pandora:**

| Metric | Value | Source |
|---|---|---|
| Mutants per changelist, traditional mutagenesis | median **820** (25th/75th pct: 460 / 1734) | TSE 2021 RQ1 |
| Mutants per changelist, 1-per-line only | median **77** (25th/75th: 31 / 138) | TSE 2021 RQ1 |
| Mutants per changelist, arid + 1-per-line | median **7** | TSE 2021 RQ1 |
| Cap on *reported* mutants | ≤ **7 × number of files in the changelist** | TSE 2021 §2.3 |
| Mutants killed by the existing test suite | **87.5%** | TSE 2021 RQ2 |
| Developer-rated productive (of mutants with feedback) | **82%** aggregate; rose **80% → 89%** over time | TSE 2021 §5 |
| Unproductive rate before suppression work | **85%** of reported mutants | TSE 2021 §1 |
| Unproductive rate after suppression | **11%** | TSE 2021 §1 |
| High-priority bugs with a fault-coupled mutant on the bug-introducing change | **70%** (1043 of 1502) | ICSE 2021 RQ3 |

The 70% coupling number is the strongest published evidence that mutation signal maps onto real defects, and it was computed at eye-watering cost: "1502 bugs, almost 400 thousand mutants, and over 33 million test target executions" (ICSE 2021 §IV). Note the important caveat in the same section: each bug-introducing change *was already covered by existing tests* — i.e. coverage was present and still let the bug through. That is exactly the Pandora thesis applied to humans.

**Operators.** Only five, for 10 languages (**C++, Java, Go, Python, TypeScript, JavaScript, Dart, SQL, Common Lisp, Kotlin**): AOR, LCR, ROR, UOI, SBR, from Mothra. ABS was dropped because it "predominantly created unproductive mutants." SBR alone produces ~68% of all mutants but survives only 12.6% of the time; UOI survives 9.5%. **TypeScript and JavaScript are first-class in Google's deployment** — this is direct evidence the technique is not C++-only.

**Effect on behaviour.** ICSE 2021 RQ1/RQ2: "As exposure to mutation testing increases, developers tend to write more tests" and "tend to write stronger tests in response to mutants," both statistically significant (Wilcoxon, p<.001) and robust to normalisation for changelist size. Median changed test hunks: 1 for mutation findings vs 0 for coverage findings.

**Known failure modes.** Equivalent mutants (handled by suppression heuristics, not solved); unproductive-but-killable mutants (the dominant class — the mutant *can* be killed but writing that test is a waste, e.g. mutating a logging call or a memory reservation); redundant mutants; and the sheer cost of mutant execution. Their honest admission: "some heuristics are unsound" and "may suppress productive mutants," accepted as a trade for perceived usefulness.

### 1.2 Meta — ACH and TestGen-LLM

**TestGen-LLM** (*Automated Unit Test Improvement using Large Language Models at Meta*, FSE 2024, <https://arxiv.org/abs/2402.09171>): an "Assured LLMSE" filter pipeline — a generated test is kept only if it builds, passes reliably (repeated runs), and strictly increases coverage. Deployment numbers: **75% of test cases built, 57% passed reliably, 25% increased coverage**; it improved **11.5% of all classes** it was applied to in Instagram and Facebook test-a-thons, and **73% of recommendations were accepted** by engineers.

Read those numbers as a warning: on Meta's own tooling, **only a quarter of LLM-generated tests were worth anything at all**, and that is *after* the model was asked to extend existing human tests rather than write from scratch.

**ACH** (*Mutation-Guided LLM-based Test Generation at Meta*, FSE 2025, <https://arxiv.org/abs/2501.12862>; engineering blog <https://engineering.fb.com/2025/02/05/security/revolutionizing-software-testing-llm-powered-bug-catchers-meta-ach/>): inverts the pipeline. Instead of generating tests for coverage, it generates *faults of a specified class* (privacy faults, described in natural language) with an LLM, then generates tests that kill them.

- 10,795 Android Kotlin classes across 7 platforms → **31,677 mutants generated, 9,095 built and passed (29%)**, 4,660 judged non-equivalent (51% of those) → **571 tests**.
- **Equivalent-mutant detection by an LLM agent: precision 0.79 / recall 0.47 raw; 0.95 / 0.96 after trivial preprocessing** (strip comments, dedupe syntactically). This is the first credible answer to the equivalent-mutant problem and it is cheap.
- **25% of mutants that build and pass were trivially equivalent** (vs 10–15% for rule-based operators) — LLM mutant generation is *noisier* per-mutant but more targeted.
- Acceptance: **73% overall** (140/191 in test-a-thons); 90% in the initial 30-test trial; 86–94% on Messenger, 56% on WhatsApp. Only **36%** were rated definitely/possibly privacy-relevant — i.e. engineers accepted tests for generic quality reasons more often than for the stated purpose.
- **The headline finding for Pandora: 49% of ACH's mutation-killing tests added no line coverage at all.** "Had we used coverage as our sole test adequacy criterion, the platforms would thus have continued to be vulnerable to regressions." Compared against TestGen-LLM: TestGen-LLM covered 32% of classes with a **2.4% mutant kill rate**; ACH covered 5.3% of classes with a **15% kill rate**.

### 1.3 LLM-generated mutants as a technique

- *A Comprehensive Study on Large Language Models for Mutation Testing* (<https://arxiv.org/abs/2406.09843>, 2024): across 851 real Java bugs and 7 LLMs, LLM mutants achieved **87.98% fault detection vs 41.64% for rule-based** (+111% relative), and were "behaviorally closer to real bugs." Costs: **+26.6pp non-compilable, +10.1pp duplicated, +3.5pp equivalent**.
- *LLMorpheus* (<https://arxiv.org/abs/2404.09952>, 2024): LLM mutation for **JavaScript**, 13 packages, explicitly "producing mutants that resemble existing bugs that cannot be produced by StrykerJS." Cost/runtime figures are in the PDF but did not extract cleanly here *(unverified)*.

This is the strongest technical direction for Pandora: **LLM-generated, issue-targeted mutants on the diff, with an LLM equivalence pre-filter, executed on cheap compute.** Meta has shipped it; nobody sells it.

### 1.4 Open-source and commercial tooling

**StrykerJS** (<https://stryker-mutator.io/>) — the only serious option for TS/JS. Adoption is real: **`@stryker-mutator/core` had 1,812,284 downloads in the week of 2026-09-13 → 2026-09-19** (<https://api.npmjs.org/downloads/point/last-week/@stryker-mutator/core>), i.e. ~190× Jazzer.js and ~6% of fast-check. Mutation testing is installed in JS; the question is whether anyone reads the output.
- `--mutate "src/app.ts:1-11"` supports **file:line-range targeting**, so a git diff can be converted into a mutation scope manually. There is **no `--since` flag** (unlike PIT); diff scoping is DIY.
- **Incremental mode** (<https://stryker-mutator.io/docs/stryker-js/incremental/>) stores results in `reports/stryker-incremental.json` and reuses a mutant result when it was killed and the killing test is unchanged, or survived and no covering test changed. Documented example: "reuse 3731 mutant results, and only 234 mutants need to run."
- **Partial results are written on interruption** and resumed by the next incremental run — this is real checkpointability, and it is the single most Pandora-relevant feature in the OSS ecosystem.
- **Correctness caveats are explicit**: changes outside mutated/test files are not detected; env/dependency changes are not detected; static mutants have no coverage so test changes for them are invisible. Test-runner reporting fidelity varies: **Jest and CucumberJS = full (exact locations); Mocha/Vitest/Tap = per-file without location; Jasmine/Karma = names only; `command` runner = nothing.** A Vitest monorepo therefore gets a *degraded* incremental cache.
- `coverageAnalysis: "perTest"` + `ignoreStatic` is the main speed lever; `concurrency` defaults to n-1 cores. **No built-in distribution across machines** — sharding must be built.
- There is **no Playwright test-runner plugin**; Playwright would have to be driven via the `command` runner, which reports *nothing* back, disabling both per-test coverage and incremental reuse. See §8.

**PIT / pitest** (Java, <https://github.com/hcoles/pitest>, 1.9k stars) — bytecode mutation, JVM reuse, coverage-guided test selection. Incremental analysis via `--historyInputLocation`/`--historyOutputLocation` (<https://pitest.org/quickstart/incremental_analysis/>) with an unusually honest warning: three of its four reuse optimisations "introduce a degree of potential error" and the underlying assumption "is currently **unproven**."

**arcmutate** (<https://docs.arcmutate.com/>) — commercial PIT plugin suite: extended and extreme mutation operators, subsumption analysis, a **git plugin for incremental analysis tied to code changes**, and native GitHub/GitLab/Bitbucket/Azure DevOps PR integration. This is the closest commercial product to Google's design. **No public pricing** (arcmutate.com did not resolve at time of research).

**cargo-mutants** (Rust, <https://github.com/sourcefrog/cargo-mutants>) — architecturally the most Pandora-shaped OSS tool:
- **`--in-diff FILE`** restricts mutants to lines in a diff — native PR scoping.
- **`--shard k/n`** distributes mutants across n independent invocations **with no runtime coordination** — embarrassingly parallel by construction, ideal for spot fleets.
- **`--baseline=skip`** lets a separate job establish baseline correctness once, so shards don't each redo it.
- **`--iterate`** skips previously caught/unviable mutants — a checkpoint.
- Outcomes: caught / missed / **unviable** (didn't compile — inconclusive) / timeout. Timeouts derived automatically from baseline build+test time.
- No published bug-discovery statistics.

**Hosted mutation-testing services:** the Stryker Dashboard (<https://dashboard.stryker-mutator.io/>) is a score-reporting dashboard, not a compute service. **I found no hosted, compute-as-a-service mutation testing product** — the category is empty apart from arcmutate's on-prem plugins. *(This is a gap, but note §9 on why the gap may exist.)*

**Test-generation products that validate only by coverage:** Qodo Cover / Cover-Agent (<https://github.com/qodo-ai/qodo-cover>, 5.6k stars) validates generated tests by requiring that they pass *and* that coverage increases — and the repo was **archived as unmaintained on 2025-06-15**. Given ACH's finding that 49% of valuable tests add no coverage, coverage-gated generation is structurally limited.

### 1.5 Cost-reduction techniques, ranked by how much they actually save

1. Diff scoping (orders of magnitude) — Google, cargo-mutants `--in-diff`.
2. Coverage-guided mutant elimination: never mutate an uncovered line (free, and it is *definitionally* correct).
3. One-mutant-per-line sampling (~10× at Google).
4. Arid-node / heuristic suppression (~another 10× at Google, plus the noise reduction).
5. Per-test coverage → run only the tests that reach the mutant (Stryker `perTest`, PIT).
6. Incremental caches — real but fragile; every implementation documents soundness caveats.
7. Predictive mutation testing (ML models predicting whether a mutant would be killed, without running it) — exists in the literature but I found no industrial deployment; Google explicitly chose *selection* over *prediction*. Treat as research-only *(unverified as deployed anywhere)*.

---

## 2. Continuous fuzzing and property-based testing

### 2.1 OSS-Fuzz / ClusterFuzz — the architecture to copy

<https://google.github.io/clusterfuzz/> · <https://google.github.io/clusterfuzz/architecture/> · <https://google.github.io/oss-fuzz/architecture/>

**The single most important design fact for Pandora:** ClusterFuzz splits bots into **preemptible bots that only run the `fuzz` task** and **non-preemptible bots that run all task types** — `progression` (is the crash still reproducible?), `regression` (bisect the introducing commit range), `minimize`, `corpus_pruning`, `analyze`. Google's instance runs on ~30,000 VMs; task distribution is via Pub/Sub.

> **Preempt the generation. Never preempt the verification.**

**Checkpoint shape:** the corpus directory in GCS (`gs://[project]-corpus.clusterfuzz-external.appspot.com/libFuzzer/[fuzzer]`), with daily zipped backups (<https://google.github.io/oss-fuzz/advanced-topics/corpora/>). A killed fuzz task loses only inputs generated since the last sync. State is an append-mostly blob store, not a live process.

**Unit of work is small:** fuzzing machines are **single-core, max 2.5 GB RAM per target**; builders are 32 CPU / 28.8 GB (<https://google.github.io/oss-fuzz/faq/>). Perfect spot-fleet shape. GCP Spot VMs are up to 91% off with 0–120s preemption notice and no SLA (<https://docs.cloud.google.com/compute/docs/instances/spot>).

**Dedup / triage:** crashes dedupe on a "crash state" signature derived from the stacktrace; regression ranges are commit intervals; corpus pruning "removes unnecessary inputs while maintaining the same code coverage" (<https://google.github.io/clusterfuzz/reference/glossary/>). Bugs are auto-filed, fixes auto-verified, issues auto-closed. Disclosure at 90 days or fix, whichever is earlier, +14-day grace (<https://google.github.io/oss-fuzz/getting-started/bug-disclosure-guidelines/>). Known gap: **timeouts and OOMs are not deduped** — one bug per target only.

**Adoption:** "over 13,000 vulnerabilities and 50,000 bugs across 1,000 projects" (OSS-Fuzz README, ~May 2025, <https://github.com/google/oss-fuzz>); ClusterFuzz docs claim 25,000+ bugs internally and 36,000+ across 550+ OSS projects as of May 2022.

### 2.2 The hard ceiling on "free compute finds more bugs"

Böhme & Falk, *Fuzzing: On the Exponential Cost of Vulnerability Discovery*, ESEC/FSE 2020 (<https://mboehme.github.io/paper/FSE20.EmpiricalLaw.pdf>), from four CPU-years over ~300 programs:

> "finding linearly more bugs in the same time requires exponentially more machines."

Worked example from the paper: if bug discovery saturates at 25,000 machines, **100× more machines (2.5M) finds five new critical bugs**; another 100× (250M) finds five or fewer more. Re-covering *known* code gets exponentially cheaper with more compute; covering *new* code only gets linearly faster.

**This is a direct constraint on Pandora's pitch.** The defensible claim is *breadth* — many independent cheap jobs across many PRs, repos and properties — not *depth*, i.e. 100× compute on one target. Sell parallel width, not endurance.

### 2.3 ClusterFuzzLite — the CI-shaped template

<https://google.github.io/clusterfuzzlite/> · <https://google.github.io/clusterfuzzlite/running-clusterfuzzlite/github-actions/>

Default budgets: **PR fuzzing 600s (10 min)**; **batch fuzzing 3600s on a `0 0/6 * * *` cron**; corpus pruning and coverage **daily at midnight**. Corpora live in a *separate git repo* under `/corpus/<target>/`, or fall back to GitHub Actions artifacts. PR signal is thin — the crashing input is an Actions artifact you download.

That budget model (short on PR, long in background, persisted corpus) is directly reusable. The weak spots — corpus-in-a-git-repo, and feedback as an artifact rather than a review comment — are product wedges.

### 2.4 LLM-generated harnesses and AI patching

**OSS-Fuzz-Gen** (<https://github.com/google/oss-fuzz-gen>): 1,300+ benchmarks across 297 projects, 13 LLMs; valid targets for 160 C/C++ projects; **max line-coverage increase 29% over existing human-written harnesses** (top case `phmap`, +205.75% relative); **30 newly reported bugs/vulns including CVE-2024-9143 in OpenSSL**. Google writeup: <https://security.googleblog.com/2024/11/leveling-up-fuzzing-finding-more.html> (2024-11-20).

**CodeMender + OSS-Fuzz** (<https://blog.google/security/from-finding-to-fixing-reducing-maintainer-burden-with-automated-patches/>, 2026-07-29): an agent takes the crash plus source context, explores hypotheses in parallel, and validates that the patch compiles and doesn't regress functional tests. Currently **limited to C/C++ memory-safety findings**, human-reviewed in beta, **no acceptance-rate or cost numbers published**. Academic precedent (<https://arxiv.org/abs/2411.03346>, 2024-11) found patches with high CodeBLEU similarity still fail the exploit input — **only the reproducer is a valid oracle.**

### 2.5 Commercial fuzzing

- **Mayhem / ForAllSecure** (<https://www.mayhem.security/>): symbolic execution + fuzzing, "0 false positives" via proof-of-vulnerability; listed customers include US DoD, Cloudflare, Roblox, Rivian. Site states Mayhem Security was **acquired by Bugcrowd** *(date/terms unverified)*. **No public pricing** — `/pricing` 404s.
- **Code Intelligence (CI Fuzz, CI Spark, Jazzer.js)**: `code-intelligence.com` **failed DNS resolution** during this research; company status **unverified**. Jazzer.js itself is still on GitHub.
- **HypoFuzz** (<https://hypofuzz.com/>): coverage-guided continuous backend for existing Hypothesis property tests, by Hypothesis's own maintainers. Free non-commercial, paid commercial. Its pitch is *literally Pandora's*: "You give HypoFuzz CPU time, and HypoFuzz gives you bugs," scaling across cores and worker nodes using "idle compute." **This is the closest shipping product to the Pandora concept, and it is a one-language niche tool.** Read that both ways: the idea is validated, and the market has not rewarded it.

### 2.6 JS/TS fuzzing — the oracle problem is the whole problem

OSS-Fuzz supports JS via Jazzer.js, which operates on JS source and handles TypeScript — **but only the `none` sanitizer is supported; ASan/UBSan are not available** (<https://google.github.io/oss-fuzz/getting-started/new-project-guide/javascript-lang/>). Without memory safety, "a bug" must be defined by detectors. Jazzer.js ships six: command injection, path traversal, prototype pollution, RCE (`eval`/`Function`), SSRF, plus custom hooks (<https://github.com/CodeIntelligenceTesting/jazzer.js/blob/main/docs/bug-detectors.md>). Otherwise the oracle collapses to "uncaught exception or timeout" — and in application TypeScript, throwing on bad input is usually *correct behaviour*. Enormous noise.

Adoption confirms it. Weekly npm downloads, week of 2026-09-13:

| Package | Weekly downloads |
|---|---|
| `fast-check` | **29,978,769** |
| `@jazzer.js/core` | **9,645** |
| `jsfuzz` | **3,657** |

(<https://api.npmjs.org/downloads/point/last-week/fast-check>, `/@jazzer.js/core`, `/jsfuzz>`.) GitHub stars: fast-check 5.1k vs Jazzer.js 356.

**Do not build Pandora on JS coverage-guided fuzzing.**

### 2.7 Property-based testing — the realistic vehicle, weakly elastic

**fast-check** (<https://fast-check.dev/docs/introduction/track-record/>) documents real finds: 6+ bugs in Jest (including `toStrictEqual`/`toEqual` asymmetry), 6+ in javascript-algorithms, 2+ in eemeli/yaml, plus js-yaml, query-string, left-pad (Unicode), javascript-stringify (`-0`), node-jsonwebtoken (prototype poisoning), and ~17 other repos. It supports integrated shrinking, replay seeds, configurable `numRuns`, and biased generation.

**Hypothesis:** MacIver & Hatfield-Dodds, JOSS 4(43), 2019-11-21 (<https://joss.theoj.org/papers/10.21105/joss.01891>).

**The elasticity problem is real and must be stated honestly.** Vanilla PBT samples i.i.d.; example #1,000,000 comes from the same distribution as example #1,000 and almost always retreads covered code. That is exactly why HypoFuzz exists — coverage guidance gives extra CPU somewhere to go. **No published data quantifying marginal bug yield vs `numRuns` for fast-check was found** *(unverified)*. Combined with Böhme's law: extra compute buys breadth (more properties, more modules, more PRs), not depth.

**Shrinking is the underrated asset.** A shrunk counterexample plus a replay seed is a self-contained, deterministic, human-checkable artifact — the exact opposite of a screenshot diff.

### 2.8 Postgres/SQL and API-layer fuzzing

- **SQLancer** (<https://github.com/sqlancer/sqlancer>): five logic-bug oracles (TLP, NoREC, PQS, DQP, CODDTest) across 19 DBMSs including PostgreSQL. Author's documented bug list is SQLite-dominated (132 fixed bugs, last entries 2019, <https://www.manuelrigger.at/dbms-bugs/>).
- **SQLsmith** (<https://github.com/anse1/sqlsmith>): 118 bugs since 2015 in PostgreSQL/SQLite3/MonetDB; supports "hundreds of sqlsmith instances" logging to a central Postgres — genuinely checkpoint-free and preemptible.
- **Blunt truth: neither finds bugs in your application.** They test the DBMS engine. A TypeScript app on Postgres gets those fixes for free and gains nothing from running them.
- **RESTler** (<https://www.microsoft.com/en-us/research/publication/restler-stateful-rest-api-fuzzing/>, ICSE 2019): infers producer-consumer dependencies from OpenAPI; **28 confirmed-and-fixed bugs in GitLab** plus several in Azure/Office365. Repo: <https://github.com/microsoft/restler-fuzzer> (2.9k stars).
- **Schemathesis** (<https://github.com/schemathesis/schemathesis>, 3.6k stars, MIT): Hypothesis-based OpenAPI/GraphQL testing — 500s, schema violations, validation bypass, stateful workflows, crash reproduction.
- Zhang & Arcuri, *Open Problems in Fuzzing RESTful APIs* (<https://arxiv.org/abs/2205.05325>): 7 fuzzers on 19 APIs, "clear limitations," notably that black-box fuzzers lack coverage analysis.

**This is the one fuzzing family that plausibly applies to the target stack**: Schemathesis/RESTler against an ephemeral per-PR API instance, seeded from the repo's OpenAPI schema. Main noise source is spec drift.

---

## 3. Deterministic simulation and autonomous testing

### 3.1 Antithesis — the best signal in the field, and the most dangerous competitor

<https://antithesis.com/> · docs <https://antithesis.com/docs/>

A custom **deterministic hypervisor** runs your whole containerised system on a single simulated CPU core, snapshots and rewinds execution, injects clock/scheduling/network/storage faults, and autonomously searches the state space. Founded Jan 2018 by Will Wilson and Dave Scherer out of the FoundationDB team. **$47M seed Feb 2024; $105M Series A Dec 2025 led by Jane Street** — who are also a named customer (<https://antithesis.com/company/about/>).

Named customers: Jane Street, MongoDB, Confluent, Ethereum Foundation, Filecoin, Turso, Synadia, etcd/Kubernetes, Tigris, ParadeDB, Mysten Labs. Best concrete case study: a MongoDB WiredTiger rollback data-loss bug in a **≤10ms window**, found by branching-checkpoint probability analysis then 100ms-interval core dumps, producing a reproducer and fix (WT-9500) — <https://antithesis.com/blog/mongo_bug/> (2024-04-22).

Homepage claims (**vendor-stated, unaudited**): "75+ severe bugs found that other testing missed," "40x faster change verification," "6 hours of Antithesis testing equals 100 engineers' worth of coverage."

**Pricing: nothing published.** The pricing page has no tiers or rates, only "book a demo," plus "We often transact via AWS Marketplace Private Offers, so purchasing Antithesis usually draws down your organization's committed spend" (<https://antithesis.com/pricing/>). Docs specify no vCPU-hours, durations or parallelism. Enterprise sales motion. The open-source angle is a giveaway program (<https://antithesis.com/blog/osgp2024/>, 2024-02-28) plus $186,000 in cash donations to Nix/FreeBSD maintainers, with **no disclosure of compute value or project count** (<https://antithesis.com/blog/oss_pledge/>, 2024-09-16).

**Repo requirements — heavy.** x86-64 Linux containers orchestrated by Docker Compose or Kubernetes; Postgres/Kafka/Redis run as real containers; **a hermetic environment with no internet access at all**, so everything must be baked into images (<https://antithesis.com/docs/getting_started/>). You write test templates that drive the system and assert properties.

**TypeScript fit is better than expected, and it matters.** There is a JavaScript SDK, and Antithesis **auto-instruments Node.js via source transformation after upload — no source or build changes** — handling webpack/esbuild/Rollup bundles via source maps. TypeScript must be pre-transpiled. **Browser-side JS is explicitly not instrumentable** (<https://antithesis.com/docs/reference/sdk/javascript_sdk/>). So: Node backend + Postgres, yes; the Playwright/browser tier, no.

**Signal delivery:** every test emails a **triage report** — findings, environment, utilisation, per-property pass/fail, with on-demand causality analysis for any failure (<https://antithesis.com/docs/reports/triage/>). Note this is *email + report*, not a PR comment: by Tricorder's rules (§8), that is the weak channel.

**Compute shape:** massively parallel, single core per simulation, with snapshot/rewind as the core primitive — **architecturally a perfect preemptible workload**. But it runs on their fleet and is sold as a service.

### 3.2 FoundationDB and TigerBeetle — the operational patterns worth stealing

**FoundationDB** (<https://apple.github.io/foundationdb/testing.html>): Flow, an actor-based C++ dialect, compiles to both production code and a single-threaded deterministic whole-cluster simulation. Fault injection covers connection failures, machine slowdowns, shutdowns/reboots, and "swizzle-clogging" (disconnect a random node set, reconnect in random order). They run **tens of thousands of simulations nightly**, estimate **~1 trillion CPU-hours of simulation**, at roughly a 10:1 real-to-simulated time factor. *(The frequently-cited Kyle Kingsbury/Jepsen remark that FDB's own testing exceeded what Jepsen could add could not be verified against a primary URL this session — **unverified**.)*

**TigerBeetle VOPR** ("Viewstamped Operation Power Ranger"): simulates a whole cluster single-threaded with injected packet loss/delay/partitions, storage read/write faults, replica crash/restart/pause and upgrade scenarios. Invoked as `./zig/zig build vopr -- <seed>`; **any failure is reproducible from its seed**. The old "VOPR Hub" is superseded by the **CFO (Continuous Fuzzing Orchestrator)**, which runs fuzzers 24/7 on a reported **1024 CPU cores**, uses a weighted fair scheduler, maintains a pool of concurrent child processes, **tracks failing seeds and prioritises them (preferring the ones that fail fastest)**, kills runs at a ~30-minute timeout, has a `budget` for periodic code refresh, and uploads to a DevHub dashboard. Writeups at <https://tigerbeetle.com/blog/> — "Protocol-Aware Deterministic Simulation Testing" (2026-08-20), "A Tale Of Four Fuzzers" (2025-11-28), "Fuzzer Blind Spots (Meet Jepsen!)" (2025-06-06), "Swarm Testing Data Structures" (2025-04-23). No published seeds/hour or bug-rate numbers.

> **The CFO is the cleanest preemptible-work design in this whole report: unit of work = one seed; killable at any instant; cost of loss = one seed; resume = re-run the seed; prioritise seeds that fail fastest.** Whatever Pandora runs, make its unit of work a seed.

### 3.3 Deterministic simulation for Node/TypeScript — candidly, no

madsim, tokio-rs/turmoil, awslabs/shuttle and tokio-rs/loom are **all Rust-only and none are usable from Node/TypeScript**. madsim mocks all I/O with a seeded runtime and can kill/restart simulated nodes; turmoil runs many hosts on one thread with a seeded network; shuttle does randomised/PCT scheduling with replayable schedule strings; loom does exhaustive C11-memory-model permutation (intractable beyond tiny tests).

For Node the ceiling is **fake timers, not deterministic scheduling**. `node:test` MockTimers (Stability 2 — Stable) fakes `setTimeout`/`setInterval`/`setImmediate`/`Date`, advanced with `tick()`/`runAll()`, and **explicitly does not control the event loop** (<https://nodejs.org/api/test.html>); real I/O ordering, microtask interleaving with syscalls, and Postgres latency stay nondeterministic. Sinon fake timers are the same category. Temporal achieves replay determinism by *constraining workflow code* (no `Date.now()`, no raw randomness, no I/O outside Activities) and replaying an event history (<https://docs.temporal.io/workflows>) — a programming-model tax, not a capability you can bolt onto an existing monorepo. Cloudflare Workers deterministic replay: **unverified**.

**The only credible DST path for a TS/Postgres stack today is buying Antithesis.**

### 3.4 Record/replay and "no written tests"

**Meticulous.ai** (<https://www.meticulous.ai/>): a script tag on dev/staging/preview records real user sessions and which code branches each flow exercises; on a PR it replays those sessions against the new build and visual-diffs. **Backend nondeterminism is handled by mocking responses from the original recordings by default** — side-effect-free, no test accounts or seed data. Claims >100 orgs including Dropbox, Brex, Notion, ElevenLabs, Wealthsimple, LaunchDarkly; "under 120 seconds at scale"; a "deterministic scheduling engine" that eliminates flakes. **Pricing not published** (`/pricing` 404s; `docs.meticulous.ai` failed TLS handshake) — **unverified**.

Two structural critiques that matter for agent-written code: (1) replaying *recorded* sessions cannot exercise code paths no human has walked — and an agent's new feature is by definition unwalked; (2) **mocked backends mean it cannot catch server-side or Postgres-level regressions at all**, which is precisely the bug class agent-written code produces. Compute is short, parallel and re-runnable (good preemptible fit) but the signal is narrow.

**Replay.io — not dead, pivoted to AI QA, and the closest product competitor.** The site now sells **Replay QA**: autonomous exploration of a web app, journey discovery, Playwright test generation, bug reports with time-travel recordings, runtime breakdowns (function calls, DOM mutations, network, state), and root-cause analysis with confidence scores and suggested fixes. Positioning is explicit: *"AI wrote the app. Replay QA finds what broke."* Continuous mode connects to a GitHub repo and runs on main updates and PRs. **Pricing: free 25 credits/month, $20/mo individual, $200/mo team** (<https://www.replay.io/>). GitHub org active through Sep 2026 (<https://github.com/replayio>).

**QA Wolf** (<https://www.qawolf.com/pricing>): self-serve Platform at **1¢ per AI credit + 15¢ per runner-minute, no per-seat fees**; Coverage-as-a-Service at custom pricing. Claims **"80%+ automated coverage within weeks"** and **"guaranteed zero flakes — you will never be alerted to a test flake,"** achieved by **humans triaging every failure within 24 hours**. Tests are plain Playwright and exportable; one customer cited at ~300 tests in 11 minutes with pre-warmed browsers (<https://www.qawolf.com/how-it-works>). **The "zero flakes" SLA is labour arbitrage, not technology** — and 15¢/runner-minute is ~$9/hour of browser compute, roughly 20–50× spot cost. That gap is the clearest commercial wedge in this report.

### 3.5 Visual regression — unit economics and why noise is the product

| Tool | Free tier | Paid | Per-snapshot overage |
|---|---|---|---|
| [Chromatic](https://www.chromatic.com/pricing) | 5,000 snapshots | Starter $179/mo (35k), Pro $399/mo (85k) | **$0.008** |
| [Argos](https://argos-ci.com/pricing) | 5,000/mo | Pro $100/mo (35k) | **$0.004** ($0.0015 Storybook) |
| [Lost Pixel](https://www.lost-pixel.com/pricing) | 7,000/mo | $100 / $250 / $670 per mo | $0.006 → $0.004 |
| [Applitools](https://applitools.com/pricing/) | trial only | **Starter $667/mo** (100k component *or* 1k page checkpoints) | n/a |
| Percy | — | **pricing not retrieved — unverified** | — |

Lost Pixel's pricing page states it is **being acquired by Figma and "sunsetting the product"** (date not stated).

Failure modes are structural, not vendor-specific. Playwright's own docs warn rendering varies by **host OS, OS version, settings, hardware, power source (battery vs adapter), and headless mode**, so baselines are per-platform (`chromium-darwin.png`) and must be regenerated in an identical environment; tuning is `maxDiffPixels`/`threshold` over pixelmatch (<https://playwright.dev/docs/test-snapshots>). Chromatic's TurboSnap and Argos's "failed builds don't count" exist precisely because snapshot counts explode.

**The real cost is not $0.004 — it is the human approval queue.** Every vendor's differentiation in this category is noise suppression. That is the same lesson as Google's arid nodes, arrived at independently.

### 3.6 Autonomous / AI test-generation agents, 2024–2026

- **Momentic** — credits, no seats: free 2,000 credits/mo (~200 runs); PAYG **$125/mo for 10k credits (~1,000 runs)**; overage $0.01875/credit; ~10 credits per run (<https://momentic.ai/pricing>). No named customers on the pricing page.
- **Octomind** — `octomind.dev` **failed DNS resolution** this session; status **unverified**. GitHub org alive with 13 MIT repos (`debugtopus`, `octomind-mcp`, Playwright batch generation) updated through Jul 2026 (<https://github.com/OctoMind-dev>).
- **Ranger** — `tryranger.com` and `ranger.ai` both **redirect to GoDaddy parked-domain sale pages**. Dead or rebranded; **unverified** which.
- **Spur** (<https://www.spurtest.com/>) — intent-level agents ("add a medium black item to cart and check out"); customers Vuori, Bombas, Living Spaces, Wondr Health; claims "80% fewer false positives," "20X faster release"; annual plans by test-run volume, **no public prices**. The customer list is entirely e-commerce, i.e. shallow flows.
- **mabl** (<https://www.mabl.com/pricing>) — "500 credits/month" starting allocation, unlimited local/CI runs, 14-day trial, quote-based.
- **Checkly** (<https://www.checklyhq.com/pricing/>) — the only transparent compute pricing in the category: Hobby free (1k browser runs, 10k API runs/mo), Starter $24/mo, Team $64/mo; **browser overage $6.50/1k runs ≈ $0.0065/run**, API $2.60/10k. That is ≈ **$0.78/browser-hour** — a useful floor-price benchmark.
- **Autify** (<https://autify.com/pricing>) — Aximo "Autonomous AI Tester" (free 2,000 credits; Core $99–120/mo; Team $450–550/mo, credits priced by underlying model) and Nexus, Playwright-based, **from $3,600/yr** with code export.
- **Testim** — absorbed into Tricentis; no public prices (<https://www.testim.io/pricing/>).

**Read of the category:** heavily funded, price-transparent at the low end, and converging on "AI explores your app and writes Playwright tests." None of them publishes a *fault-detection* number — they publish coverage, speed and flake claims. That is the same weak-proxy problem as §4.1, sold as a product.

---

## 4. Evidence on AI-written code and test quality

### 4.1 AI-written tests are measurably weak

- **Meta TestGen-LLM** (<https://arxiv.org/abs/2402.09171>): 75% built, 57% passed reliably, **25% increased coverage**. Three quarters of generated tests were worthless even against the weak coverage bar.
- **Meta ACH** (<https://arxiv.org/abs/2501.12862>): TestGen-LLM's tests had a **2.4% mutant kill rate**. This is the most damning published number in the whole space — coverage-optimised LLM tests barely detect injected faults. Mutation-guided generation raised it to 15%.
- **49% of ACH's valuable tests added no line coverage** — coverage cannot even rank them.
- **TestPilot** (Schäfer et al., <https://arxiv.org/abs/2302.06527>) is the best JS-specific data point: 25 npm packages, 1,684 API functions, **median 70.2% statement / 52.8% branch coverage** with gpt-3.5-turbo. The abstract reports **no mutation score** — the literature's default is still to measure the weak proxy.
- Product evidence: **Qodo Cover** gated generated tests on "passes + increases coverage" and was **archived unmaintained 2025-06-15** (<https://github.com/qodo-ai/qodo-cover>).

**Synthesis:** the industry's dominant acceptance criterion for AI-written tests (does it pass and raise coverage?) has a published ~2.4% correlation with fault detection. That is the clearest gap Pandora can sell into.

### 4.2 Mutants as a proxy for real faults

Just et al., *Are mutants a valid substitute for real faults in software testing?*, FSE 2014 (<https://homes.cs.washington.edu/~rjust/publ/mutants_real_faults_fse_2014.pdf>): **357 real faults**, 5 Java programs, 321 kLOC, 230,000 mutants.
- **73% of real faults are coupled to mutants.**
- **10%** would require a new or stronger mutation operator.
- **17% are not coupled to mutants at all** — an inherent ceiling.
- Mutant detection correlates with real-fault detection **more strongly than statement coverage does**, and the correlation holds independently of coverage.

Google's ICSE 2021 replication at industrial scale found 70% coupling — remarkably consistent across a decade and two codebases.

**But read this together with §5.3.** Papadakis et al. (ICSE 2018) show that mutation-score *correlations* with real-fault detection collapse from 0.35–0.75 to ~0.05–0.20 once test-suite size is controlled for. The reconciliation: an individual mutant is a good stand-in for a real fault (coupling), while an aggregate mutation *score* is a poor ranking of test suites. **Design implication: show mutants, never scores.**

### 4.3 Benchmarks and weak tests accepting wrong patches

**SWE-bench+** (<https://arxiv.org/abs/2410.06992>, 2024-10): auditing SWE-Agent+GPT-4's "resolved" instances found **32.67% solution leakage** (the fix was in the issue text or comments) and **31.08% weak tests** (the test suite could not distinguish the patch from a wrong one). Filtering both, **resolution rate fell from 12.47% to 3.97%**. Over 94% of issues predated model knowledge cutoffs. Similar problems in SWE-bench Lite and Verified.

That is the thesis of Pandora, stated as a benchmark result: **when the only gate is "the existing tests pass," roughly a third of accepted patches are accepted for the wrong reason.**

*(SWE-bench Verified's own construction numbers — how many of the original 500 tasks had underspecified issues or overly-specific tests — could not be retrieved; openai.com returned 403. Marked unverified.)*

### 4.4 Mutation scores of LLM-generated tests — the quantitative core

This is the most important evidence table in the report. Every row is a mutation score for machine-written tests.

| Study | Subject | Coverage | **Mutation score** |
|---|---|---|---|
| [TestGenEval](https://arxiv.org/html/2410.00752v2) (Meta, Oct 2024) — 68,647 tests, 1,210 file pairs, 11 Python repos | GPT-4o | 35.2% | **18.8%** |
| same | Llama-3.1-405B | 35.0% | **16.4%** |
| same | human gold suites | median 60.4% | — |
| [TestForge](https://arxiv.org/abs/2503.14713) (Mar 2025) on TestGenEval | agentic | — | **33.8%** |
| [ULT / UnLeakedTestbench](https://arxiv.org/abs/2508.00408) (Aug 2025) — 3,909 decontaminated high-complexity real Python functions | LLM average | 45.1% stmt / 30.2% branch | **40.2%** |
| same models on the older, leaked TestEval | — | 92.2% / 82.0% | 49.7% |
| [MutGen](https://arxiv.org/abs/2506.02954) (TSE, Jun 2025, 204 subjects) | observed worst case | **100%** | **4%** |
| Meta TestGen-LLM, as measured by ACH | coverage-optimised LLM tests | — | **2.4%** |
| Meta ACH | mutation-guided LLM tests | — | **15%** |

MutGen states it in one line: **"some test suites achieve 100% coverage but only 4% mutation score."** Note also the ULT result — the same models drop from 49.7% to 40.2% mutation score (and roughly halve on accuracy) when you remove benchmark contamination and use realistic code. Most published numbers are optimistic.

**Counterpoint, for honesty:** [AgoneTest](https://arxiv.org/abs/2511.20403) (ASE 2025) finds LLM Java tests "can match or exceed human-written tests in coverage and defect detection" — but only over the subset that compiles, and publishes no numbers.

### 4.5 Assertion quality, test smells, and the contamination mechanism

- **TestPilot's own data** ([ar5iv full text](https://ar5iv.labs.arxiv.org/html/2302.06527)): only a **median 61.4% of generated tests per package are "non-trivial"** — i.e. contain assertions that actually depend on package behaviour. **~38.6% of generated JS tests are effectively assertion-free.** This is the JS/TS-specific number to quote.
- [Quality Assessment of Python Tests Generated by LLMs](https://arxiv.org/abs/2506.14297) (Jun 2025; GPT-4o, Amazon Q, Llama 3.3): most generated suites contained at least one error or test smell; **assertion errors were 64% of all errors**; Lack of Cohesion in 41%.
- RL-from-automatic-feedback work reports LLMs produce undesirable test smells **up to 37% of the time** ([arXiv:2412.14308](https://arxiv.org/abs/2412.14308)).
- **VibeCheck** ([arXiv:2609.05978](https://arxiv.org/abs/2609.05978), Sep 2026) — 15 student repos in **Python and JS/TS**, agents Kiro / Antigravity / Cursor on Claude Sonnet 4.5 — names the failure mode the "**execution-adequacy gap**": tests are "often runnable but frequently lack strong assertions and meaningful behavioral coverage… weak assertions and missing edge cases occur more often than blocking failures." Qualitative, small student sample.
- Over-mocking in agent-written app tests: [Tangent](https://arxiv.org/abs/2608.08413) (Aug 2026) finds they "frequently rely on simplistic inputs, heavy mocking, and shallow validation." **Not quantified — weakly supported.**
- **Java baseline:** Tang et al., [ChatGPT vs SBST](https://ar5iv.labs.arxiv.org/html/2307.00588) (Jul 2023; 207 classes, 212 Defects4J bugs): only **69.6% of ChatGPT tests compiled and ran** unaided; coverage **55.4% vs EvoSuite 74.2%**; bug detection **21% vs 26%**; 9.8% of tests had Scary/Scariest SpotBugs issues. Authors: "the assertions generated by ChatGPT are not reliable." ChatTester ([arXiv:2305.04207](https://arxiv.org/abs/2305.04207)) improves base ChatGPT by **+34.3% compilable tests and +18.7% correct assertions** — the size of that improvement is the size of the baseline defect. MuTAP ([arXiv:2308.16557](https://arxiv.org/abs/2308.16557)) reaches **93.57% mutation score**, but only by feeding surviving mutants back into the prompt — which is exactly Pandora's loop.

**The single most important finding for Pandora.** Konstantinou, Tambon, Papadakis, [*On the risk of coding before testing*](https://arxiv.org/abs/2607.05139) (Jul 2026): **generating tests *after* faulty LLM code cuts fault detection to 14%, versus 25% when tests are generated independently** — "faults in generated code are systematically replicated in associated test artifacts." A second paper ([arXiv:2607.22883](https://arxiv.org/abs/2607.22883)) reports the same 25% → 14% drop from "misguided tests" that assert the buggy behaviour. *(Caveat: neither abstract names the benchmark or models — verify before quoting externally.)*

> **This is the mechanism, stated quantitatively: the agent that wrote the bug also wrote the test that passes it, and that halves your fault detection.** Independent, adversarial verification — generated by a *different* process with *different* information — is the direct countermeasure.

### 4.6 Field data on AI-written code

- **DORA 2024** ([Google Cloud announcement](https://cloud.google.com/blog/products/devops-sre/announcing-the-2024-dora-report)) — for a **25% increase in AI adoption**:

  | Outcome | Estimated change |
  |---|---|
  | Documentation quality | +7.5% |
  | Code quality | +3.4% |
  | Code review speed | +3.1% |
  | **Delivery throughput** | **−1.5%** |
  | **Delivery stability** | **−7.2%** |

  Trust: **39% reported little to no trust in AI-generated code**; only 24% trust it "a lot" or "a great deal" ([dora.dev/research/2024/ai-preview/](https://dora.dev/research/2024/ai-preview/)). The [2024 errata](https://dora.dev/research/2024/errata/) adjusted labels and wording but not these figures.

- **DORA 2025** ([announcement](https://cloud.google.com/blog/products/ai-machine-learning/announcing-the-2025-dora-report), 2025-09-23; ~5,000 respondents): **90% use AI at work**; >80% believe it raised productivity; **30% report little or no trust in AI-generated code** (down from 39%). Direction reversed on throughput — "a positive relationship … on both software delivery throughput and product performance" — but AI adoption **"does continue to have a negative relationship with software delivery stability."** **Exact 2025 coefficients are unverified** (the report PDF did not parse; the landing pages carry no numbers). The [2025 errata](https://dora.dev/research/2025/errata/) confirms the report reframed the outcome as *instability*.

  **Caveat to state whenever citing DORA:** self-selected, self-reported, cross-sectional; different respondent set each year; raw data not shared ([dora.dev/faq/](https://dora.dev/faq/)). Correlational. The durable point is that across two years and ~5k respondents each, **stability is the only metric AI consistently degrades.**

- **GitClear 2025** (<https://www.gitclear.com/ai_assistant_code_quality_2025_research>): **211 million changed lines**, Jan 2020 – Dec 2024. Refactored ("changed") lines fell from **25% (2021) to under 10% (2024)**; cloned code rose **8.3% → 12.3%**; copy/paste exceeded "moved" lines for the first time. GitClear sells a code-quality product and defines its own metrics.

- **METR RCT** (<https://metr.org/blog/2025-07-10-early-2025-ai-experienced-os-dev-study/>, 2025-07-10): 16 experienced OSS developers, 246 issues. With AI tools they took **19% longer**; they predicted a **24% speedup** and still believed afterwards they had been sped up 20%. Small n, narrow population, authors disclaim generalisation.

### 4.7 Review burden — the demand-side number

- **GitHub Octoverse 2025** ([github.blog](https://github.blog/news-insights/octoverse/octoverse-a-new-developer-joins-github-every-second-as-ai-leads-typescript-to-1/), 2025-10-28): **518.7M PRs merged, +29% YoY**; 986M commits (+25.1%); **1M+ PRs authored by GitHub's own coding agent between May and Sep 2025**.
- **DX** (400+ companies): median PR size **44 → 72 lines** in 11 months; self-reported AI-authored share **27.4% (Q1 2026) → 51.9% (Q2 2026)** ([Jun 2026](https://getdx.com/blog/ai-authored-code-has-nearly-doubled/)). Then: PR throughput **+37%** over four quarters, but **change confidence −6.1%** Q1→Q2 2026 and DXI 67→65 ([Sep 2026](https://getdx.com/blog/the-quality-paradox-of-ai-generated-code/)). >90% adoption eliminated the control group, so these are before/after trends, not causal.
- **Stack Overflow 2025** ([survey.stackoverflow.co/2025/ai](https://survey.stackoverflow.co/2025/ai)): **66% cite "AI solutions that are almost right, but not quite"** as their top frustration; **45.2% say debugging AI-generated code is more time-consuming**. Trust inverted year over year: 2025 = 32.7% trust vs **45.7% distrust**; 2024 = 43% trust vs 30% distrust. Favourability 72% → 59.7%.
- Reviewer cost baseline: Google measured **~60 minutes average active shepherding time per change** ([research.google](https://research.google/blog/resolving-code-review-comments-with-ml/), 2023).

### 4.8 Security — cite the delta, not one side

- **Veracode 2025 GenAI Code Security Report** ([veracode.com](https://www.veracode.com/blog/genai-code-security-report/), 2025-07-30): **45% of AI-generated samples failed security tests** across 100+ LLMs; Java 72%, C# 45%, **JavaScript 43%**, Python 38%; XSS defended in only 14% of relevant samples; larger/newer models did **not** improve. Vendor-funded, synthetic adversarial prompts, task count undisclosed.
- **Pearce et al., "Asleep at the Keyboard"** ([arXiv:2108.09293](https://arxiv.org/abs/2108.09293), IEEE S&P 2022): 89 scenarios, 1,689 programs, **~40% vulnerable**. Codex-era historical baseline.
- **Perry et al., Stanford** ([arXiv:2211.03622](https://arxiv.org/abs/2211.03622), CCS 2023): 47 participants. Secure solutions AI vs control: SQL injection in JS **24% vs 43%**; signing 3% vs 21%. AI users were **more likely to believe their code was secure**. n=47, student-heavy.
- **Agent self-refinement makes it worse:** **+37.6% critical vulnerabilities after five "improvement" rounds** ([arXiv:2506.11022](https://arxiv.org/abs/2506.11022)) — directly relevant to agent loops.
- **But repo-scale measurement is far milder:** 7,703 real AI-generated GitHub files scanned with CodeQL — **87.9% had no CWE-mapped vulnerability**; **TypeScript only 2.5–7.1%**, JS 8.7–9.0%, Python 16.2–18.5% ([arXiv:2510.26103](https://arxiv.org/abs/2510.26103), Oct 2025).

**Verdict: do not lead with security.** Lab benchmarks (40–45%) overstate in-the-wild rates (~12% of files) by roughly 4×, and **TypeScript is the least-affected language measured**. Security is a weak argument for this specific stack.

---

## 5. Seeded bugs, bebugging, and measuring defect-escape rate

### 5.1 Provenance

"Bebugging" dates to Weinberg (1970); the canonical citation is Harlan Mills, *On the Statistical Validation of Computer Programs*, IBM FSC-72-6015 (1972) — **the primary is not available online; cited-but-unverified** ([pointer](https://en.wikipedia.org/wiki/Bebugging)). The capture–recapture lineage for estimating residual defects from seeded-vs-found ratios runs through Eick et al., ICSE 1992 (<https://doi.org/10.1145/143062.143090>) and Briand et al., IEEE TSE 2000 (<https://doi.org/10.1109/32.852741>).

### 5.2 The one production precedent is Google's, and it is *not* framed as a score

Google's mutation testing service **is** bebugging in production — diff-based probabilistic seeded faults surfaced *in code review*, never as a defect-escape percentage. ICSE-SEIP 2018 reports **6,000 engineers, >14,000 authors, ~30% of diffs** (<https://research.google/pubs/state-of-mutation-testing-at-google/>), scaling to 24,000+ developers and 1,000+ projects (<https://arxiv.org/abs/2102.11378>), ~15M mutants analysed, with measurable behaviour change (<https://arxiv.org/abs/2103.07189>). **The framing lesson is the finding: nobody at Google gets told their escape rate; individual authors get told about one specific undetected change to the line they just wrote.**

### 5.3 The validity problem with any seeded-bug metric you might build

Three independent results say a "we catch X% of seeded bugs" number will be badly optimistic:

1. **Synthetic-to-real collapse.** ["Bigger Isn't Always Better"](https://arxiv.org/abs/2606.15689): 100 synthetic mutation-injected bugs + 50 real bug-fix PRs, 5 frontier models. **Best F1 = 0.847 on synthetic vs 0.066 on real PRs — 92% degradation.** F1 falls from 0.657 (<10-line diffs) to 0.043 (>150-line diffs). *(Note an ID/date discrepancy flagged during research — verify before quoting.)*
2. **Hand-seeded faults are a biased instrument.** Andrews et al., ICSE 2005 (<https://doi.org/10.1145/1062455.1062530>) found hand-seeded faults are **much harder to detect than real faults**, and that generated mutants behave more like real faults than hand-seeded ones do.
3. **Mutation scores are confounded by test-suite size.** Papadakis et al., ICSE 2018 ([PDF](https://coinse.github.io/publications/pdfs/Papadakis2018hi.pdf)), 301 real faults (CoREBench 70 + Defects4J 231): raw correlations of 0.35–0.75 collapse to **~0.05–0.20 when test-suite size is controlled** — "all correlations… are weak when controlling for test suite size." But the same paper adds: "fault detection improves significantly when test suites reach the highest mutation score levels."

> **Synthesis: mutants are good guidance and bad measurement.** Use them to tell a developer (or an agent) *"write a test that catches this specific change."* Do not use them to compute a number anyone is graded on. This reconciles Just et al.'s 73% coupling with Papadakis's weak correlation — coupling says a mutant *can* stand in for a real fault; the size confound says an aggregate *score* does not rank suites.

### 5.4 Related context

- **Code review catches few defects to begin with.** Bacchelli & Bird, ICSE 2013 (Microsoft CodeFlow, 570 comments): **defect-finding was only 14% of review comments**, ranked 4th of 9 motivations; reviews "mostly address 'micro' level and superficial concerns" ([PDF](https://sback.it/publications/icse2013.pdf)). This is the human baseline an automated verifier is competing against — and it is low.
- **Chaos engineering's analogue.** [principlesofchaos.org](https://principlesofchaos.org/) says "automate experiments and run them continuously" but **does not contain the phrase "continuous verification"** — that framing is Rosenthal/Jones/Verica *(unverified)*. Netflix's ChAP routes **1% of users to a canary and 1% to a baseline**, with auto-generated and auto-scored experiments ([arXiv:1905.04648](https://arxiv.org/abs/1905.04648)) — the closest production analogue to "inject a fault, measure whether the system notices."
- **Gremlin** ([pricing](https://www.gremlin.com/pricing)) is the nearest commercial framing ("closed-loop verification"), but for reliability, not correctness.
- **Nobody sells defect-escape-rate measurement.** No vendor found selling canary-bug measurement or escape-rate reporting as a product.

### 5.5 APR overfitting — the historical proof that weak tests accept wrong patches

- **Qi et al., ISSTA 2015** ([PDF](https://people.csail.mit.edu/rinard/paper/issta15.pdf)): GenProg reported fixing 55/105 bugs; **correct patches: 2/105**. RSRepair 2/24, AE 3/105. **104 of 110 plausible GenProg patches were functionally equivalent to deleting functionality.** A deletion-only control (Kali) matched all three tools.
- **Smith et al., ESEC/FSE 2015** ([PDF](https://people.cs.umass.edu/~brun/pubs/pubs/Smith15fse.pdf)), IntroClass, 998 programs: **the median GenProg patch passes 100% of training tests but only 75.0% of held-out tests** (mean 68.7%). For programs already passing >75% of the training suite, both tools **break more held-out tests than they fix** (p<0.001).
- **LLM era, same disease:** ["Are 'Solved Issues' in SWE-bench Really Solved Correctly?"](https://arxiv.org/abs/2503.15223) (Mar 2025): **7.8% of test-passing patches fail the developers' own suite; 29.6% of plausible patches behave differently from ground truth**, inflating reported resolution rates by ~6.2 pp. With SWE-bench+'s independent finding of **31.08% weak tests**, two separate audits converge on roughly a third of accepted patches being accepted for the wrong reason.
- **Generated tests as a patch filter actually works:** **SWT-Bench** ([arXiv:2406.12952](https://arxiv.org/abs/2406.12952), NeurIPS 2024; 2,294 instances) found that using generated tests as a filter **doubled SWE-Agent's precision to 47.8%**. **TDD-Bench Verified** ([arXiv:2412.02883](https://arxiv.org/abs/2412.02883), IBM, 449 issues): Auto-TDD + GPT-4o reaches 23.6% fail-to-pass. Current [swtbench.com](https://swtbench.com) leaders sit near **87%** — but that measures *reproducing a known issue*, not *bounding an unknown patch*, and coverage adequacy of non-fail-to-pass model tests falls **below 0.60** vs developer tests' 0.94–0.99.

> **SWT-Bench's doubling of precision is the cleanest published evidence that independently-generated verification improves the trustworthiness of agent output.** It is the closest thing to a direct proof-of-value for Pandora in the literature.

---

## 6. Dependency-upgrade automation that validates by running tests

This family is worth studying not because it is compute-hungry, but because it is the **only widely-adopted example of a bot that opens changes and lets a test suite adjudicate them** — exactly Pandora's trust model, at scale, with published confidence data.

**Dependabot** (<https://docs.github.com/en/code-security/dependabot/dependabot-version-updates/about-dependabot-version-updates>): opens PRs on a schedule; **a 3-day default cooldown** before a new release is considered; **it does not run your tests — it relies on your CI**, and "repository maintainers are responsible for reviewing test results and merging." It pauses updates if maintainers stop engaging. So the trust signal is entirely your own test suite, which is precisely the thing Pandora questions.

**Renovate** (<https://docs.renovatebot.com/configuration-options/>): the knobs that matter are CI-cost knobs — `prConcurrentLimit` (default **10**), `prHourlyLimit` (default 0 = unlimited), `commitHourlyLimit` (limits branch creation *and* rebases), `automerge` (default false), `automergeType` (`pr` / `branch` / `pr-comment`), `platformAutomerge` (default true when automerge is on). The existence and prominence of these knobs is evidence that **CI cost, not correctness, is the binding constraint on automated change volume** — a direct tailwind for a cheap-compute execution service.

**Merge Confidence** (<https://docs.renovatebot.com/merge-confidence/>) is the most interesting artifact in this section: Mend aggregates outcomes from millions of Renovate PRs since 2017 into four badges — **Age, Adoption, Passing, Confidence**. "Passing" is "the percentage of updates which have passing tests for this package," weighted toward organisations, private repos, and "projects with high test reliability." Confidence levels: Low (likely breaking) / Neutral (insufficient data) / High / Very High. Free with the Mend Renovate App; self-hosted via the `mergeConfidence:all-badges` preset; **automated merge workflows driven by it are restricted to paying Mend customers**. Languages: Go, JS, Java, Python, .NET, PHP, Ruby.

**The design lesson:** Mend built a *cross-repo, aggregated* trust signal out of test runs that were going to happen anyway, and monetised the automation layer on top while giving the signal away. A fleet running verification across many customers' repos is in the same position. Note the weighting caveat — they explicitly re-weight toward "projects with high test reliability," i.e. they had to solve a flakiness-confound problem to make the number mean anything.

**Agent-based upgraders** (Devin/Cursor-style "upgrade this dependency" agents): marketing exists; **I found no published, methodologically credible success-rate data** *(unverified)*. Treat claims in this category as unsubstantiated until a vendor publishes a denominator.

---

## 7. Ranked list

Score = (value of signal for agent-written code) × (fit for free preemptible compute) × (low noise). Each dimension 1–5.

| Rank | Technique | Signal | Preempt. fit | Low noise | Product | Verdict |
|---|---|---|---|---|---|---|
| **1** | **Adversarial re-testing of the agent's own diff** — mutate the implementation the agent just wrote; check whether the tests the agent just wrote catch it | 5 | 5 | 4 | **100** | **Build this.** Tiny scope (the diff), perfectly shardable, and the output is one sentence: *"your new test does not detect this change to your new code."* Directly attacks the 25%→14% contamination effect (§4.5) and matches SWT-Bench's filter framing (§5.5). |
| **2** | **Diff-scoped mutation testing on changed lines, surfaced in review** (StrykerJS engine + Google's suppression discipline) | 5 | 4 | 4 | **80** | The generalisation of #1 to all code, not just agent diffs. Only technique with industrial proof, first-class TS support, and a published fault-coupling number (70%). |
| **3** | **Flake detection by massive repetition** — run the existing suite N×100 under varied seeds, ordering, clock, concurrency | 3 | 5 | 5 | **75** | Underrated and the cheapest credible v1. Perfectly elastic, perfectly killable, near-zero false positives (a test that fails 3/1000 times *is* flaky), and it is the precondition for every other signal being believable. Mend had to solve the same confound to make Merge Confidence mean anything (§6). |
| **4** | **LLM-generated, issue-targeted mutants + LLM equivalence pre-filter** (Meta ACH pattern) | 5 | 4 | 3 | **60** | Build second. Meta proved it; nobody sells it; the 0.95/0.96 equivalence filter makes it affordable. Raises kill rate 2.4% → 15% over coverage-guided generation. |
| **5** | **Dependency-upgrade validation at fleet scale** (Merge Confidence–style aggregate) | 3 | 5 | 4 | **60** | Proven demand and proven monetisation, but the signal is about *dependencies*, not agent code. Adjacent revenue, not the core thesis. |
| **6** | **Deterministic simulation (Antithesis-style)** | 5 | 2 | 5 | **50** | Best signal in the field, worst fit: buy-only, opaque enterprise pricing, hermetic-container requirement, no browser-tier coverage. Steal TigerBeetle's CFO *pattern* (seed = unit of work), not the technique. §3.1, §3.3. |
| **7** | **LLM-generated property tests in fast-check, run broad** | 4 | 4 | 3 | **48** | Strong second tier. Shrinking yields a deterministic minimal reproducer — the ideal artifact. Elastic in *breadth* only (§2.7). |
| **8** | **Schemathesis / RESTler against an ephemeral per-PR API instance** | 4 | 5 | 2 | **40** | Good compute fit, real bugs, but spec-drift noise is the killer. Requires an OpenAPI schema the repo actually maintains. |
| **9** | **Seeded-bug canaries measuring the pipeline's escape rate** | 4 | 5 | 1 | **20** | **Demoted after research.** Fine as *guidance* (that's #1/#2); invalid as *measurement* — synthetic→real F1 collapses 0.847→0.066 and hand-seeded faults are unrepresentative. Do not ship an escape-rate number. §5.3. |
| 10 | **SQLancer / SQLsmith against Postgres** | 1 | 5 | 3 | **15** | Do not build. Tests Postgres, not your app. §2.8. |
| 11 | **Visual/replay regression (Meticulous, Chromatic, Percy, Argos)** | 3 | 2 | 2 | **12** | Do not build. Replay is not killable mid-run, diffs are noisy, and Meticulous's mocked backends structurally cannot see Postgres-layer regressions. §3.4–3.5. |
| 12 | **Coverage-guided fuzzing of TS application code (Jazzer.js)** | 2 | 5 | 1 | **10** | Do not build. The oracle collapses to "it threw." §2.6. |

**If forced to pick two for a v1:** #3 (flake detection by repetition) as the trust-establishing loss-leader, and #1 (mutate the agent's diff, check the agent's tests) as the differentiated signal. #3 is what makes #1 believable — a surviving mutant reported on top of a flaky suite is noise, and you only get one chance to be trusted.

### 7.1 Price benchmarks to beat

The incumbents' published unit prices set the ceiling Pandora's cheap-compute story must undercut visibly:

| Reference point | Published price | Implied hourly |
|---|---|---|
| QA Wolf runner-minute | **$0.15/min** (<https://www.qawolf.com/pricing>) | **≈ $9.00/browser-hour** |
| Checkly browser check | **$0.0065/run** (<https://www.checklyhq.com/pricing/>) | ≈ $0.78/browser-hour |
| Momentic run | ≈ **$0.19/run** (10 credits @ $0.01875) | — |
| Visual snapshot | **$0.004–0.008** (Argos / Chromatic) | — |
| Replay QA team plan | **$200/mo** flat (<https://www.replay.io/>) | — |
| GCP Spot VM | up to **91% off** on-demand (<https://docs.cloud.google.com/compute/docs/instances/spot>) | — |

QA Wolf at ~$9/browser-hour against spot compute at a small fraction of on-demand is a 20–50× gross-margin gap, and their differentiator is explicitly *humans absorbing flakes within 24 hours*. That is the arbitrage. But note the counterweight: Replay QA sells autonomous exploration + Playwright generation + root-cause analysis for **$200/month flat**, which caps what anyone can charge for "AI finds bugs in your web app" as a standalone line item.

---

## 8. Design lessons for surfacing results without creating noise

Google's static-analysis and mutation-testing work is the only body of evidence here with hard numbers on *developer tolerance*. These are the rules.

**From Tricorder** (*Tricorder: Building a Program Analysis Ecosystem*, ICSE 2015, <https://static.googleusercontent.com/media/research.google.com/en//pubs/archive/43322.pdf>; CACM version <https://cacm.acm.org/research/lessons-from-building-static-analysis-tools-at-google/>):

1. **Define the false positive from the developer's side.** Google's term is *effective false positive*: "any report that they did not want to see," including correct findings the developer judges not worth acting on. A technically-valid surviving mutant on a logging line is an effective false positive.
2. **Hard threshold: effective false positive rate < 10% for anything shown at code review.** Analyzers above it go on *probation* and are turned off if they don't improve. Measured continuously as `NotUseful / (NotUseful + PleaseFix + ApplyFix)` on a dashboard the analyzer's owner is accountable for.
3. **The actual achieved rate was ~5%** across all Tricorder analyzers, "under 4%" excluding probationary ones. **5%, not 10%, is the real bar.** Google's mutation service landed at 11–18% unproductive (82–89% productive), i.e. it sat at the edge of tolerance even after 100+ suppression rules.
4. **Latency budget: 5–10 minutes**, "ideally much less," because "the results must be available before the review is over" (mean review time >1 hour). A background verification result that arrives after merge is worth roughly zero.
5. **Show results only on changed lines, by default.** Relevance to the review at hand is what keeps the finding actionable.
6. **Two buttons, one click each: "Please Fix" and "Not useful."** Reviewer clicks "Please Fix" → becomes a request to the author. Author clicks "Not useful" → feeds the suppression pipeline. Findings are *not* blocking unless a human reviewer marks them mandatory.
7. **Peer accountability beats gating.** Results at review time mean "the reviewer will see if the author chose to ignore analysis results." Non-blocking + visible-to-reviewer is a stronger mechanism than a red X.
8. **Feed the button back into suppression.** Google's arid-node `expert` function is "based on developer feedback on reported 'Not useful'" clicks. The suppression ruleset is an *output of production*, not a design-time artifact — 100+ node rules and 200+ function-name families accumulated over years. **Budget for this. It is the moat and it is the cost.**
9. **Dashboards don't work.** Google explicitly notes that nightly results shown in code search "most developers do not use," and that they reserve that channel for high-false-positive analyses with a dedicated cleanup team. *A mutation-score dashboard is a graveyard. A single comment on the diff is a product.*
10. **Volume caps, not just quality gates.** Google caps reported mutants at **7 × number of files in the changelist**, explicitly "so that the cognitive overhead of understanding all reported mutants is not too high, which might otherwise cause developers to stop using mutation testing altogether."

**From ClusterFuzz:**

11. **Split preemptible generation from non-preemptible verification.** Never report a finding that hasn't been deterministically re-verified on stable compute. This is both a correctness rule and a noise rule.
12. **The reproducer is the artifact, not the explanation.** OSS-Fuzz ships a crashing input; fast-check ships a shrunk counterexample and a seed; a mutation service should ship the exact one-line diff plus the command that shows the tests still pass. LLM prose about why something might be wrong is not a signal.
13. **Verify the fix and close the loop automatically.** OSS-Fuzz's `progression` task re-checks and auto-closes. A finding that stays open after it's fixed trains people to ignore the channel.

**From the AI-test literature:**

14. **Generate the verification independently of the code, from different information.** Tests generated *after* faulty LLM code detect 14% of faults; tests generated independently detect 25% ([arXiv:2607.05139](https://arxiv.org/abs/2607.05139)). If Pandora's verifier sees the agent's implementation and reasoning, it inherits the agent's blind spot. Feed it the *spec*, the *diff*, and the *tests* — and have it try to break them, not explain them.

15. **Use generated tests as a filter, not an addition.** SWT-Bench's result — generated tests as a patch filter **doubled precision to 47.8%** ([arXiv:2406.12952](https://arxiv.org/abs/2406.12952)) — says the value is in *rejecting* bad patches, not in growing the suite. A Pandora finding should read "this change would not have been caught," not "here are 40 new tests."

**Pandora-specific corollaries:**

16. **Report at most one finding per PR by default.** Google gets to 7 because their diffs are small and their suppression is mature. Start at one; earn the right to more.
17. **Rank by "would this have caught a real bug," not by mutation score.** Never show an aggregate score. Scores invite goal-seeking; a single concrete survived mutant invites a test.
18. **Gate every finding behind a flake check.** Re-run the killing/non-killing decision N times. A non-deterministic test makes every mutation verdict meaningless, and shipping one bad verdict costs more trust than ten good ones earn.
19. **Instrument your own effective-false-positive rate from day one, per rule, and put an owner on it.** Ship the probation mechanism before you ship the second rule.

---

## 9. Candid: what is research-only or unlikely to work here

**Structurally wrong for a TypeScript/Playwright/Postgres monorepo:**

- **Coverage-guided fuzzing of application TypeScript.** No sanitizers are available for JS in OSS-Fuzz; the oracle degrades to "it threw an exception," which in a validating web app is usually correct behaviour. Adoption confirms the verdict: `@jazzer.js/core` gets ~9.6k weekly npm downloads against fast-check's ~30M.
- **SQLancer / SQLsmith.** They find bugs in Postgres, not in code that uses Postgres. You get those fixes by upgrading.
- **Building your own deterministic simulation for Node/TypeScript.** It requires controlling every source of nondeterminism — scheduler, clock, network, filesystem, RNG. `node:test` MockTimers explicitly does not control the event loop; every Rust DST library (madsim, turmoil, shuttle, loom) is Rust-only; Temporal buys determinism by taxing the programming model. **Buying Antithesis is the only real path, and even then it auto-instruments your Node services but cannot touch browser-side JS** (<https://antithesis.com/docs/reference/sdk/javascript_sdk/>), while demanding a hermetic, internet-free Docker Compose/K8s packaging of the entire monorepo including Postgres. Pandora should steal TigerBeetle's CFO *operational pattern* (seed as unit of work) without pretending it can deliver DST semantics.
- **Mutation testing driven through Playwright.** StrykerJS has no Playwright runner; Playwright would run via the `command` runner, which reports **nothing** back — no per-test coverage, no incremental reuse, and a full browser+server boot per mutant. The economics are hopeless. Mutation testing belongs on the unit/integration tier (Vitest/Jest), and even there Vitest only gives per-file (not per-test-location) reporting, degrading the incremental cache.

**Wrong framings, even though the underlying technique is sound:**

- **Selling "defect escape rate."** The instrument does not survive contact with reality: synthetic-injected-bug detection F1 of 0.847 collapses to 0.066 on real bug-fix PRs, and degrades from 0.657 to 0.043 as diff size grows from <10 to >150 lines ([arXiv:2606.15689](https://arxiv.org/abs/2606.15689)). Hand-seeded faults are also known to be unrepresentative (Andrews et al., ICSE 2005). Any escape-rate dashboard you ship will be wrong by roughly an order of magnitude, in the flattering direction.
- **Reporting a mutation score.** Mutation-score correlations with real-fault detection are weak once test-suite size is controlled ([Papadakis, ICSE 2018](https://coinse.github.io/publications/pdfs/Papadakis2018hi.pdf)). Google never shows one. Show the mutant.
- **Leading with security.** TypeScript is the least-affected language in repo-scale measurement (2.5–7.1% of AI-generated files with a CWE-mapped finding, vs 16.2–18.5% for Python — [arXiv:2510.26103](https://arxiv.org/abs/2510.26103)), and the 40–45% lab figures do not transfer. A Playwright/Postgres TS shop will not buy on security.

**Plausible but not proven:**

- **Predictive mutation testing** (ML predicting mutant kill without execution). Literature exists; I found no industrial deployment, and Google chose selection over prediction. Treat as research.
- **Agent-based dependency upgraders.** No published denominators.
- **LLM-as-judge for "is this test tautological."** Attractive, cheap, and completely unvalidated as a gating signal *(unverified — I found no study measuring its precision/recall against human labels)*. ACH's equivalence detector is the closest validated analogue and it needed trivial preprocessing to get from 0.79/0.47 to 0.95/0.96, which suggests careful scoping matters more than model choice.

**Economically constrained regardless of correctness:**

- **"Free compute finds proportionally more bugs" is false** for search-based techniques. Böhme & Falk: linear bug growth requires exponential machines. Pandora's compute advantage should be spent on *breadth across PRs and repos*, not endurance on one target.
- **The hosted-mutation-testing category is empty**, and that is data. arcmutate sells plugins, not compute; Stryker's dashboard sells nothing; Qodo Cover was archived; HypoFuzz is a niche tool by the Hypothesis maintainers. Either the market is genuinely unserved because nobody could make the noise economics work (Google needed 300+ hand-written suppression rules), or the buyer does not exist. **The AI-agent context is the thing that might have changed — for the first time, the entity that would act on a surviving mutant is a machine that will do it at 3am for free.** That is the bet, and it is not yet evidenced by anything published.
- **Price ceiling.** Replay QA already sells autonomous exploration + Playwright generation + root-cause analysis at **$200/month for a team** (<https://www.replay.io/>). Any Pandora pricing built on "AI finds bugs in your web app" as a standalone line item has to live under that number or sell a different unit (verification-hours, or per-agent-PR).

---

## 10. Open questions and things this research could not verify

| Item | Status |
|---|---|
| SWE-bench Verified's own construction numbers (how many of the original 500 tasks had underspecified issues or overly-specific tests) | **Unverified** — openai.com returned 403, swebench.com and the repo wiki do not state it |
| LLMorpheus dollar/token/wall-clock cost per project | **Unverified** — PDF did not extract |
| Antithesis pricing at any granularity | **Unpublished** — demo-only, AWS Marketplace private offers |
| Meticulous.ai pricing | **Unpublished** — `/pricing` 404s, docs site TLS failure |
| Percy pricing | **Not retrieved** |
| Kyle Kingsbury / Jepsen quote about FoundationDB | **Unverified** — no primary URL located |
| Code Intelligence (CI Fuzz / Jazzer.js vendor) company status | **Unverified** — DNS failure |
| Octomind company status | **Unverified** — DNS failure; GitHub org active |
| Ranger (tryranger.com, ranger.ai) | Domains **parked for sale**; dead or rebranded, unclear which |
| Mayhem/ForAllSecure acquisition by Bugcrowd; DARPA CGC history | **Unverified** from primary sources |
| Marginal bug yield vs `numRuns` for fast-check | **No published data found** |
| Precision/recall of LLM-as-judge for "is this test tautological" | **No study found** |
| Predictive mutation testing in industrial deployment | **None found** |
| DORA 2025 exact regression coefficients for AI adoption vs stability | **Unverified** — report PDF >10 MB, did not parse; landing pages carry no numbers |
| Harlan Mills, *On the Statistical Validation of Computer Programs* (IBM FSC-72-6015, 1972) | **Cited but unverified** — primary not available online |
| arXiv IDs 2606.15689, 2607.05139, 2607.22883, 2608.08413, 2609.05978 | Abstracts read, but **at least one ID/date discrepancy was flagged**; verify each before external quotation |
| "Continuous verification" as chaos-engineering terminology | **Unverified** — the phrase is not in principlesofchaos.org; attributed to Rosenthal/Jones/Verica |
| DX and Graphite figures | **Vendor-published**, self-reported estimates, no control group after >90% adoption |
| Agent-based dependency upgraders' success rates | **No published denominators** |
| Cloudflare Workers deterministic replay as a shipped feature | **Unverified** |

**Method caveat:** the session's WebSearch budget (200 calls) was exhausted partway through; the later portions of this research relied on direct WebFetch of canonical URLs and the DeepWiki MCP over GitHub repos. Several "not found" results above may reflect that constraint rather than genuine absence.
