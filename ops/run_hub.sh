#!/usr/bin/env bash
# Market hub on 127.0.0.1:8170: Hyperliquid public market data, cached, for
# any signal source on this machine. Holds no key and cannot trade. Optional:
# the engine reads Hyperliquid directly.
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m zcastor.market.hub --port "${DESK_HUB_PORT:-8170}" "$@"
