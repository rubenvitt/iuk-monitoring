#!/usr/bin/env bash
# Install/update the Beszel stack on the server. Run from the repo checkout.
set -euo pipefail
src="$(cd "$(dirname "$0")" && pwd)/beszel"
dst="$HOME/server/compose/beszel"
mkdir -p "$dst" /data/beszel /data/volumen
[ -f "$dst/.env" ] || { echo "missing $dst/.env (copy .env.example, chmod 600)" >&2; exit 1; }
cp "$src/compose.yaml" "$dst/compose.yaml"
rm -rf "$dst/volumen" && cp -r "$src/../volumen" "$dst/volumen"
cd "$dst"
docker compose up -d beszel volumen volumen-auth
# the agent (profile "agent") needs KEY/TOKEN from the hub; once .env has them:
#   mkdir -p /data/.beszel && docker compose --profile agent up -d beszel-agent
