# Repo-owned configuration contract — draft for judgment, not for merge

Status: draft, 2026-09-21. Evaluates whether Pandora's repository knowledge can
move into a file the target repository owns, and whether the result is pleasant
enough to justify rewriting the welded adapters.

Working proof: `experiments/repo-config/` (loader, classifier, CI-fact importer,
CLI, two example configurations, 97 unittest cases including a 77-case parity
table against the shipped `experiments/routing/commands.py` and a trimmed
verbatim copy of acme's real `ci.yml` under `fixtures/`).

Read §10 first if you have read this document before: it is the second pass,
and it changes the recommendation in §9.4.

---

## 1. The problem this is answering

Roughly 60% of Pandora is repo-agnostic plumbing: snapshot and manifest,
transport, admission and scheduling, container lifecycle, evidence
authentication, retention, publication fences, wait/recovery. The other 40% is
Acme. It is welded in nine places:

| Where | What is Acme-specific |
| --- | --- |
| `experiments/routing/commands.py` | Literal argv `if`/`elif` for every accepted spelling |
| `experiments/warm/validation_request.py` | A second copy of the suite table |
| `experiments/warm/workflow_options.py` | `APPS = ('web','desk')`, `apps/<app>/dist` |
| `experiments/routing/journey_updates.py` | `packages/scenarios/fixtures/...` paths |
| `experiments/warm/journey.py` | Postgres credentials, three pinned service digests, `-w packages/scenarios` |
| `experiments/warm/worker_config.py` | `demand()` hardcodes which workflows need db/pool/proxy |
| `experiments/warm/validation-stack.mjs` | Textual surgery on Acme's `startInstance({...})` |
| `experiments/warm/validation.mjs` | Turbo-argv rewriting, planner import, reporter sniffing |
| `experiments/warm/suite.mjs`, `journey.mjs` | Absolute imports of Acme `.ts` internals |
| `experiments/surface/Dockerfile` | Node 24 / pnpm 12.3.4 / Playwright 1.62.1 pins |

Every one of those is knowledge the repository already has, expressed twice, in
the wrong repository, released on Pandora's cadence rather than the repo's.

The lever: Acme's `.github/workflows/ci.yml` already states the same facts in
GitHub-Actions shape — job list, runner size, a journeys matrix
(`JOURNEY_SHARD: '1/4' … '4/4'`), surface shards
(`pnpm --filter @acme/<app> test:e2e --shard=${{ matrix.shard }}`), service
containers with env and healthchecks, and artifact paths. The proposed config is
that document, minus GitHub, plus the three things CI does not need: an argv
boundary for agents, resource requests in Pandora's admission units, and an
output-writeback allowlist.

---

## 2. Format: TOML

TOML, one file, `<repo-root>/pandora.toml`.

Reasons, in order of weight:

1. **`tomllib` is stdlib from Python 3.11.** No dependency, no vendored parser,
   no YAML-in-a-shell-script. Pandora's rule is stdlib-only and that survives.
2. **Comments.** This file will carry "why this digest", "why this suite rejects
   selectors", "why `--update` is fenced". JSON cannot hold that, and the
   knowledge is exactly the kind that rots when it has nowhere to live.
3. **Humans edit it more often than machines generate it.** The audience is a
   repo maintainer adding a command, not a program.
4. `[[jobs]]` array-of-tables reads well for what is genuinely a list of jobs,
   and inline tables keep the common one-line cases (`forms`, `run`) to one line.

Cost, stated plainly: **Pandora's floor rises from Python 3.10 to 3.11.** README
currently says 3.10+. Either bump the client requirement or vendor a ~120-line
TOML subset parser. Recommend bumping; 3.11 shipped in 2022 and the worker
already runs a newer interpreter.

Rejected alternatives: JSON (no comments, and the trailing-comma/diff ergonomics
of a 300-line file are bad); YAML (not stdlib); "reuse `ci.yml` directly"
(couples the contract to GitHub's schema, and CI's jobs are not the agent's argv
boundary — see §8).

---

## 3. Schema

The normalized shape is what `experiments/repo-config/config.py` validates. Every
table is closed: an unknown key is refused by name with the allowed set printed.
A configuration that loads is one the classifier can execute with no further
repository knowledge.

> **Stale below, in three places.** §3.4's `params`/`flags` mini-language and
> §3.5's `cpu_millis`/`memory_mib` no longer exist, and `CI = ""` in §3.9 is now
> `unset`. §10.7 states what replaced each, and why. The rest of §3 still holds.

### 3.1 Identity and matching

```toml
version = 1

[repo]
name = "acme"
entrypoints = ["pnpm"]              # which shimmed tools this config claims
root_markers = ["pnpm-workspace.yaml", "turbo.json"]

[matching]
strip_prefixes = [["run"]]          # `pnpm run X` is `pnpm X`
subdirectory = "reroot"             # reroot | local | reject
```

`entrypoints` exists because a repo may shim more than one tool; each job may
name a `tool`, defaulting to the first entrypoint. Pandora today shims `pnpm`
only, so Acme's config names one.

### 3.2 Runtime image

```toml
[runtime]
base_image = "node:24-bookworm-slim@sha256:0e0ff…"
setup = ["apt-get install …", "npm install --global pnpm@12.3.4 …", "playwright install …"]
env = { PLAYWRIGHT_BROWSERS_PATH = "/opt/playwright" }
workdir = "/workspace"
user = "node"
```

A Dockerfile *fragment* as data, not a Dockerfile path — Pandora must control the
build context, the cache mount and the final `USER`, and the survey confirms
Acme has no Dockerfile at all (nor `.nvmrc`: Node 24 comes from `engines` and
the CI `setup-node` pin, pnpm from `packageManager: pnpm@12.3.4`).

`base_image` and every service image **must be digest-pinned**; the loader
refuses a floating tag. Acme's own `compose.yml` and CI use `postgres:16`,
`edoburu/pgbouncer:latest`, `ghcr.io/neondatabase/wsproxy:latest`. That is fine
for CI, which re-pulls every run; it is not fine for a warm-image worker that
must reproduce a result. Keeping the digests in the repo's config is where the
argument about which digest to use belongs.

### 3.3 Prepare and cache key

```toml
[prepare]
argv = ["pnpm", "install", "--frozen-lockfile"]
cache_key_paths = ["pnpm-lock.yaml", "pnpm-workspace.yaml", "package.json",
                   "apps/*/package.json", "packages/*/package.json", "patches/**", ".npmrc"]
cache_key_env = []
check_argv = ["node", "tools/check-worktree-deps.mjs"]
```

`cache_key_paths` are globs over the frozen manifest; their content digests key
the dependency image, replacing today's hardcoded install-input set.
`check_argv` is the repo's own "are the installed deps right" gate — Acme sets
`verifyDepsBeforeRun: error` in `pnpm-workspace.yaml` and runs
`tools/check-worktree-deps.mjs` in seven pre-hooks, so Pandora must run the
install (exempt from the gate) and may then assert the gate itself.

### 3.4 Command matchers

A job is claimed by one or more **forms**: a literal token prefix. Aliases are
just extra forms, which is the whole answer to `pnpm test:unit` ≡
`pnpm validate unit` — because in Acme that equivalence is literally
`"test:unit": "pnpm validate unit"` in `package.json`.

```toml
[[jobs]]
id = "surface"
forms = [{ prefix = ["test:surface"] }, { prefix = ["validate", "surface"] }]
params = [
  { name = "app", kind = "enum", values = ["web", "desk"] },
  { name = "selectors", kind = "rest", required = false,
    allow_flags = [{ name = "--grep", arity = 1, max = 1 }] },
]
flags = [{ name = "--keep-going", kind = "pandora", sets = "keep_going" }]
```

Three parameter kinds and two flag kinds cover the whole current boundary:

- `enum` — a closed positional (`web|desk`, `api|scenarios`).
- `pattern` — a regex positional (`(?:S[0-6]|SX)-[0-9]{2}`).
- `rest` — everything after the positionals, in original order, with
  `path_like` validation (nonempty, no leading `-`, no absolute path, no `..`)
  and an `allow_flags` list for options that belong *to the command*, not to
  Pandora. This is exactly what makes `pnpm test:surface desk --grep --keep-going`
  mean "grep for the literal string `--keep-going`", which the shipped classifier
  also does and which any naive flag parser gets wrong.
- flag `kind = "forward"` — passed through to the container argv, optionally with
  `arity = 1`, a closed `values` list, and a `requires = { param, equals }`
  condition (`--foundation-only` only with `api`).
- flag `kind = "pandora"` — consumed by the router, sets a named boolean option
  (`keep_going`, `update`). `forward = true` additionally passes it through, which
  `--update` needs.

**Focused forms stay local** is a per-form decision, because in Acme it
genuinely differs by spelling:

```toml
forms = [
  { prefix = ["test:tools"], on_extra = { action = "reject", message = "pnpm test:tools runs the broad suite; use pnpm validate tools <test files> …" } },
  { prefix = ["validate", "tools"], on_extra = { action = "local" } },
]
```

`on_extra` fires only when a job declares *no* params and *no* flags and the
agent supplied arguments anyway. Jobs that do declare params reject with a
generated or configured usage string instead, so `pnpm journeys S0-01` and
`pnpm test:postgres scenarios --foundation-only` still produce the precise
feedback the contract promises.

`[feedback] reject_suffix = "No validation started."` is appended to every
refusal, preserving today's most load-bearing sentence.

### 3.5 Resources

```toml
cpu_millis = 2000
memory_mib = 6144
services = ["db", "pool", "proxy"]
exclusive = []                     # e.g. "docker-builder"
```

The plan's `resources` is the job's request **plus the sum of its declared
services**, in the same `cpu_millis` / `memory_mib` units
`worker_config.demand()` uses. For `pnpm journeys` that reproduces today's
`3500 / 7296` exactly (main 2000/6144 + db 500/768 + pool 500/256 + proxy
500/128), and the POC asserts it.

The important structural change: `demand()` today *infers* which roles a workflow
needs from a hardcoded `suite in ('postgres','browser-integration')` test. With a
config, the job lists its services and the sum falls out. No Pandora edit when a
new job needs a database.

The repo declares a **request**; the operator's `worker-config.json` still
declares the **cap**. Pandora core clamps and refuses a request that cannot fit.
`disk_mib` stays entirely operator-owned — a repo should not size the worker's
disk reservation.

### 3.6 Services

```toml
[[services]]
id = "db"
image = "postgres@sha256:a3b7f434…"
cpu_millis = 500
memory_mib = 768
host = "127.0.0.1"
port = 5432
env = { POSTGRES_USER = "app_owner", … }
exports = { DATABASE_OWNER_URL = "postgres://app_owner:local-owner@{host}:{port}/app" }
healthcheck = { argv = ["pg_isready", "-U", "app_owner", "-d", "app"], attempts = 60, interval_ms = 500 }
```

`exports` is how a service URL reaches a job: the service's `{host}` and
`{port}` are substituted at load time, and the resulting variables are merged
into every shard's environment underneath the job's own `run.env`. That is
precisely the `DATABASE_OWNER_URL` / `DATABASE_WS_PROXY` / `APP_API_URL` triple
that `tools/validation/heavy.mjs` sets for itself, and it is what lets
`startInstance({ external: true })` attach instead of booting Compose.

`healthcheck` replaces today's hardcoded 60×500 ms `pg_isready` loop in
`warm/journey.py`.

### 3.7 Shards

Two strategies, because Acme genuinely uses two:

```toml
# journeys: env matrix, exactly like ci.yml's JOURNEY_SHARD: '1/4' … '4/4'
shards = { strategy = "env", default = 4, min = 1, max = 32,
           env = { JOURNEY_SHARD = "{shard.index}/{shard.total}" } }

# surfaces: argv template, exactly like ci.yml's --shard=${{ matrix.shard }}
[jobs.shards]
strategy = "argv"
default = 4
argv_append = ["--shard={shard.index}/{shard.total}"]

[jobs.shards.plan]                 # optional build-once / fan-out step
run = { argv = ["pnpm", "--filter", "@acme/{p.app}", "run", "plan:e2e"] }
emits = "apps/{p.app}/e2e/dist/pandora-inventory.json"
services = []
```

`[jobs.shards.plan]` is the build-once seam. Today `surface_parent.py` runs a
plan child that builds the app once and freezes a test inventory, then dispatches
shards that reuse that build; `emits` names the machine-readable partition the
plan step writes, so Pandora can keep its "observed membership equals planned
membership" receipt (`surface_suite.validate_shard`). Without `emits`, a bare
`--shard=i/n` gives no such proof — see §8.

### 3.8 Outputs

```toml
outputs = [
  { kind = "generated", paths = ["apps/{p.app}/dist", "apps/{p.app}/e2e/dist"] },
  { kind = "artifacts", paths = ["apps/{p.app}/test-results", "apps/{p.app}/playwright-report"] },
  { kind = "writeback", requires_option = "update",
    paths = ["packages/scenarios/fixtures/{p.id}.ledger.jsonl",
             "packages/scenarios/fixtures/write-routes.json"] },
]
```

- `artifacts` — copied under the attempt's `results/`. Direct analog of CI's
  `upload-artifact` paths, and the config can be diffed against `ci.yml` to
  check they agree.
- `generated` — exclusively-owned directories replaced atomically in the
  worktree on success. Today `workflow_options.surface_outputs()`.
- `writeback` — tracked files returned into the working tree, gated on a named
  option. The loader refuses a `writeback` entry without a `requires_option`,
  and refuses an option no flag of that job sets, so a fixture allowlist cannot
  be armed by accident.

Path templates may reference resolved params (`{p.app}`, `{p.id}`) and may use
globs (`fixtures/*.ledger.jsonl` for the full-catalog update).

### 3.9 Environment, secrets, fallback

```toml
[env]
set = { CI = "true", WRANGLER_SEND_METRICS = "false" }
passthrough = ["TZ"]
reject_if_set = []                  # global guard; jobs add their own

[secrets]
exclude_globs = [".dev.vars", ".dev.vars.*", "**/.env", "**/.env.*", "**/*.pem", "**/.npmrc"]

[fallback]
action = "local"                    # local | fail
on = ["worker-unreachable", "queue-timeout"]
notice = "Remote worker unavailable; running this command locally instead. …"
```

`passthrough` is an allowlist of host variable *names* forwarded into the
container; `set` is the fixed environment. Per-job `run.env` overrides both, and
a job may set a variable to `""` to neutralize an inherited one — the journey
jobs use `CI = ""`, which is the declarative form of today's `env -u CI` child in
`warm/journey.py`.

`reject_if_set` is per-job. `pnpm journeys` refuses to start when `JOURNEY_SHARD`,
`JOURNEY_FILTER`, `JOURNEY_CONCURRENCY`, `JOURNEY_REPLAY`, `JOURNEY_TEMPLATE` or
`APP_WORLD` is set in the agent's shell, because those would silently change what
"the catalog" means. `pnpm test:unit` does not care and is not blocked — today's
`route.py` applies the guard only to suite runs, and putting it on the job
preserves that.

`secrets.exclude_globs` **adds to** Pandora's built-in exclusions; it never
subtracts. The contract sentence stays: "These exclusions are not a secret
scanner."

**Fallback (owner decision: fall back locally).** Declared per job, defaulting
from the top-level table. Semantics:

- It fires only *before dispatch*: worker unreachable, or the cumulative queue
  budget expired with no shard admitted. It never fires after a container has
  started, and never converts an infrastructure failure mid-run into a local
  rerun — that would re-run tests the worker may still be running and would
  destroy the "70, never a fabricated result" property.
- The plan carries the policy, so the client does not have to re-read the config
  to know what to do.
- The agent is told on stderr, before the local command starts, in one line:
  `[pandora] Remote worker unavailable; running this command locally instead. …`
  and the fallback is recorded in `submission.json` so the evidence path shows
  a locally-produced result was not remote evidence.
- `action = "fail"` keeps today's behavior for jobs that cannot run on a Mac
  (nothing in the Acme config needs it yet; `test:postgres` arguably does,
  since it needs a local Docker stack — see open questions).

---

## 4. What stays in Pandora core

Unchanged, repo-agnostic, and none of it moves:

- snapshot/manifest freeze, secret exclusion mechanism, source digest identity
- transport, source cache, dependency image build and GC
- admission, scheduling policy, resource ownership, execution guard, deadlines
- container and service lifecycle, cleanup verification, `pandora.attempt` labels
- evidence authentication, terminal receipts, publication fences,
  `PublicationConflict` and `pandora resolve-expectations`
- retention, wait/recovery, artifact delivery limits
- the generic parent/child dispatcher (`configured_dispatch.py`) and the
  plan → shard → aggregate receipt machinery
- **the new generic pieces**: config loader, classifier, plan renderer

## 5. What moves to the config

Every table row in §1, plus: which jobs need services, per-job resource requests,
shard defaults and bounds, artifact/generated/writeback paths, env guards, the
fallback policy, and the image pins.

## 6. What needs a seam inside Acme

Four, each small, each with a file reference. The example config marks them
`SEAM-n`.

**SEAM-1 — an explicit direct-exec flag, separate from `GITHUB_ACTIONS`.**
`/Users/you/Code/acme/tools/validate.mjs:85` is
`const direct = process.env.GITHUB_ACTIONS === 'true';`. Pandora needs `direct`
(skip Pueue, no queue slot, no worktree reservation) but *not* the rest of CI
semantics — notably `tools/validation/plan.mjs:159` refuses `--update` when
`GITHUB_ACTIONS || CI`, which is why Pandora's `journey_command()` today spawns
`env -u CI`. Change: introduce `ACME_VALIDATION_DIRECT=1`, set
`direct = process.env.ACME_VALIDATION_DIRECT === '1' || process.env.GITHUB_ACTIONS === 'true'`,
and gate the `--update` refusal on `GITHUB_ACTIONS` alone. ~3 lines.
*Until then the example config sets `GITHUB_ACTIONS = "true"` for the
`validate.mjs` jobs and bypasses `validate.mjs` entirely for journeys.*

**SEAM-2 — `startInstance` must be able to attach to a fully external stack.**
`/Users/you/Code/acme/tools/stack/instance.mjs:148` already has
`external: true` ("Under GitHub Actions the job supplies the database services"),
but it (a) hardcodes `proxy = 'localhost:5433'` instead of reading
`DATABASE_WS_PROXY`, and (b) still starts its own `wrangler dev` worker, so
there is no "an API is already listening" mode. Change: read
`DATABASE_WS_PROXY` in the `external` branch, and add `api` / `APP_API_URL` so
`startInstance` returns the existing origin instead of spawning a worker.
Additionally, `/Users/you/Code/acme/tools/browser-integration/run.mjs:108`
should read that mode from the environment rather than hardcoding
`startInstance({ signal, deployment, env, output, log })` — that exact call shape
is what `warm/validation-stack.mjs` (95 lines) currently rewrites textually, and
the rewrite breaks whenever anyone reorders those five options.

**SEAM-3 — split the surface build from the surface run.**
`apps/desk/package.json` and `apps/web/package.json` have
`"test:e2e": "vite build && vite build --mode e2e && playwright test"`. Sharding
that N ways rebuilds N times. Change: add
`"build:e2e": "vite build && vite build --mode e2e"` and make `test:e2e` call it,
so `pnpm --filter @acme/<app> exec playwright test --shard=i/n` is a legal
standalone shard invocation. ~2 lines per app.

**SEAM-4 — a plan step that emits its partition.**
Add `"plan:e2e": "pnpm run build:e2e && playwright test --list --reporter=json > e2e/dist/pandora-inventory.json"`.
This lets Pandora keep the anti-fabrication receipt it has today
(`surface_suite.validate_shard` asserts observed test ids equal planned test
ids) without importing Playwright's internals. The journeys equivalent already
exists inside `packages/scenarios/src/cli/plan.ts` (`shardJourneys`), but it is
reachable only by importing `.ts` from `warm/suite.mjs`; a
`pnpm --filter @acme/scenarios journeys --plan-only --json` verb would remove
those absolute-path imports too. ~5 lines.

Nice-to-have, not required: `node tools/validate.mjs plan <suite> [args]` already
prints the full `{suite, args, group, commands, env, report, outputs}` JSON. A
future contract version could let a job say `argv_from = "pnpm validate plan {job}"`
and stop restating argv in two repositories at all.

---

## 7. Migration order

Each step deletes something. No step requires the next one.

1. **`unit`, `tools`, `full`, `check`, `native-unit`, `agent-web`,
   `mockup-browser`, `employee-browser`** — the service-free `validate.mjs`
   jobs. Ship the loader, classifier and plan renderer; route these through it
   while `journey`/`journeys`/`surface`/`postgres` keep the welded path.
   *Deletes:* `routing/commands.py`'s `ALIASES` / `VALIDATION_SUITES` /
   `_validation_parts` / `_validation_route` (≈55 lines),
   `warm/validation_request.py` (30 lines), and the suite-table half of
   `warm/validation.mjs`. Needs SEAM-1.
2. **`postgres` and `browser-integration`** — the same jobs plus declarative
   services. *Deletes:* `warm/journey.py`'s `SERVICES` table (8 lines) and the
   readiness loop; `warm/validation-stack.mjs` in full (95 lines);
   `worker_config.demand()`'s `suite in ('postgres','browser-integration')`
   branch. Needs SEAM-2.
3. **`journey` (focused, with `--update`)** — declarative writeback allowlist.
   *Deletes:* `warm/journey.py`'s `journey_config` / `journey_command` /
   `SCENARIO_ID_PATTERN` (≈36 lines), `routing/journey_updates.py`'s `FIXTURES` /
   `PATHS` / the ledger-and-route path derivation in `declarations()` (≈43
   lines). Keeps `declarations()`'s base/target checksum fences, which are core.
4. **`surface`** — argv shard template plus a plan step. *Deletes:*
   `warm/workflow_options.py` (32 lines) and the `APPS` import chain. Needs
   SEAM-3 and SEAM-4.
5. **`journeys`** — env shard matrix plus full-catalog writeback.
   *Deletes:* `warm/suite.mjs`'s absolute `.ts` imports and `JOURNEY_*`
   construction; `routing/route.py`'s `suite_environment_error`.
6. **Runtime image** — `experiments/surface/Dockerfile` becomes generated from
   `[runtime]`. *Deletes:* the Dockerfile (8 lines) and the pin drift between it
   and the repo.
7. **Delete `notes/acme-command-catalog-2026-09-21.json`** or, better, turn
   it into a CI check in Acme that every `pandora.toml` form corresponds to a
   real `package.json` script. The catalog is unused data today; as a lint it
   would be the thing that catches a renamed script before an agent does.

Start at 1. It is the step with the best ratio of deleted welded code to new
risk, and it is the step that proves the loader and classifier in production
before any writeback or service semantics depend on them.

---

## 8. Open questions for the owner

1. **Python floor.** Bump the client to 3.11 for `tomllib`, or vendor a parser?
   (Recommend bump.)
2. **Who owns resource requests?** The config puts `cpu_millis` / `memory_mib`
   on the job, which means a repo PR can ask for more of the operator's worker.
   The operator cap still binds, but should Pandora instead take only a
   *t-shirt size* (`small|medium|large`) from the repo and map sizes to millis in
   the operator config? That is closer to `runs-on: blacksmith-4vcpu-…` and
   removes the temptation to tune.
3. **Is a config change a source change?** The config is a tracked file, so it is
   inside the frozen manifest and therefore inside the source digest. An agent
   editing `pandora.toml` mid-session changes the routing rules of its own next
   command. Proposal: the client reads the config from the *frozen snapshot*, not
   the live worktree, and prints the config digest in `submission.json`. Wanted?
4. **Can a config arm writeback for arbitrary paths?** Today `--update` can only
   return two known fixture paths. A config could allowlist
   `packages/**`. Should Pandora impose a hard ceiling (no `.ts`/`.py`/`.mjs`
   extensions? no more than N files? a required `.gitignore`-style opt-in file in
   the repo?), or trust the repo?
5. **Fallback and `--update`.** A local fallback of `pnpm journey S0-01 --update`
   writes the fixtures directly, with no manifest fence and no
   `resolve-expectations` path. Acceptable, or should `--update` forms declare
   `fallback = { action = "fail" }`?
6. **Fallback and services.** `pnpm test:postgres` falling back locally needs
   Docker Compose on the Mac. Should fallback be refused for jobs that declare
   services, or should the config say `fallback.requires = ["docker"]` and the
   client probe?
7. **Multi-config.** One file per repo, or one file per worktree profile
   (`pandora.toml` plus `pandora.local.toml`)? A monorepo with two teams may want
   different shard defaults. Recommend one file, `--shards` override only.
8. **Versioning.** `version = 1` is refused if unknown. When Pandora grows a
   field, old configs must keep working and new configs must refuse to run on old
   clients. Is "reject unknown top-level keys" (today) the right default, or does
   it need an `unstable = { … }` escape so a repo can adopt a field before every
   client updates?

---

## 9. Ergonomics

### 9.1 Line counts (measured with `wc -l`)

| | lines |
| --- | --- |
| `examples/acme.pandora.toml`, total | 306 |
| …non-comment, non-blank | **232** |
| `examples/generic.pandora.toml`, non-comment | 63 |
| Welded adapters replaced outright | **685** |
| …`routing/commands.py` | 178 |
| …`warm/validation.mjs` | 342 |
| …`warm/validation-stack.mjs` | 95 |
| …`warm/workflow_options.py` | 32 |
| …`warm/validation_request.py` | 30 |
| …`experiments/surface/Dockerfile` | 8 |
| Acme-specific lines inside surviving files | ~92 |
| …`warm/journey.py` (services, id pattern, journey argv) | 44 |
| …`routing/journey_updates.py` (fixture paths, declarations) | 43 |
| …`warm/worker_config.py` (`demand()` role branch) | 5 |
| **Welded total** | **~777** |
| New generic engine (`config.py` + `classify.py` + `plan.py`) | 919 |
| New tests | 524 |

Read that honestly: **232 repo-owned lines replace ~777 welded lines, and the
engine that makes it work is 919 lines.** For one repository this is a wash on
raw LOC. The win is not size, it is three other things:

1. The 232 lines live in the repository that knows the answers, and change on
   that repository's cadence. Adding a command no longer requires cutting a
   Pandora release.
2. The 919 lines have no `acme` in them (verifiable: `grep -ci acme` over
   `config.py`, `classify.py`, `plan.py` is 0), so they are paid once and the
   second repository costs 63 lines, not another 777.
3. The duplicated suite tables collapse. There is exactly one list of jobs
   instead of the current three (`commands.py`, `validation_request.py`, and
   `validation.mjs`'s planner assumptions).

### 9.2 What adding a command costs

Adding `pnpm check` to the configuration is nine lines, all in the repository:

```diff
+[[jobs]]
+id = "check"
+summary = "Lint, format, typecheck and docs gates"
+cpu_millis = 2000
+memory_mib = 6144
+forms = [{ prefix = ["check"] }, { prefix = ["validate", "check"] }]
+run = { argv = ["node", "tools/validate.mjs", "check"], env = { GITHUB_ACTIONS = "true" } }
+outputs = [{ kind = "artifacts", paths = ["tmp/validation"] }]
```

`pnpm test:native-unit` is the same nine lines plus
`on_extra = { action = "local" }` so focused Jest selectors stay on the Mac.

Today the same two commands require, in Pandora: an `ALIASES` entry and a
`VALIDATION_SUITES` member in `routing/commands.py`, a `SUITES` member in
`warm/validation_request.py`, a decision in `worker_config.demand()` if services
are involved, new cases in `routing/test_commands.py` and
`warm/test_validation_request.py`, and a Pandora release before any agent can use
them. Three source edits is not much; the *release* is the cost.

Measured evidence that the boundary really did move: the parity test asserts
`experiments/routing/commands.py` classifies `['check']` as `local` while the
configured classifier plans it as a remote `check` job — the only intended
difference between the two, besides subdirectory handling.

### 9.3 What fit badly, or not at all

Honest list.

- **Turbo argv rewriting does not fit.** `warm/validation.mjs`'s `fullCommands()`
  takes the planner's `pnpm exec turbo run test --filter=!@acme/agent`, splits
  out `@acme/web`, appends `--force` so Turbo cannot replay a cached
  TAP transcript, and re-runs web's Jest with `--maxWorkers=1`. That is a
  *correctness* adaptation (a cached test result is not evidence for this
  attempt) expressed as argv surgery. Nothing in the schema can state it. It has
  to either move into Acme (a `validate full --no-cache` mode) or stay as a
  Pandora escape hatch. I did not model an escape hatch on purpose — once a
  config can carry arbitrary rewrite rules it stops being data.
- **Build-once surface planning is half-declarative.** `[jobs.shards.plan]`
  states *what to run* and *what it emits*, but the partitioning logic — how a
  test inventory becomes N balanced shards — stays in Pandora, and the
  per-shard `test_ids` cannot be known at classification time. So the POC's
  `--shard=i/n` argv strategy is honest about its limits: it loses the
  observed-equals-planned membership receipt that `surface_suite.validate_shard`
  gives today. `emits` is the proposed bridge, but it is unproven; a real
  implementation needs a declared *schema* for the emitted manifest, which is
  more contract surface than I would like.
- **Journey shard balance depends on checked-out fixtures.** Acme's
  `shardJourneys` weights by `.ledger.jsonl` line counts. That is invisible to the
  config and means two agents with different local fixtures get different shard
  boundaries for the same command. Not a config problem, but the config makes it
  look simpler than it is.
- **`--update` conflict resolution stays in core, as it should.** The config can
  declare the writeback allowlist, but base/target checksum comparison, the
  "focused update modified unrelated route entries" check, `PublicationConflict`
  and `pandora resolve-expectations --keep-local` are all about *identity*, not
  about Acme. Those survive untouched — `journey_updates.declarations()` keeps
  its fences and loses only its path literals. This is the part of the split I am
  most confident about.
- **`CI = ""` as "unset" is a wart.** It works (Node treats `''` as falsy) but it
  is a coincidence of the target, not a contract. A real schema wants an explicit
  `unset = ["CI"]` list.
- **Per-form `on_extra` is more machinery than it looks.** It exists for exactly
  one asymmetry (`pnpm test:tools` rejects, `pnpm validate tools` falls back).
  Justifiable, but it is the kind of key that accumulates.
- **Two spellings per job is duplication the repo already has.** Every Acme
  job lists `["test:unit"]` and `["validate","unit"]`, which is literally
  restating `"test:unit": "pnpm validate unit"` from `package.json`. Deriving
  forms from `package.json` scripts would remove ~13 lines and one class of
  drift, at the cost of making the config non-self-contained. I chose explicit;
  the counter-argument is real.
- **Resource numbers in a repo file invite tuning.** See open question 2.

### 9.4 Recommendation

**Go, with changes.**

Go, because the split is real and the seam falls in a defensible place. The
classifier reproduced 77 of 77 argv cases from the shipped boundary — accepted,
rejected, passthrough, `run`-prefixed, and the nasty ones (`--grep --keep-going`,
`--foundation-only` conditionality, `test:tools` versus `validate tools`) —
without a line of Acme knowledge in the engine. The second example
configuration (npm + pytest + Redis, 63 lines) plans correctly through the same
code path. The identity and evidence machinery, which is where Pandora's actual
value is, does not move at all.

The changes:

1. **Do step 1 of the migration only, then re-judge.** Route the seven
   service-free `validate.mjs` jobs through the config, delete the three-way
   suite table, and live with it for a week of agent loops. If the config file
   goes stale or the error messages get worse in practice, the remaining steps
   are not worth it.
2. **Get SEAM-1 and SEAM-2 merged into Acme first.** Both are small and both
   are independently good for Acme (an explicit direct-exec flag is clearer
   than overloading `GITHUB_ACTIONS`; an `external` stack that honors
   `DATABASE_WS_PROXY` is a bug fix). If they cannot land, the config buys much
   less, because `validation-stack.mjs`'s textual surgery survives.
3. **Resolve open question 2 (t-shirt sizes) before step 2.** Once a repo PR can
   change a resource request, changing the unit later is a breaking change.
4. **Do not ship an escape hatch.** If a repo needs argv surgery, that is a
   signal the repo should grow a command, not that the config should grow a
   rewrite rule. `fullCommands()` is the test case: fix it in Acme or leave it
   welded, but do not make it configurable.

No-go conditions, stated in advance so they are falsifiable: if step 1 requires
more than two new schema keys to cover the seven jobs, or if any Acme seam
turns out to need more than ~20 lines, the split is in the wrong place and the
welded adapters should stay.

---

## 10. Riding on `ci.yml` — second pass

This section is the result of testing one idea against the real file: a
`pandora.toml` job says `ci_job = "journeys"` and **inherits** the facts the
repository already maintains in its workflow, instead of restating them.

Everything below was measured against a verbatim trim of
`/Users/you/Code/acme/.github/workflows/ci.yml` (596 lines; the jobs
`postgres`, `journeys`, `surfaces`, `browser-integration` copied line for line
into `experiments/repo-config/fixtures/acme/.github/workflows/ci.yml`). The
importer is `experiments/repo-config/ci_import.py`; its tests are
`test_ci_import.py`.

### 10.1 What imported cleanly from the real file

All four jobs import. Every field below came out of the workflow with no
hand-holding:

| Job | Inherited |
| --- | --- |
| `postgres` | services `postgres` and `wsproxy` — image, env, `ports`, and the `options:` string parsed into a healthcheck (`pg_isready -U app_owner -d app`, 10 retries, 5 s); job env `DATABASE_OWNER_URL`; `timeout-minutes: 15`; node 24 |
| `journeys` | services `postgres`, `pgbouncer`, `wsproxy`; four job env vars; the matrix shard axis `['1/4'…'4/4']` → total 4 **and** where it is consumed (`JOURNEY_SHARD=${{ matrix.shard }}`, found inside the 15-line shell step); `timeout-minutes: 60`; node 24 |
| `surfaces` | shard axis total 2, consumed as `--shard=${{ matrix.shard }}`; `timeout-minutes: 30`; node 24 — **after** declaring `ci_matrix_params = ["app"]` |
| `browser-integration` | `timeout-minutes: 15`; the artifact path `test-results/browser-integration/`; node 24; no services and no shards, which is itself the fact |

Two of those facts were **wrong in the hand-written configuration**, and the
import corrected them without anyone noticing they were wrong:

- The first draft gave `pnpm test:postgres` three services (`db`, `pool`,
  `proxy`). CI's `postgres` job has two: there is no pgbouncer, and its wsproxy
  is pointed straight at Postgres (`ALLOW_ADDR_REGEX: ^postgres:5432$`), not at
  a pooler. Pandora has been starting a pgbouncer that job never used.
- The draft's Postgres password was `local-owner`; CI's is `ci-owner`, and
  `DATABASE_OWNER_URL` embeds it. Two independently-maintained copies of a
  credential had already drifted.

That is the strongest argument in this document for reading `ci.yml` at all,
and it is an argument for *comparing*, not necessarily for *inheriting*.

### 10.2 What did not import, and why

Strictness is the point: nothing is skipped silently. Every item here is a
named `CiImportError` quoting file, job, field and offending text.

1. **The journeys artifact list.** `path:` begins with `/tmp/app-stack.log`,
   which is outside the snapshot a worker returns. Inheriting it is refused;
   the job must declare `outputs` itself. (Verified: `test_config.py`
   temporarily strips the declaration and asserts the refusal.)
2. **The surfaces artifact paths.** `apps/${{ matrix.app }}/test-results/` is a
   matrix expression, and Pandora has no matrix. Refused on inheritance; the
   config writes `apps/*/test-results` and `lint` reports the two forms as
   drift, because nothing can check that a glob and an expression cover the
   same directories.
3. **The `app` matrix dimension.** `surfaces` crosses shards with an
   agent-chosen dimension. The importer refuses until the config states
   `ci_matrix_params = ["app"]` — an explicit "this axis is an argument, not a
   shard". Without that declaration `surfaces` is un-importable, which is why
   the example lints it instead of importing it.
4. **The journeys `run:` block.** Confirmed: it is a 15-line shell script
   (background `pnpm dev:stack`, `trap`, 120-iteration `curl` poll). Only the
   shard assignment is recoverable from it. In particular `JOURNEY_CONCURRENCY=6`
   sits on the same command line and is invisible to a fact importer — the
   config sets `1` and neither side can tell they disagree. This is exactly
   review item 2 restated with a measurement: commands cannot ride on `ci.yml`,
   only facts can.
5. **`runs-on: blacksmith-4vcpu-ubuntu-2404`.** The closest thing CI has to a
   size class, deliberately not imported: mapping a vendor runner label onto a
   worker's CPU and memory is guessing, and it is the operator's guess to make.
6. **The pnpm version.** `pnpm/action-setup@v6` carries no `with:`; the version
   comes from `packageManager` in `package.json`. Nothing to import.
7. **The artifact `name:`.** Uses `${{ strategy.job-index }}`. Not read, because
   Pandora names its own results — but note that this is the one place the
   importer ignores a field rather than refusing it.
8. **`needs:` and `if:`.** Ignored by design and listed in the code as ignored,
   because a worker running one command for one agent has no job graph.

Refusals for shapes a CI maintainer may reach for, each with a fixture and a
test: `container:` jobs, job-level `uses:` (reusable workflow), service
`credentials:`, service `volumes:`, any expression that is not
`${{ matrix.<name> }}`, a shard that disappears into a composite action, a
ragged shard list, a docker `options:` flag that changes isolation (`--cpus`),
an unknown `upload-artifact` input, `node-version-file`, and YAML anchors
anywhere in the document.

### 10.3 The YAML-parser decision

Pandora's client is stdlib-only and the standard library has no YAML parser.
`ci_import.load_workflow()` implements the fallback chain and names which parser
it used.

| Option | Measured | Verdict |
| --- | --- | --- |
| (i) PyYAML if importable | 6.0.3 on the owner's Mac. A `SafeLoader` subclass rejects aliases, anchors and duplicate keys in ~15 lines. | Works, but "if importable" is not a contract: an agent's Python may not have it. |
| (ii) `ruby -ryaml -rjson` | `/usr/bin/ruby` is present; `YAML.safe_load(…, aliases: false)` refuses anchors too. A test asserts the `jobs` tree is byte-identical to PyYAML's. | Works. Costs a subprocess per parse and is macOS-shaped; a Linux client may have no ruby. |
| (iii) vendored subset parser | Not written. Block mappings, block sequences, `>-` folded scalars, `|` literals, flow sequences, quoting rules and YAML 1.1 booleans are all load-bearing in this one file. | Rejected. A subset parser that is wrong about `options: >-` is worse than no parser. |
| (iv) committed JSON snapshot | Implemented: `ci_workflow` may point at `{version, source, sha256, workflow}`, and `snapshot_is_fresh()` compares the digest. | **Ship this.** |

**Would ship (iv), with (i)/(ii) as the generator.** The repository commits
`.github/workflows/ci.pandora.json`, a CI step regenerates it and fails if it is
stale, and the Pandora client reads JSON with `json.loads`. Three reasons: the
stdlib-only rule survives intact; a YAML parse failure happens in the
repository's CI where a human is already looking, not inside an agent's command;
and the snapshot is a tracked file, so it is inside the frozen manifest and the
routing decision is reproducible from a digest. The cost is a generated file in
the repo and one lint.

One finding worth recording: both parsers turn GitHub's `on:` key into YAML
1.1's boolean `True`. It does not matter here — only `jobs:` is read — but it is
a good reminder that "the workflow is just data" is shakier than it looks.

### 10.4 Pod networking: what a shared namespace does and does not reproduce

The plan now carries a `network` section (`classify.network()`), modeling one
network namespace per run: a pause container owns the namespace and every
service plus the job container joins it with `--network container:<pause>`.

Reproduced for free:

- **`localhost:<port>` URLs work verbatim.** CI's
  `DATABASE_OWNER_URL=postgres://…@localhost:5432/app` is imported and used
  unchanged. This is the single best reason to use a pod.
- **No host ports are published**, so concurrent runs cannot collide on 5432 or
  5433 the way the current implementation can.

Not reproduced, and each one bites acme's real configuration:

- **Service-to-service names do not resolve.** Joining another container's
  namespace inherits the namespace, not Docker's embedded DNS — name resolution
  for service aliases exists only on user-defined bridge networks. So
  pgbouncer's `DB_HOST: postgres` and wsproxy's `ALLOW_ADDR_REGEX:
  ^pgbouncer:6432$` resolve nothing. **What is needed:** `/etc/hosts` is a
  per-container mount even when the namespace is shared, so every member of the
  pod gets `--add-host <service name>:127.0.0.1` for every service name. The
  plan emits that exact list (`network.add_host`), using CI's service names, not
  Pandora's role names.
- **A non-identity `ports:` mapping does not survive.** This is the one place
  the "mappings work verbatim" premise is false, and acme hits it: wsproxy is
  `ports: ['5433:80']`. In CI the job dials `localhost:5433`; in a shared
  namespace wsproxy listens on 80 and 5433 is dead. The plan emits
  `network.port_forwards` for every such mapping; each needs either a forwarder
  inside the namespace (`socat TCP-LISTEN:5433,fork,reuseaddr
  TCP:127.0.0.1:80`) or a service reconfigured to listen where CI published it.
  Postgres is safe only because `5432:5432` is an identity.
- **Two services cannot share a container port.** CI hides this because each
  service container has its own namespace. The plan emits `port_conflicts`.
- **Lifecycle constraints.** The pause container must start first and outlive
  every member; a container joining with `--network container:` may not pass
  `--publish`, `--hostname` or `--dns`. Health checks still work, because they
  are `docker exec` in the member's own container.

### 10.5 What breaks when a CI maintainer reshapes the workflow

Every one of these is loud, at config-load time, before anything runs. None is
silent. That is the good news; the bad news is in the last column.

| Reshape | What the importer says | Fixable by the Pandora owner? |
| --- | --- | --- |
| Rename `journeys` → `journey-catalog` | `ci.yml has no job 'journeys'; it defines browser-integration, journeys, postgres, surfaces` | Yes — one line |
| Move the shard into a composite action | `never consumes matrix.shard in a form pandora recognizes` | **No.** The fact genuinely left the file; the job must stop importing |
| Switch to a reusable workflow (`uses:`) | `the job is a reusable-workflow call, so there is no job body in this file to import` | **No.** Following `uses:` means another file, possibly another repo, with `with:` inputs — not attempted |
| Add `container:` to the job | refused by name | No — the import must be dropped |
| Add `credentials:` to a service | refused by name | No |
| Add `${{ github.run_id }}` to one job-level env var | the **whole job** stops importing, although that variable is irrelevant to Pandora | Only by abandoning the import for that job |
| Add a YAML anchor anywhere in the file | the **whole document** is refused, for every job | Only by asking the CI maintainer not to |
| Reorder steps | nothing breaks; the provenance string (`steps[4].run`) changes | n/a |
| Bump `upload-artifact` and gain a new input | refused with the unknown key named | Yes — one line in `ARTIFACT_WITH` |

Three of nine are not actionable on the Pandora side, and two of those three are
triggered by a change with nothing to do with the facts being imported. A
repository's CI file is maintained by people who have never heard of Pandora and
who are not doing anything wrong.

### 10.6 Verdict: lint-only

**Ship `[pins]` and `ci_lint`. Do not ship `ci_job`.**

The import works. All four real jobs import, the strictness is real (ten named
refusals, each with a fixture), and it caught two facts the hand-written
configuration had wrong. But weigh the two sides honestly:

- **What inheritance bought:** the acme example went from 232 to 187
  non-comment lines. Roughly 45 lines, of which the service definitions are 24.
- **What inheritance costs:** a YAML parse (or a snapshot freshness check) on
  the critical path of every agent command, and a new class of load-time failure
  that depends on a file the Pandora owner does not control and cannot always
  repair.

Lint gets the whole correctness benefit — it would have reported both drifted
facts — and puts the failure in the repository's CI, where a human is already
looking, instead of in an agent's loop. `python3 plan.py lint` already does
this: it reports per-field drift with a glob exceptions list and exits 65.

The pleasant part of this conclusion: `ci_import.py` is paid for either way.
Lint needs the parser, the strict field reader and the normalizer — everything
except the merge path in `config.py` that turns imported facts into a job. So
the choice is not "579 lines or nothing"; it is "do facts flow, or only get
compared". Compare.

Revisit `ci_job` only if the drift lint proves that people ignore its findings.
That is a falsifiable trigger: if `lint` reports a drifted service credential
twice in a row and nobody fixes it, inheritance has earned its risk.

### 10.7 Review items 3, 4 and 5, applied

**Item 3 — the argv mini-language shrank.** Gone: `params` (`enum`, `pattern`,
`rest`, `path_like`, `allow_flags`), flag `arity`/`values`/`requires`, and the
`{p.*}` / `{f.*}` / `{opt.*}` / `{params_json}` template vocabulary. What a job
declares now is: literal `forms`, `args = "none" | "required" | "optional"`, the
`options` Pandora itself must consume (an option that arms writeback, one that
changes shard behavior), `value_flags` (flags whose *value* Pandora must not
read), an explicit `reject` list with a message, and per-form `on_extra`.
Everything else is spliced into the repo runner's argv at `{args}`.

`value_flags` is the irreducible remainder and worth naming: without knowing
that `--grep` takes a value, Pandora cannot find its own `--keep-going` in
`pnpm test:surface desk --grep --keep-going`. One list of flag names is much
less than a parser, but it is not zero.

The price is measured, not asserted: **8 of 77 parity cases now diverge**, all
in the same direction — v0.1.1 refused locally, the configuration forwards and
lets acme's runner refuse. Each is pinned in `test_parity.DIVERGENT` with the
knowledge Pandora gave up: the journey-id pattern, the `web|desk` enum,
`--fault`'s closed value list, which positional `--foundation-only` belongs to,
argument counts, and "at most one `--grep`". Recovered by `args = "required"`:
bare `pnpm journey`, `pnpm test:surface` and `pnpm test:postgres` still refuse
locally, because *needing an argument* is part of the claim rather than part of
the grammar. Recovered by `reject`: `--ui` and `--update-snapshots`.

The honest cost of those 8: a container starts, the repo's runner prints the
error, and the agent waits longer to learn something it used to learn instantly.
A repository that minds can split literal forms (`forms = [{ prefix =
["test:postgres", "api"] }, …]`) and buy most of them back.

One more casualty: re-rooting a command run from a subdirectory used to know
which arguments were file selectors. It now guesses — a forwarded token is
re-rooted if it contains `/` or a dotted final segment. `apps/desk` +
`e2e/a.spec.ts` works; `apps/desk` + `smoke` (a `--grep`-less bare selector with
no suffix) silently does not. `test_parity` pins that limit.

**Item 4 — resources are worker-owned.** `cpu_millis` and `memory_mib` are gone
from the schema; a job declares `size = "small" | "medium" | "large"` and a
service declares `role`. `classify.WORKER` is the operator's table and carries
the **evaluated worker's real limits** (main 1000/4096), not the 2000/6144 code
defaults the first example copied from `warm/worker_config.py:10`. `pnpm
journeys` now plans 2500/5248 (medium + db + pool + proxy) rather than a
fictional 3500/7296. This worker cannot offer more than medium, so `large` maps
to the same numbers and the plan says so (`clamped_to: "medium"`) instead of
silently upgrading or refusing. A service role the worker has no limits for is a
refusal that lists the roles it does have.

**Item 5 — the verified nits.**

- `services[].memory_swap` was accepted and silently discarded. It is now an
  unknown key, refused by name.
- Image validation was a substring test for `@sha256:`. It is now a full
  reference match (`registry/name[:tag]@sha256:<64 hex>`), so
  `postgres:16 @sha256:…` and `x@sha256:abc` are both refused. Floating tags are
  refused with the `[pins]` line to add, printed.
- `CI = ""` is gone. `env.unset` and `run.unset` are explicit lists, the plan
  carries `env_unset` beside `env`, and setting and unsetting the same variable
  is a load error. `CI = ""` only ever worked because Node treats `''` as falsy.
- `runtime.setup` runs `apt-get` against a moving mirror, so a digest-pinned
  base is not reproducible on its own. `config.dependency_cache()` now folds the
  setup text, `runtime.platform` and the prepare argv into a `setup_sha256` that
  keys the dependency image, and the plan carries it. That does not make the
  build reproducible; it stops a changed setup line from silently reusing an
  image built by the old one.

### 10.8 Line counts, honestly (supersedes §9.1)

| | lines |
| --- | --- |
| `examples/acme.pandora.toml`, non-comment — first draft | 232 |
| …after items 3–5 and CI import | **187** (−19%) |
| …of which the CI import removed | ~45 (three service definitions, their env, `DATABASE_OWNER_URL`, the shard env template, three timeouts) |
| …and added | 5 (`ci_workflow`, `ci_service_roles`, three `[pins]`) |
| `examples/generic.pandora.toml`, non-comment | 61 (was 63) |
| Engine: `config.py` | 769 (was 508) |
| Engine: `classify.py` | 385 (was 364) |
| Engine: `plan.py` | 101 (was 47) |
| Engine: `ci_import.py` | 579 (new) |
| Tests | 975 (was 524) |

Read that the way §9.1 asked to be read. The argv shrink did *not* shrink the
engine: `config.py` grew by 261 lines, because what the loader lost in parser
machinery it gained in import merging, provenance, pins, size classes and lint.
The repository-side file did shrink, by 45 lines, and 24 of those were the
service table — the one thing lint can keep honest without inheriting anything.

A second repository still costs ~61 lines. That number has not moved, and it is
the one that decides whether any of this is worth it.
