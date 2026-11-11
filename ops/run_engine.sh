#!/usr/bin/env bash
# Desk engine on 127.0.0.1:8100 (live Zcastor is on 8000).
#
# Venue: hyperliquid-paper. Hyperliquid's real order book, simulated fills,
# no exchange key. One coin (runtime.json "coin", BTC); signals for other perps
# are refused with symbol_not_traded until per-ticker engine state lands. Post
# signals to POST /signal; the signal generator is private and not in this repo.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "no .env here; run ./setup.sh first"; exit 1; }

set -a
# shellcheck disable=SC1091
source .env
set +a

.venv/bin/python ops/check_isolation.py --starting engine
exec .venv/bin/python -m uvicorn zcastor.app.server:app \
  --host 127.0.0.1 --port "${DESK_ENGINE_PORT:-8100}"
