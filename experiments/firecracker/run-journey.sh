#!/bin/bash
# Runs INSIDE the guest: eichler's own runner, eichler's own compose stack, on
# the guest's own dockerd. No proxy, no netns seam, no port rewriting.
set -x
cd /work
mkdir -p /out
: > /out/mem.samples
( while :; do
    printf '%s %s %s\n' "$(date +%s)" \
      "$(cat /sys/fs/cgroup/memory.current 2>/dev/null || echo 0)" \
      "$(awk '/MemAvailable/{print $2}' /proc/meminfo)"
    sleep 2
  done ) & sampler=$!

echo "T_FIRSTCMD $(date +%s.%N)"
systemctl is-active docker || systemctl start docker
for i in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 0.5; done
echo "T_DOCKERREADY $(date +%s.%N)"
docker images --format '{{.Repository}}:{{.Tag}}'

echo "T_JOURNEY_START $(date +%s.%N)"
node tools/validation/journey-runner.mjs run "${JOURNEY:-S0-01}" --report /out/report.json
rc=$?
echo "T_JOURNEY_END $(date +%s.%N) rc=$rc"

kill $sampler 2>/dev/null
docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | tee /out/containers.txt
cp -f /out/report.json /out/report.json 2>/dev/null
awk '{if($2>m)m=$2}END{print "PEAK_CGROUP_MEM_BYTES",m}' /out/mem.samples
free -m | tee /out/free.txt
exit $rc
