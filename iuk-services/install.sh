#!/usr/bin/env bash
# Install/update the Beszel stack on the server. Run from the repo checkout.
set -euo pipefail
src="$(cd "$(dirname "$0")" && pwd)/beszel"
dst="$HOME/server/compose/beszel"
mkdir -p "$dst" /data/beszel
[ -f "$dst/.env" ] || { echo "missing $dst/.env (copy .env.example, chmod 600)" >&2; exit 1; }
cp "$src/compose.yaml" "$dst/compose.yaml"
cd "$dst"
docker compose up -d beszel
# the agent needs KEY/TOKEN from the hub: start it manually once .env has them
