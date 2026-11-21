#!/bin/bash
# Prepares the e2e image inside a builder VM, as root. build-image.sh calls it
# in three stages, with a reboot between the first two:
#
#   setup-vm.sh install   users, sshd, the runner, the hook, a provisioned worker
#   setup-vm.sh warm      canary --mark, then one signed selftest
#   setup-vm.sh seal      remove the build's leftovers before publishing
#
# The checkout it runs from is /opt/pandora-src (a git clone). Nothing here holds
# a credential: the runner is registered per VM, later, by the host's pool.
set -euo pipefail

SRC=/opt/pandora-src
HERE=$SRC/deploy/e2e-runner
RUNNER_VERSION=2.338.2
RUNNER_SHA256=baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40
RUNNER_HOME=/home/gh-runner
CI_CONFIG=$RUNNER_HOME/.config-pandora-e2e.toml
BAKE_CONFIG=$RUNNER_HOME/.config-pandora-bake.toml

as_runner() { (cd / && sudo -u gh-runner -H "$@"); }

install_stage() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl git jq openssh-client openssh-server \
    python3 rsync sudo >/dev/null

  id gh-runner >/dev/null 2>&1 || useradd -m -s /bin/bash gh-runner
  id pandora-ci >/dev/null 2>&1 || useradd -m -s /bin/bash pandora-ci
  # The worker drives Incus through sudo. Inside this disposable VM that is the
  # VM's root, not the host's.
  printf 'pandora-ci ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/pandora-ci
  chmod 0440 /etc/sudoers.d/pandora-ci
  visudo -cf /etc/sudoers.d/pandora-ci >/dev/null

  printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\n' \
    > /etc/ssh/sshd_config.d/60-pandora-e2e.conf
  systemctl enable --now ssh.socket >/dev/null 2>&1 || systemctl enable --now ssh.service
  systemctl try-restart ssh.service

  # The same client side the host runner had: gh-runner reaches the worker as
  # pandora-ci@localhost with the e2e-ci key.
  install -d -m 0700 -o gh-runner -g gh-runner "$RUNNER_HOME/.ssh"
  install -d -m 0700 -o pandora-ci -g pandora-ci /home/pandora-ci/.ssh
  [ -f "$RUNNER_HOME/.ssh/e2e-ci" ] \
    || as_runner ssh-keygen -q -t ed25519 -N '' -C e2e-ci@localhost -f "$RUNNER_HOME/.ssh/e2e-ci"
  printf 'from="127.0.0.1,::1",no-agent-forwarding,no-X11-forwarding,no-port-forwarding %s\n' \
    "$(cat "$RUNNER_HOME/.ssh/e2e-ci.pub")" > /home/pandora-ci/.ssh/authorized_keys
  chown pandora-ci:pandora-ci /home/pandora-ci/.ssh/authorized_keys
  chmod 0600 /home/pandora-ci/.ssh/authorized_keys
  printf 'Host localhost\n  IdentityFile ~/.ssh/e2e-ci\n  IdentitiesOnly yes\n' \
    > "$RUNNER_HOME/.ssh/config"
  ssh-keyscan -q localhost > "$RUNNER_HOME/.ssh/known_hosts"
  chown gh-runner:gh-runner "$RUNNER_HOME/.ssh/config" "$RUNNER_HOME/.ssh/known_hosts"
  chmod 0600 "$RUNNER_HOME/.ssh/config" "$RUNNER_HOME/.ssh/known_hosts"
  as_runner ssh -o BatchMode=yes pandora-ci@localhost true

  printf '[worker]\nhost = "pandora-ci@localhost"\nengine_root = "pandora-engine"\n' > "$CI_CONFIG"
  {
    cat "$CI_CONFIG"
    printf '\n[[repos]]\nname = "pandora"\nroot = "%s"\n' "$SRC"
  } > "$BAKE_CONFIG"
  chown gh-runner:gh-runner "$CI_CONFIG" "$BAKE_CONFIG"

  if [ ! -x "$RUNNER_HOME/actions-runner/run.sh" ]; then
    curl -fsSL -o /tmp/runner.tar.gz \
      "https://github.com/actions/runner/releases/download/v$RUNNER_VERSION/actions-runner-linux-x64-$RUNNER_VERSION.tar.gz"
    echo "$RUNNER_SHA256  /tmp/runner.tar.gz" | sha256sum -c - >/dev/null
    install -d -o gh-runner -g gh-runner "$RUNNER_HOME/actions-runner"
    as_runner tar -xzf /tmp/runner.tar.gz -C "$RUNNER_HOME/actions-runner"
    rm -f /tmp/runner.tar.gz
    "$RUNNER_HOME/actions-runner/bin/installdependencies.sh" >/dev/null
  fi
  printf 'LANG=C.UTF-8\nACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/pandora-e2e/job-started.sh\n' \
    > "$RUNNER_HOME/actions-runner/.env"
  chown gh-runner:gh-runner "$RUNNER_HOME/actions-runner/.env"

  install -d -m 0755 /opt/pandora-e2e
  install -m 0555 "$HERE/trusted_job.py" "$HERE/job-started.sh" \
    "$HERE/vm/start-runner" "$HERE/vm/run-runner" /opt/pandora-e2e/
  install -m 0644 "$HERE/vm/pandora-e2e-runner.service" /etc/systemd/system/
  systemctl daemon-reload

  chown -R gh-runner:gh-runner "$SRC"
  as_runner "$SRC/bin/pandora" --config "$BAKE_CONFIG" worker --host pandora-ci@localhost \
    provision --versions "$HERE/worker.toml" --no-canary
}

warm_stage() {
  as_runner "$SRC/bin/pandora" --config "$BAKE_CONFIG" worker --host pandora-ci@localhost \
    canary --mark --source "$SRC"
  # The CI config enrolls nothing, as on the host, so this builds and keeps the
  # selftest's own golden, and proves the worker signs.
  as_runner "$SRC/bin/pandora" --config "$CI_CONFIG" selftest --update --queue \
    --expect-signed --timeout 1800
}

seal_stage() {
  rm -f "$BAKE_CONFIG"
  rm -rf "$RUNNER_HOME/.local/state/pandora" "$RUNNER_HOME/.cache/pandora"
  test ! -e "$RUNNER_HOME/actions-runner/.runner"
  test ! -e "$RUNNER_HOME/actions-runner/.credentials"
  apt-get clean
  # Every VM from this image gets its own machine ID, so DHCP leases and the
  # journal do not collide between concurrent VMs.
  truncate -s 0 /etc/machine-id
  rm -f /var/lib/dbus/machine-id
}

case "${1:-}" in
  install) install_stage ;;
  warm) warm_stage ;;
  seal) seal_stage ;;
  *) echo "usage: $0 install|warm|seal" >&2; exit 64 ;;
esac
