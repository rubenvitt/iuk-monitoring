#!/usr/bin/env bash
# Install/update fuekw-watch on the vehicle server (run as root).
# Optional: pass a staged env file (NTFY_URL,
# HUB_HEALTH_URL); it is moved to /etc/fuekw-watch.env with mode 600.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
if [ -n "${1:-}" ]; then
  { [ -f "$1" ] && [ ! -L "$1" ]; } || { echo "not a regular file: $1" >&2; exit 1; }
  install -m 600 -o root -g root "$1" /etc/fuekw-watch.env
  rm -f "$1"
fi
[ -f /etc/fuekw-watch.env ] || { echo "missing /etc/fuekw-watch.env" >&2; exit 1; }
install -d -m 755 /opt/fuekw-watch
install -m 644 "$here/fuekw_watch.py" /opt/fuekw-watch/fuekw_watch.py
install -m 644 "$here/systemd/fuekw-watch.service" "$here/systemd/fuekw-watch.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now fuekw-watch.timer
systemctl start fuekw-watch.service
