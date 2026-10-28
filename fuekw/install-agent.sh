#!/usr/bin/env bash
# Install/update the Beszel agent on the vehicle server (run as root).
set -euo pipefail
src="$(cd "$(dirname "$0")" && pwd)/beszel-agent"
dst=/opt/beszel-agent
mkdir -p "$dst"
[ -f "$dst/.env" ] || { echo "missing $dst/.env (copy .env.example, chmod 600)" >&2; exit 1; }
cp "$src/compose.yaml" "$dst/compose.yaml"
cd "$dst"
docker compose up -d
