"""
Universe and profiles from Hyperliquid — run on your machine (Hyperliquid's
API is not reachable from the cloud workspace).

    python -m universe select              # which perps, by the rule in config/universe.json
    python -m universe history             # 14 days of 5-minute bars for every member
    python -m universe profiles            # one profile per member + index.json
    python -m universe all                 # the three, in order

Hyperliquid keeps only its most recent ~5,000 candles per interval: 5-minute
bars reach back about 17 days, 1-minute bars only 3½. Hence 5m × 14 days.

Everything is public market data: no API key is read, none is needed, and this
command cannot place an order. Output goes under data/ in this folder only.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from zcastor.record.canonical import write_canonical  # noqa: E402

from universe import history, profiles, selection  # noqa: E402
from zcastor.market.hyperliquid import (BinanceView, HLInfo, api_base,  # noqa: E402
                                        urllib_transport)

DATA = ROOT / "data"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def cmd_select(args, client) -> Path:
    config = _load_json(Path(args.rule))
    rule = selection.rule_from(config)
    doc = selection.build_universe(client, rule)
    out = Path(args.out)
    day = doc["as_of"][:10]
    digest = write_canonical(out / f"{day}.json", doc)
    shutil.copyfile(out / f"{day}.json", out / "latest.json")
    print(f"\n  universe {day}: {len(doc['members'])} members "
          f"(of {doc['considered']} contracts, {doc['passed_floors']} above the floors)")
    for m in doc["members"]:
        tag = " forced" if m["forced"] else ""
        print(f"    {str(m['rank'] or '—'):>3}  {m['symbol']:<14} "
              f"vol ${m['quote_volume_24h']/1e6:>9,.0f}M  "
              f"OI ${m['open_interest_usd']/1e6:>7,.0f}M{tag}")
    print(f"\n  rejected: {doc['rejected']}")
    print(f"  written:  {out / 'latest.json'}\n  checksum: {digest}\n")
    return out / "latest.json"


def cmd_history(args, client) -> None:
    uni = _load_json(Path(args.universe))
    symbols = [m["symbol"] for m in uni["members"]]
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    print(f"\n  {len(symbols)} contracts × {args.days} days of {args.interval} bars\n")
    history.download(client, symbols, root=Path(args.history), interval=args.interval,
                     days=args.days)
    print(f"\n  {client.requests} requests\n")


def cmd_profiles(args, client) -> None:
    uni = _load_json(Path(args.universe))
    policy = _load_json(ROOT / "config" / "policy.json")
    config = _load_json(Path(args.rule))
    print()
    index = profiles.generate(
        client, uni, history_root=Path(args.history), out_root=Path(args.out_profiles),
        policy=policy, days=args.days,
        point_value=float(config.get("btc_point_value_usd", 1.0)),
        with_funding=not args.no_funding, interval=args.interval)
    print(f"\n  {len(index['profiles'])} profiles, {len(index['missing'])} missing"
          f"\n  index checksum: {index['checksum']}\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m universe")
    ap.add_argument("command", choices=["select", "history", "profiles", "all"])
    ap.add_argument("--rule", default=str(ROOT / "config" / "universe.json"))
    ap.add_argument("--out", default=str(DATA / "universe"))
    ap.add_argument("--universe", default=str(DATA / "universe" / "latest.json"))
    ap.add_argument("--history", default=str(DATA / "history"))
    ap.add_argument("--out-profiles", default=str(DATA / "profiles"))
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--symbols", help="comma-separated, overrides the universe")
    ap.add_argument("--no-funding", action="store_true")
    args = ap.parse_args(argv)

    client = BinanceView(HLInfo(urllib_transport(api_base())))
    started = datetime.now(timezone.utc)
    if args.command in ("select", "all"):
        args.universe = str(cmd_select(args, client))
    if args.command in ("history", "all"):
        cmd_history(args, client)
    if args.command in ("profiles", "all"):
        cmd_profiles(args, client)
    secs = (datetime.now(timezone.utc) - started).total_seconds()
    print(f"  done in {secs:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
