#!/usr/bin/env bash
# Host-side copy-on-write without reflink: device-mapper snapshot of the warm
# base. The base stays read-only and shared; each run gets its own sparse COW
# file. The resulting /dev/mapper/<name> is handed to Firecracker as a drive.
#   up <name> <base.img> <cow-size> ; down <name>
set -euo pipefail
cmd=$1; name=$2
case "$cmd" in
up)
  base=$3; cowsize=$4
  t0=$(date +%s.%N)
  sudo modprobe dm-snapshot
  bl=$(sudo losetup --find --show --read-only "$base")
  truncate -s "$cowsize" "/tmp/$name.cow"
  cl=$(sudo losetup --find --show "/tmp/$name.cow")
  sectors=$(sudo blockdev --getsz "$bl")
  # 'P' = persistent, 8 = 4 KiB chunk
  sudo dmsetup create "$name" --table "0 $sectors snapshot $bl $cl P 8"
  t1=$(date +%s.%N)
  echo "$bl $cl" > "/tmp/$name.loops"
  printf 'dmsnap-up\t%.2fs\tdev=/dev/mapper/%s\tcow_ondisk=%s\n' \
    "$(echo "$t1-$t0" | bc)" "$name" "$(du -h "/tmp/$name.cow" | cut -f1)"
  ;;
down)
  printf 'dmsnap-down cow_ondisk=%s\n' "$(du -h "/tmp/$name.cow" 2>/dev/null | cut -f1)"
  sudo dmsetup remove "$name" || true
  if [ -f "/tmp/$name.loops" ]; then
    for l in $(cat "/tmp/$name.loops"); do sudo losetup -d "$l" || true; done
    rm -f "/tmp/$name.loops"
  fi
  rm -f "/tmp/$name.cow"
  ;;
esac
