#!/usr/bin/env bash
# Install/update the Beszel agent on the vehicle server (run as root).
# Optional: pass a staged env file (KEY, TOKEN, HUB_URL); it is moved to
# /opt/beszel-agent/.env with mode 600.
set -euo pipefail
src="$(cd "$(dirname "$0")" && pwd)/beszel-agent"
dst=/opt/beszel-agent
mkdir -p "$dst"
if [ -n "${1:-}" ]; then
  { [ -f "$1" ] && [ ! -L "$1" ]; } || { echo "not a regular file: $1" >&2; exit 1; }
  install -m 600 -o root -g root "$1" "$dst/.env"
  rm -f "$1"
fi
[ -f "$dst/.env" ] || { echo "missing $dst/.env (copy .env.example, chmod 600)" >&2; exit 1; }
cp "$src/compose.yaml" "$dst/compose.yaml"
cd "$dst"
docker compose up -d
