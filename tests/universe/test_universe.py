"""
Universe and profile tests. A fake Binance, no network.

The property that matters most is the last group: BTC's scale is pinned to 1.0,
so every BTC policy setting maps to itself and the ATR layer cannot move a BTC
decision. Everything else is checked so a refresh that silently produces a
different universe or a wrong tick rounding fails here first.
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from universe import history, indicators, profiles, selection  # noqa: E402
from zcastor.record.canonical import checksum_bytes  # noqa: E402

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
OLD = NOW_MS - 400 * 86_400_000
POLICY = json.loads((ROOT / "config" / "policy.json").read_text())


def contract(sym, *, status="TRADING", kind="PERPETUAL", quote="USDC",
             underlying="COIN", onboard=OLD, tick="0.10", step="0.001"):
    return {"symbol": sym, "status": status, "contractType": kind,
            "quoteAsset": quote, "marginAsset": quote, "underlyingType": underlying,
            "onboardDate": onboard, "pricePrecision": 2, "quantityPrecision": 3,
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": tick, "minPrice": "0.1"},
                {"filterType": "LOT_SIZE", "stepSize": step, "minQty": step, "maxQty": "1000"},
                {"filterType": "MARKET_LOT_SIZE", "stepSize": step, "maxQty": "100"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"}]}


class FakeBinance:
    """Volumes and OI in USD are chosen so the expected ranking is obvious."""

    def __init__(self, bars=None):
        self.contracts = [
            contract("BTC"), contract("ETHUSDT", tick="0.01"),
            contract("SOLUSDT", tick="0.010"), contract("DOGEUSDT", tick="0.00001"),
            contract("THINUSDT"),                                  # below volume floor
            contract("NEWUSDT", onboard=NOW_MS - 10 * 86_400_000),  # too new
            contract("BTCDOMUSDT", underlying="INDEX"),             # not a coin
            contract("ETHUSDT_261225", kind="CURRENT_QUARTER"),     # not perpetual
            contract("OLDUSDT", status="SETTLING"),                 # not trading
            contract("BTC_USDT", quote="USDT"),                     # other quote
        ]
        self.volume = {"BTC": 9e9, "ETHUSDT": 5e9, "SOLUSDT": 2e9,
                       "DOGEUSDT": 8e8, "THINUSDT": 1e6, "NEWUSDT": 9e8}
        self.mark = {"BTC": 80_000.0, "ETHUSDT": 4_000.0, "SOLUSDT": 200.0,
                     "DOGEUSDT": 0.2, "THINUSDT": 1.0, "NEWUSDT": 1.0}
        # OI in USD: BTC 8bn, ETH 4bn, SOL 1bn, DOGE 5bn (DOGE strong on OI)
        self.oi_usd = {"BTC": 8e9, "ETHUSDT": 4e9, "SOLUSDT": 1e9,
                       "DOGEUSDT": 5e9, "THINUSDT": 1e5}
        self.bars = bars or {}
        self.oi_calls = []
        self.used_weight = 0
        self.requests = 0

    def exchange_info(self):
        return {"symbols": self.contracts}

    def tickers_24h(self):
        return [{"symbol": s, "quoteVolume": str(v)} for s, v in self.volume.items()]

    def premium_index(self):
        return [{"symbol": s, "markPrice": str(p), "lastFundingRate": "0.0001"}
                for s, p in self.mark.items()]

    def open_interest(self, sym):
        self.oi_calls.append(sym)
        return {"openInterest": str(self.oi_usd.get(sym, 0) / self.mark[sym])}

    def book_tickers(self):
        return [{"symbol": "ETHUSDT", "bidPrice": "4000.00", "askPrice": "4000.02"},
                {"symbol": "BTC", "bidPrice": "80000.00", "askPrice": "80000.10"}]

    def funding_history(self, sym, start, end):
        return [{"fundingRate": "0.0001"}, {"fundingRate": "-0.0003"}]

    def klines(self, sym, interval, start, end, limit=1000):
        out = [k for k in self.bars.get(sym, []) if start <= k[0] <= end]
        return out[:limit]


RULE = selection.rule_from({"top_n": 2, "min_quote_volume_24h_usd": 5e8,
                            "min_open_interest_usd": 5e8, "min_listing_age_days": 90,
                            "always_include": ["BTC"], "exclude": []})


# ── selection ─────────────────────────────────────────────────────────────────

def test_structural_filters_reject_for_the_stated_reason():
    keep, rejected = selection.eligible(FakeBinance().exchange_info(), RULE, NOW_MS)
    assert set(keep) == {"BTC", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "THINUSDT"}
    assert rejected == {"not_trading": 1, "not_perpetual": 1, "other_quote": 1,
                        "not_a_coin": 1, "too_new": 1}


def test_open_interest_is_only_asked_for_contracts_above_the_volume_floor():
    fake = FakeBinance()
    selection.build_universe(fake, RULE, NOW)
    assert "THINUSDT" not in fake.oi_calls


def test_rank_is_the_mean_of_volume_rank_and_oi_rank():
    doc = selection.build_universe(FakeBinance(), RULE, NOW)
    # volume: BTC 1, ETH 2, SOL 3, DOGE 4 ; OI: BTC 1, DOGE 2, ETH 3, SOL 4
    # means: BTC 1.0, ETH 2.5, DOGE 3.0, SOL 3.5 → top 2 is BTC, ETH
    assert [m["symbol"] for m in doc["members"]] == ["BTC", "ETHUSDT"]
    assert doc["members"][1]["score"] == 2.5


def test_a_forced_contract_outside_the_top_n_is_added_and_marked():
    rule = dict(RULE, top_n=1, always_include=["BTC", "SOLUSDT"])
    doc = selection.build_universe(FakeBinance(), rule, NOW)
    by = {m["symbol"]: m for m in doc["members"]}
    assert by["SOLUSDT"]["forced"] is True and by["SOLUSDT"]["rank"] is None
    assert by["BTC"]["forced"] is False


def test_the_universe_document_carries_its_rule():
    doc = selection.build_universe(FakeBinance(), RULE, NOW)
    assert doc["rule"]["top_n"] == 2 and doc["as_of"] == "2026-10-20T12:00:00Z"


def test_underscore_keys_in_the_config_are_notes_not_rules():
    rule = selection.rule_from({"_status": "draft", "top_n": 5})
    assert "_status" not in rule and rule["top_n"] == 5


# ── history ───────────────────────────────────────────────────────────────────

def klines_for(day0, days, *, start_price=100.0, step=0.0, rng=1.0, minutes=1440):
    out = []
    t0 = int(day0.timestamp() * 1000)
    p = start_price
    for d in range(days):
        for m in range(minutes):
            t = t0 + (d * 1440 + m) * 60_000
            o = p
            p = p + step
            out.append([t, f"{o:.2f}", f"{max(o, p) + rng / 2:.2f}",
                        f"{min(o, p) - rng / 2:.2f}", f"{p:.2f}", "1.0",
                        t + 59_999, "100.0", 5, "0.5", "50", "0"])
    return out


def test_only_complete_days_are_fetched_and_today_is_never_written():
    today = NOW.replace(hour=0, minute=0)
    fake = FakeBinance({"ETHUSDT": klines_for(today - timedelta(days=3), 4)})
    with tempfile.TemporaryDirectory() as d:
        rep = history.download(fake, ["ETHUSDT"], root=Path(d), days=3, now=NOW,
                               log=lambda *_: None)
        files = sorted(p.name for p in (Path(d) / "ETHUSDT" / "1m").glob("*.csv"))
        assert files == ["2026-10-17.csv", "2026-10-18.csv", "2026-10-19.csv"]
        assert rep["ETHUSDT"] == {"fetched": 3, "skipped": 0, "short": 0}
        again = history.download(fake, ["ETHUSDT"], root=Path(d), days=3, now=NOW,
                                 log=lambda *_: None)
        assert again["ETHUSDT"]["skipped"] == 3


def test_a_short_day_is_kept_and_reported():
    day = NOW.replace(hour=0, minute=0) - timedelta(days=1)
    fake = FakeBinance({"SOLUSDT": klines_for(day, 1, minutes=600)})
    with tempfile.TemporaryDirectory() as d:
        rep = history.download(fake, ["SOLUSDT"], root=Path(d), days=1, now=NOW,
                               log=lambda *_: None)
        assert rep["SOLUSDT"]["short"] == 1


def test_a_rebuilt_day_file_hashes_the_same():
    day = NOW.replace(hour=0, minute=0) - timedelta(days=1)
    fake = FakeBinance({"ETHUSDT": klines_for(day, 1)})
    digests = []
    for _ in range(2):
        with tempfile.TemporaryDirectory() as d:
            history.download(fake, ["ETHUSDT"], root=Path(d), days=1, now=NOW,
                             log=lambda *_: None)
            digests.append(checksum_bytes(
                (Path(d) / "ETHUSDT" / "1m" / "2026-10-19.csv").read_bytes()))
    assert digests[0] == digests[1]


# ── indicators ────────────────────────────────────────────────────────────────

def flat_bars(n, rng=2.0, price=100.0):
    return [{"t": i * 60, "open": price, "high": price + rng / 2,
             "low": price - rng / 2, "close": price} for i in range(n)]


def test_resampling_aligns_to_utc_boundaries_and_keeps_extremes():
    bars = flat_bars(10)
    bars[3]["high"] = 150.0
    five = indicators.resample(bars, 5)
    assert len(five) == 2 and five[0]["t"] == 0 and five[1]["t"] == 300
    assert five[0]["high"] == 150.0 and five[1]["high"] == 101.0


def test_atr_of_constant_bars_is_their_range():
    assert abs(indicators.median_atr(flat_bars(60, rng=3.0)) - 3.0) < 1e-12


def test_session_ranges_use_zcastors_session_table():
    from zcastor.market.session import SESSION_START_UTC_MIN
    bars = flat_bars(1440, rng=1.0)
    for b in bars:                       # London 08:00–13:30 gets a wide bar
        if (b["t"] // 60) == 9 * 60:
            b["high"] = 110.0
    r = indicators.session_ranges(bars, SESSION_START_UTC_MIN)
    assert r["London"] == 110.0 - 99.5 and r["Asian"] == 1.0


# ── profiles ──────────────────────────────────────────────────────────────────





def test_converted_distances_are_rounded_to_the_contracts_tick():
    assert profiles.round_to_tick(12.3456, "0.01") == (12.35, 1235)
    assert profiles.round_to_tick(0.000123456, "0.00001") == (0.00012, 12)


def test_spread_settings_are_costs_and_are_not_scaled_by_atr():
    prev = profiles.policy_preview(POLICY, {tf: 0.05 for tf in profiles.TIMEFRAMES}, "0.01")
    assert prev["spread_hard_block_pts"]["basis"] == "cost"
    assert "price" not in prev["spread_hard_block_pts"]
    assert prev["resolver.default_spread_pts"]["basis"] == "cost"


def test_every_point_setting_in_the_policy_appears_in_the_preview():
    prev = profiles.policy_preview(POLICY, {tf: 1.0 for tf in profiles.TIMEFRAMES}, "0.10")
    keys = {".".join(map(str, p)) for p, _ in profiles._walk(POLICY)
            if any("pts" in str(x) for x in p)}
    assert keys == set(prev) and len(keys) >= 30


def test_generate_writes_canonical_profiles_and_an_index():
    day0 = NOW.replace(hour=0, minute=0) - timedelta(days=2)
    fake = FakeBinance({"BTC": klines_for(day0, 2, start_price=80_000, rng=400),
                        "ETHUSDT": klines_for(day0, 2, start_price=4_000, rng=20)})
    uni = {"as_of": "2026-10-20T00:00:00Z",
           "members": [{"symbol": "BTC"}, {"symbol": "ETHUSDT"}, {"symbol": "SOLUSDT"}]}
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        history.download(fake, ["BTC", "ETHUSDT"], root=root / "h", days=2,
                         now=NOW, log=lambda *_: None)
        idx = profiles.generate(fake, uni, history_root=root / "h", out_root=root / "p",
                                policy=POLICY, days=2, now=NOW, log=lambda *_: None)
        assert [e["symbol"] for e in idx["profiles"]] == ["BTC", "ETHUSDT"]
        assert idx["missing"] == ["SOLUSDT"]
        eth = json.loads((root / "p" / "ETHUSDT.json").read_text())
        assert abs(eth["btc_scale"]["by_tf"]["1h"] - 0.05) < 0.01
        assert eth["spec"]["tick_size"] == "0.01" and eth["spread"]["ticks"] == 2
        btc = json.loads((root / "p" / "BTC.json").read_text())
        assert btc["btc_scale"]["pinned"] is True and btc["stage"] == "reference"
        raw = (root / "p" / "ETHUSDT.json").read_bytes()
        assert checksum_bytes(raw) == idx["profiles"][1]["checksum"]


def test_profiles_refuse_to_run_without_btc_to_scale_against():
    try:
        profiles.generate(FakeBinance(), {"members": [{"symbol": "ETHUSDT"}]},
                          history_root=Path("/nonexistent"), out_root=Path("/nonexistent"),
                          policy=POLICY)
    except ValueError as exc:
        assert "BTC" in str(exc)
    else:
        raise AssertionError("must refuse")



# ── end to end on Hyperliquid (fake) ──────────────────────────────────────────

def test_select_history_profiles_on_hyperliquid_5m_bars():
    sys.path.insert(0, str(ROOT / "tests" / "market"))
    from fake_hl import FakeHL
    from zcastor.market.hyperliquid import BinanceView, HLInfo
    clock = lambda: NOW.timestamp()  # noqa: E731
    view = BinanceView(HLInfo(FakeHL(now_ms=NOW_MS), sleep=lambda s: None,
                              clock=clock), clock=clock)
    rule = selection.rule_from({"top_n": 3, "min_quote_volume_24h_usd": 1e7,
                                "min_open_interest_usd": 1e6,
                                "min_listing_age_days": 90,
                                "always_include": ["BTC"], "exclude": []})
    doc = selection.build_universe(view, rule, now=NOW)
    names = [m["symbol"] for m in doc["members"]]
    assert names[0] == "BTC" and "TINY" not in names and "OLD" not in names
    assert "kPEPE" in [m["symbol"] for m in selection.build_universe(
        view, dict(rule, top_n=10), now=NOW)["members"]], "1000x names keep their case"
    assert doc["source"] == "hyperliquid-public"
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        report = history.download(view, names, root=root / "h", interval="5m",
                                  days=3, now=NOW, log=lambda *_: None)
        assert all(r["fetched"] == 3 and r["short"] == 0 for r in report.values())
        idx = profiles.generate(view, doc, history_root=root / "h",
                                out_root=root / "p", policy=POLICY, days=3,
                                now=NOW, log=lambda *_: None, interval="5m")
        assert idx["missing"] == []
        eth = json.loads((root / "p" / "ETH.json").read_text())
        assert eth["venue"] == "hyperliquid"
        assert eth["window"]["bar_minutes"] == 5 and eth["window"]["missing_bars"] == 0
        assert eth["spec"]["tick_size"] == "0.1"
        assert eth["funding"]["interval_hours"] == 1
        assert all(v is not None for v in eth["btc_scale"]["by_tf"].values())
        btc = json.loads((root / "p" / "BTC.json").read_text())
        assert btc["btc_scale"]["pinned"] is True

if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
                passed += 1
            except Exception as e:
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
