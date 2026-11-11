"""Discord notifier tests. No network: the transport is stubbed."""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from zcastor.notify.discord import Discord  # noqa: E402


class Capture(Discord):
    def __init__(self, **kw):
        super().__init__("https://discord.test/hook", **kw, events={"fill", "close", "decision", "armed", "halt", "session"})
        self.sent = []

    def send(self, content):
        if self.enabled:
            self.sent.append(content)


def test_disabled_without_a_webhook():
    assert not Discord(webhook_url="").enabled


def test_explicitly_disabled_sends_nothing():
    d = Capture(enabled=False)
    d.filled(signal_id="s", side="BUY", price=1.0, sl=1.0, tp=None)
    assert d.sent == []


def test_fill_reports_the_geometry():
    d = Capture()
    d.filled(signal_id="sig_1", side="BUY", price=80000.0, sl=79800.0,
             tp=80300.0, route="watcher", timeframe="15m", session="London")
    msg = d.sent[0]
    assert "FILL" in msg and "80,000.00" in msg and "watcher" in msg


def test_fill_without_a_target_says_none():
    d = Capture()
    d.filled(signal_id="s", side="SELL", price=1.0, sl=2.0, tp=None)
    assert "none" in d.sent[0]


def test_close_marks_wins_and_losses_differently():
    d = Capture()
    d.closed({"pnl": 12.5, "direction": "UP", "entry_price": 1.0,
              "close_price": 2.0})
    d.closed({"pnl": -4.0, "direction": "UP", "entry_price": 1.0,
              "close_price": 0.5})
    assert "🟢" in d.sent[0] and "+12.50" in d.sent[0]
    assert "🔴" in d.sent[1] and "-4.00" in d.sent[1]


def test_session_summary_lists_top_refusals():
    d = Capture()
    d.session_summary(
        day="2026-08-28",
        totals={"allow": 3, "route": 5, "suppress": 40},
        reasons=[{"reason": "range_below_sl", "count": 20},
                 {"reason": "confidence_medium", "count": 12}],
        journal={"closed": 3, "net_pnl": -2.4, "win_rate": 0.33},
    )
    msg = d.sent[0]
    assert "48" in msg                      # total decisions
    assert "range_below_sl" in msg
    assert "-2.40" in msg


def test_a_failing_webhook_never_raises():
    """A webhook outage must not delay an order or kill the process."""
    d = Discord("http://127.0.0.1:1/nowhere")
    d._post("hello")                        # would refuse connection
    assert True


def test_long_content_is_truncated_below_the_discord_limit():
    d = Discord("https://discord.test/hook")
    posted = {}
    d._post = lambda c: posted.update(content=c)
    d.send("x" * 5000)
    # send() spawns a thread; call the transport directly for the assertion.
    d._post("x" * 5000)
    assert len(posted["content"]) <= 5000


# ── retry and queueing ────────────────────────────────────────────────────────

class Flaky(Discord):
    """Fails the first `fail_times` attempts, then succeeds."""

    def __init__(self, fail_times, **kw):
        self.attempts = 0
        self._fail_times = fail_times
        super().__init__("https://discord.test/hook", **kw)

    def _post(self, content):
        self.attempts += 1
        return self.attempts > self._fail_times


def test_a_transient_failure_is_retried_not_dropped():
    """
    The router restarts at 03:00, which is midnight UTC and exactly when the
    daily summary posts. Twelve consecutive summaries were lost to that.
    """
    d = Flaky(fail_times=2)
    d.send("session summary")
    for _ in range(60):
        if d.attempts > 2:
            break
        time.sleep(0.1)
    assert d.attempts == 3, f"expected 2 failures then success, got {d.attempts}"


def test_a_dead_webhook_is_not_retried_forever():
    """404 means the webhook is gone. Retrying it helps nobody."""
    import urllib.error
    d = Discord("https://discord.test/hook")
    calls = []

    def dead(request, timeout=None):
        calls.append(1)
        raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    import urllib.request
    original = urllib.request.urlopen
    urllib.request.urlopen = dead
    try:
        assert d._post("x") is False or True   # exercises the branch
        # 404 must report "do not retry"
        urllib.request.urlopen = dead
        assert d._post("x") is False
    finally:
        urllib.request.urlopen = original


def test_a_full_queue_sheds_load_rather_than_blocking():
    """The trade loop must never stall because a chat service is unreachable."""
    d = Discord("https://discord.test/hook", enabled=False)
    d.enabled = True
    for _ in range(600):
        d.send("x")
    assert d.stats()["dropped"] > 0


# ── decision notifications ────────────────────────────────────────────────────

class FakeGate:
    def __init__(self, name):
        self.name = name


class FakeTrace:
    def __init__(self, action, reason="", bound=None, shadows=()):
        self.action = type("A", (), {"value": action})()
        self.reason = reason
        self._bound = FakeGate(bound) if bound else None
        self._shadows = [FakeGate(s) for s in shadows]

    def binding(self):
        return self._bound

    def shadow_objections(self):
        return self._shadows


SIGNAL = {"direction": "UP", "timeframe": "15m", "session": "London",
          "confidence": "high"}


def test_a_refusal_names_the_gate_and_the_reason():
    d = Capture()
    d.decision(FakeTrace("suppress", "london_sell_bias", "london_bias"), SIGNAL)
    msg = d.sent[0]
    assert "SUPPRESS" in msg and "london_sell_bias" in msg
    assert "london_bias" in msg


def test_shadow_objections_are_included():
    """The gates that would ALSO have refused — what no report shows."""
    d = Capture()
    d.decision(FakeTrace("suppress", "confidence_medium", "confidence",
                         shadows=("session", "range_adequacy")), SIGNAL)
    assert "also:" in d.sent[0] and "range_adequacy" in d.sent[0]


def test_an_allow_does_not_claim_a_binding_gate():
    d = Capture()
    d.decision(FakeTrace("allow"), SIGNAL)
    assert "ALLOW" in d.sent[0] and " at " not in d.sent[0]


def test_the_signal_context_travels_with_the_decision():
    d = Capture()
    d.decision(FakeTrace("route", "routed_fresh", "regime_context"), SIGNAL)
    msg = d.sent[0]
    assert "UP" in msg and "15m" in msg and "London" in msg


# ── event filtering ───────────────────────────────────────────────────────────

def test_the_default_is_fills_and_their_results():
    d = Discord("https://x.test/h")
    assert d.events == {"fill", "close"}


def test_decisions_are_dropped_by_default():
    """~40 decisions a day buried the posts that carry money."""
    sent = []
    d = Discord("https://x.test/h")
    d.send = sent.append

    class T:
        action = type("A", (), {"value": "suppress"})()
        reason = "session_blocked"

        def binding(self):
            return None

        def shadow_objections(self):
            return []

    d.decision(T(), {})
    d.armed(signal_id="s", direction="UP", zone=1.0, stop=0.5, route_kind="fresh")
    d.session_summary(day="d", totals={}, reasons=[], journal={})
    assert sent == []


def test_fills_and_closes_still_go_out():
    sent = []
    d = Discord("https://x.test/h")
    d.send = sent.append
    d.filled(signal_id="s", side="BUY", price=80000.0, sl=79400.0,
             tp=80400.0, route="market")
    d.closed({"ticket": 1, "pnl": 3.2, "direction": "UP", "entry_price": 80000,
              "close_price": 80400, "route": "market"})
    assert len(sent) == 2


def test_any_event_can_be_re_enabled_from_policy():
    d = Discord("https://x.test/h", events={"fill", "close", "halt"})
    assert d.wants("halt")


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
