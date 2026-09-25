#!/bin/sh
# Make one machine into a Pandora worker, and say what it changed.
#
# POSIX sh on purpose: this is the one piece of Pandora that runs before
# Pandora is on the worker, so it may assume a shell, coreutils, apt and
# passwordless sudo and nothing else. It is fed on stdin by
# `pandora worker provision`, which prepends the variable assignments below.
#
# Every step prints one tab-separated line:
#
#   STEP  <present|created|changed|skipped|failed>  <name>  <detail>
#
# and the closing survey prints `FACT <key> <value>` lines. The caller turns
# those into the report and into drift. Nothing here prints JSON, because
# quoting JSON from sh is how a provisioning script starts lying.
#
# Re-running is the normal case: every step checks before it acts, so a second
# run prints `present` for everything and touches nothing.
set -eu

step() { printf 'STEP\t%s\t%s\t%s\n' "$1" "$2" "$3"; }
fact() { printf 'FACT\t%s\t%s\n' "$1" "$2"; }
I="sudo incus"
P="sudo incus --project $PROJECT"
LIB=/usr/local/lib/pandora
UNITS=/etc/systemd/system
USER_UNITS=$HOME/.config/systemd/user

# --- 1. packages -----------------------------------------------------------
# A pin is a dpkg version; `*` means "present, any version". An unpinned
# package is never upgraded here, because an upgrade nobody asked for is the
# thing `unattended-upgrades = false` exists to prevent.
want_install=''
for spec in $PACKAGES; do
  name=${spec%%=*}; version=${spec#*=}
  have=$(dpkg-query -W -f='${Version}' "$name" 2>/dev/null || true)
  if [ -z "$have" ]; then
    want_install="$want_install $name"
    [ "$version" = '*' ] || want_install="$want_install=$version"
  elif [ "$version" != '*' ] && [ "$have" != "$version" ]; then
    want_install="$want_install $name=$version"
  fi
done
if [ -n "$want_install" ]; then
  sudo apt-get update -qq
  # shellcheck disable=SC2086
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $want_install >/dev/null
  step changed packages "installed:$want_install"
else
  step present packages 'all at the declared versions'
fi

# --- 2. unattended upgrades ------------------------------------------------
# Decision 2 of the direction note: a worker's package set changes when a
# person rebuilds it against a canary, never at 06:00 because a mirror moved.
if [ "$UNATTENDED" = 'false' ]; then
  changed=no
  for unit in unattended-upgrades apt-daily.timer apt-daily-upgrade.timer; do
    if systemctl is-enabled "$unit" >/dev/null 2>&1; then
      sudo systemctl disable --now "$unit" >/dev/null 2>&1 || true
      changed=yes
    fi
  done
  conf=/etc/apt/apt.conf.d/20auto-upgrades
  wanted='APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Unattended-Upgrade "0";'
  if [ "$(cat $conf 2>/dev/null || true)" != "$wanted" ]; then
    printf '%s\n' "$wanted" | sudo tee "$conf" >/dev/null
    changed=yes
  fi
  [ "$changed" = yes ] && step changed unattended-upgrades 'disabled' \
                       || step present unattended-upgrades 'already disabled'
else
  step skipped unattended-upgrades 'left as the image had it'
fi

# --- 3. directory layout ---------------------------------------------------
made=''
for d in "$ROOT" "$ROOT/bin" "$ROOT/worker" "$ROOT/worker/receipts" \
         "$ENGINE_ROOT" "$ENGINE_ROOT/runs" "$ENGINE_ROOT/src"; do
  [ -d "$d" ] || { mkdir -p "$d"; made="$made $d"; }
done
[ -n "$made" ] && step created layout "$made" || step present layout "$ROOT, $ENGINE_ROOT"
# The admission floor lives beside the ledger rather than in the manifest the
# engine would otherwise have to find: the engine reads one number, on a path
# it already owns, on the hot path of every submission.
if [ "$(cat "$ENGINE_ROOT/disk_floor" 2>/dev/null || true)" = "$DISK_FLOOR_GIB" ]; then
  step present disk-floor "${DISK_FLOOR_GIB}GiB"
else
  printf '%s\n' "$DISK_FLOOR_GIB" > "$ENGINE_ROOT/disk_floor"
  step changed disk-floor "${DISK_FLOOR_GIB}GiB"
fi
if [ "$(cat "$ENGINE_ROOT/run_disk_gib" 2>/dev/null || true)" = "$RUN_DISK_GIB" ]; then
  step present run-disk-quota "${RUN_DISK_GIB}GiB per run"
else
  printf '%s\n' "$RUN_DISK_GIB" > "$ENGINE_ROOT/run_disk_gib"
  step changed run-disk-quota "${RUN_DISK_GIB}GiB per run"
fi

# --- 4. the pool's backing device -----------------------------------------
# A real deploy passes DEVICE=/dev/sdb and none of the loop machinery runs.
# A box with no spare device gets a loop file plus a systemd unit that
# re-attaches it at boot, because `losetup` by hand is POC blocker 4.
sudo mkdir -p "$LIB"
if [ -n "$DEVICE" ]; then
  BACKING=$DEVICE
  step skipped pool-device "real device $DEVICE, no loop file"
else
  BACKING=''
  # Adopt before creating. A worker whose pool already exists is backed by some
  # file somewhere, and minting a second one beside it would leave the unit
  # attaching an empty image at boot while the real pool stayed unattached --
  # which is the failure this whole step exists to prevent.
  # findmnt names the filesystem by UUID symlink, which `losetup` will not take.
  mounted=$(findmnt -n -o SOURCE "/var/lib/incus/storage-pools/$POOL" 2>/dev/null | head -1 || true)
  mounted=$(readlink -f "$mounted" 2>/dev/null || true)
  adopted=$(losetup -n -O BACK-FILE "$mounted" 2>/dev/null | head -1 || true)
  if [ -n "$adopted" ] && [ -f "$adopted" ] && [ "$adopted" != "$POOL_FILE" ]; then
    step present pool-file-adopted "$adopted (the pool's existing backing file)"
    POOL_FILE=$adopted
  fi
  if [ ! -f "$POOL_FILE" ]; then
    truncate -s "${LOOP_GIB}G" "$POOL_FILE"
    step created pool-file "$POOL_FILE ${LOOP_GIB}G"
  else
    step present pool-file "$POOL_FILE"
  fi
  attach=$LIB/pool-attach
  wanted_attach='#!/bin/sh
# Attach the pool file to any free loop device. Which number it lands on does
# not matter: Incus records the pool by filesystem UUID, not by device path.
set -eu
f=$1
[ -f "$f" ] || { echo "no pool file $f" >&2; exit 1; }
modprobe loop 2>/dev/null || true
if losetup -j "$f" | grep -q .; then exit 0; fi
losetup --find "$f"'
  if [ "$(cat "$attach" 2>/dev/null || true)" != "$wanted_attach" ]; then
    printf '%s\n' "$wanted_attach" | sudo tee "$attach" >/dev/null
    sudo chmod 755 "$attach"
    step changed pool-attach "$attach"
  else
    step present pool-attach "$attach"
  fi
  unit=$UNITS/pandora-pool.service
  wanted_unit="[Unit]
Description=Pandora storage pool backing device
After=local-fs.target
Before=incus.service incus.socket incus-startup.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$attach $POOL_FILE
[Install]
WantedBy=multi-user.target"
  if [ "$(cat "$unit" 2>/dev/null || true)" != "$wanted_unit" ]; then
    printf '%s\n' "$wanted_unit" | sudo tee "$unit" >/dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable pandora-pool.service >/dev/null 2>&1
    step changed pandora-pool.service 'written and enabled'
  elif ! systemctl is-enabled pandora-pool.service >/dev/null 2>&1; then
    sudo systemctl enable pandora-pool.service >/dev/null 2>&1
    step changed pandora-pool.service 'enabled'
  else
    step present pandora-pool.service 'enabled'
  fi
  sudo systemctl start pandora-pool.service >/dev/null 2>&1 || true
  BACKING=$(losetup -j "$POOL_FILE" | cut -d: -f1 | head -1)
  [ -n "$BACKING" ] || { step failed pool-device 'loop attach produced no device'; exit 3; }
fi

# --- 5. incus database and the pool ---------------------------------------
# This script arrives on the worker's stdin (`sh -s`). `incus ... create` reads
# a YAML body from stdin whenever stdin is not a terminal, so every create here
# takes </dev/null or Incus swallows the rest of this script as its config.
$I admin init --minimal </dev/null >/dev/null 2>&1 || true
if $I storage show "$POOL" >/dev/null 2>&1; then
  step present pool "$POOL"
else
  # An existing btrfs filesystem on the device is adopted rather than
  # recreated, so re-provisioning a worker never eats its goldens.
  $I storage create "$POOL" btrfs source="$BACKING" </dev/null >/dev/null
  step created pool "$POOL on $BACKING"
fi
sudo btrfs quota enable "/var/lib/incus/storage-pools/$POOL" >/dev/null 2>&1 || true

# --- 6. bridge and the forwarding rules ------------------------------------
if $I network show "$BRIDGE" >/dev/null 2>&1; then
  step present bridge "$BRIDGE"
else
  $I network create "$BRIDGE" ipv4.address="$SUBNET" ipv4.nat=true ipv6.address=none </dev/null >/dev/null
  step created bridge "$BRIDGE $SUBNET"
fi
rules=$LIB/net-rules
wanted_rules='#!/bin/sh
# The host FORWARD policy is DROP with only Docker'"'"'s jumps installed, so the
# managed bridge needs two explicit ACCEPTs. iptables rules do not survive a
# reboot, which is why this is a unit and not a line in a setup script.
set -eu
b=$1
i=0
while [ $i -lt 60 ]; do
  incus network list >/dev/null 2>&1 && ip link show "$b" >/dev/null 2>&1 && break
  i=$((i+1)); sleep 1
done
for d in -i -o; do
  iptables -C FORWARD $d "$b" -j ACCEPT 2>/dev/null || iptables -I FORWARD $d "$b" -j ACCEPT
done'
if [ "$(cat "$rules" 2>/dev/null || true)" != "$wanted_rules" ]; then
  printf '%s\n' "$wanted_rules" | sudo tee "$rules" >/dev/null
  sudo chmod 755 "$rules"
  step changed net-rules "$rules"
else
  step present net-rules "$rules"
fi
netunit=$UNITS/pandora-net.service
wanted_netunit="[Unit]
Description=Pandora bridge forwarding rules
After=incus.service incus-startup.service network-online.target
Wants=incus-startup.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$rules $BRIDGE
[Install]
WantedBy=multi-user.target"
if [ "$(cat "$netunit" 2>/dev/null || true)" != "$wanted_netunit" ]; then
  printf '%s\n' "$wanted_netunit" | sudo tee "$netunit" >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable pandora-net.service >/dev/null 2>&1
  step changed pandora-net.service 'written and enabled'
elif ! systemctl is-enabled pandora-net.service >/dev/null 2>&1; then
  sudo systemctl enable pandora-net.service >/dev/null 2>&1
  step changed pandora-net.service 'enabled'
else
  step present pandora-net.service 'enabled'
fi
sudo systemctl start pandora-net.service >/dev/null 2>&1 || true

# --- 7. project and profile ------------------------------------------------
if $I project show "$PROJECT" >/dev/null 2>&1; then
  step present project "$PROJECT"
else
  $I project create "$PROJECT" </dev/null >/dev/null -c features.images=true -c features.profiles=true \
     -c features.storage.volumes=true -c features.networks=false
  step created project "$PROJECT"
fi
$P profile show "$PROFILE" >/dev/null 2>&1 || $P profile create "$PROFILE" </dev/null >/dev/null
$P profile device add "$PROFILE" root disk path=/ pool="$POOL" >/dev/null 2>&1 || true
$P profile device add "$PROFILE" eth0 nic network="$BRIDGE" name=eth0 >/dev/null 2>&1 || true
profile_changed=no
for kv in security.nesting=true security.syscalls.intercept.mknod=true \
          security.syscalls.intercept.setxattr=true security.idmap.isolated=true; do
  key=${kv%%=*}; value=${kv#*=}
  have=$($P profile get "$PROFILE" "$key" 2>/dev/null || true)
  if [ "$have" != "$value" ]; then
    $P profile set "$PROFILE" "$key=$value" >/dev/null
    profile_changed=yes
  fi
done
[ "$profile_changed" = yes ] && step changed profile "$PROFILE nesting+idmap defaults" \
                             || step present profile "$PROFILE"

# --- 8. the engine service -------------------------------------------------
# A user unit, with linger, because the engine owns files in the worker user's
# home and a system unit would have to be told which user that is twice.
boot=$ROOT/bin/engine-boot
wanted_boot="#!/bin/sh
# What has to be true before the first run of the day is admitted: the layout
# exists, the pool is mounted, and any attempt whose supervisor died with the
# machine is closed rather than left looking alive.
set -eu
ROOT=$ROOT
ENGINE_ROOT=$ENGINE_ROOT
mkdir -p \"\$ENGINE_ROOT/runs\" \"\$ENGINE_ROOT/src\" \"\$ROOT/worker/receipts\"
i=0
while [ \$i -lt 120 ]; do
  sudo incus --project $PROJECT list >/dev/null 2>&1 && break
  i=\$((i+1)); sleep 1
done
b=\$(ls -1dt \"\$ENGINE_ROOT\"/bundles/*/ 2>/dev/null | head -1)
if [ -n \"\$b\" ]; then
  cd \"\$b\" && PYTHONPATH=\"\$b\" python3 -m pandora.engine.service \\
    --root \"\$ENGINE_ROOT\" reconcile > \"\$ROOT/worker/boot-reconcile.json\" 2>&1 || true
fi
date -Is > \"\$ROOT/worker/booted\""
if [ "$(cat "$boot" 2>/dev/null || true)" != "$wanted_boot" ]; then
  printf '%s\n' "$wanted_boot" > "$boot"
  chmod 755 "$boot"
  step changed engine-boot "$boot"
else
  step present engine-boot "$boot"
fi
# The engine's one long-lived process: turbo's remote cache for runs, on the
# runs' bridge only (`pandora.engine.turbocache`). It runs from the newest
# shipped bundle, so it needs one to exist; until the first run ships it, the
# unit exits 75 and systemd retries.
serve=$ROOT/bin/engine-serve
wanted_serve="#!/bin/sh
set -eu
b=\$(ls -1dt \"$ENGINE_ROOT\"/bundles/*/ 2>/dev/null | head -1)
[ -n \"\$b\" ] || { echo 'no engine bundle shipped yet' >&2; exit 75; }
cd \"\$b\"
PYTHONPATH=\"\$b\" exec python3 -m pandora.engine.turbocache --root \"$ENGINE_ROOT\" serve --bridge $BRIDGE"
if [ "$(cat "$serve" 2>/dev/null || true)" != "$wanted_serve" ]; then
  printf '%s\n' "$wanted_serve" > "$serve"
  chmod 755 "$serve"
  step changed engine-serve "$serve"
else
  step present engine-serve "$serve"
fi
mkdir -p "$USER_UNITS"
wanted_engine="[Unit]
Description=Pandora engine: boot reconcile, then turbo's remote cache for runs
After=default.target
[Service]
Type=simple
ExecStartPre=$boot
ExecStart=$serve
Restart=always
RestartSec=5
[Install]
WantedBy=default.target"
if [ "$(cat "$USER_UNITS/pandora-engine.service" 2>/dev/null || true)" != "$wanted_engine" ]; then
  printf '%s\n' "$wanted_engine" > "$USER_UNITS/pandora-engine.service"
  systemctl --user daemon-reload
  systemctl --user enable pandora-engine.service >/dev/null 2>&1
  # A oneshot that already ran reads as active; the new shape has to start.
  systemctl --user restart pandora-engine.service >/dev/null 2>&1 || true
  step changed pandora-engine.service 'written, enabled and restarted'
elif ! systemctl --user is-enabled pandora-engine.service >/dev/null 2>&1; then
  systemctl --user enable pandora-engine.service >/dev/null 2>&1
  step changed pandora-engine.service 'enabled'
else
  step present pandora-engine.service 'enabled'
fi
if [ "$(loginctl show-user "$WORKER_USER" -p Linger --value 2>/dev/null || true)" = 'yes' ]; then
  step present linger "$WORKER_USER"
else
  sudo loginctl enable-linger "$WORKER_USER"
  step changed linger "enabled for $WORKER_USER"
fi
systemctl --user start pandora-engine.service >/dev/null 2>&1 || true

# --- 9. the manifest -------------------------------------------------------
# Both sides through a command substitution: it strips trailing newlines, and
# comparing a stripped file against an unstripped variable never matches.
if [ "$(cat "$ROOT/worker/versions.toml" 2>/dev/null || true)" = "$(printf '%s' "$MANIFEST")" ]; then
  step present manifest "$MANIFEST_DIGEST"
else
  printf '%s' "$MANIFEST" > "$ROOT/worker/versions.toml"
  step changed manifest "$MANIFEST_DIGEST"
fi

# --- 10. survey ------------------------------------------------------------
for spec in $PACKAGES; do
  name=${spec%%=*}
  fact "package.$name" "$(dpkg-query -W -f='${Version}' "$name" 2>/dev/null || echo -)"
done
fact host "$(hostname)"
fact kernel "$(uname -r)"
fact cores "$(nproc)"
fact memory_mib "$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1024 ))"
fact incus_version "$(incus --version 2>/dev/null || echo -)"
fact pool.backing "${BACKING:-$DEVICE}"
fact pool.present "$($I storage show "$POOL" >/dev/null 2>&1 && echo yes || echo no)"
fact project.present "$($I project show "$PROJECT" >/dev/null 2>&1 && echo yes || echo no)"
fact bridge.present "$($I network show "$BRIDGE" >/dev/null 2>&1 && echo yes || echo no)"
fact unit.pandora-pool "$(systemctl is-enabled pandora-pool.service 2>/dev/null || echo -)"
fact unit.pandora-net "$(systemctl is-enabled pandora-net.service 2>/dev/null || echo -)"
fact unit.pandora-engine "$(systemctl --user is-enabled pandora-engine.service 2>/dev/null || echo -)"
fact unattended-upgrades "$(systemctl is-enabled unattended-upgrades 2>/dev/null || echo disabled)"
fact linger "$(loginctl show-user "$WORKER_USER" -p Linger --value 2>/dev/null || echo -)"
fact forward.rules "$(sudo iptables -S FORWARD | grep -c -- "$BRIDGE" || true)"
fact root "$ROOT"
fact engine_root "$ENGINE_ROOT"
