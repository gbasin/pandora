#!/bin/bash
# Run from an uploaded, trial-owned directory on the disposable worker.
set -euo pipefail
name="pandora-smoke-$(basename "$PWD")"
[[ "$name" =~ ^pandora-smoke-[a-zA-Z0-9-]+$ ]] || exit 64
image=$(cat image-id)
sha256sum -c source.sha256
mkdir workspace
chmod 777 workspace
cleanup() {
  sudo docker stop --time 10 "$name" >/dev/null 2>&1 || true
  sudo docker inspect "$name" > container.json 2>/dev/null || true
  sudo docker rm -f "$name" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
sudo docker create --name "$name" --label pandora.experiment=surface-smoke \
  --cpus=2 --memory=6g --memory-swap=6g --pids-limit=512 --shm-size=1g \
  --cap-drop=ALL --security-opt=no-new-privileges --init \
  -e CI=true -e npm_config_update_notifier=false \
  --mount "type=bind,src=$PWD,dst=/input,readonly" \
  --mount "type=bind,src=$PWD/workspace,dst=/workspace" \
  "$image" bash /input/in-container.sh "$@" > container-id
# The worker deadline is independent of the local SSH process. It also bounds
# an abandoned smoke run. A production queue needs explicit client-loss policy.
sudo systemd-run --quiet --unit="$name-deadline" --on-active=20m \
  /usr/bin/docker stop --time 10 "$name"
set +e
sudo docker start --attach "$name" > >(tee stdout.log) 2> >(tee stderr.log >&2)
status=$?
set -e
printf '%s\n' "$status" > docker-exit-code
cleanup
trap - EXIT
sudo systemctl stop "$name-deadline.timer"
exit "$status"
