#!/usr/bin/env bash
# Build the guest rootfs ext4 image: Dockerfile -> docker export -> mke2fs -d.
# This is the "repo toolchain image -> VM rootfs" pipeline, done the cheap way.
set -euo pipefail
SPIKE=${SPIKE:-$HOME/spike-fc}
SRC=${SRC:-$SPIKE/scripts}
KVER=${KVER:-$(uname -r)}
SIZE=${SIZE:-6G}
OUT=$SPIKE/img/rootfs.ext4
D="sudo docker"

cd "$SRC"
[ -f authorized_keys ] || cp "$HOME/.ssh/fcspike.pub" authorized_keys

t0=$(date +%s.%N)
$D build -f Dockerfile.rootfs -t fcspike/rootfs:base . >/dev/null
t1=$(date +%s.%N)
echo "docker build: $(echo "$t1-$t0" | bc)s"

rm -f "$SPIKE/img/rootfs.tar"
cid=$($D create fcspike/rootfs:base /bin/true)
$D export "$cid" -o "$SPIKE/img/rootfs.tar"
$D rm "$cid" >/dev/null
t2=$(date +%s.%N)
echo "docker export: $(echo "$t2-$t1" | bc)s  $(du -h "$SPIKE/img/rootfs.tar" | cut -f1)"

# unpack + graft the host's kernel modules (guest runs the host's kernel image)
sudo rm -rf "$SPIKE/img/rootdir"; mkdir -p "$SPIKE/img/rootdir"
sudo tar -xf "$SPIKE/img/rootfs.tar" -C "$SPIKE/img/rootdir"
sudo mkdir -p "$SPIKE/img/rootdir/lib/modules"
sudo cp -a "/lib/modules/$KVER" "$SPIKE/img/rootdir/lib/modules/"
# docker needs these at boot; depmod was already run by the distro on the host
printf 'overlay\nbr_netfilter\nip_tables\niptable_nat\niptable_filter\nnf_nat\nxt_conntrack\nxt_addrtype\nxt_MASQUERADE\nveth\nbridge\n' \
  | sudo tee "$SPIKE/img/rootdir/etc/modules-load.d/docker.conf" >/dev/null
echo '/dev/root / ext4 defaults 0 1' | sudo tee "$SPIKE/img/rootdir/etc/fstab" >/dev/null
# docker bind-mounts /etc/{resolv.conf,hosts,hostname} during build, so `docker
# export` writes them out EMPTY. An empty /etc/hosts makes `localhost` unresolvable,
# which is exactly how eichler's workerd failed the first time.
sudo rm -f "$SPIKE/img/rootdir/etc/resolv.conf"
printf 'nameserver 8.8.8.8\nnameserver 1.1.1.1\n' | sudo tee "$SPIKE/img/rootdir/etc/resolv.conf" >/dev/null
printf '127.0.0.1\tlocalhost\n::1\tlocalhost ip6-localhost ip6-loopback\n' \
  | sudo tee "$SPIKE/img/rootdir/etc/hosts" >/dev/null
echo 'fcvm' | sudo tee "$SPIKE/img/rootdir/etc/hostname" >/dev/null

rm -f "$OUT"
sudo mke2fs -q -t ext4 -d "$SPIKE/img/rootdir" -L rootfs "$OUT" "$SIZE"
sudo chown "$USER" "$OUT"
t3=$(date +%s.%N)
echo "mke2fs -d: $(echo "$t3-$t2" | bc)s"
ls -l "$OUT"; du -h --apparent-size "$OUT" | cut -f1
