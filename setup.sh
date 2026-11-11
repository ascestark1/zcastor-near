#!/usr/bin/env bash
# One-time setup for the desk. Run from anywhere:   ~/zcastor-near/setup.sh
#
# Judges: you don't need this. The README's `python3 -m venv` line is enough.
# This script is for running the desk next to a live Zcastor install; without
# one it just makes a venv, .env and config/runtime.json.
#
# Writes only inside this folder. It READS live Zcastor's package list (pip
# freeze) so the desk gets exactly the same package versions, and never
# installs into, edits or restarts anything in ~/zcastor-v2.
#
# Safe to run again: it keeps an existing .venv, .env and runtime.json.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
LIVE="${ZCASTOR_LIVE_DIR:-$HOME/zcastor-v2}"
cd "$ROOT"

case "$ROOT/" in
  "$LIVE/"*) echo "  refusing: $ROOT is inside live Zcastor ($LIVE)"; exit 1 ;;
esac
case "$LIVE/" in
  "$ROOT/"*) echo "  refusing: live Zcastor ($LIVE) is inside $ROOT"; exit 1 ;;
esac

echo "  desk:  $ROOT"
echo "  live:  $LIVE (read only)"

# Same interpreter as live Zcastor, so behaviour matches; its own venv, so a
# package installed here (NEAR SDKs from 1 Nov) can never change the engine
# that trades.
if [ -x "$LIVE/.venv/bin/python" ]; then
  PY="$LIVE/.venv/bin/python"
else
  PY="$(command -v python3)"
fi
if [ ! -d .venv ]; then
  echo "  creating .venv with $("$PY" -c 'import sys; print(sys.version.split()[0])')"
  "$PY" -m venv .venv
fi
.venv/bin/python -m pip install -q --upgrade pip

if [ -x "$LIVE/.venv/bin/python" ]; then
  # Pin to live's exact versions. Lines with ' @ ' are local paths; skip them.
  "$LIVE/.venv/bin/python" -m pip freeze --exclude-editable 2>/dev/null \
    | grep -v ' @ ' > .venv/live-constraints.txt || true
  .venv/bin/pip install -q -r requirements.txt -c .venv/live-constraints.txt
  echo "  packages pinned to live Zcastor's versions"
else
  .venv/bin/pip install -q -r requirements.txt
  echo "  live venv not found; installed latest versions"
fi

mkdir -p data logs

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "  created .env from .env.example (fill it in; keys must differ from live)"
fi

if [ ! -f config/runtime.json ]; then
  .venv/bin/python - "$ROOT" <<'PYEOF'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
rt = json.loads((root / "config" / "runtime.example.json").read_text())
rt["dossier_root"] = str(root / "data" / "dossiers")   # absolute: never resolves elsewhere
(root / "config" / "runtime.json").write_text(json.dumps(rt, indent=2) + "\n")
print("  created config/runtime.json")
PYEOF
fi

# A runtime.json from the Binance desk (before 24 Oct 2026) moves to Hyperliquid
# paper. Everything else in it is kept.
.venv/bin/python - "$ROOT" <<'PYEOF'
import json, sys
from pathlib import Path
p = Path(sys.argv[1]) / "config" / "runtime.json"
rt = json.loads(p.read_text())
if str(rt.get("venue", "")).lower() == "binance":
    for k in ("binance_symbol", "binance_volume"):
        rt.pop(k, None)
    rt.update({"venue": "hyperliquid-paper", "symbol": "BTC"})
    for k, v in (("coin", "BTC"), ("paper_volume", 0.001), ("paper_balance", 10000),
                 ("paper_leverage", 3), ("capture_origins", True)):
        rt.setdefault(k, v)
    p.write_text(json.dumps(rt, indent=2) + "\n")
    print("  moved config/runtime.json from Binance to Hyperliquid paper")
PYEOF

echo
.venv/bin/python tests/run_all.py | tail -2
.venv/bin/python ops/check_isolation.py || true
echo "  next: .venv/bin/python -m universe all   (Hyperliquid perps, profiles)"
