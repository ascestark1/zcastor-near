#!/usr/bin/env python3
"""
Funding quotes through NEAR Intents (1-Click API).

    .venv/bin/python near/intents.py quote --agent btc --usdc 5
    .venv/bin/python near/intents.py tokens --symbol USDC

How an agent's Hyperliquid perps balance gets funded: USDC from NEAR (or any
supported chain) goes to a 1-Click deposit address, and NEAR Intents delivers
it into the recipient's Hyperliquid perps balance. This tool asks for that
quote with `dry: true`, which prices the route and moves nothing.

It can't move funds. There is no `--live` flag and no key in this file: a real
transfer is the owner sending `amountIn` to the quoted deposit address from
their own wallet. Intents has no testnet (HACKATHON.md Q-007), so dry quotes
are what a testnet desk can honestly show.

Every quote is saved under data/near/quotes/ with the request that produced it.

Environment:
    NEAR_REFUND_ACCOUNT   mainnet account refunds would go to (e.g. you.near)
    ONECLICK_JWT          optional; without it the API adds its unauthenticated fee
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://1click.chaindefuser.com"
# Hyperliquid perps USDC, from the 1-Click Hyperliquid guide (8 decimals).
HYPERLIQUID_USDC = "1cs_v1:hypercore:hip1:0x6d1e7cde53ba9467b783cb7c530ce054"
# Native USDC on NEAR (6 decimals). Looked up from /v0/tokens when reachable.
NEAR_USDC = "nep141:17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1"
MIN_USDC = 0.5
FLAT_FEE_USDC = 0.2

Http = Callable[[str, str, Optional[dict], dict], Any]


class IntentsError(RuntimeError):
    pass


def http(method: str, url: str, body: Optional[dict], headers: dict) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", "User-Agent": "zcastor-near", **headers})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        text = exc.read().decode(errors="replace")[:400] if exc.fp else ""
        raise IntentsError(f"1-Click answered HTTP {exc.code}: {text}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise IntentsError(f"cannot reach 1-Click: {getattr(exc, 'reason', exc)}") from None


class OneClick:
    def __init__(self, transport: Http = http, jwt: str = "", base: str = BASE) -> None:
        self._http = transport
        self._base = base.rstrip("/")
        jwt = jwt or os.environ.get("ONECLICK_JWT", "")
        self._headers = {"Authorization": f"Bearer {jwt}"} if jwt else {}
        self.authenticated = bool(jwt)

    def tokens(self) -> list:
        return self._http("GET", f"{self._base}/v0/tokens", None, self._headers)

    def near_usdc(self) -> str:
        """Native USDC on NEAR, from the live token list; the known id if that fails."""
        try:
            for t in self.tokens():
                if (t.get("symbol") == "USDC" and t.get("blockchain") == "near"
                        and t.get("assetId") == NEAR_USDC):
                    return t["assetId"]
        except IntentsError:
            pass
        return NEAR_USDC

    def funding_request(self, *, usdc: float, recipient: str, refund_to: str,
                        origin_asset: str = NEAR_USDC, origin_decimals: int = 6,
                        dry: bool = True, minutes: int = 30,
                        now: Optional[datetime] = None) -> dict:
        if usdc < MIN_USDC:
            raise IntentsError(f"minimum Hyperliquid deposit is {MIN_USDC} USDC")
        if not (recipient.startswith("0x") and len(recipient) == 42):
            raise IntentsError("recipient must be a Hyperliquid 0x address")
        if not refund_to:
            raise IntentsError("set NEAR_REFUND_ACCOUNT (where a failed route refunds)")
        deadline = (now or datetime.now(timezone.utc)) + timedelta(minutes=minutes)
        return {
            "dry": bool(dry),
            "swapType": "EXACT_INPUT",
            "slippageTolerance": 100,
            "originAsset": origin_asset,
            "depositType": "ORIGIN_CHAIN",
            "destinationAsset": HYPERLIQUID_USDC,
            "amount": str(int(round(usdc * 10 ** origin_decimals))),
            "recipient": recipient,
            "recipientType": "DESTINATION_CHAIN",
            "refundTo": refund_to,
            "refundType": "ORIGIN_CHAIN",
            "deadline": deadline.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }

    def quote(self, request: dict) -> dict:
        if not request.get("dry"):
            # This tool prices routes. Moving money is the owner's act, from
            # their own wallet, never something a script here does.
            raise IntentsError("refusing a non-dry quote: this tool only prices routes")
        return self._http("POST", f"{self._base}/v0/quote", request, self._headers)


def summarise(request: dict, response: dict) -> dict:
    q = response.get("quote", response) or {}
    return {
        "route": "USDC on NEAR -> Hyperliquid perps balance, via NEAR Intents 1-Click",
        "dry_run": True,
        "send": q.get("amountInFormatted") or q.get("amountIn"),
        "receive": q.get("amountOutFormatted") or q.get("amountOut"),
        "minimum_received": q.get("minAmountOutFormatted") or _units(q.get("minAmountOut"), 8),
        "estimated_seconds": q.get("timeEstimate"),
        "recipient": request["recipient"],
        "refund_to": request["refundTo"],
        "deadline": request["deadline"],
        "flat_fee_usdc": FLAT_FEE_USDC,
        "deposit_address": q.get("depositAddress"),
    }


def _units(raw, decimals: int):
    """A raw integer amount as a decimal string; Hyperliquid USDC has 8 places."""
    try:
        return f"{int(raw) / 10 ** decimals:.6f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return raw


def save(request: dict, response: dict, root: Path = ROOT / "data" / "near" / "quotes") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    path.write_text(json.dumps({"request": request, "response": response}, indent=2))
    return path


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="near/intents.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("quote", help="dry-run quote to fund an agent's Hyperliquid balance")
    q.add_argument("--agent", help="agent name; uses its address from data/near/addresses.json")
    q.add_argument("--recipient", help="a Hyperliquid 0x address, instead of --agent")
    q.add_argument("--usdc", type=float, required=True)
    q.add_argument("--refund-to", default=os.environ.get("NEAR_REFUND_ACCOUNT", ""))
    t = sub.add_parser("tokens")
    t.add_argument("--symbol", default="")
    a = ap.parse_args(argv)

    api = OneClick()
    try:
        if a.cmd == "tokens":
            rows = [x for x in api.tokens()
                    if not a.symbol or x.get("symbol") == a.symbol.upper()]
            for x in rows:
                print(f"{x.get('symbol', ''):8} {x.get('blockchain', ''):10} "
                      f"{x.get('decimals', '')!s:3} {x.get('assetId', '')}")
            return 0
        recipient = a.recipient
        if not recipient:
            if not a.agent:
                ap.error("give --agent or --recipient")
            sys.path.insert(0, str(ROOT / "near"))
            import vault_client
            owner = os.environ.get("NEAR_OWNER", "")
            account = a.agent if "." in a.agent else f"{a.agent}.{owner}"
            recipient = vault_client.cached_address(account)
            if not recipient:
                raise IntentsError(f"no address known for {account}; run "
                                   f"`near/vault_client.py address {a.agent}` first")
        request = api.funding_request(usdc=a.usdc, recipient=recipient,
                                      refund_to=a.refund_to,
                                      origin_asset=api.near_usdc())
        response = api.quote(request)
    except IntentsError as exc:
        print(f"quote failed: {exc}", file=sys.stderr)
        return 1
    path = save(request, response)
    out = summarise(request, response)
    out["saved"] = str(path)
    out["authenticated"] = api.authenticated
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
