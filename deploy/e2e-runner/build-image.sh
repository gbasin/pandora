#!/bin/bash
# Builds the e2e VM image on the host, as root, from a Pandora checkout:
#
#   sudo deploy/e2e-runner/build-image.sh /opt/pandora-e2e/src
#
# Launches a builder VM in the e2e project, copies the checkout in, runs
# vm/setup-vm.sh install, reboots, runs warm and seal, and publishes the stopped
# VM as image `pandora-e2e-<commit>`. Then it points the `pandora-e2e` alias,
# which the pool launches from, at the new image. The previous image stays, so
# a rollback is one alias move. The builder VM is deleted on every exit.
set -euo pipefail

SRC=${1:?usage: build-image.sh <pandora checkout>}
PROJECT=${E2E_PROJECT:-pandora-e2e}
PROFILE=${E2E_PROFILE:-pandora-e2e}
ALIAS=${E2E_IMAGE:-pandora-e2e}
BASE=${E2E_BASE_IMAGE:-images:ubuntu/26.04}

[ "$(id -u)" -eq 0 ] || { echo 'build-image.sh: run as root' >&2; exit 64; }
REV=$(git -C "$SRC" rev-parse --short=12 HEAD)
TAG=$ALIAS-$REV
I=(incus --project "$PROJECT")
NAME=e2ebuild-$(date +%s)

if "${I[@]}" image alias list --format csv | grep -q "^$TAG,"; then
  echo "build-image.sh: $TAG already exists; pointing $ALIAS at it"
else
  trap '"${I[@]}" delete --force "$NAME" >/dev/null 2>&1 || true' EXIT

  wait_agent() {
    for _ in $(seq 1 100); do
      "${I[@]}" exec "$NAME" -- true >/dev/null 2>&1 && return 0
      sleep 3
    done
    echo "build-image.sh: the agent in $NAME never answered" >&2
    return 1
  }

  "${I[@]}" launch "$BASE" "$NAME" --vm --profile "$PROFILE"
  wait_agent
  "${I[@]}" exec "$NAME" -- install -d -m 0755 /opt/pandora-src
  tar -C "$SRC" -cf - . | "${I[@]}" exec "$NAME" -- tar -C /opt/pandora-src -xf -
  "${I[@]}" exec "$NAME" -- bash /opt/pandora-src/deploy/e2e-runner/vm/setup-vm.sh install
  # A new worker reboots once before it takes work (docs/worker.md).
  "${I[@]}" restart "$NAME"
  wait_agent
  "${I[@]}" exec "$NAME" -- bash /opt/pandora-src/deploy/e2e-runner/vm/setup-vm.sh warm
  "${I[@]}" exec "$NAME" -- bash /opt/pandora-src/deploy/e2e-runner/vm/setup-vm.sh seal
  "${I[@]}" stop "$NAME"
  "${I[@]}" publish "$NAME" --alias "$TAG" --compression zstd
fi

FINGERPRINT=$("${I[@]}" image alias list --format csv | awk -F, -v t="$TAG" '$1 == t {print $2}')
[ -n "$FINGERPRINT" ] || { echo "build-image.sh: no image behind $TAG" >&2; exit 1; }
"${I[@]}" image alias delete "$ALIAS" >/dev/null 2>&1 || true
"${I[@]}" image alias create "$ALIAS" "$FINGERPRINT"
echo "build-image.sh: $ALIAS -> $TAG ($FINGERPRINT)"
