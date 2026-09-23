# Rebuild a Pandora worker

This is the procedure. Follow it in order. Do not skip the canary.

A worker is disposable. Nothing on it is the only copy of anything: the ledger
records attempts, the source cache is a cache, and the goldens rebuild from
their toolchain descriptions. Rebuild the worker rather than repair it.

## 1. Prepare a fresh virtual machine

1. Create an Ubuntu 26.04 VM with at least 4 vCPU, 15 GiB of memory and 96 GB
   of disk.
2. Attach a spare block device for the storage pool. Use at least 40 GB.
3. Add your SSH key to the `ubuntu` user.
4. Confirm passwordless sudo. Run `ssh ubuntu@<ip> sudo -n true`.
5. Record the new host address. You need it in step 2 and step 5.

Do not install Incus by hand. Step 2 installs it at the declared version.

## 2. Write the versions manifest

1. Copy `scripts/versions.toml` to a file for this worker.
2. Set `device` to the spare block device, for example `/dev/sdb`.
3. Set `loop_size_gib` only if there is no spare device. See "No spare device".
4. Pin `incus` and `incus-client` to exact dpkg versions.
5. Leave `unattended_upgrades = false`. See "Upgrade cadence".
6. Set `disk_floor_gib` to the pool free space below which new runs are
   refused. Four GiB is the default.
7. Set `run_disk_gib` to the per-run root quota. This limits *referenced*
   bytes, which include the golden's. A 4 GiB golden under a 12 GiB quota
   gives the run about 8 GiB of its own writes.
8. Set `golden_keep` to the number of goldens kept per repository.

### Example

```toml
[packages]
incus = "6.0.5-8"
incus-client = "6.0.5-8"
btrfs-progs = "*"
git = "*"
rsync = "*"
python3 = "*"

[worker]
root = "~/pandora"
engine_root = "~/pandora-engine"
project = "pandora"
pool = "pandorapool"
profile = "runner"
bridge = "pandorabr0"
subnet = "10.141.0.1/24"
user = "ubuntu"

device = "/dev/sdb"
loop_size_gib = 18

disk_floor_gib = 4
run_disk_gib = 12
golden_keep = 2
unattended_upgrades = false
```

A `*` means "present, any version". Any other value is an exact dpkg version,
and a near-miss is reported as drift rather than accepted as a match.

## 3. Provision

Run this from the control machine.

```sh
pandora worker provision \
  --host ubuntu@<new-ip> \
  --versions ./versions.toml \
  --journey scripts/toolchains/eichler-journeys.json \
  --surfaces scripts/toolchains/eichler-surfaces.json \
  --source /home/ubuntu/pandora-engine/src/eichler/latest
```

`provision` installs the declared packages, disables unattended upgrades,
creates the pool on the device, creates the bridge and its forwarding rules,
creates the `pandora` project and the `runner` profile, enables the boot units,
writes the manifest, and then runs the canary.

The first run on a fresh machine builds both goldens. Allow 20 minutes. A
golden build downloads the base image, installs the toolchain, runs
`pnpm install --frozen-lockfile` and pulls the service images.

Read the report. Every step says `present`, `created`, `changed` or `skipped`.

Run `provision` again. The second run must report `0 changed`. If it does not,
a step is not idempotent. Do not continue.

### No spare device

Pass `--loop-file 18G` instead of setting `device`. `provision` then creates a
sparse file under the layout root and a `pandora-pool.service` unit that
re-attaches it to a loop device before Incus starts. The pool is recorded by
filesystem UUID, so the loop number does not have to be stable.

A loop file is slower and it is one more thing that can fail at boot. Use a
real device on any worker that takes real work.

## 4. Prove the worker

`provision` runs the canary and writes the ready state from its verdict. Check
the verdict yourself.

1. Confirm the canary reports `pass` with `0 failure(s)`.
2. Confirm `pandora worker status --host ubuntu@<new-ip>` reports `state:
   ready`.
3. Confirm `status` reports no drift lines.
4. Confirm both goldens are listed with a size.

A worker that fails the canary is never marked `ready`. Do not cut over to it.

Reboot the new worker once before you cut over. Run
`sudo reboot`, wait, then run `pandora worker status` again. The pool, the
goldens and the forwarding rules must all come back. This is the one failure a
canary cannot see, because a canary runs on a machine that is already up.

## 5. Cut over

1. Stop submitting new work. Tell the people using it, or stop their daemons.
2. Refuse new admissions on the old worker. Run
   `pandora worker --host <old> gc --dry-run` first to see what is live, then
   set the floor above the pool's free space so admission closes:
   `ssh <old> 'echo 999 > ~/pandora-engine/disk_floor'`.
   Submissions now answer `disk-floor` with the arithmetic, and the client
   falls back to a local run.
3. Wait for the running attempts to finish. Poll
   `pandora worker --host <old> stats` until `held_mib` is 0 and `running` is
   empty.
4. Point the client at the new worker. Edit `[worker] host` in
   `~/.config/pandora/config.toml`.
5. Restart the client daemon. Run `pandora daemon --restart` if launchd runs it
   (`pandora daemon --install`). Otherwise stop it and run `pandora daemon`.
6. Run one real command end to end. Confirm it lands on the new worker.
7. Retire the old worker. Run `pandora worker --host <old> reconcile` to close
   any attempt whose supervisor is gone, then destroy the VM.

Keep the old worker for one working day before destroying it. A cut-over that
has to be reversed is reversed by editing one line back.

## 6. Keep it

Run `pandora worker gc --dry-run` weekly. Read what it would remove. Run
`pandora worker gc` when you agree with it. The sweep removes leaked run
instances, leaked storage volumes and goldens past the keep count. It never
removes a golden a live attempt needs, and it writes a receipt under
`<root>/worker/receipts/`.

Run `pandora worker status` after any manual change to the worker. Drift
between the manifest and the machine makes `status` report `drifted` rather
than `ready`, whatever the last canary said, because that canary ran against a
different machine.

## Upgrade cadence

Unattended upgrades are off. A worker's package set changes when a person
rebuilds it, never at 06:00 because a mirror moved. A run that passed yesterday
and fails today must have changed because the code changed.

The cadence:

* **Monthly.** Refresh the pins in `versions.toml` to the current archive
  versions. Rebuild a worker with them. Run the canary. Cut over if it passes.
* **On a CVE with a known exploit in Incus, the kernel or btrfs-progs.**
  Rebuild inside one working day. Do not patch the running worker in place: a
  patched worker no longer matches its manifest, and its last canary result is
  about a different machine.
* **Never on a schedule the worker decides for itself.**

Rebuild rather than upgrade. The whole procedure above is under 30 minutes of
waiting and about 5 minutes of attention, and it ends with a canary. An
in-place upgrade ends with a machine nobody has proved.
