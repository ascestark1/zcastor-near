"""
The allocator: agents bid for the desk's shared risk budget, the vault decides.
Runs against SimVault, which applies the contract's refusal rules in memory.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "near"))

import allocator as al  # noqa: E402

OWNER = "desk.testnet"


def desk(**records):
    agents = {}
    for name in ("btc", "eth", "sol"):
        a = dict(al.DEMO_AGENT)
        a.update(records.get(name, {}))
        agents[f"{name}.{OWNER}"] = a
    return al.SimVault(al.DEMO_LIMITS, agents, owner=OWNER)


def bid(agent, score, risk=5000, sig=None, leverage=10, notional=250000):
    return al.Bid(f"{agent}.{OWNER}", sig or f"{agent}-{score}", "Buy", notional, risk,
                  leverage, score)


def test_the_best_bids_are_funded_and_the_last_one_is_refused_by_the_desk_budget():
    v = desk()
    bids = [bid("eth", 0.58, sig="e2"), bid("btc", 0.81, sig="b1"), bid("sol", 0.74, sig="s1"),
            bid("btc", 0.66, sig="b2"), bid("eth", 0.77, sig="e1")]
    r = al.run_round(v, bids)
    assert [x["signal_hash"] for x in r["bids"]] == ["b1", "e1", "s1", "b2", "e2"]
    assert [x["granted"] for x in r["bids"]] == [True, True, True, True, False]
    assert r["bids"][-1]["actual"] == "over_desk_budget"
    assert r["granted_cents"] == 20000 and r["bid_for_cents"] == 25000
    assert r["all_as_predicted"]


def test_the_vault_decides_not_the_allocator():
    # Ranked first, still refused: the bid breaks the per-trade limit.
    v = desk()
    r = al.run_round(v, [bid("btc", 0.99, risk=9000, sig="greedy"), bid("eth", 0.2, sig="ok")])
    assert r["bids"][0]["signal_hash"] == "greedy"
    assert r["bids"][0]["granted"] is False and r["bids"][0]["actual"] == "over_trade_risk"
    assert r["bids"][1]["granted"] is True
    assert len(v.decisions) == 2          # the refusal is recorded too


def test_an_agent_cannot_take_more_than_its_own_budget():
    v = desk()
    r = al.run_round(v, [bid("btc", 0.9, sig="1"), bid("btc", 0.8, sig="2"),
                         bid("btc", 0.7, sig="3")])
    assert [x["actual"] for x in r["bids"]] == ["within_limits", "within_limits",
                                                "over_agent_budget"]
    assert r["funded_by_agent_cents"] == {f"btc.{OWNER}": 10000}


def test_a_losing_record_gives_an_agent_less_say():
    # Same score; btc has lost 3 of 3, eth is new.
    v = desk(btc={"wins": 0, "losses": 3})
    ranked = al.rank([bid("btc", 0.8), bid("eth", 0.8)], dict(v.view("list_agents")))
    assert ranked[0][0].agent == f"eth.{OWNER}"
    assert al.reputation({"wins": 0, "losses": 3}) == 0.2
    assert al.reputation({}) == 0.5
    assert al.reputation({"wins": 3, "losses": 0}) == 0.8


def test_ties_are_broken_the_same_way_every_time():
    a, b = bid("eth", 0.5, sig="x"), bid("btc", 0.5, sig="y")
    agents = dict(desk().view("list_agents"))
    assert [x[0].agent for x in al.rank([a, b], agents)] == \
           [x[0].agent for x in al.rank([b, a], agents)] == [f"btc.{OWNER}", f"eth.{OWNER}"]


def test_a_round_accounts_for_risk_already_open():
    v = desk()
    v.request_permit("sol", signal_hash="earlier", side="Buy", notional_cents=250000,
                     risk_cents=5000, leverage_x10=10)
    r = al.run_round(v, [bid("btc", 0.9, sig="1"), bid("eth", 0.8, sig="2"),
                         bid("btc", 0.7, sig="3"), bid("eth", 0.6, sig="4")])
    assert r["room_before_cents"] == 15000
    assert [x["granted"] for x in r["bids"]] == [True, True, True, False]


def test_a_halted_desk_funds_nobody():
    v = desk()
    v._book.halted = True
    r = al.run_round(v, [bid("btc", 0.9), bid("eth", 0.8)])
    assert r["granted"] == 0 and {x["actual"] for x in r["bids"]} == {"desk_halted"}


def test_nothing_more_is_sent_once_the_vault_is_unreachable():
    v = desk()
    sent = []
    real = v.request_permit

    def flaky(agent, **kw):
        sent.append(kw["signal_hash"])
        if len(sent) == 2:
            raise RuntimeError("rpc down")
        return real(agent, **kw)

    v.request_permit = flaky
    r = al.run_round(v, [bid("btc", 0.9, sig="1"), bid("eth", 0.8, sig="2"),
                         bid("sol", 0.7, sig="3")])
    assert sent == ["1", "2"]
    assert [x["actual"] for x in r["bids"]] == ["within_limits", "vault_unreachable", "not_sent"]
    assert not r["all_as_predicted"]


def test_release_frees_the_risk_a_round_reserved():
    v = desk()
    r = al.run_round(v, al.demo_bids(al.snapshot(v), "t"))
    assert r["granted"] == 4 and r["refused"] == 1
    assert al.release(v, r) == 4
    assert v.view("get_status")["open_risk_cents"] == 0


def test_the_demo_always_asks_for_one_bid_more_than_fits():
    v = desk()
    assert len(al.demo_bids(al.snapshot(v), "t")) == 5
    v.request_permit("btc", signal_hash="x", side="Buy", notional_cents=250000,
                     risk_cents=5000, leverage_x10=10)
    assert len(al.demo_bids(al.snapshot(v), "t")) == 4


def test_the_offline_demo_runs_from_the_command_line():
    assert al.main(["demo", "--offline"]) == 0


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
