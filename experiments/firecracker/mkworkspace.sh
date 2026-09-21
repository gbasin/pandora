#!/usr/bin/env bash
# Workspace hand-off measurements. Firecracker has virtio-blk only: no virtiofs,
# no bind mount. Every option below ends in a block device.
#
#   src   <img> <size> <dir>   mke2fs -d straight from a source tree (per-run snapshot)
#   blank <img> <size>         empty ext4 (per-run scratch / overlay upper)
#   copy  <src> <dst>          cp --sparse=always of a warm base (no reflink on ext4)
#   cpraw <src> <dst>          plain cp (worst case)
# Each prints seconds and the bytes actually consumed.
set -euo pipefail
cmd=$1; shift
t0=$(date +%s.%N)
case "$cmd" in
  src)   img=$1; size=$2; dir=$3; label=${4:-work}; rm -f "$img"
         sudo mke2fs -q -t ext4 -d "$dir" -L "$label" "$img" "$size" ;;
  blank) img=$1; size=$2; label=${3:-scratch}; rm -f "$img"
         sudo mke2fs -q -t ext4 -L "$label" "$img" "$size" ;;
  copy)  cp --sparse=always "$1" "$2" ;;
  cpraw) cp "$1" "$2" ;;
  *) echo "usage: $0 src|blank|copy|cpraw ..." >&2; exit 2 ;;
esac
t1=$(date +%s.%N)
out=${2:-$1}; [ "$cmd" = src ] || [ "$cmd" = blank ] && out=$1
sudo chown "$USER" "$out" 2>/dev/null || true
printf '%s\t%.2fs\tapparent=%s\tondisk=%s\n' "$cmd" "$(echo "$t1-$t0" | bc)" \
  "$(du -h --apparent-size "$out" | cut -f1)" "$(du -h "$out" | cut -f1)"
