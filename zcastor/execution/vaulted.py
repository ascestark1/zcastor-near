"""
Orders that need the vault's permission.

`VaultedOrders` wraps the venue's order placer. The engine and the entry
watcher call it exactly as they call any placer; before it passes an order on,
it asks the vault contract on NEAR for a permit for that trade's notional, risk
and leverage.

- **Granted**: the order goes to the venue. The permit id is remembered against
  the ticket, and when the position closes the permit is settled with the
  realised PnL, which frees its risk on chain and scores the agent.
- **Refused**: no order. The engine records the decision as suppressed with the
  vault's own reason (`vault_refused:over_trade_risk`).
- **Vault unreachable**: no order. A desk that cannot ask is a desk that may
  not trade (`vault_refused:vault_unreachable`). Failing open here would make
  the limits optional exactly when something is wrong.
- **Granted but the venue rejects the order**: the permit is settled at zero so
  its risk does not stay reserved.

Asking costs one NEAR transaction (a few seconds) before a fill. That is the
price of the limits living somewhere the trading software cannot edit.

Permits are kept in a small JSON file so a restart does not strand open risk
on chain.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("zcastor.execution.vaulted")


class VaultedOrders:
    def __init__(self, inner: Any, vault: Any, *, agent: str, leverage: float,
                 store: Path) -> None:
        self.inner = inner
        self.vault = vault
        self.agent = agent
        self.leverage_x10 = int(round(float(leverage) * 10))
        self._store = Path(store)
        self._lock = threading.Lock()
        self._signal_ref = ""
        self._permits: dict[str, int] = {}
        try:
            self._permits = {str(k): int(v) for k, v in
                             json.loads(self._store.read_text()).items()}
        except (OSError, ValueError):
            pass
        self.last_decision: Optional[dict] = None

    # The engine reads `orders.volume`; anything else falls through too.
    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def bind_signal(self, signal_id: str, origin_hash: str = "") -> None:
        """Called by the engine so the on-chain decision names its signal."""
        self._signal_ref = origin_hash or signal_id

    def _save(self) -> None:
        self._store.parent.mkdir(parents=True, exist_ok=True)
        self._store.write_text(json.dumps(self._permits, indent=1, sort_keys=True))

    def market_order(self, *, side: str, price: float, sl: float,
                     tp: Optional[float] = None) -> dict:
        qty = float(self.inner.volume)
        notional_cents = int(round(qty * float(price) * 100))
        risk_cents = max(1, int(round(qty * abs(float(price) - float(sl)) * 100)))
        try:
            decision = self.vault.request_permit(
                self.agent, signal_hash=self._signal_ref or "unbound",
                side="Buy" if str(side).upper() == "BUY" else "Sell",
                notional_cents=notional_cents, risk_cents=risk_cents,
                leverage_x10=self.leverage_x10)
        except Exception as exc:  # noqa: BLE001
            logger.error("vault unreachable, order withheld: %s", exc)
            self.last_decision = None
            return {"error": f"vault unreachable: {exc}",
                    "refused": "vault_unreachable"}

        self.last_decision = decision
        if not decision or not decision.get("granted"):
            reason = (decision or {}).get("reason", "no_decision")
            logger.info("vault refused %s %s: %s (decision %s)", self.agent, side,
                        reason, (decision or {}).get("id"))
            return {"error": f"vault refused: {reason}", "refused": reason,
                    "vault_decision": (decision or {}).get("id")}

        permit = int(decision["id"])
        result = self.inner.market_order(side=side, price=price, sl=sl, tp=tp)
        if "error" in result:
            self._settle(permit, 0, why="order failed after grant")
            return result
        with self._lock:
            self._permits[str(result["ticket"])] = permit
            self._save()
        logger.info("vault permit %s granted for ticket %s (risk $%.2f)",
                    permit, result["ticket"], risk_cents / 100)
        return dict(result, vault_permit=permit)

    def close_position(self, ticket: int, side: str) -> dict:
        return self.inner.close_position(ticket, side)

    def on_closed(self, trade: dict) -> None:
        """Settle the permit behind a closed trade. Safe to call for any trade."""
        key = str(trade.get("ticket"))
        with self._lock:
            permit = self._permits.get(key)
        if permit is None:
            return
        if self._settle(permit, int(round(float(trade.get("pnl") or 0) * 100)),
                        why=f"ticket {key} closed"):
            with self._lock:
                self._permits.pop(key, None)
                self._save()

    def _settle(self, permit: int, pnl_cents: int, why: str) -> bool:
        try:
            self.vault.settle(self.agent, permit, pnl_cents)
            logger.info("vault permit %s settled, pnl $%.2f (%s)",
                        permit, pnl_cents / 100, why)
            return True
        except Exception as exc:  # noqa: BLE001
            # Left in the store, so the next close poll tries again.
            logger.error("could not settle vault permit %s (%s): %s", permit, why, exc)
            return False

    def retry_settlements(self, journal: Any) -> None:
        """Settle permits whose trades closed while the vault was unreachable."""
        with self._lock:
            pending = dict(self._permits)
        for ticket in pending:
            trade = journal.get(int(ticket))
            if trade and trade.get("state") == "closed":
                self.on_closed(trade)
