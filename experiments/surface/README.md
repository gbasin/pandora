# Surface baseline

This experiment establishes whether the owned Linux worker can run Acme's
web surface suite. It submits one **committed revision**. It does not
submit dirty edits, route agent commands, queue concurrent jobs, or replace CI.
Run one invocation at a time until admission control is implemented.

## Prepare the disposable worker

Use an x86 Linux worker with Docker, systemd, 16 GiB RAM, and SSH access.
Use a dedicated worker with no production credentials. This prototype runs
trusted repository code and does not claim hostile multi-tenant isolation.

Copy `Dockerfile` to the worker. Build the image:

```sh
sudo docker build -t pandora-surface:smoke .
```

The base image digest, pnpm version, and Playwright version are pinned. Apt
packages remain resolved at build time. Each submission records the resulting
image ID. Update the Playwright image pin when the target lockfile changes.

## Run the baseline

From this directory on the Mac:

```sh
python3 smoke.py \
  --host ubuntu@WORKER_IP \
  --repo /path/to/acme \
  --revision COMMIT_SHA \
  --output /tmp/pandora-baseline-01 \
  smoke.spec.ts
```

Omit the final selector to run the whole surface suite. The output directory must
not exist. The program leaves the target checkout, index, and HEAD unchanged.
No local dependency install or browser execution occurs.

Each invocation uploads a gzip-compressed Git archive. The worker verifies its
SHA256 before execution. A fresh container extracts the source, installs frozen
dependencies, builds the fixture app, and runs Playwright serially. It uses two
CPUs, at most 6 GiB RAM, no swap, and at most 512 processes. The container has no
Docker socket, host credentials, privileged capabilities, or published ports.
It has ordinary outbound network access for package installation.

The terminal receives stdout and stderr. Local evidence contains source and image
identity, complete streams, JUnit, available Playwright artifacts, cgroup memory
peak and CPU counters, and final Docker state. Setup failures are identified by
`results/phase`. An OOM may prevent in-container metrics from being written;
inspect `container.json` and treat missing results as incomplete evidence.

## Cancellation and retention

This is a baseline harness, not the final foreground command contract. A worker
systemd timer stops the named container after 20 minutes even if the SSH client
disappears. Normal completion removes the container. Shell exit traps attempt to
stop and remove it on interruption. Client disconnection semantics are not yet
validated. Do not assume an interrupted SSH session proves remote cleanup.

The extracted workspace remains under `~/pandora-smoke/<attempt>/workspace` for
inspection. There is no shared dependency cache or automated retention sweep yet.
Do not use repeated baseline runs as the warm-iteration performance measurement.
The `submission.json` file records the exact attempt and remote directory.

Delete the disposable cloud VM after the trial. Stopping Docker or shutting down
the VM does not terminate cloud billing. This harness does not provision or
delete provider resources.
