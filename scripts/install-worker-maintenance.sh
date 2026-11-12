#!/bin/sh
# Run on the worker as its user, with a verified engine bundle path.
set -eu
bundle=${1:?usage: install-worker-maintenance.sh BUNDLE [ENGINE_ROOT]}
engine_root=${2:-"$HOME/pandora-engine"}
case "$bundle" in /*) ;; *) echo 'bundle path must be absolute' >&2; exit 64;; esac
case "$engine_root" in /*) ;; *) echo 'engine root must be absolute' >&2; exit 64;; esac
case "$bundle:$engine_root" in *[!a-zA-Z0-9_./:-]*) echo 'unsupported characters in paths' >&2; exit 64;; esac
[ -f "$bundle/pandora/.bundle" ] || { echo 'not a verified engine bundle' >&2; exit 64; }
(cd "$bundle" && PYTHONPATH="$bundle" /usr/bin/python3 -m pandora.engine.service expire-queued --help >/dev/null)
units="$HOME/.config/systemd/user"
mkdir -p "$units"
for action in retain expire-queued; do
  if [ "$action" = retain ]; then
    args='retain --keep 604800 --keep-failed 604800'
    schedule='OnCalendar=*-*-* 03:30:00 UTC
Persistent=true'
  else
    args='expire-queued'
    schedule='OnBootSec=2min
OnUnitActiveSec=5min'
  fi
  cat > "$units/pandora-$action.service" <<EOF
[Unit]
Description=Pandora worker $action
[Service]
Type=oneshot
Environment=PYTHONPATH=$bundle
WorkingDirectory=$bundle
ExecStart=/usr/bin/python3 -m pandora.engine.service --root $engine_root $args
TimeoutStartSec=30min
EOF
  cat > "$units/pandora-$action.timer" <<EOF
[Unit]
Description=Schedule Pandora worker $action
[Timer]
$schedule
[Install]
WantedBy=timers.target
EOF
done
systemctl --user daemon-reload
systemctl --user enable --now pandora-retain.timer pandora-expire-queued.timer
systemctl --user list-timers pandora-retain.timer pandora-expire-queued.timer --no-pager
