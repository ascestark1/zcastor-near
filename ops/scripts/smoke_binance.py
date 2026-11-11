#!/usr/bin/env python3
"""
Binance adapter smoke test — READ ONLY by default.

Checks the same things the MT5 smoke test does, so the two venues can be
compared directly: tick shape, balance, notional, klines, and the Proof of
State hash. Places no orders unless --trade is passed, and then only on testnet.

    export BINANCE_API_KEY=... BINANCE_API_SECRET=...
    python3 ops/scripts/smoke_binance.py
    python3 ops/scripts/smoke_binance.py --trade    # one round trip, testnet
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zcastor.execution.binance_orders import BinanceOrderPlacer  # noqa: E402
from zcastor.market.binance import (  # noqa: E402
    MAINNET, TESTNET, BinanceClient, BinanceError, BinanceMarketData,
)
from zcastor.market.session import SessionOpens, current_session  # noqa: E402
from zcastor.record import proof  # noqa: E402

OK, BAD, INFO = "  ok  ", " FAIL ", "  ··  "


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="use mainnet (reads only)")
    ap.add_argument("--trade", action="store_true",
                    help="place and close one minimum-size order")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--volume", type=float, default=0.001)
    args = ap.parse_args()

    base = MAINNET if args.live else TESTNET
    print(f"\n=== Binance smoke test ({'MAINNET' if args.live else 'testnet'}) ===\n")

    client = BinanceClient(base_url=base)
    sessions = SessionOpens(lambda f, t: md.get_rates_range(f, t))
    md = BinanceMarketData(client, symbol=args.symbol, volume=args.volume,
                           session_opens=sessions)
    md.begin()
    failures = 0

    tick = md.get_tick()
    if "error" in tick:
        print(f"{BAD} tick: {tick['error']}")
        return 1
    print(f"{OK} tick   ask={tick['ask']:,.2f} bid={tick['bid']:,.2f} "
          f"spread={tick['spread_pts']}")
    print(f"{INFO}        XM quotes the same instrument ~40 wide. At a 55pt "
          f"stop that is a 69% break-even win rate; here it is ~40%.")

    if not client.api_key:
        print(f"{INFO} no API key set — skipping account checks")
    else:
        balance = md.get_balance()
        if balance is None:
            print(f"{BAD} balance unreadable")
            failures += 1
        else:
            print(f"{OK} balance {balance:,.2f} USDT  "
                  f"free {md.get_free_margin():,.2f}")

    notional = md.get_margin_for("UP", args.volume, tick["ask"])
    print(f"{OK} notional {args.volume} {args.symbol} costs {notional:,.2f}")

    bars = md.get_rates_range(time.time() - 3600, time.time())
    if bars:
        print(f"{OK} klines  {len(bars)} M1 bars, last close {bars[-1]['close']:,.2f}")
    else:
        print(f"{BAD} no klines returned")
        failures += 1

    session = current_session()
    open_price = md.session_open_price(session)
    print(f"{OK} session {session} open "
          f"{open_price:,.2f}" if open_price else
          f"{INFO} session {session} open unavailable")

    state = md.state(session=session)
    print(f"{OK} state   {proof.state_hash(**state)}")

    if args.trade:
        if args.live:
            print(f"\n{BAD} refusing to trade on mainnet from a smoke test")
            return 1
        print("\n-- placing one order, then closing it --")
        placer = BinanceOrderPlacer(client, symbol=args.symbol,
                                    volume=args.volume)
        result = placer.market_order(side="BUY", price=tick["ask"],
                                     sl=tick["ask"] * 0.99)
        if "error" in result:
            print(f"{BAD} order: {result['error']}")
            return 1
        print(f"{OK} filled ticket={result['ticket']} @ {result['price']:,.2f}")
        closed = placer.close_position(result["ticket"], "BUY")
        if "error" in closed:
            print(f"{BAD} close: {closed['error']}")
            return 1
        print(f"{OK} closed @ {closed['close_price']:,.2f}   "
              f"round trip cost {result['price']-closed['close_price']:+.2f}")

    print(f"\n=== {'PASSED' if not failures else f'{failures} FAILURE(S)'} "
          f"at {datetime.now(timezone.utc):%H:%M:%S} UTC ===\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
