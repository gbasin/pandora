---
status: log
---
# Per-run Docker API proxy, 2026-09-21

Acme's own journey runner booted acme's own Compose stack and ran journey
`S0-01` to a pass through a Pandora-owned Docker socket, with no Pandora
knowledge anywhere in the acme repository and no acme knowledge anywhere
in the proxy. Every container, network and volume the run created carried
`pandora.run=proof-x`. A second run started at the same time could not list or
stop any of them. A `SIGKILL` of the runner mid-run left the stack orphaned and
`sweep` removed all five objects and produced a clean receipt.

The design works on Docker Desktop. It has one unresolved cost on Linux, stated
in full below: the API worker is a host process that reaches Postgres through a
*published* port, and a published port lands in the worker host's network
namespace, not in the run container's. That question was not testable here.

## What was built

`experiments/docker-proxy/` (Pandora, Python standard library only):

| File | Lines |
| --- | --- |
| `policy.py` — every decision as pure functions | 408 |
| `proxy.py` — framing, streaming, hijacks, lookups, `sweep`, `usage`, CLI | 673 |
| `engine.py` — Engine API client over a Unix socket | 71 |
| `test_policy.py` + `test_proxy.py` — 47 tests against a fake daemon | 631 |
| `test_integration.py` — 12 tests, skipped unless `PANDORA_DOCKER_IT=1` | 253 |
| **total** | **2036** |

`tools/validation/journey-runner.mjs` (acme, 358 lines plus a 107-line test,
a 41-line inventory CLI and a 52-line refactor of `packages/scenarios`): `plan
--shards N` emits the shard inventory through the existing `shardJourneys`
logic and refuses a set that is not a partition; `run [<id>|--shard i/N]` boots
the stack through acme's own `startInstance`, runs, writes a JSON report of
the journeys it observed, and tears the stack down. Profile is explicit
environment (`JOURNEY_CONCURRENCY`, `JOURNEY_REPLAY`, `JOURNEY_CI`); nothing
sniffs `GITHUB_ACTIONS` or `CI`. Selection lives in one function,
`selectJourneys`, that both the inventory and the suite call, so a plan cannot
describe a run that did not happen. `pnpm journey`/`pnpm journeys` are *not*
rewired — `tools/notes/journey-runner.md` records that as a stated gap, and
`ci.yml` was not touched.

None of the acme work depends on the proxy. That is the point: the runner is
what acme wants anyway, and the proxy is invisible to it.

## API surface covered

Injected on create: `Labels["pandora.run"]`, `HostConfig.CgroupParent`, and
`Memory`/`NanoCpus` when the request states none. Bind sources under the client
worktree path are rewritten onto the run directory and then required to stay
inside it. Networks and volumes are labelled the same way.

Refused, with a JSON `message` the client prints verbatim: `Privileged`,
`CapAdd`, `Devices`, `DeviceRequests`, `DeviceCgroupRules`, `SecurityOpt`,
`VolumesFrom`, a non-`runc` runtime, `NetworkMode=host` or `container:…`,
`PidMode`/`IpcMode`/`UTSMode`/`CgroupnsMode`/`UsernsMode` set to `host` or
`container:…`, binds of `/`, of any `docker.sock`, of `/proc`, `/sys`, `/dev`,
`/run`, `/etc`, `/boot`, or of anything resolving outside the run directory, a
`local` volume whose `DriverOpts.device` is a host path, and a non-bridge
network driver.

Scoped: `containers/json`, `networks`, `volumes` and `events` get
`label=pandora.run=<id>` folded into the caller's own filter set; every call
naming one container, network, volume or exec answers 404 unless that object
carries the run's label. Exec ids are not labelled objects, so the proxy mints
and remembers the ones created through it.

**Image policy: pull allowed, build denied.** Reads (`GET /images/…`) and pulls
(`POST /images/create`) pass through, because a repository legitimately names
images the worker has not cached. `POST /build` and `/session` are refused: a
build is an unbounded workload that neither the reservation nor the label
accounting can see, and a BuildKit session hijacks the connection into a gRPC
stream this proxy does not read. Image delete, image prune and push are refused
because the image cache is shared between runs. Every `prune` endpoint is
refused for the same reason. `swarm`, `nodes`, `services`, `secrets`,
`configs`, `plugins`, `commit` and `system` are refused.

Framing handled: keep-alive (the proxy loops rather than piping, so every
request on a connection is policed and not only the first); rewritten bodies
with a recomputed `Content-Length`; chunked request bodies decoded before a
rewrite; chunked responses streamed chunk by chunk so `events` and `logs
--follow` never buffer; `101` upgrades and framing-free `200`s handed to a
full-duplex relay, with whatever the readers buffered past the head flushed
into it first; API version prefixes stripped before routing.

## Results

Docker Desktop 29.6.1, linux/arm64, 8 GiB VM, cgroup v2. Host load average 53
at the start of the passing run and 12 at the start of the kill run; the
machine was running other agents' acme stacks throughout (19 containers
alive during the run, none of them touched).

**The run passes unmodified.** `journey-runner.mjs run S0-01` with
`DOCKER_HOST` pointed at the proxy for run `proof-x`: exit 0, 710.8 s wall,
journey 641.9 s, `S0-01: pass … replayed`, 45 of 45 stage-routes covered by
passing replays, 0 infrastructure failures, 0 unrun journeys. The report names
`S0-01` as the only journey observed. Nothing in acme was changed to make
this work.

**Everything is labelled.** All five objects the run created:

| Object | `pandora.run` |
| --- | --- |
| `app-validation-e909988c-postgres-1` (`postgres:16`) | `proof-x` |
| `app-validation-e909988c-pgbouncer-1` (`edoburu/pgbouncer:latest`) | `proof-x` |
| `app-validation-e909988c-wsproxy-1` (`ghcr.io/neondatabase/wsproxy:latest`) | `proof-x` |
| `app-validation-e909988c_default` (network) | `proof-x` |
| `app-validation-e909988c_postgres-data` (volume) | `proof-x` |

**Cross-run invisibility.** A second proxy for run `proof-y` ran concurrently
against the same daemon. Through it, `docker ps -a`, `docker network ls` and
`docker volume ls` all printed nothing, while the same commands through
`proof-x`'s socket listed exactly its three containers. `docker stop
app-validation-e909988c-wsproxy-1` through `proof-y` failed with `Error
response from daemon: pandora-proxy: no such container for this run: …` and the
container stayed running. The host had 19 containers at that moment; a run saw
three or zero.

**Usage.** 170 samples at 2 s over the run:

| Container | Peak RSS | Peak CPU | Pandora's static reservation |
| --- | --- | --- | --- |
| postgres | 73.5 MiB | 80.9 % | 768 MiB |
| wsproxy | 12.6 MiB | 7.4 % | 128 MiB |
| pgbouncer | 2.6 MiB | 4.1 % | 256 MiB |
| **total** | **86.1 MiB** | — | **1152 MiB** |

The services cost **86 MiB at peak against 1152 MiB reserved**, a 13× over-
reservation, and against the evaluated worker's 4096 MiB main slot the whole
service stack is 2 % of it. This is one journey at concurrency 1; a six-lane CI
arrangement moves Postgres, not pgbouncer or wsproxy. The number that matters
is that it is now *measured per run from the label* rather than declared, so
admission can stop guessing. `usage()` is what an admission controller would
read; `watch` is the same sampled, keeping peaks.

**Kill and sweep.** A second run (`proof-k`) was started, allowed to bring all
three services to `running`, and then `SIGKILL`ed as a process group. No report
was written — the runner died before its `finally`. All three containers were
still running, with the network and the volume. `sweep(proof-k)` removed 3
containers, 1 network and 1 volume, reported `errors: []`, `remaining:
{containers: [], networks: [], volumes: []}` and `clean: true`. The receipt
names every object by id and name, so an operator's check is a JSON comparison
rather than a `docker ps` by eye.

After the *successful* run, `sweep(proof-x)` found nothing to remove and
returned `clean: true` with zero counts: acme's own teardown had already
removed everything through the proxy. Both facts matter — the receipt proves
cleanup whether the repository managed it or not.

**A denied request, as `docker compose` sees it.** Acme's real
`tools/stack/compose.yml` and `compose.ephemeral.yml` were run through the
proxy with a third `-f` adding `network_mode: host` to Postgres:

```
Network proof-d-stack_default  Created
Volume proof-d-stack_postgres-data  Created
Container proof-d-stack-postgres-1  Creating
service:postgres:1 Error response from daemon: pandora-proxy: refused NetworkMode=host.
A run shares the worker's network namespace with every other run; publish ports instead.
```

Exit 1, the message printed verbatim and repeated on the dependent service, and
a subsequent `down -v` left nothing behind. It fails loudly and legibly: the
refusal names the field, says why, and says what to do instead. A repository
author reading it does not need to know a proxy exists to act on it.

## What acme's stack forced the proxy to special-case

1. **Compose sends the legacy map form of a filter.** Its first network lookup
   is `filters={"name":{"<project>_default":true}}`. Adding a list-valued
   `label` beside a map-valued `name` makes the daemon answer `invalid filter`,
   because it unmarshals the whole object as one shape. Every filter key has to
   be normalised to the list form. This broke `compose up` on its very first
   call and would never have shown up against a hand-written client.
2. **`containers/create` and `containers/{id}/exec` answer chunked**, with no
   `Content-Length`. The created container id and the exec id are only readable
   by parsing a chunked body. Without that, `compose exec` fails at the upgrade
   with `unable to upgrade to tcp, received 404`.
3. **Exec ids are not labelled objects.** There is nothing to look up, so the
   proxy has to remember the ones it minted.
4. **Predefined networks vanish.** `bridge`, `host` and `none` carry no label,
   so `docker network ls` through the proxy lists nothing. Compose did not care
   because it creates its own project network, but a tool that expects `bridge`
   to exist would break.
5. **Buffered bytes are part of a hijacked stream.** The response head and the
   first output of `logs --follow` arrive in one packet; a relay that reads the
   socket directly and forgets the reader's buffer truncates silently. This was
   a real bug, caught by a unit test.
6. **Healthchecks never reach the proxy.** Postgres's `pg_isready` runs inside
   the daemon, so `up -d --wait` worked unchanged. This is luck worth naming:
   had acme polled health with `docker exec`, every poll would have needed an
   ownership lookup.
7. **Acme's stack has no bind mounts and no init-SQL bind.** `postgres-data`
   is a named volume; migrations run from the host over `pg`. The rewrite was
   therefore never exercised by acme, only by unit tests and a synthetic
   integration case. A repository that binds certificates or seed SQL would be
   the first real test of it.
8. **Fixed ports exist but are overridden.** `compose.yml` declares
   `127.0.0.1:5432:5432` and `127.0.0.1:5433:80`; `compose.ephemeral.yml`
   replaces both with `!override ['127.0.0.1::5432']`, so every validation run
   takes an ephemeral port. `pnpm dev:stack` does not, and the proxy does not
   rewrite port publishing, so two runs on fixed ports would still collide.
9. **`host.docker.internal` is not used anywhere in acme.** One fewer
   Linux-only surprise.
10. **`docker compose ls --all`** — what acme's `pruneProjects` recovery
    path uses — works, and now sees only the run's own projects. That is the
    desired scoping and also means a run can no longer clean up a leftover from
    a different run.
11. **The compose plugin is a client-side CLI plugin.** It has to be installed
    in the run's image. The proxy has nothing to do with finding it.

## Docker Desktop versus Linux

`HostConfig.CgroupParent` **is honoured** by Docker Desktop's Linux VM, which
was not a given. `docker inspect` reported the requested
`pandora-proof-x.slice`, and a container run with `--cgroupns=host` showed
`0::/pandora-probe.slice/<id>` in `/proc/self/cgroup`, with the parent cgroup
surviving the container. So a per-run slice is real here and `memory.current`
on the slice would meter a whole run in one read. Two caveats: with a cgroup
namespace (the default) a container cannot see its own path, so this is only
observable from outside; and Docker does not remove a parent cgroup it created,
so the empty slice directory lingers.

### The open question: published ports and the run container's netns

Acme's API worker is `wrangler dev --local` — a **host process**, workerd,
not a container. `startInstance` publishes Postgres and wsproxy on ephemeral
loopback ports, asks `docker compose port postgres 5432`, and builds
`postgres://app_owner:…@127.0.0.1:<port>/app`. The migration and the per-journey
database clones connect the same way, from the host, over `pg`.

On this Mac that works because the run *is* the host. On the Linux worker the
run executes **inside a run container**, and the Compose services are
**siblings** created by the same daemon. A published port binds in the worker
host's network namespace. `127.0.0.1:<port>` inside the run container's
namespace is the run container's own loopback, where nothing is listening. The
run would fail at the first `pg` connect, before any journey starts.

Three ways out, cheapest first.

1. **`--network host` for the run container.** Free, and it throws away exactly
   the isolation the proxy exists to provide: every run would see every other
   run's published ports and the worker's own services, and the proxy would be
   refusing `NetworkMode=host` for the services while the run itself used it.
   If this is the answer, the design has lost most of its value.
2. **Attach the run container to the run's networks, and forward.** The proxy
   already sees `POST /networks/create`; it can connect the run container to
   that network server-side, with no acme change. But acme asks for
   `127.0.0.1:<published port>`, so something must still listen there: a
   per-port TCP forwarder started inside the run container's namespace, and a
   rewrite of the inspect and `port` responses to report ports the proxy is
   brokering. That is response-body rewriting, which this proxy deliberately
   avoids today, plus a race against `compose port` being called immediately
   after `up --wait`. Estimate 200–300 lines and a new class of bug.
3. **Let the stack be addressed by service name when the caller is inside the
   network.** The run container joins the compose network (step 2's first half,
   free and invisible to acme), and `startInstance` reports
   `postgres:5432` and `wsproxy:80` instead of the published ports when an
   explicit variable says the caller shares the network. That is a handful of
   acme lines, defensible on their own terms — a consumer inside a network
   should use service DNS — and it deletes the whole port problem. It is a seam,
   but a far smaller and more honest one than an external-stack mode.

Option 3 is the recommended path. Note that it does **not** re-introduce the
drift risk of the alternative design: the variable says "I am on your network",
not "here is a Postgres I built for you".

### What must be verified on the Linux worker

1. That a sibling container's `127.0.0.1:<port>` publication is in fact
   unreachable from inside the run container. Expected, never measured.
2. That `docker compose port postgres 5432` returns `127.0.0.1:<port>` and not
   `0.0.0.0:<port>` for the `127.0.0.1::5432` spec on Linux.
3. That `POST /networks/<id>/connect` for the run container, issued by the
   worker on the real socket while `compose up` is in flight, is safe and gives
   the run container working DNS for `postgres` and `wsproxy`.
4. That `CgroupParent` is honoured under the host's cgroup driver. With the
   **systemd** driver a parent must be a real `.slice` and may need creating with
   `systemd-run` first; with **cgroupfs** Docker creates the path, as Desktop
   does. Which driver the worker runs decides whether Pandora pre-creates the
   slice.
5. That `memory.current` and `memory.peak` on the run's slice aggregate the
   sibling containers. If so, `usage()` becomes one file read instead of N
   stats calls, which matters at worker scale.
6. That the bind rewrite behaves when the run directory genuinely differs from
   the client worktree path. Acme exercises none of it; only unit tests do.
7. That the proxy can open the real socket while the run cannot: socket group
   membership and the run container's user, verified by attempting both.
8. That `docker compose` (the CLI plugin) and the `docker` CLI are present in
   the run image, and that the plugin's own API calls are all in the surface
   above. A newer Compose may add calls this proxy refuses.
9. That a pull through the proxy from inside the run container works, including
   what happens to registry credentials: the run's auth header is forwarded
   unchanged today, which is a decision to make explicit rather than inherit.
10. That killing the run container leaves the sibling stack running and that
    `sweep`, run by the worker on the real socket, still removes it. Proven on
    Desktop; the Linux case differs only in who the parent process is.
11. That the daemon's ephemeral port range does not collide across many
    concurrent runs on one worker. With `127.0.0.1::` the daemon allocates, so
    this should be free — worth one deliberate check at the worker's real
    concurrency.

## Security posture, stated honestly

**This is policy for code the worker already trusts to run. It is not
containment.** The proxy narrows the Docker API surface; it does not narrow the
container's. A run still chooses its own images and executes whatever is in
them, reaches the network without restriction, and touches the kernel through
ordinary syscalls. Nothing here inspects image contents, restricts egress, or
reduces kernel attack surface. A run that wants to exfiltrate the worker's
secrets does not need the Docker API to do it.

What the proxy does buy is that an *honest* repository cannot accidentally take
the worker down, cannot accidentally see or delete another run's containers,
and cannot leave objects Pandora cannot find. Against a hostile repository it
raises the cost of the easy escapes — socket binds, `--privileged`, host
namespaces, `/` mounts — and nothing more. The label is also not a security
boundary: a run could set `pandora.run` to another run's id in its own
`Labels`, and the proxy overwrites it on create, but a run that reached the
real socket some other way could forge it freely.

## Failure modes

- **A Compose or CLI upgrade adds a call the proxy refuses.** The default is
  deny, so the failure is loud and immediate, but it is a failure. This is the
  main ongoing maintenance cost: the proxy tracks the Docker client's API use,
  not just the daemon's API.
- **A response-shape assumption breaks.** The exec and create ids are parsed
  out of chunked bodies. A daemon that framed them differently would silently
  stop populating the ownership cache; the fallback is a lookup, so this
  degrades rather than fails.
- **Ownership lookups cost a round trip each.** Positive results are cached,
  negative ones are not. A run that inspects many foreign ids does a lookup per
  request.
- **A proxy that dies mid-run breaks the run.** Every Docker call fails at once.
  The stack is still labelled, so `sweep` still cleans up, but the run is lost.
- **`sweep` can only see what is labelled.** A container created by some path
  that bypassed the proxy is invisible to it. On a worker where the run has no
  other route to a daemon, that is a closed set; if it ever has one, the
  guarantee is void.
- **The label is scoped by socket, not by identity.** Anything that can open the
  run's socket is the run. File permissions are the whole boundary.
- **Empty cgroup slices accumulate**, one per run, until something removes them.

## The alternative: declared services and an external-stack seam

This is what `experiments/warm/journey.py` does today for this one workload:
three service definitions with pinned digests, environment lists and memory
caps (180 lines), plus `experiments/warm/validation-stack.mjs` doing textual
surgery on acme's `startInstance({…})` call (95 lines). Acme's
`startInstance` already has an `external` mode that reads `DATABASE_OWNER_URL`
and hard-codes `localhost:5433`.

| | Proxy (this POC) | Pandora boots declared services |
| --- | --- | --- |
| Pandora LOC | 1152 production, 884 test, repo-agnostic. Plus 0–300 unbuilt for the Linux netns question | 275 today for one workload; generalising means a service schema, health-wait, network, port allocation and env templating for every repo |
| Acme LOC | 0 for the proxy; option 3 above adds a handful | a maintained external-stack seam, plus keeping it honest as the stack changes |
| Config lines per repo | 6 per-run values, none of them repo knowledge | ~20 lines of services restated from `compose.yml`, per repo |
| Drift risk | near zero — the proxy names no image, service or port | high and silent. `journey.py` already pins `ALLOW_ADDR_REGEX=^pgbouncer:6432$` where acme's compose says `^(pgbouncer:6432\|postgres:5432)$`, and pins digests acme moves by retagging `:latest` |
| Accounting | exact, and covers services Pandora has never heard of. Measured 86.1 MiB peak against 1152 MiB reserved | exact for what Pandora created; a service acme adds is invisible |
| Cleanup proof | one label, one receipt, survives `SIGKILL` of the run | exact for what Pandora created; nothing catches what it did not |
| Linux risk | the published-port question above, unanswered | none — there is no second party creating containers |
| Ongoing cost | tracking the Docker client's API surface | tracking every repository's stack |

The honest summary: the declared-services design has no unknowns and no new
protocol machinery, and its cost is a per-repository configuration that drifts
from the repository and is wrong in a way nobody notices until a journey fails
strangely. The proxy design has one real unknown and 1152 lines of protocol
handling, and its cost is bounded by the Docker API rather than by the number
of repositories Pandora serves.

## Recommendation

**Adopt the proxy, and answer the Linux networking question first.**

The evidence for it is strong: acme's real stack, acme's real runner, a
real journey, passing unmodified, fully labelled, invisible to a concurrent
run, swept clean after a `SIGKILL`, with per-container usage that immediately
showed the current reservation to be 13× too large. None of that required a
line of Pandora knowledge in acme, and none of it will need changing when
acme changes its stack. That last property is the one the declared-services
design cannot buy at any price.

The order of work:

1. Spend a day on the Linux worker answering items 1–5 of the verification
   list, in particular whether option 3 — the run container joined to the
   compose network, acme reporting service names when a variable says the
   caller shares it — actually gets `wrangler` talking to Postgres.
2. If it does, take the proxy as the mechanism and the repo-config POC as the
   declaration of *what to run*, not *what to boot*. The two compose cleanly:
   the config names commands, the proxy accounts for whatever they create.
3. If it does not, and the only Linux answer is `--network host` for the run
   container, fall back to declared services. A proxy that cannot keep runs in
   separate network namespaces is not buying enough to justify 1152 lines of
   protocol handling.

Either way, keep the proxy's `sweep` and `usage`. They are 150 lines, they
depend only on a label Pandora controls, and under the declared-services design
they are still the cheapest cleanup proof and the only per-run accounting that
does not require Pandora to have created every container itself.
