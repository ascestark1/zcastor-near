"""
The perp universe — which Hyperliquid perps the desk analyses.

The rule lives in config/universe.json and is part of the record: the universe
document carries the rule and its checksum, so "why was SOL in the desk on
9 November and not on the 2nd" has an answer anyone can check. A ticker joining
or leaving is a decision, and it is recorded like one.

Two floors, then a rank. A contract must be trading, perpetual, USDC-margined,
a coin, old enough to have a history (where the venue publishes a listing
date; Hyperliquid does not, so its floors and the history check do that job), and above
both liquidity floors. Survivors are ranked on each metric in `rank_by`
(1 = best) and ordered by the mean of their ranks, so a contract that is
huge on volume but thin on open interest does not outrank one that is solid on
both. `always_include` contracts are added if the rank did not pick them, and
marked `forced`, because the record must show they were chosen by hand.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Optional

DAY_MS = 86_400_000

DEFAULT_RULE: dict[str, Any] = {
    "quote_asset": "USDC",
    "contract_type": "PERPETUAL",
    "underlying_types": ["COIN"],
    "top_n": 30,
    "min_quote_volume_24h_usd": 50_000_000,
    "min_open_interest_usd": 20_000_000,
    "min_listing_age_days": 90,
    "rank_by": ["quote_volume_24h", "open_interest_usd"],
    "always_include": ["BTC"],
    "exclude": [],
}


def rule_from(config: Mapping[str, Any]) -> dict[str, Any]:
    """Merge a config file over the defaults; underscore keys are notes."""
    rule = dict(DEFAULT_RULE)
    rule.update({k: v for k, v in config.items() if not k.startswith("_")})
    return rule


def eligible(exchange_info: Mapping[str, Any], rule: Mapping[str, Any],
             now_ms: int) -> tuple[dict[str, dict], dict[str, int]]:
    """Contracts that pass the structural filters, and counts of the rest."""
    rejected: dict[str, int] = {}
    keep: dict[str, dict] = {}
    excluded = {s.upper() for s in rule.get("exclude", [])}
    forced = {s.upper() for s in rule.get("always_include", [])}
    min_age = int(rule.get("min_listing_age_days", 0)) * DAY_MS
    kinds = set(rule.get("underlying_types") or [])

    def reject(why: str) -> None:
        rejected[why] = rejected.get(why, 0) + 1

    for info in exchange_info.get("symbols", []):
        # Names are kept exactly as the venue spells them: Hyperliquid's 1000x
        # contracts are lower-case-k (kPEPE), and upper-casing them would lose
        # their tickers. Rule lists still match case-insensitively.
        sym = str(info.get("symbol", ""))
        key = sym.upper()
        if info.get("status") != "TRADING":
            reject("not_trading"); continue
        if info.get("contractType") != rule["contract_type"]:
            reject("not_perpetual"); continue
        if info.get("quoteAsset") != rule["quote_asset"] or \
           info.get("marginAsset", rule["quote_asset"]) != rule["quote_asset"]:
            reject("other_quote"); continue
        if kinds and info.get("underlyingType") not in kinds and key not in forced:
            reject("not_a_coin"); continue
        if key in excluded:
            reject("excluded_by_rule"); continue
        onboard = int(info.get("onboardDate") or 0)
        if min_age and onboard and now_ms - onboard < min_age and key not in forced:
            reject("too_new"); continue
        keep[sym] = info
    return keep, rejected


def rank(candidates: Mapping[str, Mapping[str, float]],
         metrics: list[str]) -> list[tuple[str, float]]:
    """Mean rank across metrics, best first. Ties go to higher volume."""
    positions: dict[str, list[int]] = {s: [] for s in candidates}
    for m in metrics:
        ordered = sorted(candidates, key=lambda s: -float(candidates[s].get(m) or 0))
        for i, sym in enumerate(ordered, start=1):
            positions[sym].append(i)
    scored = [(s, sum(p) / len(p)) for s, p in positions.items()]
    scored.sort(key=lambda t: (t[1], -float(candidates[t[0]].get("quote_volume_24h") or 0)))
    return scored


def build_universe(client, rule: Mapping[str, Any],
                   now: Optional[datetime] = None,
                   source: str = "hyperliquid-public") -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)

    info, rejected = eligible(client.exchange_info(), rule, now_ms)
    tickers = {t["symbol"]: t for t in client.tickers_24h() if t.get("symbol") in info}
    marks = {p["symbol"]: p for p in client.premium_index() if p.get("symbol") in info}
    by_upper = {s.upper(): s for s in info}
    forced = [by_upper.get(s.upper(), s) for s in rule.get("always_include", [])]

    vol_floor = float(rule.get("min_quote_volume_24h_usd", 0))
    oi_floor = float(rule.get("min_open_interest_usd", 0))

    stats: dict[str, dict[str, float]] = {}
    for sym in info:
        qv = float(tickers.get(sym, {}).get("quoteVolume") or 0)
        mark = float(marks.get(sym, {}).get("markPrice") or 0)
        stats[sym] = {"quote_volume_24h": qv, "mark_price": mark,
                      "funding_rate_last": float(marks.get(sym, {}).get("lastFundingRate") or 0)}

    # Open interest costs one call per contract, so only ask for contracts that
    # cleared the volume floor (and the forced ones, whose numbers we report).
    passing: dict[str, dict[str, float]] = {}
    for sym, s in stats.items():
        if s["quote_volume_24h"] < vol_floor and sym not in forced:
            rejected["below_volume_floor"] = rejected.get("below_volume_floor", 0) + 1
            continue
        oi = float(client.open_interest(sym).get("openInterest") or 0)
        s["open_interest_usd"] = oi * s["mark_price"]
        if s["open_interest_usd"] < oi_floor and sym not in forced:
            rejected["below_open_interest_floor"] = rejected.get("below_open_interest_floor", 0) + 1
            continue
        passing[sym] = s

    metrics = list(rule.get("rank_by") or ["quote_volume_24h"])
    ranked = rank({s: v for s, v in passing.items()}, metrics)
    top_n = int(rule.get("top_n", 30))

    chosen = [s for s, _ in ranked[:top_n]]
    score = dict(ranked)
    members = []
    for i, sym in enumerate(chosen, start=1):
        members.append(_member(sym, i, score[sym], passing[sym], info[sym], False))
    for sym in forced:
        if sym not in chosen and sym in passing:
            members.append(_member(sym, None, score.get(sym), passing[sym], info[sym], True))
        elif sym not in passing:
            rejected["forced_but_unavailable"] = rejected.get("forced_but_unavailable", 0) + 1

    return {
        "schema": "zcastor/universe",
        "schema_version": "1",
        "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "rule": dict(rule),
        "considered": len(info),
        "passed_floors": len(passing),
        "rejected": dict(sorted(rejected.items())),
        "members": members,
    }


def _member(sym, position, score, s, info, forced) -> dict[str, Any]:
    return {
        "symbol": sym,
        "rank": position,
        "score": round(score, 3) if score is not None else None,
        "forced": forced,
        "quote_volume_24h": round(s["quote_volume_24h"], 2),
        "open_interest_usd": round(s.get("open_interest_usd", 0.0), 2),
        "mark_price": s["mark_price"],
        "funding_rate_last": s["funding_rate_last"],
        "listed_at": _iso_ms(info.get("onboardDate")),
    }


def _iso_ms(ms) -> Optional[str]:
    if not ms:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%d")
