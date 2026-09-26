---
status: log
---
# One Firecracker microVM per run, 2026-09-21

Acme's own `journey-runner.mjs` booted acme's own Compose stack on a
dockerd **inside a Firecracker microVM** and ran journey `S0-01` to a pass, with
no Docker API proxy, no label scheme, no port brokering and no acme change of
any kind. Two restored clones of one snapshot ran the same journey at the same
time, each with its own Postgres on its own `127.0.0.1:5432`. `SIGKILL` of the
firecracker process removed 3 containers and 105 processes instantly and left
exactly four named host objects behind, all of them created by Pandora and known
before the run started.

The same workload also ran in an **Incus unprivileged system container** with
nesting, in 62.7 s against the microVM's 141.4 s, cloning in 0.35 s against the
microVM's 0.54 s, and deleting mid-run in 1.1 s. That result changes the
recommendation; see §11.

Host: OVHcloud b3-16 (OpenStack), Ubuntu 26.04, kernel 7.0.0-14-generic, AMD
EPYC-Milan, 4 vCPU, 15 GiB RAM, ext4 root, nested virt present (`/dev/kvm`,
`vmx|svm` on all 4 CPUs). Another agent was running a Docker-proxy spike against
the host dockerd throughout; none of its objects were touched, and the host
dockerd's configuration was not changed.

Everything below is under `experiments/firecracker/`.

---

## 1. What was built

| File | What it does |
| --- | --- |
| `extract-vmlinux.py` | carves the uncompressed ELF kernel out of the distro bzImage |
| `Dockerfile.rootfs` + `build-rootfs.sh` | Dockerfile → `docker export` → `mke2fs -d` guest image |
| `fc-net`, `fc-net.service` | guest network/hostname from one kernel-cmdline token |
| `fc-workspace`, `fc-workspace.service` | mounts the per-run disks by filesystem label |
| `fcvm.py` | boot, ssh, snapshot, kill, RSS — one firecracker process per run |
| `mkworkspace.sh` | the workspace hand-off options, each timed |
| `dmsnap.sh` | device-mapper copy-on-write over a shared read-only warm base |
| `warm-prep.sh` | turns a source disk into a warm base (deps + pre-pulled images) |
| `run-journey.sh`, `measure-run.sh` | the measured end-to-end run |
| `snapclone.sh` | snapshot one VM, restore N clones, each in its own netns + mntns |
| `clone-journey.sh` | N clones running `S0-01` at once |
| `cleanup.sh` | inventory every host object the spike created, and reap it |
| `incus-setup.sh`, `incus-trial.sh` | the Incus system-container comparison |

### 1.1 Kernel: do not build one, carve the distro's

Firecracker on x86_64 boots only an uncompressed ELF `vmlinux`; every distro
ships a compressed bzImage. Building a kernel on a 4-vCPU box is an hour, and
what you get is *your* config, not a config known to run Docker.

The host kernel's config already has everything:

```
CONFIG_VIRTIO_MMIO=y  CONFIG_VIRTIO_BLK=y  CONFIG_VIRTIO_NET=y   (built in)
CONFIG_OVERLAY_FS=m  CONFIG_BRIDGE=m  CONFIG_VETH=m
CONFIG_NF_TABLES=m  CONFIG_IP_NF_IPTABLES=m  CONFIG_NETFILTER_XT_MATCH_ADDRTYPE=m
```

so `extract-vmlinux.py /boot/vmlinuz-$(uname -r) vmlinux` (0.38 s; found a zstd
payload at offset `0x52cc`, 71,001,072 bytes) plus a copy of `/lib/modules/$(uname -r)`
(173 MiB) into the rootfs gives a guest that is byte-for-byte the host's kernel
with the host's modules. `CONFIG_ACPI=y` means Firecracker v1.17's ACPI device
discovery works with no `virtio_mmio.device=` cmdline. The guest `uname -r`
reports `7.0.0-14-generic`, `docker info` reports overlay2 and cgroup v2, and
`lsmod` shows overlay/br_netfilter/veth/bridge loaded from
`/etc/modules-load.d/docker.conf`.

Nothing had to be compiled. This is the cheapest part of the whole design and it
was the part expected to be hardest.

### 1.2 Rootfs: a Dockerfile, exported

`Dockerfile.rootfs` is an ordinary image — `ubuntu:26.04`, systemd, `docker.io`
29.1.3, `docker-compose-v2`, `docker-buildx`, node 24.9.0 from nodejs.org, pnpm
12.3.4, git, python3, openssh. `build-rootfs.sh` builds it, `docker export`s a
container of it, grafts in `/lib/modules`, and writes the tree straight into an
ext4 image with `mke2fs -d` (no loop mount, no root needed beyond `mke2fs`).

This is the answer to "how would a repo-specific toolchain image be declared":
it is a Dockerfile, and the conversion to a rootfs is three commands. The
`[runtime]` table of `notes/repo-config-contract-draft.md` §3.2 — `base_image` +
`setup` + `env` — maps onto it unchanged.

**Two bugs worth writing down, because they will bite anyone who does this.**

1. Docker bind-mounts `/etc/resolv.conf`, `/etc/hosts` and `/etc/hostname` into
   the build container, so `docker export` writes all three out **empty**. An
   empty `/etc/hosts` makes `localhost` unresolvable. That is exactly how the
   first real run died: `workerd` failed with
   `kj/async-io-unix.c++:1293: failed: DNS lookup failed.; params.host = localhost`,
   every `/harness/health` answered 500, and the journey timed out. `build-rootfs.sh`
   now writes all three files into the exported tree.
2. `/dev/disk/by-label/*` is a udev race at early boot. `fc-workspace` ran, found
   nothing, and mounted no workspace at all while `lsblk` a second later showed
   the labels fine. Use `blkid -L <label>`, which scans the devices directly.

Pipeline cost, measured:

| Step | Cold | Warm (layer cache) |
| --- | --- | --- |
| `docker build` | 24.8 s | 1.1 s |
| `docker export` (960 MiB tar) | 5.2 s | 4.7–7.3 s |
| `mke2fs -d` → 6 GiB image | 5.4 s | 5.9–6.0 s |
| **total** | **~35 s** | **~13 s** |

Resulting image: 6 GiB apparent, **1.3 GiB on disk**.

### 1.3 Network

Per-VM tap `fctap<N>` with a /30 (`172.30.N.1` host, `172.30.N.2` guest), one
MASQUERADE rule and two FORWARD ACCEPTs per run, all additive and all removable.
The guest configures itself from a single kernel-cmdline token,
`fcnet=172.30.N.2/30,172.30.N.1,vmN`, read by a 20-line `fc-net` script — no
DHCP, no cloud-init, no `ip=` kernel support needed (Ubuntu has no
`CONFIG_IP_PNP`). Egress worked immediately: image pulls and the npm registry
both reachable from inside the guest.

Exec is `sshd` over that tap. vsock was not needed; `CONFIG_VIRTIO_VSOCKETS=m` is
present if a guest agent is preferred later.

---

## 2. Workspace hand-off without virtiofs

Acme: **375 MiB, 4,852 tracked files** (shipped with
`git ls-files -z | rsync --from0 --files-from=-`, 90 s over the wire, tracked
files only). Warm base adds `node_modules` 1.2 GiB, pnpm store 29 MiB, and a
docker data root with the three stack images pre-pulled, 510 MiB. Host root is
ext4: **no reflink**.

| Option | Time | Bytes on disk |
| --- | --- | --- |
| (a) `mke2fs -d` source snapshot into a 1.2 GiB image | **1.52 s** | 409 MiB |
| (a) same into a 9 GiB image | **0.84 s** | 446 MiB |
| blank ext4 scratch, 4 GiB (overlay upper / outputs) | **0.03 s** | 68 MiB |
| (b1) `cp --sparse=always` of the 2.3 GiB warm base | **2.98 s** | **2.2 GiB per run** |
| (b2) **device-mapper snapshot** of the warm base | **0.06–0.13 s** | 8 KiB at start; **145 MiB** written by a full `S0-01` |
| (b3) guest overlayfs: read-only base + scratch disk | 0.03 s (the scratch) | scratch only |

Warm base itself: 9 GiB apparent, **2.3 GiB on disk**.

**`mke2fs -d` is fast enough that a per-run source snapshot needs no cleverness
at all**: 1.5 s and 409 MiB for the whole of acme. The interesting question is
only the 1.4 GiB of dependencies, and there `dm-snapshot` wins outright — 0.06 s
and a COW file that grows by what the run actually writes.

**The guest-overlayfs option (b3) is a trap**: Docker's `overlay2` storage driver
refuses an overlayfs backing filesystem, so a warm base carrying
`/work/docker-root` cannot be handed over as an overlay lower. It works for
source and `node_modules` and not for the docker image cache, which is the part
you most want warm. `dm-snapshot` gives a real read-write ext4 and has neither
problem. `fc-workspace` supports both and selects by label.

**What btrfs or xfs-reflink on the host would change**: `cp --reflink=always` of
the warm base would land between the two — near-instant like dm-snapshot, but
producing an ordinary file rather than a device that has to be created, chowned
and removed. It removes the `dmsetup`/`losetup` lifecycle (see §7 — that
lifecycle leaked a loop device once during this spike). Incus on a btrfs pool
showed what this looks like: clone in **0.08 s**, zero extra bytes, one command.
On this host, `dm-snapshot` is the ext4 substitute for reflink and costs about
15 lines.

**Getting outputs back.** The run gets a third disk labelled `out`; after
shutdown the host mounts it read-only and copies. The mount-and-copy took
**0.36 s**. *Partially verified*: the disk mounted and copied cleanly but came
back empty — the runner's report did not land on it and I did not diagnose why
before moving on. Reading a block device after shutdown is mechanically fine;
that one wiring bug is unresolved. **NOT RUN:** copying outputs over vsock or
ssh while the run is live.

---

## 3. `S0-01` end to end in a microVM

Warm path: dm-snapshot of the warm base as the workspace, 4 vCPU, 6144 MiB,
outputs disk attached.

```
{"label":"warm-s001","exit":0,"disk_prep_s":0.3,"boot_to_ssh_s":7.77,
 "journey_s":132.44,"output_readback_s":0.36,"total_s":141.4,
 "fc_peak_rss_mib":3149.1,"configured_mem_mib":6144,"cow_written_mib":145}
```

```
S0-01: pass 89.9s replayed
stage S0: 1 pass, 0 fail, 0 not-yet-implemented
stage-routes: 45 observed, 45 selected, 45 replayed, 45/45 covered by passing replays
```

Phase breakdown:

| Phase | Seconds |
| --- | --- |
| disk prep (dm-snapshot + outputs disk + tap + NAT) | 0.30 |
| cold boot → sshd | 7.77 |
| sshd → `docker info` answers (cold `systemctl start docker`) | 5.09 |
| `docker` ready → journey exit (compose up, migrations, 45 stage-routes, teardown) | 132.44 |
| read the outputs disk back on the host | 0.36 |
| **total** | **141.4** |

`systemd-analyze` inside the guest: 6.6 s userspace, `graphical.target` at 6.6 s.

The mid-run guest state is the whole argument:

```
Stack app-validation-6da1a395: API http://127.0.0.1:36651, Postgres 127.0.0.1:32768.
```

`wrangler dev --local` is a host process in the guest; Postgres is a container in
the same guest; they talk over the guest's own loopback. There is no netns seam,
because the run *is* the host. Separately, with acme's plain `compose.yml`
(fixed `127.0.0.1:5432:5432` and `127.0.0.1:5433:80`, no ephemeral override) the
stack came up and `ss -ltn` showed both ports bound — in a VM, fixed ports are
free, and two runs on fixed ports do not collide.

**Memory.** Configured 6144 MiB, host RSS of the firecracker process peaked at
**3149 MiB**. Inside the guest, `MemAvailable` fell from 5.64 GiB to 4.58 GiB, so
the run's own working set was about **1.1 GiB**; the other ~2 GiB of host RSS is
guest page cache (reading `node_modules`, the docker image layers) that the guest
never frees and the host cannot tell apart from anonymous memory. **This is the
memory-rigidity problem in one number: 1.1 GiB of demand costs 3.1 GiB of host
RSS and reserves 6 GiB.** Against the proxy POC's measurement — 86 MiB peak for
the three services — a microVM is roughly a 30× accounting markup on the same
workload, because it also pays for the guest kernel, systemd, dockerd and the
guest's file cache.

**Cold path.** Not run as one wall clock; the components are:
`mke2fs -d` source disk 1.5 s + boot 7.8 s + dockerd 5.1 s + `pnpm install
--frozen-lockfile` **56.7 s** (cold, no store) + three image pulls **≈100 s**
(approximate — read off log timestamps, not separately instrumented) + the
journey. So a genuinely cold run is roughly **+160 s** over the warm one, which
is why the warm base exists.

---

## 4. Snapshot and restore

Snapshot taken of a VM with dockerd up **and acme's compose stack running and
healthy** (postgres healthy, pgbouncer, wsproxy, fixed ports bound):

```
snapshot  pause=0.01s  create=4.99s  cowcopy=0.03s   mem=4.1G  snap=28K  cow=44M
```

Restore:

```
clone2  disk=0.11s  net=0.15s  spawn=0.04s  load=0.02s  to_ssh=0.22s  total=0.54s
clone3  disk=0.11s  net=0.16s  spawn=0.04s  load=0.02s  to_ssh=0.13s  total=0.47s
```

**Restore-to-ready is 0.5 s, with three containers already running and Postgres
already healthy.** Against the warm cold-boot path — 7.8 s boot + 5.1 s dockerd +
however long `compose up --wait` and migrations take — that is the single biggest
number in this document.

Both clones then ran `S0-01` concurrently and both passed:

```
clone3  rc=0  wall=141.9s     S0-01: pass 84.2s replayed   45/45 covered
clone2  rc=0  wall=145.0s     S0-01: pass 88.0s replayed   45/45 covered
```

Two concurrent runs cost ~5 % on journey time versus one alone on a 4-vCPU host,
so this workload is not CPU-bound at two lanes.

### 4.1 What restoring N clones actually requires

The Firecracker API has `network_overrides` on `/snapshot/load`, and that is all
the help you get. Everything else about "this clone is not the VM I snapshotted"
is the caller's problem:

1. **The drive path is baked into the snapshot and has no override.** Loading a
   clone whose device is `/dev/mapper/fcrun2` fails with
   `Error restoring MMIO devices: Block: ... No such file or directory /dev/mapper/fcrun1`.
   Fix used here: run each clone under `unshare -m` with its own device
   bind-mounted over the recorded path. The jailer's chroot solves it the same
   way; this is the same trick without the chroot.
2. **The disk must match the memory.** The clone's block device is a fresh
   `dm-snapshot` over the same read-only base with a **copy of the source VM's
   COW file taken while the VM was paused** (0.03 s, 44 MiB here). Get this wrong
   and you have a kernel whose page cache disagrees with its disk.
3. **Every clone wakes up as the source.** Same MAC, same IP, same hostname, same
   `/etc/machine-id`:

   | | clone 2 | clone 3 |
   | --- | --- | --- |
   | `hostname` | `vm1` | `vm1` |
   | `/etc/machine-id` | `a0b8e047…b786` | `a0b8e047…b786` (identical) |
   | `date` | 21:10:58 | 21:10:48 (host: 21:25:11) |
   | 16 bytes of `/dev/urandom` | `bb16319d…` | `ba4ffb86…` (differ) |

   So each clone got its own **network namespace** with the tap and addresses
   reused verbatim. Hostname and machine-id collisions did not break acme
   (the stack names its project from a content hash, not the hostname) but they
   would break anything that registers by hostname.
4. **The clock is frozen at the snapshot instant** — 14 minutes behind the host
   when the clone was used. `clone-journey.sh` steps it with `date -s` before the
   run; anything checking token expiry would fail otherwise. Firecracker's own
   guidance is the same.
5. **Entropy.** The two clones' first `/dev/urandom` reads differed, but they
   start from an identical pool state. Firecracker has a virtio-rng device;
   a serious deployment should attach it and reseed after restore.

### 4.2 Costs and what was not tried

- **The memory file is the configured `mem_size`, not the working set.** A
  4096 MiB VM produces a 4.1 GiB `mem.file`. Ten warm templates is 41 GiB. The
  snapshot proper is 28 KiB.
- **NOT RUN: diff snapshots.** `track_dirty_pages` is wired into `fcvm.py` but
  never exercised. This is the documented fix for the file size (base + small
  diffs).
- **NOT RUN: UFFD.** The memory backend used was `File`, which means the host
  page-cache maps the whole file per clone. A UFFD handler is the documented way
  to share one memory image across many clones without N copies of the
  page-cache cost, and is what makes "N clones from one snapshot" cheap in
  memory rather than merely fast. **Without it, the 0.5 s restore is real but the
  memory saving is not.**
- **NOT RUN: the jailer.** Downloaded, never used. Every VM here ran as the
  unprivileged `ubuntu` user in the `kvm` group, with `sudo` only for tap, NAT
  and device-mapper.

---

## 5. Concurrency, resources, kill

**Concurrency.** Two microVMs at 4096 MiB each on a 15 GiB / 4-vCPU host ran
`S0-01` simultaneously to a pass (§4). Host RSS about 1.1 GiB each at journey
start. No port collisions are possible: each guest has its own loopback. **NOT
RUN: 4–20 concurrent runs** — the host has 4 vCPU and 15 GiB, and two 6 GiB VMs
already reserve most of it.

**Memory.** `mem_size_mib` is fixed at boot and the guest will use all of it for
page cache. A balloon device is declared in `fcvm.py` (`deflate_on_oom`, stats
polling) but was **NOT RUN**. The honest position: with a fixed `mem_size`,
"admit on learned peak usage" admits against a number ~3× the run's real demand,
because the guest's file cache is indistinguishable from its working set from
outside. A balloon can return memory, but only when something inside the guest
decides to give it up, so the reservation the scheduler must hold is still the
configured size unless Pandora runs a balloon agent that shrinks aggressively —
which then costs the run its page cache, i.e. the warm-disk advantage.

**Hard memory cap.** A microVM's cap is structural: the guest physically cannot
address more than `mem_size`, and an OOM inside it kills a guest process, never
a host process and never another run. This is strictly better than a cgroup cap
and needs no enforcement code. **NOT RUN: an actual in-guest OOM.**

**CPU.** `vcpu_count` fixes the number of vCPU threads; host `cpu.weight` over
those threads is the soft-CPU knob. **NOT RUN.**

**Kill and cleanup.** VM 2 was running acme's stack (3 containers, 105
processes in the guest). `kill -9` of the firecracker pid:

| | Before | After kill | After reap |
| --- | --- | --- | --- |
| guest containers | 3 | — | — |
| guest processes | 105 | — | — |
| host processes matching `postgres` | 0 | **0** | 0 |
| firecracker process | 1 | 0 | 0 |
| netns `fcns2` | 1 | **1** | 0 |
| dm device `fcrun2` | 1 | **1** | 0 |
| loop devices | 2 | **2** | 0 |
| COW file | 1 | **1** | 0 |

**A run's containers are not host objects, so there is nothing to sweep.** What
survives a `SIGKILL` is a fixed set of four host objects that Pandora created
itself and named after the run: a netns, a dm device, its loop devices and a COW
file. `cleanup.sh inventory` lists them by class and the final inventory after
teardown is `(none)` on every line. This replaces the label sweep entirely: there
is no enumeration of unknown objects, and no possibility of a run creating
something Pandora cannot find, because a run cannot create host objects at all.

One honest failure: an aborted clone attempt left `/dev/loop1 -> /tmp/fcrun2.cow
(deleted)` behind. `losetup` and `dmsetup` have their own lifecycle and must be
reaped explicitly; on a reflink filesystem this whole class of leak disappears.

---

## 6. Incus system containers — the cheaper cousin

Same property as a microVM (the run owns a private dockerd, localhost just works,
no API proxy) with no VM and no KVM. Incus 6.0.5 from the Ubuntu 26.04 archive,
btrfs pool on a loop file, its own bridge `incusbrfc`, host dockerd untouched.

**Docker runs in an unprivileged container on kernel 7.0.** Three profile keys,
no more:

```
security.nesting=true
security.syscalls.intercept.mknod=true
security.syscalls.intercept.setxattr=true
```

Inside: `docker.io` 29.1.3, **Storage Driver: overlayfs** (real overlay2, not
fuse-overlayfs, not vfs), Cgroup Driver systemd, Cgroup Version 2. No AppArmor
denials for docker.

Two environment gotchas, neither Incus's fault: the managed bridge's dnsmasq
answers AAAA while the bridge is IPv4-only, so `apt` and `curl` needed forced
IPv4; and the host's `FORWARD` policy is `DROP` with only Docker's jumps
installed, so the Incus bridge needed explicit `-I FORWARD -i/-o incusbrfc -j
ACCEPT`.

### Numbers

| Step | Time | Notes |
| --- | --- | --- |
| launch `images:ubuntu/26.04` | 0.38 s | 5.6 s including the first image download |
| toolchain (docker, compose, node 24, pnpm 12.3.4, git, python3) | 21.7 s | |
| acme source in, `tar` through `incus exec` | 2.2 s | 375 MiB, no image build |
| `pnpm install --frozen-lockfile` + 3 image pulls | 19.0 s | pnpm alone: "Done in 11s" |
| stop + `incus snapshot create` | 0.9 s | |
| **golden instance, total** | **44.2 s** | vs ~35 s for the microVM rootfs build *plus* a separate 160 s warm-disk prep |
| `incus copy fc-golden/warm <name>` (btrfs) | **0.08 s** | snapshot, ~0 extra bytes |
| `incus start` → ready | **0.27–0.31 s** | |
| `systemctl start docker` → `docker info` | **0.21 s** | |
| `S0-01` in a clone | **62.7 s** wall, journey 50.3 s | exit 0, 45/45 stage-routes |
| two clones concurrently | 62.67 s and 62.70 s | both pass, 45/45 each |
| instance cgroup `memory.peak` during the run | **2,896 MiB** | |
| `incus delete -f` **mid-run** | **1.1 s** | instance + its 3 containers + subvolume gone |

Limits, read straight out of the cgroup:

| Incus setting | cgroup result |
| --- | --- |
| `limits.memory=1GiB`, `enforce=soft` | `memory.high=1073741824`, `memory.max=max` |
| `limits.memory=1GiB`, `enforce=hard` | `memory.max=1073741824`, `memory.high=max` |
| `limits.cpu.allowance=50%` | `cpu.weight=50`, `cpu.max` unlimited |
| `limits.cpu.priority=5` (allowance unset) | `cpu.weight=95` |

The percentage form of `limits.cpu.allowance` is a **weight**, not a quota —
exactly the "soft CPU by weight" model the owner chose. The `100ms/200ms` form
sets `cpu.max` instead (not exercised).

**One bad result, stated plainly.** With `memory.max=512MiB` a node loop
allocating 64 MiB buffers hit the limit **349,230 times in 120 s with
`oom_kill 0`**: it livelocked in reclaim instead of being killed. The host stayed
healthy (8.9 GiB available throughout) and the container stayed `RUNNING`, so
containment held — but the run neither finished nor died. A cgroup hard cap is
not self-terminating; Pandora would need a watchdog on `memory.events` /
`memory.stat` pressure, or `memory.oom.group`. A microVM has no equivalent
failure mode: the guest simply has no more RAM and its own OOM killer fires.

**Incus can also run QEMU VMs.** `incus launch --vm` worked out of the box:
51.6 s launch-to-exec including a 1.6 GiB image download, and the guest's own
boot was **18.8 s** (7.8 s kernel + 2.8 s initrd + 8.3 s userspace) against
Firecracker's 6.3–8.3 s to sshd. So the same API gives a hardware-isolation tier
later at roughly 2.4× the boot cost — which is the strongest argument for
starting with Incus. **NOT RUN:** virtiofs sharing of the workspace into an Incus
VM.

### The other cousins — NOT RUN

- **Sysbox.** `sysbox-ce` is maintained (Nestybox → Docker), but it installs as a
  **Docker runtime**, which requires editing `/etc/docker/daemon.json` and
  restarting the host dockerd. The other agent was using that daemon, so this was
  deliberately not attempted. Ubuntu 26.04 / kernel 7.0 support is unverified;
  Sysbox historically tracks kernel changes closely (it needs shiftfs or
  idmapped mounts) and 26.04 is not on its tested matrix as far as I know.
  Incus gets the same "docker inside, unprivileged" property with no host dockerd
  change at all, which is why it was done instead.
- **Privileged `docker:dind` in a plain container.** Not run. It would give the
  private dockerd, but `--privileged` is full host access — strictly worse than
  both alternatives here — and it still needs a per-run `/var/lib/docker` volume
  and an image-cache story (registry mirror or pre-seeded layer store) that Incus
  gets for free from the golden instance.
- **Kata Containers, gVisor.** Not run. Kata is another Docker/containerd runtime
  (same host-dockerd constraint); gVisor's syscall surface does not run dockerd
  well and is aimed at a different threat model.

---

## 7. What Firecracker deletes from the design, and what it adds

### Deletes

| Gone | Evidence |
| --- | --- |
| **The Docker API proxy, ~1,152 production LOC + 884 test** | a run talks to its own dockerd over its own socket; there is nothing to police |
| **The 0–300 unbuilt LOC for the Linux netns question** | `wrangler` reached `127.0.0.1:32768` in the guest and the journey passed; option 3 of the proxy note (an acme seam saying "I share your network") is unnecessary |
| **The label scheme and `sweep`** | a run creates no host objects; kill the process and 3 containers plus 105 processes are gone at once |
| **Port-collision handling** | fixed ports `127.0.0.1:5432`/`:5433` bound inside two guests at once |
| **Tracking the Docker client's API surface** | a Compose upgrade that adds a call is not Pandora's problem any more; this was named as the proxy's main ongoing maintenance cost |
| **The proxy's security caveats** | "policy, not containment" becomes actual containment: a separate kernel per run |
| **Empty cgroup slices accumulating** | no cgroup slices |
| **The declared-services alternative entirely** | no service schema, no pinned digests restated from `compose.yml`, no `validation-stack.mjs` textual surgery, no drift |

### Adds

| New | Cost measured here |
| --- | --- |
| **KVM requirement** | see §8 — this is the hard one |
| **A kernel + rootfs image pipeline** | ~35 s cold / ~13 s warm, and `extract-vmlinux.py` (60 lines) |
| **A disk pipeline** | `dmsnap.sh` + `mkworkspace.sh`, ~80 lines; on a reflink filesystem most of it disappears |
| **A guest agent** | sshd here; ~0 lines, but it is a component |
| **tap + NAT per run** | 5 iptables/ip commands per run, removable |
| **Per-run boot cost** | 7.8 s to sshd + 5.1 s to dockerd, or 0.5 s from a snapshot |
| **Memory rigidity** | 1.1 GiB of demand → 3.1 GiB host RSS → 6 GiB reserved |
| **Debugging ergonomics** | the run's filesystem is inside a block device inside a dm-snapshot; an operator cannot `ls` a failed workspace without mounting the COW. There is no host-side process tree to inspect, no `docker logs`. The serial console log is the only thing available when the guest fails before sshd, and it is where both of this spike's real bugs were found. |
| **Clone bookkeeping** | netns + mntns per clone, clock step, machine-id/hostname collisions, memory-file size |
| **Host object lifecycle** | `dmsetup`/`losetup` leaked once in this spike |

---

## 8. Requirements and ops

**Nested virtualisation by provider class.** Verified here: this OVHcloud b3-16
OpenStack VM exposes `/dev/kvm` and all four CPUs report `svm`. The rest is
recalled, not tested in this spike, and should be checked before it becomes a
plan:

- **AWS EC2, non-metal: no.** VMX/SVM is not exposed on ordinary instance types;
  Firecracker needs a `*.metal` instance. This is the expensive one — the
  cheapest metal instances are an order of magnitude above a 4-vCPU VM.
- **GCP: yes on most instance types**, via the nested-virtualisation licence on
  the image (N1/N2/N2D/C2/C3 families, Intel Haswell+ / AMD).
- **Azure: yes on v3-and-later series** (Dv3/Ev3 onward).
- **Hetzner dedicated (AX/EX): yes** — bare metal, so KVM is native. **Hetzner
  Cloud (CX/CPX/CCX): no.**
- **OVHcloud b3: yes** (measured).

The practical consequence: "Firecracker per run" quietly constrains where the
worker can live, and on the most common cloud it constrains it to bare metal. An
Incus system container runs anywhere Linux runs.

**Privileges.** Firecracker itself needs only `/dev/kvm` (group `kvm`). The
spike's `sudo` use is tap creation, NAT rules, `dmsetup`/`losetup` and mounting
the outputs disk — all Pandora's own operations, none of them the run's. The
jailer adds a chroot, cgroup and seccomp wrapper; **NOT RUN**, and for a
mutually-trusted small team it is optional, though it is also the clean answer to
the drive-path problem in §4.1.

**arm64.** Not tested (this host is x86_64). Firecracker supports aarch64; the
kernel story is *simpler* there because aarch64 Linux boots an uncompressed
`Image` natively, so `extract-vmlinux.py` is unnecessary. The rootfs pipeline is
architecture-neutral. Apple-silicon developer machines cannot host it (no KVM);
that is a Linux-worker design either way.

**Repo-specific toolchain.** A Dockerfile, converted by `build-rootfs.sh`. The
`[runtime]` table of the repo-config draft describes it already. Per-repo warm
bases are `mke2fs -d` + one prep run, and per-run is a dm-snapshot.

---

## 9. Side by side

| | Docker API proxy (POC) | **Firecracker per run** | **Incus system container per run** |
| --- | --- | --- | --- |
| Pandora LOC | 1,152 prod + 884 test, plus 0–300 unbuilt for the netns question | ~600 across 14 scripts here; no protocol handling | ~120 in one script; the rest is `incus` |
| Acme LOC | 0, or "a handful" for the network seam | **0** | **0** |
| `S0-01` result | pass (on Mac, loaded host) | **pass**, 45/45 stage-routes | **pass**, 45/45 stage-routes |
| `S0-01` wall | 710.8 s (load avg 53, not comparable) | **141.4 s** (journey 89.9 s) | **62.7 s** (journey 50.3 s) |
| Time to first command | n/a | 7.8 s cold, 0.5 s from snapshot | **0.3 s** from a clone |
| dockerd ready | host daemon, always | +5.1 s cold, 0 s from snapshot | **0.21 s** |
| Per-run warm hand-off | none needed | dm-snapshot 0.06 s / 145 MiB written | btrfs clone **0.08 s** / ~0 bytes |
| Fixed ports across runs | **collide** (proxy does not rewrite publishing) | free | free |
| Host-process→container localhost | **unsolved on Linux** | free | free |
| Accounting | exact per container, 86 MiB measured | whole run, but 3.1 GiB host RSS for 1.1 GiB of demand | whole run, `memory.peak` = 2,896 MiB |
| Hard memory cap | cgroup; OOM kills a host process | **structural** — guest cannot address more | cgroup; contained but **livelocked** rather than killed in this test |
| Soft CPU | cgroup weight | host `cpu.weight` on vCPU threads (NOT RUN) | `limits.cpu.allowance=50%` → `cpu.weight=50` |
| Cleanup after SIGKILL | label sweep, receipt, 150 LOC | kill the process; 4 named host objects remain | `incus delete -f`, **1.1 s**, nothing remains |
| What a run can reach | the worker's kernel, network, filesystem | **its own kernel only** | the host kernel, user-namespaced |
| Requires | nothing | **KVM** (see §8) | nothing |
| Ongoing maintenance | the Docker client's API surface | kernel + rootfs image pipeline | an `incus` version |
| Debugging a failed run | `docker logs`, host `ps` | serial console + mount the COW | `incus exec`, `incus file pull` |
| Upgrade path to hardware isolation | none | already there | `incus launch --vm`, same API, 18.8 s boot |

---

## 10. Failure modes found, in order of how much time they cost

1. **`docker export` empties `/etc/hosts`.** Cost: one full failed `S0-01`.
   `localhost` unresolvable, `workerd` 500s, journey timeout.
2. **`/dev/disk/by-label` races udev at early boot.** Cost: two boots with no
   workspace mounted and a confusing "docker root is `/var/lib/docker`".
3. **Firecracker does not exit when the guest powers off.** A `poweroff`'d guest
   left the process alive, still holding the warm base open, spamming
   `Net: The device is not yet activated` once a second. The host must reap the
   process; do not treat guest shutdown as run completion.
4. **Firecracker restores a drive at the recorded path with no override** (§4.1).
5. **`overlay2` refuses an overlayfs backing**, so the guest-overlay hand-off
   cannot carry a warm docker image cache.
6. **`/dev/mapper/*` is `root:disk 0660`**; the unprivileged firecracker process
   needs it chowned, per run.
7. **`losetup` leaked** a device pointing at a deleted COW file.
8. **Incus `memory.max` livelocked** rather than OOM-killing (§6).

---

## 11. Recommendation

**Do not adopt Firecracker for this. Adopt an Incus system container per run, and
keep Firecracker as the second tier behind the same abstraction.**

The spike answered its question in Firecracker's favour on every property that
was in doubt, and then a cheaper thing answered it better:

- **The netns question that blocked the proxy is genuinely dead** in both
  designs. `wrangler dev --local` talking to a Compose Postgres over
  `127.0.0.1` worked unmodified in a microVM and in a system container, with
  fixed ports, with two runs at once. Whatever Pandora does next, it should not
  be the 200–300 lines of port brokering the proxy note costed, and it should
  not be declared services.
- **Cleanup stops being a problem worth code.** The proxy needs a label, a sweep
  and a receipt because a run's containers are the worker's containers. When the
  run owns its own dockerd, a `SIGKILL` takes the containers with it and the
  residue is a fixed list Pandora wrote down when it created the run. Firecracker
  leaves four host objects; Incus leaves none after a 1.1 s `delete -f`.
- **Firecracker's advantages are real but narrow for this user.** A structural
  memory cap, a separate kernel, and 0.5 s restore-to-ready with a booted stack
  are genuine. But the stated context is *a few mutually-trusted users* on *one
  16–32 core box*, and against that context: containment buys little (the proxy
  note already concedes a run that wants the worker's secrets does not need
  Docker to get them), the memory cap costs a 3× reservation markup, and the
  0.5 s restore is undercut by a 0.35 s clone that needs no memory file, no
  mount namespace, no clock step and no UFFD work to be cheap.
- **Firecracker's costs are broad.** It requires KVM, which on the most common
  cloud means bare metal. It requires a kernel and rootfs pipeline, a disk
  pipeline, a guest agent and a tap/NAT lifecycle — about 600 lines here, less
  than the proxy but not nothing, and every one of §10's eight failure modes was
  in that machinery rather than in the workload. And it makes debugging a failed
  run materially worse, which for a tool whose whole job is telling an agent why
  its tests failed is not a small thing.
- **The workload ran 2.3× faster in a container** (62.7 s vs 141.4 s), on the
  same host, from the same source, with the same runner. That is not a tuning
  gap to close later; it is virtio-blk, a second kernel and a second page cache.

The order of work I would propose:

1. **Build the executor seam first**, with two implementations behind it:
   `prepare workspace → start unit → exec argv with env → stream → exit code →
   collect outputs → destroy`. Everything measured here fits that seam, and so
   does the current container executor.
2. **Ship the Incus implementation.** Golden instance per dependency key (44 s),
   clone per run (0.35 s), `limits.memory` with `enforce=hard` plus a watchdog on
   `memory.events` because §6 shows the cap alone does not terminate,
   `limits.cpu.allowance=<N>%` for soft CPU by weight, `incus delete -f` for
   cleanup. Admission reads `memory.peak` from the instance cgroup, which is the
   "learned peak" input the owner wanted, measured per run with no proxy.
3. **Keep this Firecracker work as the hardware-isolation tier**, to be turned on
   when the trust assumption changes — untrusted repos, a hostile PR, or paying
   customers. Before it ships, three things here are unfinished and must be:
   diff snapshots, a UFFD memory backend, and the balloon. Without them the
   snapshot story is fast but not memory-cheap.
4. **Do not resurrect the Docker API proxy.** Its 1,152 lines exist to make a
   shared daemon safe to hand to a repository. Both designs in this note delete
   the shared daemon. Keep only what the proxy note itself recommends keeping —
   per-run usage accounting — and take it from the run's cgroup instead: one file
   read, `memory.peak`, no daemon involved.

The one result that would change this recommendation: if `incus copy` of a warm
instance proved unsound under real concurrency, or if a repo turned out to need
something `security.nesting` cannot give (a custom kernel module, a different
kernel version, `/dev/fuse` gymnastics), the microVM path is built and measured
and step 3 becomes step 2.

---

## 12. Cleanup

Created and removed. Final `cleanup.sh inventory`:

```
== firecracker processes  (none)
== fctap devices          (none)
== iptables nat           (none)
== iptables filter        (none)
== device-mapper          (none)
== loop devices           (none)
== mounts                 (none)
== cow files              (none)
```

`ip netns list` is empty. Incus was purged (`apt-get purge incus incus-base
incus-client`, `/var/lib/incus` removed, `incusbr0` deleted, its FORWARD rules
removed); `which incus` finds nothing. The `fcspike/rootfs:base` image was
removed from the host dockerd. The `ubuntu` user was added to the `kvm` group
and that was left in place.

Left behind under `~/spike-fc` (about 6 GiB): the firecracker binaries, the
extracted `vmlinux`, `rootfs.ext4`, `warmbase.ext4`, the snapshot, the logs and
the scripts. `~/pandora-warm` was **not** removed — disk never went below 19 GiB
free. The other agent's `~/spike-proxy`, its containers and the host dockerd's
configuration were not touched.
