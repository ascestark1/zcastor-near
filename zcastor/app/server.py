"""
The server. Wiring, and nothing else.

Every decision this file makes is about which objects to construct and in what
order — no trading logic, no thresholds, no reasoning. If a rule is being applied
here, it is in the wrong place.

Two ordering constraints that matter:

**The policy is hashed once at startup**, from the file's bytes, and that hash
goes on every decision made by this process. Rehashing per signal would let a
mid-session edit change the recorded policy without a restart, which is exactly
the kind of silent drift the record exists to rule out. Changing policy means
restarting, and the restart is the event.

**Anchoring never blocks a signal.** The outbox drains on its own timer. The
`/signal` handler touches disk once — an append — and returns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from ..anchor.base import NullBackend
from ..anchor.index import AnchorIndex
from ..anchor.outbox import Outbox
from ..data.journal import Journal
from ..data.ledger import EdgeLedger
from ..execution.booking import Booker, ClosePoller
from ..execution.engine import Engine
from ..entry.watcher import EntryWatcher
from ..execution.orders import OrderPlacer
from ..gates.cooldown import EntryCooldownGate
from ..gates.assembly import build_gates
from ..record.capture import OriginCapture
from .venue import resolve_venue
from .anchor_guard import check_anchor_config
from . import lock as engine_lock
from ..record.repo_sync import RepoSync
from ..gates.feed import FeedGate
from ..gates.pipeline import GatePipeline
from ..market.mt5 import MarketData, connect
from ..market.levels import HourlyLevels
from ..market.session import SessionOpens, current_session
from ..notify.discord import Discord
from ..feed.journal import SignalFeed
from ..record.canonical import checksum_bytes
from ..record.proof import process_hash
from ..record.publisher import Publisher
from ..record.rollover import SessionRollover
from ..resolve.resolver import OutcomeResolver
from ..risk.coherence import ModelCoherence, RiskAdapter, SignalCoherence
from ..risk.zones import ZoneTracker
from ..targets.optimizer import TargetOptimizer

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
LOGS = ROOT / "logs"

logger = logging.getLogger("zcastor")


def _environment() -> str:
    return os.environ.get("NVNM_ENV", "testnet")


def _anchor_backend():
    """
    NullBackend unless ANCHOR_BACKEND=nvnm is set explicitly.

    Opt-in, not opt-out. A misconfigured environment should leave records
    queued on disk, not send half-formed transactions to a live chain — and
    the outbox means nothing is lost while the backend is null.
    """
    if os.environ.get("ANCHOR_BACKEND", "null").lower() != "nvnm":
        return NullBackend()

    from ..anchor.nvnm import ENVIRONMENTS, build_backend   # noqa: PLC0415
    env = _environment()
    backend = build_backend(
        private_key=os.environ["NVNM_PRIVATE_KEY"],
        environment=env,
        rpc_url=os.environ.get("NVNM_RPC_URL", ""),
        chain_id=int(os.environ.get("NVNM_CHAIN_ID", 0) or 0),
        legacy_tx=os.environ.get("NVNM_LEGACY_TX", "").lower() == "true",
    )
    logger.info("anchoring to NVNM %s (chain %s, gas %s) as %s",
                env, ENVIRONMENTS.get(env, {}).get("chain_id"),
                ENVIRONMENTS.get(env, {}).get("gas_symbol"), backend.address)
    return backend


def _setup_logging() -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOGS / "zcastor.log")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger("zcastor")
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler())
    # uvicorn's access log buries gate decisions. In v1 this looked like the
    # gates had gone silent; they were being drowned by "POST /signal 200 OK".
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


class Zcastor:
    """Everything, assembled once."""

    def __init__(self) -> None:
        policy_path = ROOT / "config" / "policy.json"
        self.policy = json.loads(policy_path.read_text())
        self.policy_hash = checksum_bytes(policy_path.read_bytes())

        runtime_path = ROOT / "config" / "runtime.json"
        self.runtime = (json.loads(runtime_path.read_text())
                        if runtime_path.exists() else {})

        # Held for the life of the process; ops scripts check it before writing.
        engine_lock.acquire(DATA)
        venue = resolve_venue(self.runtime)
        self.venue = venue
        symbol = self.runtime.get("symbol", "BTCUSD")
        volume = float(self.runtime.get("volume", 0.01))

        self.journal = Journal(DATA / "trades.jsonl")
        sessions = SessionOpens(lambda f, t: self.market.get_rates_range(f, t))

        # One port, two venues. Nothing below this line knows which.
        if venue == "hyperliquid-paper":
            # Hyperliquid's real order book, simulated fills (market/hl_paper.py).
            # No exchange key exists on this box until NEAR issues one.
            from ..market.hl_paper import (HyperliquidMarketData, PaperBook,
                                           PaperOrderPlacer)
            from ..market.hyperliquid import HLInfo, api_base, urllib_transport
            testnet = os.environ.get("HYPERLIQUID_TESTNET", "false").lower() == "true"
            symbol = str(self.runtime.get("coin", "BTC"))
            volume = float(self.runtime.get("paper_volume", 0.001))
            info = HLInfo(urllib_transport(api_base(testnet)),
                          weight_budget=int(self.runtime.get("weight_budget", 300)))
            book = PaperBook(DATA / "paper_book.jsonl",
                             start_balance=float(self.runtime.get("paper_balance", 10_000)))
            self.market = HyperliquidMarketData(
                info, book, coin=symbol, volume=volume,
                leverage=float(self.runtime.get("paper_leverage", 3)),
                session_opens=sessions)
            szd = None
            try:
                specs, _ = info.meta_and_ctxs()
                szd = next((int(s["szDecimals"]) for s in specs
                            if s.get("name") == symbol), None)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not read %s size rules: %s", symbol, exc)
            self.orders = PaperOrderPlacer(self.market, volume=volume,
                                           sz_decimals=szd)
            # One engine trades one coin until per-ticker state exists. Signals
            # for any other perp are refused at the door (see /signal).
            self.traded_symbol = symbol
            logger.info("venue: Hyperliquid order book at %s, PAPER fills, %s",
                        api_base(testnet), symbol)
        else:
            mt5 = connect()
            self.market = MarketData(mt5, symbol=symbol, volume=volume,
                                     session_opens=sessions)
            self.orders = OrderPlacer(mt5, symbol=symbol, volume=volume,
                                      magic=int(self.runtime.get("magic", 777001)))
            logger.info("venue: MT5, %s", symbol)

        # The vault on NEAR (near/vault): every order needs its permit first.
        # Off unless runtime.json says "vault": {"enabled": true}.
        self.vault = None
        vcfg = dict(self.runtime.get("vault") or {})
        if vcfg.get("enabled"):
            import sys
            sys.path.insert(0, str(ROOT / "near"))
            from vault_client import Vault
            from ..execution.vaulted import VaultedOrders
            self.vault = Vault()
            agent = str(vcfg.get("agent") or symbol.lower())
            self.orders = VaultedOrders(
                self.orders, self.vault, agent=agent,
                leverage=float(self.runtime.get("paper_leverage", 1)),
                store=DATA / "near" / "permits.json")
            self.vault_report_seconds = int(vcfg.get("report_seconds", 300))
            logger.info("vault: %s, agent %s; every order needs a permit",
                        self.vault.vault, self.vault.account(agent))
        self.ledger = EdgeLedger(self.policy, DATA / "edge_ledger.jsonl",
                                 policy_hash=self.policy_hash)
        self.zones = ZoneTracker(self.policy)
        self.risk = RiskAdapter(
            zones=self.zones,
            signal_coherence=SignalCoherence(self.policy),
            model_coherence=ModelCoherence(self.policy,
                                           DATA / "signal_outcomes.json"),
            journal=self.journal,
        )
        self.registries_by_kind = {
            "policy": "afritensor-policy",
            "decisions": "afritensor-decisions",
            "dossiers": "afritensor-dossiers",
            "archive": "afritensor-archive",
        }
        self.anchor_index = AnchorIndex(DATA / "anchor_index.jsonl")
        self.outbox = Outbox(DATA / "anchor_outbox.jsonl")
        self.backend = _anchor_backend()
        # Registries configured but anchoring off is a misconfiguration, not a
        # mode: refuse to start rather than mark records anchored that never
        # reached a chain. ZCASTOR_ALLOW_NULL_ANCHOR=yes for a deliberate run.
        if check_anchor_config(self.backend.name, self.runtime,
                               os.environ) == "offline-allowed":
            logger.warning("ANCHORING IS OFF — %s=yes. Records are queued, "
                           "not anchored.", "ZCASTOR_ALLOW_NULL_ANCHOR")

        # The dossier repo. Separate from the code repo: this one is public,
        # the code repo holds credentials and must not be.
        self.publisher = Publisher(
            Path(self.runtime.get("dossier_root", ROOT.parent / "zcastor-dossiers")),
            uri_template=self.runtime.get("dossier_uri_template", ""),
        )
        # Anchored URIs point into this repo, so every publish schedules a
        # push. Startup pushes too, to clear anything a past run left behind.
        self.repo_sync = RepoSync(
            self.publisher.root,
            enabled=bool(self.runtime.get("dossier_autopush", True))
            and self.publisher.is_public)
        self.publisher.on_published = self.repo_sync.request
        self.repo_sync.request("startup")
        self.code_version = os.environ.get("ZCASTOR_COMMIT", "unknown")
        self.process_hash = process_hash(self.policy, self.code_version)

        # Publish the policy before anything decides anything. A decision
        # referencing a policy hash must always resolve to a retrievable policy.
        self._anchor_policy()

        registry_ids = self.runtime.get("registry_ids", {})
        self.rollover = SessionRollover(
            publisher=self.publisher,
            outbox=self.outbox,
            anchor_index=self.anchor_index,
            policy_version=str(self.policy.get("policy_version", "")),
            process_hash=self.process_hash,
            # addRecord identifies the registry by numeric id. Without this the
            # rollover queues records that can never be sent.
            registry_id=int(registry_ids.get("decisions", 0)),
            venue=self.venue,
        )
        if not registry_ids.get("decisions"):
            logger.warning("no decisions registry id in runtime.json — "
                           "decision logs will queue but cannot anchor")

        self.resolver = OutcomeResolver(
            policy=self.policy,
            market=self.market,
            pending_path=DATA / "pending_signals.json",
            outcomes_path=DATA / "signal_outcomes.json",
        )

        self.discord = Discord(
            venue=self.venue,
            events=set(self.policy.get("notify_events", ["fill", "close"])))

        # The signed copy of fills and closes that followers trade from (see
        # zcastor/feed). Off unless runtime.json sets "signal_feed": true.
        # Signed by the anchoring key unless ZCASTOR_FEED_KEY names another.
        self.feed = SignalFeed(
            DATA / "signal_feed.jsonl",
            private_key=(os.environ.get("ZCASTOR_FEED_KEY")
                         or os.environ.get("NVNM_PRIVATE_KEY", "")),
            venue=self.venue, symbol=symbol, policy=self.policy_hash,
            enabled=bool(self.runtime.get("signal_feed", False)))
        self.feed.online()

        levels_cfg =dict(self.policy.get("watcher", {}).get("hourly_levels", {}))
        self.levels = HourlyLevels(
            lambda f, t: self.market.get_rates_range(f, t),
            lookback_hours=int(levels_cfg.get("lookback_hours", 168)),
            k=int(levels_cfg.get("swing_k", 3)),
            refresh_seconds=float(levels_cfg.get("refresh_seconds", 900)),
        ) if levels_cfg.get("enabled", True) else None
        # Load now rather than on the first routed signal, so /health reflects
        # a real feed from the start. A failure here is not fatal — refresh
        # returns False and the next query tries again. It runs off the startup
        # path: on 2 Oct 2026 a slow bridge held this call for most of an hour,
        # and with it the whole engine, the watcher and the anchoring outbox.
        if self.levels is not None:
            threading.Thread(target=self.levels.refresh, kwargs={"force": True},
                             name="levels-preload", daemon=True).start()

        # Held on the instance so the engine can report fills to it.
        self.cooldown = EntryCooldownGate()
        # Held so origin capture can snapshot its staleness state.
        self.feed_gate = FeedGate()

        self.booker = Booker(market=self.market, journal=self.journal,
                             ledger=self.ledger,
                             on_closed=self._notify_closed,
                             policy_hash=self.policy_hash)
        self.poller = ClosePoller(market=self.market, journal=self.journal,
                                  booker=self.booker)

        self.engine = Engine(
            pipeline=GatePipeline(self._gates()),
            market=self.market,
            orders=self.orders,
            risk=self.risk,
            targets=TargetOptimizer(self.policy),
            ledger=self.ledger,
            journal=self.journal,
            positions=_Positions(self.market),
            policy=self.policy,
            policy_hash=self.policy_hash,
            decision_log=self.rollover.log,
            outbox=self.outbox,
            watcher=None,                  # set immediately below
            publisher=self.publisher,
            resolver=self.resolver,
            anchor_index=self.anchor_index,
            code_version=self.code_version,
            registries={"dossiers": "afritensor-dossiers"},
            registry_ids=self.runtime.get("registry_ids", {}),
            environment=_environment(),
        )

        # The watcher needs the engine's fill handler and the engine needs the
        # watcher, so one of them is wired after construction. The watcher is
        # the leaf, so it goes second.
        self.watcher = EntryWatcher(
            policy=self.policy,
            market=self.market,
            orders=self.orders,
            on_fill=self._on_watcher_fill,
            levels=self.levels,
            cooldown=self.cooldown,
        )
        self.engine.watcher = self.watcher

        # Origin capture (record/capture.py): keeps each decision's inputs in
        # data/origins/ so replay can re-decide it. On only when runtime.json
        # says "capture_origins": true, so an engine that never asked for it
        # behaves exactly as before.
        if self.runtime.get("capture_origins", False):
            self.engine.capture = OriginCapture(
                DATA / "origins", policy=self.policy,
                policy_hash=self.policy_hash, symbol=symbol, venue=self.venue,
                components=lambda: {
                    "zones": self.zones,
                    "signal_coherence": self.risk._signal,
                    "cooldown": self.cooldown,
                    "feed": self.feed_gate,
                    "levels": self.levels,
                })
            logger.info("origin capture on: %s", DATA / "origins")

        # Two callbacks that close the record lifecycle: remember what landed,
        # then move it to resolved once price has spoken.
        self.outbox.on_anchored = self.engine.on_anchored
        self.engine.on_fill_notify = self._notify_fill
        if self.discord.wants("decision"):
            self.engine.on_decision_notify = self.discord.decision
        self.resolver.on_resolved = self.engine.on_outcome_resolved

    def _anchor_policy(self) -> None:
        """
        Publish the policy and anchor its checksum.

        Every decision record cites a policy hash. Publishing the document is
        not enough on its own: without the hash on chain, a reader can fetch
        the policy but has nothing attesting it existed in that form at that
        time. The document could be written afterwards to fit the decisions.

        Anchoring is idempotent — the outbox drops a checksum it has already
        sent — so this runs on every start without producing duplicates.
        """
        published = self.publisher.publish_policy(self.policy, self.policy_hash)
        registry_id = int(self.runtime.get("registry_ids", {}).get("policy", 0))
        if not registry_id:
            logger.warning("no policy registry id — the policy is published "
                           "but not anchored")
            return

        self.outbox.enqueue(
            op="add_record",
            registry=self.registries_by_kind.get("policy", "afritensor-policy"),
            registry_id=registry_id,
            agent="operator",
            uri=published.get("uri", ""),
            checksum=self.policy_hash.replace("sha256:", ""),
            status="Active",
            metadata={"kind": "policy",
                      "policy_version": str(self.policy.get("policy_version", "")),
                      "signal_id": f"policy:{self.policy_hash[7:23]}"},
        )
        logger.info("policy %s queued for registry %d",
                    self.policy_hash[:19], registry_id)

    def _on_watcher_fill(self, entry, result) -> None:
        # Watcher fills start the cooldown too, so neither path stacks on the other.
        self.cooldown.record_fill(result["price"], result["side"])
        self.engine.on_watcher_fill(entry, result)
        kw = dict(signal_id=entry.signal_id, side=result["side"],
                  price=result["price"], sl=result["sl"], tp=result.get("tp"),
                  route="watcher", timeframe=entry.timeframe,
                  session=entry.session)
        self.feed.fill(**kw)
        self.discord.filled(**kw)

    def _notify_fill(self, **kw) -> None:
        # Start the cooldown from an actual fill, never from a decision — a
        # refused signal must not block the next one.
        self.cooldown.record_fill(kw.get("price", 0), kw.get("side", ""))
        # Feed first: followers act on it, and every second of delay is
        # slippage on their side. Discord is for people and can wait.
        self.feed.fill(**kw)
        self.discord.filled(**kw)

    def _notify_closed(self, trade: dict) -> None:
        if self.vault is not None:
            # Frees the trade's risk on chain and scores the agent.
            self.orders.on_closed(trade)
        self.feed.closed(trade)
        self.discord.closed(trade)

    def _gates(self) -> list:
        # Built in gates/assembly.py so the replay harness runs the same stack.
        return build_gates(self.policy, levels=self.levels,
                           cooldown=self.cooldown, feed=self.feed_gate)


class _Positions:
    """The `ctx.positions` gates hold — a count, nothing more."""

    def __init__(self, market) -> None:
        self._market = market

    def live(self) -> list:
        return self._market.get_open_positions()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_logging()
    app.state.z = Zcastor()
    logger.info("zcastor v2 up — policy %s", app.state.z.policy_hash[:19])

    tasks = [asyncio.create_task(_poll_closes(app.state.z)),
             asyncio.create_task(_drain_outbox(app.state.z)),
             asyncio.create_task(_roll_sessions(app.state.z)),
             asyncio.create_task(_poll_watcher(app.state.z)),
             asyncio.create_task(_resolve_outcomes(app.state.z))]
    if app.state.z.vault is not None:
        tasks.append(asyncio.create_task(_report_to_vault(app.state.z)))
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        # Publish the partial day on the way out. A restart mid-session must
        # not strand the decisions made before it.
        try:
            app.state.z.rollover.close(start_new=False)
            app.state.z.outbox.drain(app.state.z.backend)
        except Exception:  # noqa: BLE001
            logger.exception("shutdown rollover failed")
        # Push what the shutdown just published; the URIs anchored above
        # should resolve the moment they land.
        try:
            app.state.z.repo_sync.flush()
        except Exception:  # noqa: BLE001
            logger.exception("shutdown dossier push failed")
        finally:
            # Released last, after the final writes. A crash leaves it behind,
            # but the lock checks whether its pid is alive, so a stale lock
            # never blocks recovery.
            engine_lock.release(DATA)


async def _poll_closes(z: Zcastor, interval: int = 20) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(z.poller.poll)
        except Exception:  # noqa: BLE001
            logger.exception("close poller cycle failed")


async def _report_to_vault(z: Zcastor) -> None:
    """
    Post the desk's equity to the vault, which halts the desk on chain if it has
    fallen past the owner's drawdown limit. Also settles any permit whose trade
    closed while the vault could not be reached.
    """
    while True:
        try:
            equity = await asyncio.to_thread(z.market.get_equity)
            if equity is not None:
                halted = await asyncio.to_thread(
                    z.vault.report_equity, int(round(float(equity) * 100)))
                if halted:
                    logger.warning("the vault has HALTED the desk; every order "
                                   "will be refused until the owner resumes it")
            await asyncio.to_thread(z.orders.retry_settlements, z.journal)
        except Exception as exc:  # noqa: BLE001
            logger.warning("vault report failed: %s", exc)
        await asyncio.sleep(z.vault_report_seconds)


async def _drain_outbox(z: Zcastor, interval: int = 30) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(z.outbox.drain, z.backend)
            z.outbox.maybe_compact()
        except Exception:  # noqa: BLE001
            logger.exception("outbox drain cycle failed")


async def _poll_watcher(z: Zcastor) -> None:
    """
    The tightest loop in the system. An armed entry is waiting for a price it
    may only touch briefly, so a slow poll means a missed fill.
    """
    interval = float(z.policy.get("watcher", {}).get("poll_seconds", 5))
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(z.watcher.poll)
        except Exception:  # noqa: BLE001
            logger.exception("watcher cycle failed")


async def _resolve_outcomes(z: Zcastor) -> None:
    interval = float(z.policy.get("resolver", {}).get("poll_seconds", 60))
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(z.resolver.tick)
        except Exception:  # noqa: BLE001
            logger.exception("outcome resolver cycle failed")


async def _roll_sessions(z: Zcastor, interval: int = 60) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            result = await asyncio.to_thread(z.rollover.tick)
            if result:
                # The engine holds a reference to the old log; repoint it.
                z.engine.decision_log = z.rollover.log
                logger.info("rolled into %s", z.rollover.day)
                z.discord.session_summary(
                    day=result["day"], totals=result["totals"],
                    reasons=z.rollover.log.ranked_reasons(),
                    journal=z.journal.stats())
        except Exception:  # noqa: BLE001
            logger.exception("session rollover failed")


app = FastAPI(title="Zcastor v2", lifespan=lifespan)

# The dashboard runs in a browser on a different port, so a JSON POST triggers
# a preflight the API has to answer or the signal never arrives. Origins are
# restricted to localhost: this binds to 127.0.0.1 and is not a public API, so
# there is no reason to accept a signal from an arbitrary website.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(https?://)?(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.post("/signal")
async def signal(request: Request):
    payload = await request.json()
    z: Zcastor = request.app.state.z
    # The desk runs one dashboard per perp but, for now, one single-coin engine.
    # A signal for another coin must never be decided against this coin's price.
    traded = getattr(z, "traded_symbol", None)
    sent = str(payload.get("symbol", "")) if isinstance(payload, dict) else ""
    if traded and sent and sent != traded:
        return JSONResponse(status_code=409, content={
            "action": "reject", "reason": "symbol_not_traded",
            "detail": f"this engine trades {traded}; signal was for {sent}"})
    trace = await asyncio.to_thread(z.engine.process, payload)
    return {
        "signal_id": trace.signal_id,
        "action": trace.action.value,
        "reason": trace.reason,
        "route_kind": trace.route_kind or None,
    }


@app.get("/health")
async def health(request: Request):
    z: Zcastor = request.app.state.z
    return {
        "ok": True,
        "policy": z.policy_hash,
        "session": current_session(),
        "balance": z.market.get_balance(),
        "open_trades": len(z.journal.open_tickets()),
        "armed_entries": z.watcher.pending_count(),
        "awaiting_measurement": z.resolver.pending_count(),
        "outbox": z.outbox.stats(),
        "anchor": z.backend.name,
        "environment": _environment(),
        # What is running, not what the environment says now — the two
        # diverge the moment .env is edited without a restart.
        "venue": z.venue,
        "records": z.anchor_index.stats(),
        "levels": z.levels.stats() if z.levels else None,
        "discord": z.discord.stats(),
        "dossier_push": z.repo_sync.last_result,
        "signal_feed": z.feed.stats(),
    }


@app.get("/decisions")
async def decisions(request: Request):
    z: Zcastor = request.app.state.z
    return {"day": z.rollover.day,
            "totals": z.rollover.log.summary(),
            "reasons": z.rollover.log.ranked_reasons(),
            "count": len(z.rollover.log.entries)}


@app.post("/rollover")
async def force_rollover(request: Request):
    """Publish and anchor the current session now. Operator action, for testing
    the record path without waiting for UTC midnight."""
    z: Zcastor = request.app.state.z
    result = await asyncio.to_thread(z.rollover.close, start_new=True)
    z.engine.decision_log = z.rollover.log
    return result


@app.get("/pending")
async def pending(request: Request):
    z: Zcastor = request.app.state.z
    return {"count": z.watcher.pending_count(), "entries": z.watcher.snapshot()}


@app.get("/published")
async def published(request: Request):
    return request.app.state.z.publisher.stats()


@app.get("/outcomes")
async def outcomes(request: Request):
    return request.app.state.z.resolver.stats()


@app.get("/edge")
async def edge(request: Request):
    z: Zcastor = request.app.state.z
    return {"stats": z.ledger.stats(), "segments": z.ledger.snapshot()}


@app.get("/journal")
async def journal(request: Request):
    return request.app.state.z.journal.stats()
