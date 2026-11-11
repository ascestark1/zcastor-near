"""
Pipeline semantics tests.

The one that matters most is test_routed_signal_skips_market_only_gates: it is a
regression test for the v1 bug where a signal routed to the watcher could still
be killed by `range_below_sl` or `insufficient_rr` — gates that judge a market
entry against a market stop, applied to a trade that was going to use a
structural one.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from zcastor.gates.base import (  # noqa: E402
    Gate, GateContext, Status, Verdict, allow, reject, route, suppress,
)
from zcastor.gates.pipeline import GatePipeline  # noqa: E402


# ── fixtures ──────────────────────────────────────────────────────────────────

def ctx() -> GateContext:
    return GateContext(
        signal={"direction": "UP", "timeframe": "15m"},
        origin_hash="sha256:origin",
        policy={"min_rr": 1.0},
    )


def gate(name, decision, **flags):
    ns = {"name": name, "evaluate": lambda self, c: decision, **flags}
    return type(f"G_{name}", (Gate,), ns)()


def run(gates):
    return GatePipeline(gates).run(ctx(), signal_id="sig_test", policy_hash="sha256:policy")


def status_of(trace, name):
    return next(r.status for r in trace.results if r.name == name)


# ── tests ─────────────────────────────────────────────────────────────────────

def test_all_allow_is_executable():
    t = run([gate("a", allow()), gate("b", allow())])
    assert t.action is Verdict.ALLOW
    assert t.executable and not t.routed


def test_reject_short_circuits_and_produces_no_record():
    t = run([
        gate("feed", reject("stale_feed")),
        gate("never", suppress("should_not_run")),
    ])
    assert t.action is Verdict.REJECT
    assert t.reason == "stale_feed"
    assert not t.recordable
    assert len(t.results) == 1, "gates after a REJECT must not run"


def test_first_suppress_binds_later_ones_are_shadow():
    t = run([
        gate("london", suppress("london_sell_bias")),
        gate("range", suppress("range_below_sl")),
    ])
    assert t.reason == "london_sell_bias"
    assert status_of(t, "london") is Status.BINDING
    assert status_of(t, "range") is Status.SHADOW
    assert [r.name for r in t.shadow_objections()] == ["range"]


def test_routed_signal_skips_market_only_gates():
    """The v1 regression. `range` must never run once the signal is routed."""
    range_gate = gate("range", suppress("range_below_sl"), runs_when_routed=False)
    rr_gate = gate("rr", suppress("insufficient_rr"), runs_when_routed=False)

    t = run([gate("zone", route("exhaustion")), range_gate, rr_gate])

    assert t.routed
    assert t.route_kind == "exhaustion"
    assert status_of(t, "range") is Status.SKIPPED
    assert status_of(t, "rr") is Status.SKIPPED


def test_gate_that_applies_to_routed_signals_still_binds():
    t = run([
        gate("zone", route("fresh")),
        gate("deadman", suppress("deadman_halt")),  # runs_when_routed defaults True
    ])
    assert t.action is Verdict.SUPPRESS
    assert t.reason == "deadman_halt"
    assert t.route_kind == "", "a bind must clear the route"


def test_route_after_suppress_does_not_arm():
    t = run([
        gate("session", suppress("session_blocked")),
        gate("zone", route("exhaustion")),
    ])
    assert t.action is Verdict.SUPPRESS
    assert status_of(t, "zone") is Status.SHADOW


def test_expensive_gates_skipped_once_bound_cheap_ones_continue():
    t = run([
        gate("session", suppress("session_blocked")),
        gate("cheap", suppress("also_bad")),
        gate("viability", suppress("not_viable"), expensive=True),
    ])
    assert status_of(t, "cheap") is Status.SHADOW
    assert status_of(t, "viability") is Status.SKIPPED


def test_advisory_gate_never_binds():
    t = run([gate("model", suppress("low_wr"), advisory=True), gate("b", allow())])
    assert t.action is Verdict.ALLOW
    assert status_of(t, "model") is Status.ADVISORY


def test_raising_gate_fails_closed():
    class Boom(Gate):
        name = "boom"
        def evaluate(self, c):
            raise RuntimeError("broker timeout")

    t = run([Boom(), gate("after", allow())])
    assert t.action is Verdict.SUPPRESS
    assert t.reason == "gate_error:boom"


def test_gate_returning_junk_fails_closed():
    class Junk(Gate):
        name = "junk"
        def evaluate(self, c):
            return True

    t = run([Junk()])
    assert t.action is Verdict.SUPPRESS
    assert t.reason == "gate_contract:junk"


def test_need_raises_on_missing_derived_value():
    class Reader(Gate):
        name = "reader"
        def evaluate(self, c):
            return allow(v=c.need("sl_pts"))

    t = run([Reader()])
    # The KeyError is caught by the pipeline and fails closed, loudly.
    assert t.reason == "gate_error:reader"
    assert "KeyError" in t.results[0].detail["error"]


def test_duplicate_gate_names_rejected():
    try:
        GatePipeline([gate("dup", allow()), gate("dup", allow())])
    except ValueError as e:
        assert "duplicate" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_trace_serialises_for_the_record_layer():
    t = run([gate("zone", route("fresh")), gate("b", allow())])
    d = t.to_dict()
    assert d["action"] == "route"
    assert d["policy_hash"] == "sha256:policy"
    assert d["origin_hash"] == "sha256:origin"
    assert [g["name"] for g in d["gates"]] == ["zone", "b"]


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
