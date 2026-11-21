# The e2e runner

Pandora's e2e workflows (`e2e.yml`, and `release-please.yml`'s `verify` job)
need a real worker: Incus, a btrfs pool, a bridge, and a worker user with
passwordless sudo. This repository is public, so a fork's pull request must
never run on that machine. This page explains how each e2e job gets its own
one-job VM on the shared host, why, and how to install, verify and roll it
back. The files are in [`deploy/e2e-runner/`](../deploy/e2e-runner).

## Why the old runner had to go

Until this change, the runner `worker-e2e` ran on the shared host as
`gh-runner`. Its key `~/.ssh/e2e-ci` reached `pandora-ci@localhost`, and
`pandora-ci` had `ALL=(ALL) NOPASSWD:ALL`. Job code was therefore one `ssh`
and one `sudo` away from root on the host, which holds every VM on it (the CI
controller, Frank and its data, the backups) and the CI secrets.

Only e2e.yml's job `if:` kept fork pull requests off that runner, and a
fork's own copy of the workflow can delete it. Since 2026-11-21 the
repository requires approval for runs from all outside contributors. That
is a mitigation: one approved run, or a mistaken approval, still has root.

### What the worker needs root for

Narrowing `pandora-ci`'s sudo to "the commands the e2e needs" does not remove
that path, because the commands it needs are root-equivalent:

* `sudo incus --project pandora ...` for every instance operation
  (`pandora/executor/incus.py`, `IncusDriver.base`). The Incus admin socket
  is root: a caller can launch `security.privileged=true` with the host's `/`
  as a disk device, or pass `--project` for any other project.
* `sudo tee <cgroup>/memory.high`, `memory.swap.max`, `memory.oom.group` and
  `cgroup.kill` (`incus.py`, `harden` and the kill path). A sudoers glob on
  the path still admits `..`.
* `sudo bash -c 'cd <cgroup> && ... cat ...'` for usage samples (`incus.py`,
  `usage`). A sudoers glob in a `-c` string admits any shell.
* `sudo chown -R` on pulled artifacts (`pandora/engine/runner.py`, collect).
* `sudo btrfs qgroup show`, `quota rescan` and `filesystem usage` on the pool
  mount, and `sudo iptables -S FORWARD` in `worker status`
  (`pandora/worker/facts.py`).
* Provisioning (`pandora/worker/provision.sh`) needs apt, systemd units and
  Incus objects. The e2e jobs do not provision, but the worker was made with it.

The worker also shared the live worker's Incus project, pool and bridge
(`pandora`, `pandorapool`, `pandorabr0`), so e2e runs sat beside the team's
runs. The sound fix moves the whole worker, sudo and all, into a VM that
exists for one job.

## Shape

```text
host (root)
├── pandora-e2e-pool.service      e2e_pool.py: mints JIT runners, launches and retires VMs
├── /etc/pandora-e2e/github-token fine-grained token, gbasin/pandora, Administration write
├── pandorae2ebr0 + ACL pandora-e2e-vm   public IPv4 only, never the host or its VMs
└── Incus project pandora-e2e
    ├── image pandora-e2e -> pandora-e2e-<commit>
    └── e2e-<12 hex>   ephemeral VM, one job each
        ├── gh-runner: actions runner (--jitconfig), job-started hook
        └── pandora-ci@localhost: a ready Pandora worker, its own Incus, sudo
```

| Resource | Name |
| --- | --- |
| Incus project | `pandora-e2e` (own images and profiles, default networks) |
| Bridge and ACL | `pandorae2ebr0` (10.142.0.1/24, no IPv6), `pandora-e2e-vm` |
| Storage pool | `pandora-e2e`, btrfs, 120 GiB loop file: each VM is a copy-on-write clone of the image |
| Profile | `pandora-e2e`: 4 vCPU, 8 GiB, 40 GiB root on `pandora-e2e`, port isolation |
| Scripts and hook | `/usr/local/libexec/pandora-e2e/`, root-owned, in the VM and, for Phase 1, on the host |
| Image alias | `pandora-e2e`, pointing at `pandora-e2e-<12 hex commit>` |
| Runner labels | `self-hosted`, `Linux`, `X64`, `e2e` |
| Runner and VM names | `e2e-<12 hex>` |

### Lifecycle

1. The pool keeps `idle_target` (1) runners idle and never more than
   `max_vms` (2) VMs.
2. To add one, it launches an ephemeral VM from `pandora-e2e`, waits for the
   Incus agent, mints a JIT configuration for that VM's name, and sends it to
   `/usr/local/libexec/pandora-e2e/start-runner` on stdin. The configuration
   never appears on the host's argv, environment or disk. In the VM it reaches
   the runner as `ACTIONS_RUNNER_INPUT_JITCONFIG`, not as an argument.
3. `pandora-e2e-runner.service` runs `run.sh --jitconfig` as `gh-runner`. The
   runner takes one job and exits. `ExecStopPost` powers the VM off, and Incus
   deletes it. Nothing from the job survives.
4. The next pass sees the VM gone, removes the runner registration if GitHub
   has not, and launches a replacement.
5. A VM whose runner never started (600 s), a runner offline past
   `gone_grace_seconds` (180 s), a job past `job_max_seconds` (2 h, above every
   e2e timeout), and a runner idle past 6 h are deleted and deregistered. A VM
   that Incus fails to delete still counts against `max_vms` until it is gone.
6. A failed launch waits 30 s before the next one, doubling to at most 30 min,
   and resets on a success.

The pool keeps no state of its own. Creation, start and busy times are
`user.e2e.*` keys on each VM, so a restart adopts the running VMs. Stopping
the pool never interrupts a job.

### The image

`build-image.sh` launches a builder VM from `images:ubuntu/26.04`, copies a
Pandora checkout to `/opt/pandora-src`, and runs `vm/setup-vm.sh`:

* `install`: `gh-runner` and `pandora-ci`, sshd, the `e2e-ci` key and
  `~/.config-pandora-e2e.toml` exactly as on the old host, the runner
  (2.338.2, SHA-256 checked), the hook, the runner unit, and
  `pandora worker provision --versions deploy/e2e-runner/worker.toml`.
* A reboot, as every new worker gets.
* `warm`: `worker canary --mark` against the checkout, which builds Pandora's
  golden and writes the ready state, then `selftest --update --queue
  --expect-signed`, which builds the selftest's golden and proves the worker
  signs.
* `seal`: removes the build's client state, proves there is no runner
  registration, and empties `/etc/machine-id` so each VM gets its own.

Every VM therefore boots as a ready worker with warm goldens. The verdict key
is made during the build, so every VM from one image shares that image's key. That key is not in `.github/pandora/allowed_signers`, so an e2e
verdict never lets CI skip the suite.

### The job-started hook

`trusted_job.py` runs as the runner's `ACTIONS_RUNNER_HOOK_JOB_STARTED`,
after GitHub assigns the job and before its first step. A workflow file cannot
change it. It admits `push`, `schedule`, `workflow_dispatch` and
`merge_group`, and `pull_request` only when the head repository is
`gbasin/pandora` (by repository ID when known). It refuses forks, Dependabot,
`pull_request_target`, `workflow_run` and every other event. A refusal kills
every `gh-runner` process, so no step runs, `if: always()` included. In the VM
the runner unit then powers the VM off.

The hook is the boundary that keeps fork code out of the VM. The VM is the
boundary that keeps any job, trusted or not, out of the host.

## Runbook

Run the commands on the operator Mac unless a step says otherwise. Set these
variables once:

```sh
H=frank-operator
REV=<the reviewed commit of this change>
RUNNER=actions.runner.gbasin-pandora.worker-e2e.service
```

### Phase 1: the hook on the current host runner

This phase closes the fork path today. It does not remove the root path for
trusted jobs. Phase 2 does that.

1. Put a root-owned checkout on the host:

   ```sh
   ssh $H "sudo git clone -q https://github.com/gbasin/pandora.git /opt/pandora-e2e/src \
     && sudo git -C /opt/pandora-e2e/src checkout -q --detach $REV"
   ```

2. Install the hook outside `/opt/pandora-e2e`, which Phase 2 makes root-only:

   ```sh
   ssh $H 'sudo install -d -m 0755 -o root -g root /usr/local/libexec/pandora-e2e \
     && sudo install -m 0555 -o root -g root /opt/pandora-e2e/src/deploy/e2e-runner/trusted_job.py \
          /opt/pandora-e2e/src/deploy/e2e-runner/job-started.sh /usr/local/libexec/pandora-e2e/'
   ```

3. Check the verdicts without running a refusal. A refusal kills every
   `gh-runner` process, so never run `job-started.sh` by hand. The first
   payload is a fork, the second a branch of this repository:

   ```sh
   ssh $H 'cd /tmp \
     && printf "%s" "{\"pull_request\":{\"head\":{\"repo\":{\"full_name\":\"x/pandora\"}},\"user\":{\"login\":\"x\"}}}" > e2e-fork.json \
     && printf "%s" "{\"pull_request\":{\"head\":{\"repo\":{\"full_name\":\"gbasin/pandora\"}},\"user\":{\"login\":\"gbasin\"}}}" > e2e-same.json \
     && for f in e2e-fork.json e2e-same.json; do sudo -u gh-runner env -i GITHUB_EVENT_NAME=pull_request \
          GITHUB_REPOSITORY=gbasin/pandora GITHUB_EVENT_PATH=/tmp/$f /usr/bin/python3 -I -c "import sys, os; sys.path.insert(0, \"/usr/local/libexec/pandora-e2e\"); import trusted_job; print(trusted_job.verdict(os.environ))"; done; \
     rm -f e2e-fork.json e2e-same.json'
   ```

   The first line must name `x/pandora` as the head repository. The second
   line must be `None`.

4. Add the hook and an automatic restart to the runner unit, and make the
   runner's `.env` root-owned so a job cannot change the runner's settings:

   ```sh
   ssh $H "sudo install -d /etc/systemd/system/$RUNNER.d \
     && printf '[Service]\nEnvironment=ACTIONS_RUNNER_HOOK_JOB_STARTED=/usr/local/libexec/pandora-e2e/job-started.sh\nRestart=on-failure\nRestartSec=30\n' \
        | sudo tee /etc/systemd/system/$RUNNER.d/10-trusted-jobs.conf >/dev/null \
     && sudo chown root:root /home/gh-runner/actions-runner/.env \
     && sudo chmod 0644 /home/gh-runner/actions-runner/.env \
     && sudo systemctl daemon-reload"
   ```

5. Wait until the runner is idle. This prints `idle` when no job runs:

   ```sh
   ssh $H 'pgrep -u gh-runner -f Runner.Worker >/dev/null && echo busy || echo idle'
   ```

6. Restart the runner: `ssh $H sudo systemctl restart $RUNNER`.
7. Dispatch the e2e workflow: `gh workflow run e2e.yml -R gbasin/pandora --ref main`.
8. Open the run's `Set up runner` or `Set up job` group. It must say that a
   job started hook ran `/usr/local/libexec/pandora-e2e/job-started.sh`. Both jobs must
   pass.

Rollback: remove `/etc/systemd/system/$RUNNER.d/10-trusted-jobs.conf`, run
`sudo systemctl daemon-reload`, and restart the runner when it is idle. The
hook files and the root-owned `.env` can stay.

### Phase 2: one-job VMs

1. Check capacity on the host. The pool needs 8 vCPU and 16 GiB at
   `max_vms = 2`, and a 120 GiB btrfs loop file (sparse) for the VMs and
   images:

   ```sh
   ssh $H 'nproc; grep MemAvailable /proc/meminfo; df -h /var/lib/incus; ip -4 route'
   ```

   Continue only when at least 24 GiB is available, 120 GiB of disk is free,
   and no route overlaps `10.142.0.0/24`. Otherwise set `E2E_SUBNET` in step 4.

2. Update the checkout if `REV` changed since Phase 1:

   ```sh
   ssh $H "sudo git -C /opt/pandora-e2e/src fetch -q origin && sudo git -C /opt/pandora-e2e/src checkout -q --detach $REV"
   ```

3. Create the token. In GitHub, open Settings, Developer settings, Fine-grained
   tokens, Generate new token. Set the resource owner to `gbasin`. Select only
   the `gbasin/pandora` repository. Grant `Administration: Read and write` and
   nothing else. Set an expiry of 90 days. Copy the token, then install it
   from the clipboard:

   ```sh
   pbpaste | ssh $H 'sudo install -d -m 0700 /etc/pandora-e2e \
     && sudo sh -c "umask 077; cat > /etc/pandora-e2e/github-token"'
   pbcopy < /dev/null
   ```

4. Prepare the host. Add each OVH failover IP routed to this server to
   `E2E_EXTRA_EXCLUDE`, separated by spaces. The script already excludes every
   address on the host's interfaces. If the host firewall is installed
   (`/etc/eichler-host-firewall`), the script first writes
   `/etc/eichler-host-firewall/local.d/bridge-pandorae2ebr0.nft`, which adds
   `pandorae2ebr0` to the `guest_bridges` set, and reloads
   `eichler-host-firewall`, so the VMs get DHCP and DNS from the host. Then it
   creates the bridge. If the firewall is installed later, run this step
   again:

   ```sh
   ssh $H 'sudo E2E_EXTRA_EXCLUDE="" /opt/pandora-e2e/src/deploy/e2e-runner/setup-host.sh /opt/pandora-e2e/src'
   ```

5. Build the image. It takes about 15 to 30 minutes:

   ```sh
   ssh $H 'sudo /opt/pandora-e2e/src/deploy/e2e-runner/build-image.sh /opt/pandora-e2e/src'
   ```

   The last line names the alias, the tag and the fingerprint. Record the
   fingerprint.

6. Prove the boundary and the worker in a test VM. Its name is outside the
   pool's `e2e-<12 hex>` pattern, so the pool ignores it:

   `HOST_LL` is the host bridge's IPv6 link-local address. Bash's `/dev/tcp`
   takes the `%eth0` zone in it:

   ```sh
   CTRL_IP=$(ssh $H sudo incus list compute-control --project compute -c 4 -f csv | cut -d' ' -f1)
   HOST_LL=$(ssh $H ip -6 -o addr show dev pandorae2ebr0 scope link | awk '{split($4, a, "/"); print a[1]}')
   echo "${HOST_LL:-no link-local address: drop the last probe}"
   ssh $H "sudo incus --project pandora-e2e launch pandora-e2e e2etest --vm --ephemeral --profile pandora-e2e \
     && until sudo incus --project pandora-e2e exec e2etest -- true 2>/dev/null; do sleep 3; done \
     && sudo incus --project pandora-e2e exec e2etest -- bash -c '
          probe() { timeout 5 bash -c \"</dev/tcp/\$1/\$2\" 2>/dev/null && echo \"REACHED \$1:\$2\" || echo \"blocked \$1:\$2\"; }
          probe api.github.com 443
          probe 5.135.138.35 22
          probe 10.142.0.1 22
          probe 10.4.79.1 22
          probe $CTRL_IP 22
          probe 169.254.169.254 80
          probe smtp.gmail.com 25
          probe $HOST_LL%eth0 22'"
   ```

   Only `api.github.com:443` may say `REACHED`. Then run a signed selftest in
   the same VM:

   ```sh
   ssh $H "sudo incus --project pandora-e2e exec e2etest -- sudo -u gh-runner -H sh -c \
     'cd /opt/pandora-src && bin/pandora --config ~/.config-pandora-e2e.toml selftest --expect-signed --timeout 1800'"
   ssh $H sudo incus --project pandora-e2e delete --force e2etest
   ```

   The selftest must exit 0.

7. Wait until the host runner is idle (Phase 1, step 5). Then stop and
   disable it:

   ```sh
   ssh $H "sudo systemctl disable --now $RUNNER"
   ```

8. Start the pool:

   ```sh
   ssh $H 'sudo systemctl enable --now pandora-e2e-pool.service'
   ```

9. Watch the first pass. Within about two minutes, one `e2e-<12 hex>` VM is
   running and one runner of that name is `online`:

   ```sh
   ssh $H 'sudo journalctl -u pandora-e2e-pool -n 20 --no-pager; sudo cat /run/pandora-e2e/status.json'
   gh api repos/gbasin/pandora/actions/runners --jq '.runners[] | [.name, .status, .busy] | @tsv'
   ```

10. Dispatch the e2e workflow: `gh workflow run e2e.yml -R gbasin/pandora --ref main`.
11. Verify the run. Both jobs pass. Each job's `Set up job` group names an
    `e2e-<12 hex>` runner, and the two names differ. After each job, its VM is
    gone (`ssh $H sudo incus --project pandora-e2e list`) and a new idle VM
    replaces it.
12. Keep the host runner disabled. Continue to Phase 3 after three green runs
    on the pool: this dispatch, one pull request from a branch, and one daily
    schedule.

Rollback, before Phase 3:

```sh
ssh $H 'sudo systemctl disable --now pandora-e2e-pool.service \
  && sudo python3 -I /opt/pandora-e2e/e2e_pool.py drain'
ssh $H "sudo systemctl enable --now $RUNNER"
```

To return to a previous image, point the alias back at its fingerprint. The
pool launches from the alias, so the next VM uses it:

```sh
ssh $H 'sudo incus --project pandora-e2e image alias delete pandora-e2e \
  && sudo incus --project pandora-e2e image alias create pandora-e2e <previous fingerprint>'
```

### Phase 3: remove the root path from the host

Do this phase only after Phase 2, step 12. It cannot be undone by a command.
Rolling back after it means fixing the VM pool forward, not restoring the host
runner.

1. Check what removing `pandora-ci` would take from the live worker. The
   live worker's manifest must not name `pandora-ci` or its home, and no file
   outside `/home/pandora-ci` may belong to it:

   ```sh
   ssh $H 'grep -n pandora-ci /home/ubuntu/pandora/worker/versions.toml || echo "manifest: clean"; \
     sudo find / -xdev -user pandora-ci -not -path "/home/pandora-ci/*" -not -path /home/pandora-ci 2>/dev/null | head'
   ```

   Then list the e2e engine's leftovers in the live project, and check that
   the live worker's gc would remove them. Run the gc as a dry run from a Mac
   that points at the live worker:

   ```sh
   ssh $H "sudo incus --project pandora list -c ns -f csv | grep -v '^golden-' | head"
   pandora worker gc --dry-run
   ```

   Continue only when the manifest is clean, `find` prints nothing, no
   `run-*` instance of the e2e engine is running, and the dry run accounts for
   the e2e selftest golden (removed, or kept on purpose). Write down any
   golden it keeps.
2. Stop the `pandora-ci` user manager, which linger keeps running. Then
   confirm that nothing runs as the two users:

   ```sh
   ssh $H 'sudo loginctl disable-linger pandora-ci; sudo loginctl terminate-user pandora-ci 2>/dev/null; sleep 5; pgrep -u gh-runner,pandora-ci -l || echo none'
   ```

   The output must be `none`.

3. Delete the old registration:

   ```sh
   ID=$(gh api repos/gbasin/pandora/actions/runners --jq '.runners[] | select(.name == "worker-e2e") | .id')
   gh api -X DELETE repos/gbasin/pandora/actions/runners/$ID
   ```

4. Remove the unit, the sudo rule and both users:

   ```sh
   ssh $H "cd /home/gh-runner/actions-runner && sudo ./svc.sh uninstall \
     && sudo rm -rf /etc/systemd/system/$RUNNER.d && sudo systemctl daemon-reload \
     && sudo rm /etc/sudoers.d/pandora-ci && sudo visudo -c \
     && sudo userdel -r gh-runner && sudo userdel -r pandora-ci"
   ```

5. Verify: `ssh $H 'sudo grep -rl pandora-ci /etc/sudoers.d; id pandora-ci; id gh-runner'`
   prints no file and two `no such user` lines.

Instances the old e2e engine left in the live `pandora` project, such as its
selftest golden, belong to the live worker's `pandora worker gc` from now on.
Do not delete them by hand. Step 1 checked that the gc accounts for them.

## Operating

* Kill switch: `sudo systemctl disable --now pandora-e2e-pool.service`, then
  `sudo python3 -I /opt/pandora-e2e/e2e_pool.py drain`. Queued e2e jobs wait
  until the pool runs again. Revoke the token in GitHub to stop all minting.
* Token rotation: replace `/etc/pandora-e2e/github-token` as in Phase 2,
  step 3. The pool reads it on every request, so no restart is needed. Rotate
  before the 90-day expiry. An expired token shows as `HTTP 401` in
  `journalctl -u pandora-e2e-pool`.
* Image rebuild: rebuild after a change to `deploy/e2e-runner/vm/`, to
  `worker.toml`, to the selftest's toolchain, or when GitHub requires a newer
  runner version. Update `RUNNER_VERSION` and `RUNNER_SHA256` in
  `vm/setup-vm.sh` for the last one. Old images stay until you delete them
  with `incus --project pandora-e2e image delete`.
* Pool settings: edit `/etc/pandora-e2e/pool.toml`, then restart the pool.
  [`pool.example.toml`](../deploy/e2e-runner/pool.example.toml) lists every key.

## Residual risk

* A repository writer's job (a branch, a dispatch, the schedule) runs as root
  in its VM. It reaches that job's `GITHUB_TOKEN`, the public internet, and
  nothing on the host or its other VMs. Its traffic leaves through the host's
  IP. `release-please.yml`'s `verify` job holds a `contents: write` token and
  could alter that release's assets, as it could before.
* Leaving the VM needs a QEMU, KVM or Incus agent exploit. That is the
  remaining path to host root.
* The pool's token has `Administration: write` on this repository, which is
  more than runner registration: it can change repository settings. It lives
  only on the host, root 0600, and the pool's client makes only the three
  runner calls. Whoever holds host root already holds everything on it.
* A leaked JIT configuration admits one runner for one job with label `e2e`.
  A leaked token admits runners with any label in this repository until it is
  revoked.
* Third-party actions (`actions/checkout@v4`,
  `softprops/action-gh-release@v2`) are pinned by tag, not commit. They run
  inside the VM.
* Between Phase 1 and Phase 3, the old host runner still exists. In Phase 1
  the hook keeps fork code off it, but a trusted job there still has the root
  path, and the e2e engine still shares the live worker's Incus project.
