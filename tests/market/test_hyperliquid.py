"""
Hyperliquid market data and the hub, against a fake /info endpoint.
"""

import json
import sys
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "market"))

from fake_hl import FakeHL  # noqa: E402

from zcastor.market.hub import Hub, serve  # noqa: E402
from zcastor.market.hyperliquid import (MAX_CANDLES, BinanceView, HLHTTPError,  # noqa: E402
                                        HLInfo, request_weight, tick_size)


class Clock:
    def __init__(self, t=1_761_300_000.0):
        self.t = t
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def view(fake=None, clock=None, budget=800):
    clock = clock or Clock()
    fake = fake or FakeHL(now_ms=int(clock.t * 1000))
    info = HLInfo(fake, weight_budget=budget, sleep=clock.sleep, clock=clock)
    return BinanceView(info, clock=clock), fake, clock


# ── price rules ──────────────────────────────────────────────────────────────

def test_tick_size_follows_hyperliquid_price_rules():
    assert tick_size(110_000, 5) == "1"          # integers always valid
    assert tick_size(2_400, 4) == "0.1"          # 5 significant figures
    assert tick_size(180.25, 2) == "0.01"
    assert tick_size(0.0105, 0) == "0.000001"    # decimal cap: 6 - szDecimals
    assert tick_size(1.5, 1) == "0.0001"


# ── weights and throttling ───────────────────────────────────────────────────

def test_weights():
    assert request_weight({"type": "l2Book"}) == 2
    assert request_weight({"type": "metaAndAssetCtxs"}) == 20
    assert request_weight({"type": "candleSnapshot"}, [0] * 600) == 30


def test_client_sleeps_into_next_minute_before_exceeding_budget():
    v, fake, clock = view(budget=50)
    for _ in range(3):
        v.refresh(force=True)
    assert clock.slept, "third 20-weight call must wait for the next minute"


def test_transient_errors_retry_then_succeed():
    clock = Clock()
    fake = FakeHL(now_ms=int(clock.t * 1000))
    state = {"n": 0}

    def flaky(body):
        state["n"] += 1
        if state["n"] < 3:
            raise HLHTTPError(429, "slow down")
        return fake(body)

    info = HLInfo(flaky, sleep=clock.sleep, clock=clock)
    specs, _ = info.meta_and_ctxs()
    assert specs and state["n"] == 3


def test_network_failure_ends_with_a_clear_message():
    clock = Clock()

    def down(body):
        raise HLHTTPError(0, "network: no route")

    info = HLInfo(down, sleep=clock.sleep, clock=clock, max_retries=2)
    try:
        info.meta_and_ctxs()
        assert False
    except RuntimeError as exc:
        assert "cannot reach Hyperliquid" in str(exc)


# ── the Binance-shaped view ──────────────────────────────────────────────────

def test_exchange_info_lists_every_perp_with_specs():
    v, *_ = view()
    syms = {s["symbol"]: s for s in v.exchange_info()["symbols"]}
    assert syms["BTC"]["status"] == "TRADING"
    assert syms["OLD"]["status"] == "BREAK"
    assert syms["ETH"]["quoteAsset"] == "USDC"
    f = {x["filterType"]: x for x in syms["ETH"]["filters"]}
    assert f["PRICE_FILTER"]["tickSize"] == "0.1"
    assert f["LOT_SIZE"]["stepSize"] == "0.0001"


def test_one_context_call_serves_every_symbol():
    v, fake, _ = view()
    for coin in ("BTC", "ETH", "SOL", "kPEPE"):
        v.ticker_24h(coin)
        v.premium(coin)
        v.open_interest(coin)
    assert [c["type"] for c in fake.calls] == ["metaAndAssetCtxs"]


def test_funding_is_reported_as_an_eight_hour_rate():
    v, *_ = view()
    p = v.premium("BTC")
    assert float(p["hourlyFundingRate"]) == 0.0000125
    assert abs(float(p["lastFundingRate"]) - 0.0001) < 1e-12


def test_ticker_fields():
    v, *_ = view()
    t = v.ticker_24h("ETH")
    assert float(t["openPrice"]) == 2400 * 0.98
    assert float(t["quoteVolume"]) == 1.2e9


def test_klines_are_binance_arrays_in_order():
    v, *_ = view()
    rows = v.klines("ETH", "15m", limit=100)
    assert len(rows) == 100
    assert all(len(r) == 12 for r in rows)
    assert [r[0] for r in rows] == sorted(r[0] for r in rows)
    assert rows[1][0] - rows[0][0] == 900_000
    assert float(rows[0][2]) >= float(rows[0][3])


def test_kline_history_stops_where_hyperliquid_stops():
    v, *_ = view()
    rows = v.klines("BTC", "1m", start_ms=0, end_ms=int(v._clock() * 1000),
                    limit=100_000)
    assert len(rows) == MAX_CANDLES


def test_book_and_funding_history():
    v, *_ = view()
    b = v.book_ticker("SOL")
    assert float(b["askPrice"]) > float(b["bidPrice"])
    now = int(v._clock() * 1000)
    hist = v.funding_history("SOL", now - 30 * 86_400_000, now)
    assert len(hist) >= 700        # paginated past the 500-row page


# ── the hub ──────────────────────────────────────────────────────────────────

def test_hub_caches_live_candles_and_refetches_only_the_tail():
    v, fake, clock = view()
    hub = Hub(v, clock=clock)
    first = hub.klines("ETH", "5m", 1500)
    assert len(first) == 1500
    n = len(fake.calls)
    hub.klines("ETH", "5m", 1500)
    assert len(fake.calls) == n, "a fresh cache must not call Hyperliquid"
    clock.t += 300
    fake.now_ms = int(clock.t * 1000)
    again = hub.klines("ETH", "5m", 1500)
    tail = fake.calls[-1]["req"]
    assert tail["startTime"] >= first[-1][0], "stale cache fetches the tail only"
    assert again[-1][0] == first[-1][0] + 300_000


def test_hub_rejects_unknown_symbols():
    v, *_ = view()
    hub = Hub(v)
    try:
        hub.ticker("NOPE")
        assert False
    except ValueError:
        pass


def test_hub_over_http_answers_every_dashboard_route():
    v, fake, clock = view()
    hub = Hub(v, clock=clock)
    server = serve(hub, port=0, ctx_ttl=3600)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    def get(path):
        req = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10)
        assert req.headers["Access-Control-Allow-Origin"] == "*"
        return json.loads(req.read())

    try:
        assert get("/fapi/v1/ticker/24hr?symbol=BTC")["lastPrice"]
        assert get("/fapi/v1/premiumIndex?symbol=BTC")["lastFundingRate"]
        assert get("/fapi/v1/openInterest?symbol=BTC")["openInterest"]
        kl = get("/fapi/v1/klines?symbol=BTC&interval=1d&limit=30")
        assert len(kl) == 30
        page = get(f"/fapi/v1/klines?symbol=BTC&interval=15m&limit=500"
                   f"&endTime={kl[-1][0]}")
        assert len(page) == 500 and page[-1][0] <= kl[-1][0]
        bb = get("/v5/market/tickers?category=linear&symbol=BTC")
        assert float(bb["result"]["list"][0]["markPrice"]) > 0
        h = get("/health")
        assert h["ok"] and h["coins"] == 6
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/fapi/v1/ticker/24hr?symbol=XX")
            assert False
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
    finally:
        server.shutdown()



def test_scale_comes_from_the_profile_then_falls_back_to_price_ratio():
    import tempfile
    v, *_ = view()
    with tempfile.TemporaryDirectory() as d:
        hub = Hub(v, profiles=Path(d))
        assert hub.scale("BTC") == {"symbol": "BTC", "scale": 1.0, "basis": "reference"}
        r = hub.scale("ETH")
        assert r["basis"].startswith("price_ratio") and abs(r["scale"] - 2400 / 110000) < 1e-12
        (Path(d) / "ETH.json").write_text(json.dumps({
            "venue": "hyperliquid", "generated_at": "2026-10-24T00:00:00Z",
            "btc_scale": {"by_tf": {"1h": 0.0411}}}))
        r = hub.scale("ETH")
        assert r["scale"] == 0.0411 and r["basis"].startswith("profile")
        (Path(d) / "SOL.json").write_text(json.dumps({          # an old Binance profile
            "venue": "binance-usdm", "btc_scale": {"by_tf": {"1h": 0.5}}}))
        assert hub.scale("SOL")["basis"].startswith("price_ratio")

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
