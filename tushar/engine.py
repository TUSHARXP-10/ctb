"""The engine: wires everything together and runs the loops.

  price loop     every 3s   mark prices -> dashboard, big-move detection
  monitor loop   every 5s   reconcile positions, fire paper stops, local stop guard, alerts, equity
  news loop      every 5m   RSS + Fear & Greed
  scanner loop   every 5m   setups across the top perpetuals
  scheduler                  Claude cycles: on schedule, on request, or woken by events
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Any

from .brain import make_brain
from .brain.prompts import build_briefing
from .config import PUBLIC_MARKET_URL, Settings
from .db import DB
from .desk import Desk, trade_stats, utc_day_start
from .events import EventBus
from .exchange.base import ExchangeError
from .market import MarketService
from .news import NewsDesk
from .radar import LiquidationRadar
from .risk import RiskManager
from .scanner import Scanner

log = logging.getLogger("tushar.engine")

CYCLE_TIMEOUT = 1500  # seconds (a full team cycle runs 5 Claude sessions)


class Engine:
    def __init__(self, settings: Settings, db_path: str | Path | None = None, sim_now_ms: int | None = None):
        self.s = settings
        self.db = DB(db_path or Path(settings.data_dir) / "tushar.db")
        changed = settings.apply_overrides(self.db.get_settings())
        if changed:
            log.info("runtime settings restored from the database: %s", sorted(changed))
        self.bus = EventBus()
        if settings.claude_use_subscription and os.environ.pop("ANTHROPIC_API_KEY", None):
            log.warning("ANTHROPIC_API_KEY removed from the environment so Claude runs on your subscription login")

        if settings.market_source == "sim":
            from .exchange.sim import SimMarket
            self.source = SimMarket(seed=settings.sim_seed, now_ms=sim_now_ms)
        else:
            from .exchange.binance import BinanceFutures
            if settings.uses_exchange_account:
                self.source = BinanceFutures(settings.binance_url, settings.binance_api_key,
                                             settings.binance_api_secret, public_fallback_url=PUBLIC_MARKET_URL)
            else:
                self.source = BinanceFutures(PUBLIC_MARKET_URL)
        self.market = MarketService(self.source, settings)
        if settings.uses_exchange_account:
            self.broker = self.source
        else:
            from .exchange.paper import PaperBroker
            self.broker = PaperBroker(self.market.price, settings.paper_start_balance, settings.taker_fee_pct,
                                      state=self.db.get("paper_state"), save_fn=lambda st: self.db.set("paper_state", st))
        self.scanner = Scanner(self.market, settings)
        self.news = NewsDesk(self.db, self.bus)
        self.risk = RiskManager(settings)
        self.desk = Desk(settings, self.db, self.bus, self.market, self.broker, self.scanner, self.news, self.risk)
        self.radar = LiquidationRadar(self.bus)
        self.desk.radar = self.radar
        self.brain = make_brain(settings.brain, self)
        self.team: dict = {"mode": settings.desk_mode, "cycle_id": None, "agents": {}}
        self._restore_team()
        self.telegram = None
        if settings.telegram_bot_token:
            from .telegram import TelegramBot
            self.telegram = TelegramBot(self, settings.telegram_bot_token, settings.telegram_chat_id)

        self.started_at = time.time()
        self.cycle_lock = asyncio.Lock()
        self.cycle_running = False
        self.current_cycle: int | None = None
        self.last_cycle_started: float | None = self.db.get("last_cycle_started")
        self.last_cycle_finished: float | None = self.db.get("last_cycle_finished")
        self.next_cycle_at: float | None = None
        self.claude_session: dict = self.db.get("claude_session") or {}
        self.usage: dict = {}
        self.usage_blocked_until: float | None = None
        self.consecutive_failures = 0
        self.errors: deque[dict] = deque(maxlen=50)
        self._wake = asyncio.Event()
        self._wake_reasons: list[str] = []
        self._tasks: list[asyncio.Task] = []
        self._move_alerted: dict[str, float] = {}
        self._last_equity_at = 0.0
        self._last_protect_check = 0.0
        self._orders_by_sym: dict[str, tuple[float, list]] = {}
        self._cycle_task: asyncio.Task | None = None
        self._stopping = False
        self.ready = False

    # ================================================================ lifecycle
    async def start(self, loops: bool = True) -> None:
        await self.market.refresh_symbols()
        await self.broker.prepare()
        await self.market.refresh_tickers()
        await self.market.refresh_prices()
        try:
            await self.desk.account(max_age=0)
        except ExchangeError as e:
            self.log_error(f"account check failed: {e}")
            raise
        self.ready = True
        self._replay_mind()
        self.bus.publish("status", self.status())
        log.info("Tushar.ai ready: %s, brain=%s, model=%s", self.s.mode_label, self.brain.name, self.s.claude_model)
        if loops:
            coros = [self._price_loop(), self._monitor_loop(), self._news_loop(), self._scanner_loop(),
                     self._scheduler_loop(), self._daily_loop()]
            if self.s.market_source == "sim":
                coros.append(self.radar.run_sim(self.market))
            elif self.s.liquidation_radar:
                coros.append(self.radar.run())
            else:
                self.radar.status = "off"
            if self.telegram:
                coros.append(self.telegram.run())
            for coro in coros:
                self._tasks.append(asyncio.create_task(coro))

    async def stop(self) -> None:
        self._stopping = True
        for t in self._tasks:
            t.cancel()
        if self._cycle_task:
            self._cycle_task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.source.close()

    def _replay_mind(self) -> None:
        """Put the last cycles' thinking back in the live feed after a restart."""
        if self.bus.mind:
            return
        cycles = self.db.query("SELECT id, started_at, trigger, status, finished_at, summary FROM cycles "
                               "ORDER BY id DESC LIMIT 2")[::-1]
        for c in cycles:
            self.bus.mind.append({"type": "cycle_start", "ts": c["started_at"],
                                  "data": {"cycle_id": c["id"], "trigger": c["trigger"]}})
            for e in self.db.query("SELECT kind, data, ts FROM events WHERE cycle_id=? AND kind IN "
                                   "('thought','thinking','tool_call','tool_result') ORDER BY id DESC LIMIT 300",
                                   (c["id"],))[::-1]:
                self.bus.mind.append({"type": e["kind"], "ts": e["ts"], "data": e["data"]})
            if c["finished_at"]:
                self.bus.mind.append({"type": "cycle_end", "ts": c["finished_at"], "data": {
                    "cycle_id": c["id"], "status": c["status"],
                    "duration_s": round(c["finished_at"] - c["started_at"], 1)}})

    def log_error(self, text: str) -> None:
        log.error(text)
        item = {"ts": time.time(), "text": text}
        self.errors.append(item)
        self.bus.publish("log", {"level": "error", "text": text})

    # ================================================================ loops
    async def _price_loop(self) -> None:
        last_tickers = 0.0
        while True:
            try:
                await self.market.refresh_prices()
                if time.time() - last_tickers > 15:
                    await self.market.refresh_tickers()
                    last_tickers = time.time()
                self.bus.publish("tick", self.ticker_rows())
                self._detect_big_moves()
            except Exception as e:  # noqa: BLE001
                self.log_error(f"price loop: {e}")
                await asyncio.sleep(5)
            await asyncio.sleep(3)

    async def _monitor_loop(self) -> None:
        while True:
            try:
                await self.monitor_once()
            except Exception as e:  # noqa: BLE001
                self.log_error(f"monitor: {e}")
            await asyncio.sleep(5)

    async def _news_loop(self) -> None:
        while True:
            try:
                n = await self.news.refresh()
                if n:
                    log.info("news: %d new headlines", n)
            except Exception as e:  # noqa: BLE001
                self.log_error(f"news: {e}")
            await asyncio.sleep(300)

    async def _scanner_loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                res = await self.scanner.scan(max_age=240)
                self.bus.publish("scanner", res)
            except Exception as e:  # noqa: BLE001
                self.log_error(f"scanner: {e}")
            await asyncio.sleep(300)

    async def _scheduler_loop(self) -> None:
        await asyncio.sleep(8)
        if self.last_cycle_finished is None or time.time() - self.last_cycle_finished > self.s.cycle_minutes * 60:
            self.wake("startup")
        while True:
            self.next_cycle_at = self._compute_next_cycle()
            self.bus.publish("status", self.status())
            timeout = max(1.0, self.next_cycle_at - time.time())
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            reasons = self._wake_reasons[:]
            self._wake_reasons.clear()
            self._wake.clear()
            if not reasons and time.time() < self.next_cycle_at - 1:
                continue
            trigger = "; ".join(dict.fromkeys(reasons)) if reasons else f"scheduled (every {self.s.cycle_minutes} min)"
            skip = self._skip_reason(manual="manual" in trigger)
            if skip:
                if reasons:
                    self.bus.publish("notice", {"level": "info", "text": f"Cycle skipped: {skip}"})
                self.last_cycle_finished = time.time()  # wait a full interval before trying again
                continue
            gap = time.time() - (self.last_cycle_started or 0)
            if "manual" not in trigger and gap < self.s.min_cycle_gap_minutes * 60:
                await asyncio.sleep(self.s.min_cycle_gap_minutes * 60 - gap)
            self._cycle_task = asyncio.create_task(self.run_cycle(trigger))
            try:
                await self._cycle_task
            except asyncio.CancelledError:
                if self._stopping:
                    raise
                continue  # the kill switch cancelled the cycle; keep scheduling

    def _compute_next_cycle(self) -> float:
        base = self.last_cycle_finished or time.time()
        nxt = base + self.s.cycle_minutes * 60
        req = self.desk.requested_next_check
        if req and req > (self.last_cycle_started or 0):
            nxt = req
        if self.usage_blocked_until and self.usage_blocked_until > nxt:
            nxt = self.usage_blocked_until
        if self.consecutive_failures:
            nxt = max(nxt, base + min(60, 2 ** self.consecutive_failures) * 60)
        return nxt

    def _skip_reason(self, manual: bool = False) -> str | None:
        st = self.desk.state
        if st.get("kill_switch"):
            return "kill switch is on"
        if st.get("paused") and not manual:
            return "trading is paused"
        if self.usage_blocked_until and time.time() < self.usage_blocked_until:
            return "Claude usage limit reached; waiting for the window to reset"
        if self.desk.halted_reason() and not self.db.open_trades() and not manual:
            return "halted for the day with no open positions"
        return None

    def wake(self, reason: str) -> None:
        self._wake_reasons.append(reason)
        self._wake.set()

    def _detect_big_moves(self) -> None:
        for sym in self.s.watchlist + [t["symbol"] for t in self.db.open_trades()]:
            mv = self.market.move_pct(sym, 15)
            if mv is None or abs(mv) < self.s.big_move_pct:
                continue
            if time.time() - self._move_alerted.get(sym, 0) < 1800:
                continue
            self._move_alerted[sym] = time.time()
            text = f"big move: {sym} {mv:+.2f}% in 15 min"
            self.bus.publish("notice", {"level": "info", "text": text})
            self.wake(text)

    # ================================================================ monitor
    async def monitor_once(self) -> None:
        events = []
        if hasattr(self.broker, "on_tick"):
            events = self.broker.on_tick()
            if events:
                self.desk.invalidate()
        await self.reconcile(events)
        await self._check_alerts()
        await self._check_pending()
        now = time.time()
        if now - self._last_equity_at >= 60:
            self._last_equity_at = now
            acct = await self.desk.account(max_age=0)
            self.db.execute("INSERT OR REPLACE INTO equity(ts,equity,wallet,upnl) VALUES(?,?,?,?)",
                            (now, acct.equity, acct.wallet_balance, acct.unrealized_pnl))
            await self._check_daily_loss(acct)
        acct = await self.desk.account(max_age=4)
        self.bus.publish("account", {**acct.to_dict(), "open_trades": self.db.open_trades()})

    async def reconcile(self, paper_events: list[dict] | None = None) -> None:
        if self.desk._trade_lock.locked():
            return  # an order is in flight; check again on the next tick
        acct = await self.desk.account(max_age=0)
        by_sym = {p.symbol: p for p in acct.positions}
        open_trades = self.db.open_trades()
        known = set()
        for t in open_trades:
            sym = t["symbol"]
            known.add(sym)
            pos = by_sym.get(sym)
            if pos is None or pos.side != t["side"]:
                await self._finalize_external(t, paper_events or [])
                continue
            if abs(pos.qty - t["qty"]) / max(t["qty"], 1e-12) > 0.001:
                self.db.update("trades", t["id"], {"qty": pos.qty})
            # Local guard: enforce the stop/target if the exchange order is missing.
            await self._guard(t, pos, await self._orders_for(sym))
        for sym, pos in by_sym.items():
            if sym not in known:
                self._adopt(pos)

    async def _orders_for(self, symbol: str) -> list | None:
        """Open orders for one symbol (cached 10s). None when the exchange can't tell us."""
        cached = self._orders_by_sym.get(symbol)
        if cached and time.time() - cached[0] < 10:
            return cached[1]
        try:
            orders = await self.broker.open_orders(symbol)
        except ExchangeError as e:
            log.warning("open orders %s failed: %s", symbol, e)
            return None
        self._orders_by_sym[symbol] = (time.time(), orders)
        return orders

    async def _guard(self, t: dict, pos, orders: list | None) -> None:
        sym, long_ = t["symbol"], t["side"] == "long"
        mark = self.market.price(sym) or pos.mark_price
        known = orders is not None
        sym_orders = [o for o in (orders or []) if o.symbol == sym]
        has_sl = any(o.kind == "SL" for o in sym_orders) or not known
        has_tp = any(o.kind == "TP" for o in sym_orders) or not known
        local = (t.get("extra") or {}).get("protection") == "local"
        stop, tp = t.get("stop_loss"), t.get("take_profit")
        if stop and (local or not has_sl):
            if (long_ and mark <= stop) or (not long_ and mark >= stop):
                self.bus.publish("notice", {"level": "warn", "text": f"{sym}: stop {stop} hit, closing (local guard)"})
                self.desk.actor = "guard"
                try:
                    await self.desk.close_position(sym, 1.0, "stop loss (local guard)", exit_reason="stop_loss")
                finally:
                    self.desk.actor = "claude"
                return
        if tp and (local or not has_tp):
            if (long_ and mark >= tp) or (not long_ and mark <= tp):
                self.bus.publish("notice", {"level": "info", "text": f"{sym}: target {tp} hit, closing (local guard)"})
                self.desk.actor = "guard"
                try:
                    await self.desk.close_position(sym, 1.0, "take profit (local guard)", exit_reason="take_profit")
                finally:
                    self.desk.actor = "claude"
                return
        # Re-place missing exchange protection at most once a minute.
        if (not has_sl or not has_tp) and stop and tp and time.time() - self._last_protect_check > 60:
            self._last_protect_check = time.time()
            from decimal import Decimal
            try:
                await self.broker.cancel_protection(sym)
                await self.broker.place_protection(sym, t["side"], Decimal(str(pos.qty)),
                                                   stop=Decimal(str(stop)), target=Decimal(str(tp)))
                extra = t.get("extra") or {}
                extra["protection"] = "exchange"
                self.db.update("trades", t["id"], {"extra": extra})
                self.desk.invalidate()
                self._orders_by_sym.pop(sym, None)
                log.info("re-placed protection for %s", sym)
            except ExchangeError as e:
                extra = t.get("extra") or {}
                if extra.get("protection") != "local":
                    extra["protection"] = "local"
                    self.db.update("trades", t["id"], {"extra": extra})
                    self.bus.publish("notice", {"level": "warn",
                                                "text": f"{sym}: exchange stop/target unavailable ({e}); local guard active"})

    async def _finalize_external(self, t: dict, paper_events: list[dict]) -> None:
        sym = t["symbol"]
        fills = await self.desk.trade_fills(t)
        exit_price = self.market.price(sym) or t["entry_price"]
        ev = next((e for e in paper_events if e["symbol"] == sym), None)
        if ev:
            exit_price = ev["price"]
        reason = "external"
        if ev:
            reason = {"SL": "stop_loss", "TP": "take_profit", "LIQUIDATION": "liquidation"}[ev["kind"]]
        done = self.desk.finalize_trade(t, exit_price, reason, "closed on the exchange", fills=fills)
        if reason == "external":
            ep = done["exit_price"]
            stop, tp = t.get("stop_loss"), t.get("take_profit")
            if stop and tp:
                reason = "stop_loss" if abs(ep - stop) < abs(ep - tp) else "take_profit"
                self.db.update("trades", t["id"], {"exit_reason": reason})
        try:
            await self.broker.cancel_protection(sym)  # the other leg (TP after SL, or vice versa)
        except ExchangeError:
            pass
        pnl = done.get("pnl") or 0
        self.bus.publish("notice", {"level": "info" if pnl >= 0 else "warn",
                                    "text": f"{sym} {t['side']} closed by {reason}: {pnl:+.2f} USDT"})
        self.wake(f"position closed: {sym} {t['side']} {reason} {pnl:+.2f} USDT")

    def _adopt(self, pos) -> None:
        tid = self.db.insert("trades", {
            "symbol": pos.symbol, "side": pos.side, "qty": pos.qty, "entry_price": pos.entry_price,
            "stop_loss": None, "take_profit": None, "initial_stop": None, "leverage": pos.leverage,
            "risk_usd": None, "notional": pos.notional, "opened_at": time.time(), "status": "open",
            "setup": "adopted", "thesis": "Position found on the exchange that Tushar.ai did not open.",
            "extra": {"protection": "none", "actor": "external"},
        })
        self.bus.publish("notice", {"level": "warn", "text": f"Adopted {pos.symbol} {pos.side} (trade #{tid}); "
                                    "it has no stop until Claude sets one."})
        self.wake(f"adopted external position {pos.symbol}")

    async def _check_alerts(self) -> None:
        for a in self.desk.active_alerts():
            p = self.market.price(a["symbol"])
            if p is None:
                continue
            if (a["condition"] == "above" and p >= a["price"]) or (a["condition"] == "below" and p <= a["price"]):
                self.db.update("alerts", a["id"], {"status": "triggered", "triggered_at": time.time()})
                text = f"alert: {a['symbol']} {a['condition']} {a['price']:g} (now {p:g})" + \
                       (f" - {a['note']}" if a.get("note") else "")
                self.bus.publish("alert_hit", {"alert": a, "price": p, "text": text})
                self.bus.publish("alerts", self.desk.active_alerts())
                self.wake(text)

    async def _check_pending(self) -> None:
        for p in self.desk.pending_entries():
            now = time.time()
            if now >= p["expires_at"]:
                self.db.update("pending_entries", p["id"], {"status": "expired"})
                self.bus.publish("pending", self.desk.pending_entries())
                continue
            price = self.market.price(p["symbol"])
            if price is None:
                continue
            hit = price >= p["entry"] if p["condition"] == "above" else price <= p["entry"]
            if not hit:
                continue
            self.db.update("pending_entries", p["id"], {"status": "triggering", "triggered_at": now})
            self.desk.actor = "pending"
            try:
                res = await self.desk.open_position(
                    p["symbol"], p["side"], p["stop_loss"], p["take_profit"], p["risk_pct"], p["leverage"],
                    p["confidence"], p["setup"], f"{p['thesis']} [pending entry #{p['id']} at {p['entry']:g}]")
            except Exception as e:  # noqa: BLE001
                res = {"status": "ERROR", "error": str(e)}
            finally:
                self.desk.actor = "claude"
            status = {"FILLED": "filled", "BLOCKED": "blocked"}.get(res.get("status"), "error")
            self.db.update("pending_entries", p["id"], {"status": status, "result": res})
            text = (f"pending entry #{p['id']} {p['symbol']} {p['side']} at {p['entry']:g}: {status}"
                    + (f" ({'; '.join(res.get('blocks', []))})" if status == "blocked" else "")
                    + (f" ({res.get('error')})" if status == "error" else ""))
            self.bus.publish("notice", {"level": "info" if status == "filled" else "warn", "text": text})
            self.bus.publish("pending", self.desk.pending_entries())
            self.wake(text)

    async def _check_daily_loss(self, acct) -> None:
        if self.desk.halted_reason():
            return
        sod = self.desk.start_of_day_equity()
        if not sod:
            return
        drawdown = sod - acct.equity
        limit = sod * self.s.max_daily_loss_pct / 100
        if drawdown >= limit:
            self.desk.halt_for_today(f"equity down {drawdown:,.2f} USDT today (limit {limit:,.2f}); "
                                     "no new entries until 00:00 UTC")

    # ================================================================ cycles
    async def run_cycle(self, trigger: str) -> dict:
        async with self.cycle_lock:
            self.cycle_running = True
            started = time.time()
            self.last_cycle_started = started
            self.db.set("last_cycle_started", started)
            cid = self.db.insert("cycles", {"started_at": started, "trigger": trigger, "brain": self.brain.name,
                                            "model": self.s.claude_model, "status": "running"})
            self.current_cycle = cid
            self.desk.cycle_id = cid
            self.desk.actions = []
            self.desk.actor = "claude"
            self.desk.requested_next_check = None
            self.bus.publish("cycle_start", {"cycle_id": cid, "trigger": trigger, "brain": self.brain.name,
                                             "model": self.s.claude_model})
            self.bus.publish("status", self.status())
            result: dict[str, Any] = {}
            status = "done"
            error = None
            try:
                closed = self.db.unreviewed_closed()[-6:]
                briefing = await self.build_briefing(trigger, closed)
                self.db.add_event(cid, "briefing", {"text": briefing})
                result = await asyncio.wait_for(self.brain.run_cycle(briefing, cid, trigger=trigger),
                                                timeout=CYCLE_TIMEOUT)
                for t in closed:
                    if not self.db.one("SELECT reviewed FROM trades WHERE id=?", (t["id"],))["reviewed"]:
                        self.db.update("trades", t["id"], {"reviewed": 1})
                if result.get("is_error") or result.get("assistant_error"):
                    status = "error"
                    error = result.get("assistant_error") or "; ".join(result.get("errors") or []) or \
                        (result.get("result") or "")[:300]
                self.consecutive_failures = 0 if status == "done" else self.consecutive_failures + 1
            except asyncio.TimeoutError:
                status, error = "error", f"cycle timed out after {CYCLE_TIMEOUT}s"
                self.consecutive_failures += 1
            except asyncio.CancelledError:
                status, error = "cancelled", "cancelled"
                raise
            except Exception as e:  # noqa: BLE001
                status, error = "error", f"{type(e).__name__}: {e}"
                self.consecutive_failures += 1
                log.exception("cycle %s failed", cid)
            finally:
                finished = time.time()
                self.cycle_running = False
                self.current_cycle = None
                self.last_cycle_finished = finished
                self.db.set("last_cycle_finished", finished)
                self.db.update("cycles", cid, {
                    "finished_at": finished, "status": status, "num_turns": result.get("num_turns"),
                    "duration_ms": int((finished - started) * 1000),
                    "cost_usd": result.get("cost_usd"), "input_tokens": result.get("input_tokens"),
                    "output_tokens": result.get("output_tokens"), "summary": (result.get("summary") or "")[:3000],
                    "error": error, "actions": self.desk.actions,
                })
                if error:
                    self.log_error(f"cycle #{cid}: {error}")
                    self._maybe_usage_block(error)
                self.bus.publish("cycle_end", {"cycle_id": cid, "status": status, "error": error,
                                               "summary": result.get("summary"), "actions": self.desk.actions,
                                               "duration_s": round(finished - started, 1),
                                               "num_turns": result.get("num_turns")})
                self.bus.publish("status", self.status())
            return {"cycle_id": cid, "status": status, "error": error, **result}

    def _maybe_usage_block(self, error: str) -> None:
        low = error.lower()
        if any(k in low for k in ("rate_limit", "usage limit", "rate limit", "429", "limit reached")):
            resets = (self.usage or {}).get("resets_at")
            self.usage_blocked_until = float(resets) if resets and resets > time.time() else time.time() + 1800
            self.bus.publish("notice", {"level": "warn", "text": "Claude usage limit hit; cycles resume when it resets."})

    async def build_briefing(self, trigger: str, closed: list[dict]) -> str:
        desk = self.desk
        portfolio = await desk.get_portfolio()
        overview = await desk.get_market_overview()
        try:
            scan = await self.scanner.scan(max_age=240)
        except Exception as e:  # noqa: BLE001
            scan = {"hits": [], "scanned": 0, "error": str(e)}
        news = await desk.get_news(hours=12, limit=20)
        stats = trade_stats(self.db.recent_trades(300, status="closed"))
        liq = self.radar.summary(60, 5) if self.radar.events else None
        return build_briefing(trigger=trigger, settings=self.s, portfolio=portfolio, overview=overview, scan=scan,
                              news=news, closed=closed, memories=self.db.memories(15), stats=stats,
                              outlook=desk.outlook, alerts=desk.active_alerts(), usage=self.usage, liquidations=liq)

    def note_usage(self, info: dict) -> None:
        self.usage = {**info, "updated": time.time()}
        if info.get("status") == "rejected":
            resets = info.get("resets_at")
            self.usage_blocked_until = float(resets) if resets else time.time() + 1800
        self.bus.publish("usage", self.usage)

    def note_claude_session(self, info: dict) -> None:
        self.claude_session = {**info, "seen": time.time()}
        self.db.set("claude_session", self.claude_session)
        self.bus.publish("status", self.status())

    # ================================================================ team
    def _restore_team(self) -> None:
        row = self.db.one("SELECT cycle_id FROM reports ORDER BY id DESC LIMIT 1")
        if not row:
            return
        self.team["cycle_id"] = row["cycle_id"]
        for r in self.db.query("SELECT agent, data, ts FROM reports WHERE cycle_id=?", (row["cycle_id"],)):
            self.team["agents"][r["agent"]] = {"status": "done", "ts": r["ts"], "report": r["data"]}

    def team_mode_note(self, cycle_id: int, mode: str) -> None:
        self.team = {"mode": mode, "cycle_id": cycle_id,
                     "agents": {} if mode.startswith("team") else {"pm": self.team.get("agents", {}).get("pm", {})}}
        self.bus.publish("team", self.team_view())

    def agent_status(self, cycle_id: int, agent: str, status: str) -> None:
        a = self.team["agents"].setdefault(agent, {})
        a.update({"status": status, "ts": time.time()})
        self.bus.publish("agent_status", {"cycle_id": cycle_id, "agent": agent, "status": status})

    def agent_report(self, cycle_id: int, agent: str, report: dict) -> None:
        self.db.insert("reports", {"ts": time.time(), "cycle_id": cycle_id, "agent": agent, "data": report})
        a = self.team["agents"].setdefault(agent, {})
        a.update({"status": "error" if report.get("error") else "done", "ts": time.time(), "report": report})
        self.bus.publish("agent_report", {"cycle_id": cycle_id, "agent": agent, "report": report})

    def team_view(self) -> dict:
        from .brain.prompts import PM_META, TEAM
        meta = {k: {kk: v[kk] for kk in ("name", "emoji", "color")} for k, v in TEAM.items()}
        meta["pm"] = PM_META
        return {**self.team, "meta": meta, "order": ["chartist", "macro", "flow", "risk", "pm"],
                "desk_mode": self.s.desk_mode, "analyst_model": self.s.analyst_model, "pm_model": self.s.claude_model}

    # ================================================================ daily report
    async def _daily_loop(self) -> None:
        from datetime import datetime, timedelta, timezone
        while True:
            now = datetime.now(timezone.utc)
            nxt = now.replace(hour=self.s.daily_report_hour_utc, minute=5, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            await asyncio.sleep(max(30.0, (nxt - now).total_seconds()))
            if self.s.daily_report:
                try:
                    await self.make_daily_report()
                except Exception as e:  # noqa: BLE001
                    self.log_error(f"daily report: {e}")

    async def make_daily_report(self) -> dict:
        from datetime import datetime, timezone
        since = time.time() - 86400
        trades = [t for t in self.db.recent_trades(300) if (t["closed_at"] or t["opened_at"]) >= since]
        stats = trade_stats([t for t in trades if t["status"] == "closed"])
        acct = await self.desk.account(max_age=0)
        eq = self.db.one("SELECT equity FROM equity WHERE ts>=? ORDER BY ts LIMIT 1", (since,))
        start_eq = eq["equity"] if eq else acct.equity
        facts = {
            "equity_now": round(acct.equity, 2), "equity_24h_ago": round(start_eq, 2),
            "change_24h": round(acct.equity - start_eq, 2),
            "change_24h_pct": round((acct.equity / start_eq - 1) * 100, 2) if start_eq else None,
            "stats_24h": stats, "open_positions": len(acct.positions),
            "trades_24h": [{"symbol": t["symbol"], "side": t["side"], "pnl": t["pnl"], "r": t["r_multiple"],
                            "exit_reason": t["exit_reason"], "setup": t["setup"], "review": t["review"]}
                           for t in trades],
            "outlook": self.desk.outlook,
        }
        context = ("Facts for the last 24 hours (UTC):\n" + json.dumps(facts, default=str) +
                   "\n\nWrite the daily trading diary now.")
        text = await asyncio.wait_for(self.brain.daily_report(context), timeout=600)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rid = self.db.insert("daily_reports", {"ts": time.time(), "day": day, "text": text, "stats": facts})
        rep = {"id": rid, "day": day, "text": text, "stats": facts, "ts": time.time()}
        self.bus.publish("daily_report", rep)
        return rep

    # ================================================================ controls
    async def run_now(self) -> dict:
        if self.cycle_running:
            return {"ok": False, "error": "a cycle is already running"}
        self.wake("manual run from dashboard")
        return {"ok": True}

    def pause(self) -> None:
        self.desk.state["paused"] = True
        self.desk.save_state()
        self.bus.publish("status", self.status())

    def resume(self) -> None:
        self.desk.state["paused"] = False
        self.desk.save_state()
        self.bus.publish("status", self.status())

    async def kill(self) -> list[dict]:
        """Kill switch: stop Claude, close every position, block new entries."""
        self.desk.state["kill_switch"] = True
        self.desk.save_state()
        if self._cycle_task and not self._cycle_task.done():
            self._cycle_task.cancel()
        self.desk.actor = "kill_switch"
        try:
            closed = await self.desk.flatten_all("kill switch")
        finally:
            self.desk.actor = "claude"
        self.bus.publish("notice", {"level": "warn", "text": f"KILL SWITCH: closed {len(closed)} position(s)."})
        self.bus.publish("status", self.status())
        return closed

    def reset_kill(self) -> None:
        self.desk.state["kill_switch"] = False
        self.desk.save_state()
        self.bus.publish("status", self.status())

    async def close_manual(self, symbol: str) -> dict:
        self.desk.actor = "manual"
        try:
            return await self.desk.close_position(symbol, 1.0, "closed from dashboard", exit_reason="manual")
        finally:
            self.desk.actor = "claude"

    def update_settings(self, values: dict) -> dict:
        changed = self.s.apply_overrides(values)
        if changed:
            self.db.save_settings({k: getattr(self.s, k) for k in changed})
            self.bus.publish("settings", self.s.public_dict())
            self._wake.set()  # re-plan the schedule
        return changed

    async def chat(self, message: str) -> str:
        history = self.db.query("SELECT role, text FROM chat ORDER BY id DESC LIMIT 10")[::-1]
        self.db.insert("chat", {"ts": time.time(), "role": "owner", "text": message[:4000]})
        self.bus.publish("chat", {"role": "owner", "text": message})
        try:
            answer = await asyncio.wait_for(self.brain.chat(message, history), timeout=600)
        except Exception as e:  # noqa: BLE001
            answer = f"(chat failed: {type(e).__name__}: {e})"
        self.db.insert("chat", {"ts": time.time(), "role": "tushar", "text": answer})
        self.bus.publish("chat", {"role": "tushar", "text": answer})
        return answer

    # ================================================================ views
    def ticker_rows(self) -> dict:
        syms = list(dict.fromkeys(self.s.watchlist + [t["symbol"] for t in self.db.open_trades()]))
        out = {}
        for s in syms:
            m = self.market.marks.get(s, {})
            t = self.market.tickers.get(s, {})
            out[s] = {"price": self.market.price(s), "chg24": t.get("change_pct"), "funding": m.get("funding_rate"),
                      "vol": t.get("quote_volume"), "high": t.get("high"), "low": t.get("low")}
        return out

    def status(self) -> dict:
        st = self.desk.state
        if st.get("kill_switch"):
            state = "kill"
        elif st.get("paused"):
            state = "paused"
        elif self.desk.halted_reason():
            state = "halted"
        else:
            state = "active"
        return {
            "state": state, "halt_reason": self.desk.halted_reason(), "cycle_running": self.cycle_running,
            "current_cycle": self.current_cycle, "next_cycle_at": self.next_cycle_at,
            "last_cycle_started": self.last_cycle_started, "last_cycle_finished": self.last_cycle_finished,
            "brain": self.brain.name, "model": self.s.claude_model, "mode": self.s.mode_label,
            "claude": self.claude_session, "usage": self.usage, "usage_blocked_until": self.usage_blocked_until,
            "uptime_s": round(time.time() - self.started_at), "ready": self.ready,
            "news_ok": self.news.healthy, "consecutive_failures": self.consecutive_failures,
            "desk_mode": self.s.desk_mode, "radar": self.radar.status,
            "telegram": (self.telegram.status if self.telegram else "off"),
        }

    async def full_state(self) -> dict:
        db = self.db
        try:
            acct = (await self.desk.account(max_age=3)).to_dict()
        except Exception as e:  # noqa: BLE001
            acct = {"error": str(e)}
        try:
            orders = [o.to_dict() for o in await self.desk.open_orders(max_age=5)]
        except Exception:  # noqa: BLE001
            orders = []
        closed = db.recent_trades(300, status="closed")
        realized_today = sum(t["pnl"] or 0 for t in closed if (t["closed_at"] or 0) >= utc_day_start())
        equity = db.query("SELECT ts, equity FROM equity ORDER BY ts DESC LIMIT 2000")[::-1]
        return {
            "status": self.status(),
            "settings": self.s.public_dict(),
            "account": acct,
            "orders": orders,
            "open_trades": db.open_trades(),
            "trades": db.recent_trades(60),
            "stats": trade_stats(closed),
            "realized_today": realized_today,
            "start_of_day_equity": self.desk.start_of_day_equity(),
            "tickers": self.ticker_rows(),
            "outlook": self.desk.outlook,
            "mind": list(self.bus.mind),
            "cycles": db.query("SELECT id, started_at, finished_at, trigger, brain, model, status, num_turns, "
                               "duration_ms, cost_usd, summary, error FROM cycles ORDER BY id DESC LIMIT 30"),
            "news": self.news.latest(hours=48, limit=60),
            "news_status": self.news.status,
            "fng": self.news.fng,
            "alerts": self.desk.active_alerts(),
            "pending": self.desk.pending_entries(),
            "memories": db.memories(40),
            "scanner": self.scanner.last,
            "equity": equity,
            "sim": db.one("SELECT * FROM sims ORDER BY id DESC LIMIT 1"),
            "chat": db.query("SELECT role, text, ts FROM chat ORDER BY id DESC LIMIT 30")[::-1],
            "team": self.team_view(),
            "liquidations": {"summary": self.radar.summary(60, 8), "recent": self.radar.recent(30),
                             "buckets": self.radar.buckets(120, 5)},
            "daily_reports": db.query("SELECT id, ts, day, text FROM daily_reports ORDER BY id DESC LIMIT 14"),
            "errors": list(self.errors)[-10:],
        }
