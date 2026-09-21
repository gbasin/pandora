#!/usr/bin/env bash
# One measured run: host-side CoW disk -> boot -> journey -> read outputs back.
# Prints the timings pandora would record per run.
#   measure-run.sh <vm-n> <label> [journey]
set -euo pipefail
SPIKE=${SPIKE:-$HOME/spike-fc}
N=$1; LABEL=$2; J=${3:-S0-01}
MEM=${MEM:-6144}; VCPU=${VCPU:-2}
cd "$SPIKE"

t_prep0=$(date +%s.%N)
bash scripts/dmsnap.sh down "fcrun$N" >/dev/null 2>&1 || true
bash scripts/dmsnap.sh up "fcrun$N" img/warmbase.ext4 6G >/dev/null
sudo chown "$USER" "/dev/mapper/fcrun$N"
bash scripts/mkworkspace.sh blank "img/out$N.ext4" 512M out >/dev/null
python3 scripts/fcvm.py net up "$N" >/dev/null 2>&1
t_prep1=$(date +%s.%N)

t_boot0=$(date +%s.%N)
sg kvm -c "cd $SPIKE && python3 scripts/fcvm.py boot $N --vcpus $VCPU --mem $MEM \
  --workspace /dev/mapper/fcrun$N --outputs img/out$N.ext4 --timeout 120"
t_boot1=$(date +%s.%N)

scp -i ~/.ssh/fcspike -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR scripts/run-journey.sh "root@172.30.$N.2:/root/" >/dev/null

# sample host RSS of the firecracker process for the whole run
pid=$(cat "run/vm$N/pid")
( while kill -0 "$pid" 2>/dev/null; do
    awk '/VmRSS/{print systime(), $2}' "/proc/$pid/status" 2>/dev/null
    sleep 2
  done ) > "logs/$LABEL.rss" &
sampler=$!

t_run0=$(date +%s.%N)
set +e
JOURNEY=$J python3 scripts/fcvm.py ssh "$N" -- "JOURNEY=$J bash /root/run-journey.sh" \
  > "logs/$LABEL.log" 2>&1
rc=$?
set -e
t_run1=$(date +%s.%N)
kill $sampler 2>/dev/null || true

# read outputs back off the block device, host-side, after the run
t_out0=$(date +%s.%N)
python3 scripts/fcvm.py ssh "$N" -- "sync" >/dev/null 2>&1 || true
python3 scripts/fcvm.py kill "$N" >/dev/null
mkdir -p "out/$LABEL"
sudo mount -o ro "img/out$N.ext4" /mnt 2>/dev/null && sudo cp -r /mnt/. "out/$LABEL/" && sudo umount /mnt
sudo chown -R "$USER" "out/$LABEL"
t_out1=$(date +%s.%N)

peak=$(awk '{if($2>m)m=$2}END{print m}' "logs/$LABEL.rss")
cow=$(du -m "/tmp/fcrun$N.cow" | cut -f1)
python3 - "$LABEL" "$rc" "$t_prep0" "$t_prep1" "$t_boot0" "$t_boot1" "$t_run0" "$t_run1" \
         "$t_out0" "$t_out1" "$peak" "$MEM" "$cow" <<'PY'
import json, sys
l, rc, p0, p1, b0, b1, r0, r1, o0, o1, peak, mem, cow = sys.argv[1:]
f = float
print(json.dumps({
  "label": l, "exit": int(rc),
  "disk_prep_s": round(f(p1)-f(p0), 2),
  "boot_to_ssh_s": round(f(b1)-f(b0), 2),
  "journey_s": round(f(r1)-f(r0), 2),
  "output_readback_s": round(f(o1)-f(o0), 2),
  "total_s": round(f(o1)-f(p0), 2),
  "fc_peak_rss_mib": round(int(peak)/1024, 1),
  "configured_mem_mib": int(mem),
  "cow_written_mib": int(cow),
}))
PY
