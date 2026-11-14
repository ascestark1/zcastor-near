#!/usr/bin/env python3
"""
The desk's allocator: agents bid for one shared risk budget; the vault decides.

    .venv/bin/python near/allocator.py demo --offline      # no keys, no network
    .venv/bin/python near/allocator.py plan  bids.json     # read the vault, predict
    .venv/bin/python near/allocator.py round bids.json     # ask the vault, for real
    .venv/bin/python near/allocator.py demo --release      # a full round on testnet

Each agent trades its own market and has its own budget, but the desk has one
budget for all of them (`total_risk_cents` in the vault). When several agents
have a signal at the same time, they can't all be funded. A round works like
this:

1. Every agent submits a bid: the trade it wants (side, notional, risk,
   leverage) and a score, its own estimate of the trade's edge.
2. The allocator weights each score by the agent's record, read from the vault:
   (wins + 1) / (wins + losses + 2). An agent that has lost more than it has
   won gets less say; a new agent starts at one half.
3. Bids are sent to the vault in that order, one permit request each.
4. The vault grants permits until a limit binds. The bids that lose are refused
   by the contract and stored on chain with the reason (`over_desk_budget`,
   `over_agent_budget`, ...), like any other refusal.

The allocator only chooses the order of the queue. It holds no key that can
change a limit and it can't grant anything: a bid it ranks first is still
refused if it breaks a limit. Before sending anything it predicts each outcome
from the vault's current state, and the report shows prediction next to what
the vault actually did.

What it does not do: check that a score is honest. A score is the agent's own
claim; the record weighting is the only thing that disciplines it.

A bids file is a JSON list:
    [{"agent": "btc", "signal_hash": "sig-1", "side": "Buy",
      "notional": 2500, "risk": 50, "leverage": 1.0, "score": 0.81}, ...]
(dollars; `agent` is a short name or a full account id).

Environment: NEAR_VAULT, NEAR_OWNER, NEAR_NETWORK (as for vault_client.py).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "near"))


@dataclass(frozen=True)
class Bid:
    agent: str               # full account id
    signal_hash: str
    side: str                # "Buy" | "Sell"
    notional_cents: int
    risk_cents: int
    leverage_x10: int
    score: float             # the agent's own estimate of edge, 0..1


def reputation(agent: dict) -> float:
    """An agent's say, from its on-chain record. New agents start at one half."""
    wins, losses = int(agent.get("wins", 0)), int(agent.get("losses", 0))
    return (wins + 1) / (wins + losses + 2)


class Book:
    """
    The vault's refusal rules (`refusal_reason` in near/vault/src/lib.rs),
    applied to a snapshot of its state. Used to predict a round before it is
    sent, and by SimVault. The contract is the authority; this is a mirror.
    """

    def __init__(self, limits: dict, status: dict, agents: dict[str, dict]) -> None:
        self.limits = limits
        self.halted = bool(status.get("halted"))
        self.open_risk = int(status.get("open_risk_cents", 0))
        self.agents = {k: dict(v) for k, v in agents.items()}

    def reason(self, bid: Bid) -> Optional[str]:
        a, lim = self.agents.get(bid.agent), self.limits
        if a is None:
            return "not_an_agent"
        if self.halted:
            return "desk_halted"
        if not a.get("enabled", True):
            return "agent_disabled"
        if bid.risk_cents == 0 or bid.notional_cents == 0:
            return "zero_size"
        if bid.leverage_x10 > a["max_leverage_x10"] or bid.leverage_x10 > lim["max_leverage_x10"]:
            return "over_leverage"
        if bid.notional_cents > a["max_notional_cents"]:
            return "over_agent_notional"
        if bid.risk_cents > lim["per_trade_risk_cents"]:
            return "over_trade_risk"
        if int(a.get("open_risk_cents", 0)) + bid.risk_cents > a["risk_budget_cents"]:
            return "over_agent_budget"
        if self.open_risk + bid.risk_cents > lim["total_risk_cents"]:
            return "over_desk_budget"
        return None

    def take(self, bid: Bid) -> str:
        """Decide a bid and, if it is granted, reserve its risk."""
        why = self.reason(bid)
        if why is None:
            self.open_risk += bid.risk_cents
            a = self.agents[bid.agent]
            a["open_risk_cents"] = int(a.get("open_risk_cents", 0)) + bid.risk_cents
        return why or "within_limits"

    def room(self) -> int:
        return max(0, int(self.limits["total_risk_cents"]) - self.open_risk)


def rank(bids: list[Bid], agents: dict[str, dict]) -> list[tuple[Bid, float]]:
    """Highest weighted score first. Ties: smaller risk, then agent, then signal."""
    weighted = [(b, round(b.score * reputation(agents.get(b.agent, {})), 6)) for b in bids]
    return sorted(weighted, key=lambda x: (-x[1], x[0].risk_cents, x[0].agent,
                                           x[0].signal_hash))


def snapshot(vault: Any) -> Book:
    agents = {str(k): v for k, v in vault.view("list_agents")}
    return Book(vault.view("get_limits"), vault.view("get_status"), agents)


def plan(bids: list[Bid], book: Book) -> list[dict]:
    """What the vault should do with each bid, in the order they will be sent."""
    rows = []
    for order, (bid, weight) in enumerate(rank(bids, book.agents)):
        rows.append({"order": order, **asdict(bid), "weighted_score": weight,
                     "predicted": book.take(bid)})
    return rows


def run_round(vault: Any, bids: list[Bid]) -> dict:
    """Predict, then ask the vault for each permit in ranked order."""
    before = snapshot(vault)
    room_before = before.room()
    rows = plan(bids, before)
    for row in rows:
        try:
            d = vault.request_permit(
                row["agent"], signal_hash=row["signal_hash"], side=row["side"],
                notional_cents=row["notional_cents"], risk_cents=row["risk_cents"],
                leverage_x10=row["leverage_x10"])
        except Exception as exc:  # noqa: BLE001
            # A desk that can't ask may not trade. Nothing further is sent.
            row["actual"], row["granted"], row["error"] = "vault_unreachable", False, str(exc)[:200]
            break
        row["decision_id"] = d.get("id")
        row["granted"] = bool(d.get("granted"))
        row["actual"] = d.get("reason", "")
    for row in rows:
        row.setdefault("actual", "not_sent")
        row.setdefault("granted", False)
        row["as_predicted"] = row["actual"] == row["predicted"]

    funded: dict[str, int] = {}
    for row in rows:
        if row["granted"]:
            funded[row["agent"]] = funded.get(row["agent"], 0) + row["risk_cents"]
    return {
        "desk_budget_cents": int(before.limits["total_risk_cents"]),
        "room_before_cents": room_before,
        "bid_for_cents": sum(b.risk_cents for b in bids),
        "granted_cents": sum(funded.values()),
        "funded_by_agent_cents": funded,
        "granted": sum(1 for r in rows if r["granted"]),
        "refused": sum(1 for r in rows if not r["granted"]),
        "all_as_predicted": all(r["as_predicted"] for r in rows),
        "bids": rows,
    }


def release(vault: Any, report: dict) -> int:
    """Settle every permit a round was granted, at zero. For demos only."""
    done = 0
    for row in report["bids"]:
        if row.get("granted") and row.get("decision_id") is not None:
            vault.settle(row["agent"], int(row["decision_id"]), 0)
            row["released"] = True
            done += 1
    return done


class SimVault:
    """An in-memory vault with the contract's rules, for `--offline` and tests."""

    def __init__(self, limits: dict, agents: dict[str, dict], owner: str = "desk.testnet") -> None:
        self.owner = owner
        self._book = Book(limits, {"halted": False, "open_risk_cents": 0}, agents)
        self.decisions: list[dict] = []

    def account(self, name: str) -> str:
        return name if "." in name else f"{name}.{self.owner}"

    def view(self, method: str, args: Optional[dict] = None) -> Any:
        if method == "get_limits":
            return dict(self._book.limits)
        if method == "get_status":
            return {"halted": self._book.halted, "open_risk_cents": self._book.open_risk}
        if method == "list_agents":
            return [[k, dict(v)] for k, v in self._book.agents.items()]
        raise KeyError(method)

    def request_permit(self, agent: str, *, signal_hash: str, side: str,
                       notional_cents: int, risk_cents: int, leverage_x10: int) -> dict:
        bid = Bid(self.account(agent), signal_hash, side, notional_cents, risk_cents,
                  leverage_x10, 0.0)
        why = self._book.take(bid)
        d = {"id": len(self.decisions), "agent": bid.agent, "granted": why == "within_limits",
             "reason": why, "risk_cents": risk_cents, "signal_hash": signal_hash}
        self.decisions.append(d)
        return d

    def settle(self, agent: str, permit_id: int, pnl_cents: int) -> None:
        d = self.decisions[permit_id]
        self._book.open_risk -= d["risk_cents"]
        self._book.agents[d["agent"]]["open_risk_cents"] -= d["risk_cents"]


DEMO_LIMITS = {"total_risk_cents": 20000, "per_trade_risk_cents": 5000, "max_leverage_x10": 30}
DEMO_AGENT = {"enabled": True, "risk_budget_cents": 10000, "max_notional_cents": 500000,
              "max_leverage_x10": 30, "open_risk_cents": 0, "wins": 0, "losses": 0}
DEMO_SCORES = (0.81, 0.77, 0.74, 0.66, 0.58, 0.52, 0.47, 0.41, 0.36)


def demo_bids(book: Book, tag: str) -> list[Bid]:
    """One more bid than the desk has room for, spread across the agents."""
    names = sorted(a for a, v in book.agents.items() if v.get("enabled", True))
    size = int(book.limits["per_trade_risk_cents"])
    count = min(len(DEMO_SCORES), book.room() // size + 1)
    return [Bid(names[i % len(names)], f"{tag}-{i}", "Buy" if i % 2 == 0 else "Sell",
                size * 50, size, 10, DEMO_SCORES[i]) for i in range(count)]


def load_bids(path: Path, vault: Any) -> list[Bid]:
    out = []
    for b in json.loads(path.read_text()):
        out.append(Bid(vault.account(b["agent"]), str(b["signal_hash"]),
                       str(b["side"]).capitalize(),
                       int(round(float(b["notional"]) * 100)),
                       int(round(float(b["risk"]) * 100)),
                       int(round(float(b.get("leverage", 1.0)) * 10)),
                       float(b.get("score", 0.5))))
    return out


def show(report: dict) -> None:
    d = lambda c: f"${c / 100:,.0f}"  # noqa: E731
    print(f"desk budget {d(report['desk_budget_cents'])}, room {d(report['room_before_cents'])}, "
          f"bids for {d(report['bid_for_cents'])}")
    for r in report["bids"]:
        verdict = "GRANTED" if r["granted"] else "REFUSED"
        print(f"  {r['order'] + 1}. {r['agent'].split('.')[0]:<5} {r['signal_hash']:<14} "
              f"risk {d(r['risk_cents']):>5}  score {r['weighted_score']:.3f}  "
              f"{verdict}  {r['actual']}"
              + ("" if r["as_predicted"] else f"   (predicted {r['predicted']})"))
    by = ", ".join(f"{a.split('.')[0]} {d(c)}" for a, c in report["funded_by_agent_cents"].items())
    print(f"funded {d(report['granted_cents'])}: {by or 'nothing'}; "
          f"{report['refused']} refused; "
          f"{'every outcome as predicted' if report['all_as_predicted'] else 'PREDICTION MISSED'}")


def save(report: dict, root: Path = ROOT / "data" / "near" / "rounds") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    path.write_text(json.dumps(report, indent=2))
    return path


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="near/allocator.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "round"):
        p = sub.add_parser(name)
        p.add_argument("bids", type=Path)
        if name == "round":
            p.add_argument("--release", action="store_true",
                           help="settle the granted permits at zero afterwards")
    p = sub.add_parser("demo")
    p.add_argument("--offline", action="store_true", help="simulated vault; no keys")
    p.add_argument("--release", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "demo" and a.offline:
        vault: Any = SimVault(DEMO_LIMITS, {f"{n}.desk.testnet": dict(DEMO_AGENT)
                                            for n in ("btc", "eth", "sol")})
    else:
        import vault_client
        try:
            vault = vault_client.Vault()
        except vault_client.VaultError as exc:
            print(f"failed: {exc}", file=sys.stderr)
            return 1

    try:
        if a.cmd == "plan":
            print(json.dumps(plan(load_bids(a.bids, vault), snapshot(vault)), indent=2))
            return 0
        bids = (demo_bids(snapshot(vault), time.strftime("round-%H%M%S", time.gmtime()))
                if a.cmd == "demo" else load_bids(a.bids, vault))
        report = run_round(vault, bids)
        if a.release:
            report["released"] = release(vault, report)
    except Exception as exc:  # noqa: BLE001
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    show(report)
    if not (a.cmd == "demo" and a.offline):
        print(f"saved {save(report)}")
    return 0 if report["all_as_predicted"] else 2


if __name__ == "__main__":
    sys.exit(main())
