#!/usr/bin/env bash
# Desk signal feed on 127.0.0.1:8190 (live feed is on 8090).
# Tokens live in this folder's data/feed_tokens.json, separate from live:
#   .venv/bin/python -m zcastor.feed.serve --add-follower NAME
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "no .env here; run ./setup.sh first"; exit 1; }
set -a
# shellcheck disable=SC1091
source .env
set +a
.venv/bin/python ops/check_isolation.py --starting feed
exec .venv/bin/python -m zcastor.feed.serve --host 127.0.0.1 --port "${DESK_FEED_PORT:-8190}"
