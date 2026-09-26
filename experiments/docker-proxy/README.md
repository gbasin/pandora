# Per-run Docker API proxy

A run gets its own Unix socket. `DOCKER_HOST=unix:///…/docker.sock` points at a
listener Pandora owns, one per run, so the socket identifies the run and the
repository's own tooling — `docker compose` included — needs no Pandora
knowledge to be accounted for, scoped, and cleaned up.

```sh
python3 -B proxy.py serve \
  --run r-123 \
  --socket /run/pandora/r-123/docker.sock \
  --run-dir /srv/pandora/runs/r-123/source \
  --client-root /Users/dev/Code/acme-wt/feature \
  --cgroup-parent pandora-r-123.slice \
  --memory 1073741824 --nanocpus 2000000000

python3 -B proxy.py usage --run r-123     # live memory and CPU, for admission
python3 -B proxy.py watch --run r-123     # the same, sampled, keeping peaks
python3 -B proxy.py sweep --run r-123     # remove everything labelled, print a receipt
```

`--docker` (or `PANDORA_DOCKER_SOCKET`) names the real socket. `--client-root`
defaults to `--run-dir`, which is the identity rewrite a developer's machine
needs. `--allow-path` may be repeated to permit a shared read-only path such as
a package cache.

## What the proxy does to each request

| | |
| --- | --- |
| `POST /containers/create` | injects `pandora.run`, sets `CgroupParent`, fills `Memory`/`NanoCpus` when the request states none, rewrites bind sources under the client root onto the run directory, then refuses anything that leaves it |
| `POST /networks/create`, `POST /volumes/create` | injects `pandora.run` |
| `GET /containers/json`, `/networks`, `/volumes`, `/events` | folds `label=pandora.run=<id>` into the caller's own filter set |
| anything naming one container, network, volume or exec | 404 unless that object carries the run's label |
| `GET /images/*`, `POST /images/create` | allowed: reads and pulls are shared and harmless |
| `POST /build`, `/session`, `/commit`, image delete, any `prune` | refused with a message |
| everything else | refused with a message |

A run cannot set `Privileged`, add capabilities, request devices, set
`SecurityOpt`, pick a non-default runtime, use `VolumesFrom`, or join a host or
another container's network, PID, IPC, UTS, cgroup or user namespace. It cannot
bind `/`, the daemon socket, `/proc`, `/sys`, `/dev`, `/run`, `/etc`, `/boot`,
or any path that resolves outside its run directory.

This is **policy for code the worker already trusts to run, not containment**.
The run still chooses its images, reaches the network, and touches the kernel
through ordinary syscalls. The proxy narrows the Docker API surface; it does
not narrow the container's.

## Files

| File | Lines | |
| --- | --- | --- |
| `policy.py` | 408 | every decision, as pure functions of a request line and a parsed body |
| `proxy.py` | 673 | the bytes: framing, streaming, hijacks, ownership lookups, `sweep`, `usage`, the command line |
| `engine.py` | 71 | a Docker Engine API client over a Unix socket |
| `test_policy.py`, `test_proxy.py` | 631 | 47 tests, against a fake daemon; no dockerd needed |
| `test_integration.py` | 253 | 12 tests against the local daemon, skipped unless `PANDORA_DOCKER_IT=1` |

```sh
python3 -B -m unittest test_policy test_proxy
PANDORA_DOCKER_IT=1 python3 -B -m unittest test_integration
```

The integration module labels and names everything it creates after
`it-<pid>-<n>` and sweeps that label in teardown whether the test passed or
not, so it is safe on a machine running other work.

`evidence/2026-09-21/` holds the run of acme's journey `S0-01` through the
proxy, the SIGKILL and sweep receipt, and what a refused request looks like to
`docker compose`. [`../../notes/docker-proxy-poc-2026-09-21.md`](../../notes/docker-proxy-poc-2026-09-21.md)
reads them.
