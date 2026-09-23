#!/usr/bin/env bash
# Snapshot a booted, warmed VM and restore clones from it.
#
# Two things have to line up for a clone:
#  * the DISK must be in exactly the state the memory snapshot saw, so the clone
#    gets a new device-mapper snapshot over the same read-only base with a COPY
#    of the source VM's COW file, taken while the VM was paused;
#  * every clone wakes up believing it is the source: same guest MAC, same IP,
#    same hostname, same machine-id, same clock. So each clone runs in its own
#    NETWORK namespace (tap and addresses reused verbatim) and its own MOUNT
#    namespace (its device bind-mounted over the path the snapshot recorded,
#    because Firecracker has network_overrides on load but no drive override).
#
#   take        <src-n> <name>   pause src, snapshot, copy its COW, resume
#   clone       <name> <dst-n>   restore one clone
#   ssh         <dst-n> <cmd..>  run a command in a clone (enters its netns)
#   clean-clone <dst-n>          remove everything the clone created
set -euo pipefail
SPIKE=${SPIKE:-$HOME/spike-fc}
cd "$SPIKE"
cmd=$1

case "$cmd" in
take)
  n=$2; name=$3
  mkdir -p "snap/$name"
  t0=$(date +%s.%N)
  curl -s --unix-socket "run/vm$n/api.sock" -X PATCH http://localhost/vm \
       -H 'Content-Type: application/json' -d '{"state":"Paused"}'
  t1=$(date +%s.%N)
  curl -s --unix-socket "run/vm$n/api.sock" -X PUT http://localhost/snapshot/create \
       -H 'Content-Type: application/json' \
       -d "{\"snapshot_path\":\"$SPIKE/snap/$name/snap.file\",\"mem_file_path\":\"$SPIKE/snap/$name/mem.file\",\"snapshot_type\":\"Full\"}"
  t2=$(date +%s.%N)
  cp --sparse=always "/tmp/fcrun$n.cow" "snap/$name/disk.cow"
  t3=$(date +%s.%N)
  curl -s --unix-socket "run/vm$n/api.sock" -X PATCH http://localhost/vm \
       -H 'Content-Type: application/json' -d '{"state":"Resumed"}'
  printf 'snapshot\tpause=%.2fs\tcreate=%.2fs\tcowcopy=%.2fs\tmem=%s\tsnap=%s\tcow=%s\n' \
    "$(echo "$t1-$t0"|bc)" "$(echo "$t2-$t1"|bc)" "$(echo "$t3-$t2"|bc)" \
    "$(du -h "snap/$name/mem.file"|cut -f1)" "$(du -h "snap/$name/snap.file"|cut -f1)" \
    "$(du -h "snap/$name/disk.cow"|cut -f1)"
  ;;

clone)
  name=$2; d=$3
  ns="fcns$d"
  t0=$(date +%s.%N)
  sudo dmsetup remove "fcrun$d" 2>/dev/null || true
  cp --sparse=always "snap/$name/disk.cow" "/tmp/fcrun$d.cow"
  bl=$(sudo losetup --find --show --read-only img/warmbase.ext4)
  cl=$(sudo losetup --find --show "/tmp/fcrun$d.cow")
  sectors=$(sudo blockdev --getsz "$bl")
  sudo dmsetup create "fcrun$d" --table "0 $sectors snapshot $bl $cl P 8"
  echo "$bl $cl" > "/tmp/fcrun$d.loops"
  sudo chown "$USER" "/dev/mapper/fcrun$d"
  t1=$(date +%s.%N)

  sudo ip netns del "$ns" 2>/dev/null || true
  sudo ip link del "fcveth$d" 2>/dev/null || true
  sudo ip netns add "$ns"
  sudo ip link add "fcveth$d" type veth peer name "fcvpeer$d"
  sudo ip link set "fcvpeer$d" netns "$ns"
  sudo ip addr add "192.168.$((100+d)).1/30" dev "fcveth$d"
  sudo ip link set "fcveth$d" up
  sudo ip netns exec "$ns" ip addr add "192.168.$((100+d)).2/30" dev "fcvpeer$d"
  sudo ip netns exec "$ns" ip link set "fcvpeer$d" up
  sudo ip netns exec "$ns" ip link set lo up
  sudo ip netns exec "$ns" ip route add default via "192.168.$((100+d)).1"
  sudo ip netns exec "$ns" ip tuntap add "fctap$d" mode tap
  sudo ip netns exec "$ns" ip addr add 172.30.1.1/30 dev "fctap$d"
  sudo ip netns exec "$ns" ip link set "fctap$d" up
  sudo ip netns exec "$ns" sysctl -qw net.ipv4.ip_forward=1
  sudo ip netns exec "$ns" iptables -t nat -A POSTROUTING -o "fcvpeer$d" -j MASQUERADE
  sudo iptables -t nat -C POSTROUTING -s "192.168.$((100+d)).0/30" -j MASQUERADE 2>/dev/null || \
    sudo iptables -t nat -A POSTROUTING -s "192.168.$((100+d)).0/30" -j MASQUERADE
  t2=$(date +%s.%N)

  mkdir -p "run/vm$d"; rm -f "run/vm$d/api.sock"
  kvmgid=$(getent group kvm | cut -d: -f3)
  sudo ip netns exec "$ns" unshare -m sh -c "
      mount --bind /dev/mapper/fcrun$d /dev/mapper/fcrun1
      exec setpriv --reuid=$(id -u) --regid=$(id -g) --groups=$(id -g),$kvmgid \
        $SPIKE/bin/firecracker --api-sock $SPIKE/run/vm$d/api.sock" \
    > "run/vm$d/console.log" 2>&1 &
  for _ in $(seq 1 500); do [ -S "run/vm$d/api.sock" ] && break; sleep 0.01; done
  t3=$(date +%s.%N)

  out=$(curl -s --unix-socket "run/vm$d/api.sock" -X PUT http://localhost/snapshot/load \
        -H 'Content-Type: application/json' -d "{
 \"snapshot_path\":\"$SPIKE/snap/$name/snap.file\",
 \"mem_backend\":{\"backend_type\":\"File\",\"backend_path\":\"$SPIKE/snap/$name/mem.file\"},
 \"enable_diff_snapshots\":false,\"resume_vm\":true,
 \"network_overrides\":[{\"iface_id\":\"eth0\",\"host_dev_name\":\"fctap$d\"}]}")
  t4=$(date +%s.%N)
  [ -n "$out" ] && echo "load says: $out"

  for _ in $(seq 1 600); do
    sudo ip netns exec "$ns" timeout 1 bash -c '(echo > /dev/tcp/172.30.1.2/22)' 2>/dev/null && break
    sleep 0.05
  done
  t5=$(date +%s.%N)
  pgrep -f "run/vm$d/api.sock" | head -1 > "run/vm$d/pid"
  printf 'clone%s\tdisk=%.2fs\tnet=%.2fs\tspawn=%.2fs\tload=%.2fs\tto_ssh=%.2fs\ttotal=%.2fs\n' \
    "$d" "$(echo "$t1-$t0"|bc)" "$(echo "$t2-$t1"|bc)" "$(echo "$t3-$t2"|bc)" \
    "$(echo "$t4-$t3"|bc)" "$(echo "$t5-$t4"|bc)" "$(echo "$t5-$t0"|bc)"
  ;;

ssh)
  d=$2; shift 2
  exec sudo ip netns exec "fcns$d" sudo -u "$USER" ssh -i "$HOME/.ssh/fcspike" \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
    root@172.30.1.2 "$@"
  ;;

clean-clone)
  d=$2
  if [ -f "run/vm$d/pid" ]; then sudo kill -9 "$(cat "run/vm$d/pid")" 2>/dev/null || true; fi
  sudo iptables -t nat -D POSTROUTING -s "192.168.$((100+d)).0/30" -j MASQUERADE 2>/dev/null || true
  sudo ip netns del "fcns$d" 2>/dev/null || true
  sudo ip link del "fcveth$d" 2>/dev/null || true
  sudo dmsetup remove "fcrun$d" 2>/dev/null || true
  if [ -f "/tmp/fcrun$d.loops" ]; then
    for l in $(cat "/tmp/fcrun$d.loops"); do sudo losetup -d "$l" 2>/dev/null || true; done
    rm -f "/tmp/fcrun$d.loops"
  fi
  rm -f "/tmp/fcrun$d.cow"
  echo "clone $d cleaned"
  ;;
esac
