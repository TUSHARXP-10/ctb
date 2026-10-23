"""The trading desk: every action Claude can take, with the risk gate in front.

All brains (Claude agent, Claude CLI, mock) go through these methods, so the
same sizing, limits and logging apply no matter who is trading.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from . import timemachine as tmach
from .exchange.base import Account, ExchangeError, Position
from .market import MarketService, closed_only, r
from .risk import EntryRequest, RiskContext, RiskManager, open_risk_usd

log = logging.getLogger("tushar.desk")


def utc_day_start(ts: float | None = None) -> float:
    d = datetime.fromtimestamp(ts or time.time(), tz=timezone.utc)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def trade_stats(trades: list[dict]) -> dict:
    closed = [t for t in trades if t["status"] == "closed" and t.get("pnl") is not None]
    if not closed:
        return {"trades": 0}
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    rs = [t["r_multiple"] for t in closed if t.get("r_multiple") is not None]
    by_setup: dict[str, dict] = {}
    for t in closed:
        b = by_setup.setdefault(t.get("setup") or "unlabeled", {"n": 0, "wins": 0, "pnl": 0.0, "r": []})
        b["n"] += 1
        b["wins"] += t["pnl"] > 0
        b["pnl"] += t["pnl"]
        if t.get("r_multiple") is not None:
            b["r"].append(t["r_multiple"])
    for b in by_setup.values():
        b["avg_r"] = round(sum(b["r"]) / len(b["r"]), 2) if b["r"] else None
        b["pnl"] = round(b["pnl"], 2)
        del b["r"]
    streak, last = 0, None
    for t in sorted(closed, key=lambda x: x["closed_at"], reverse=True):
        w = t["pnl"] > 0
        if last is None or w == last:
            streak += 1
            last = w
        else:
            break
    return {
        "trades": len(closed), "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1),
        "total_pnl": round(sum(t["pnl"] for t in closed), 2),
        "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "best": round(max(t["pnl"] for t in closed), 2), "worst": round(min(t["pnl"] for t in closed), 2),
        "streak": f"{streak} {'win' if last else 'loss'}{'es' if not last and streak > 1 else ('s' if streak > 1 else '')}",
        "by_setup": by_setup,
    }


class Desk:
    def __init__(self, settings, db, bus, market: MarketService, broker, scanner, news, risk: RiskManager):
        self.s = settings
        self.db = db
        self.bus = bus
        self.market = market
        self.broker = broker
        self.scanner = scanner
        self.news = news
        self.risk = risk
        self.cycle_id: int | None = None
        self.actor = "claude"
        self.requested_next_check: float | None = None
        self.wake_requests: list[str] = []
        self.state: dict = db.get("desk_state") or {"paused": False, "kill_switch": False,
                                                     "halt_day": None, "halt_reason": None}
        self.outlook: dict | None = db.get("outlook")
        self._acct: tuple[float, Account] | None = None
        self._realized: tuple[float, float] | None = None
        self._tm_cache: dict[tuple, tuple[float, dict]] = {}
        self._orders_cache: tuple[float, list] | None = None
        self.actions: list[dict] = []
        self._trade_lock = asyncio.Lock()

    # ================================================================ state
    def save_state(self) -> None:
        self.db.set("desk_state", self.state)

    def halted_reason(self) -> str | None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.state.get("halt_day") == day:
            return self.state.get("halt_reason")
        return None

    def halt_for_today(self, reason: str) -> None:
        self.state["halt_day"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.state["halt_reason"] = reason
        self.save_state()
        self.bus.publish("notice", {"level": "warn", "text": f"Trading halted for the day: {reason}"})

    async def account(self, max_age: float = 2.0) -> Account:
        if self._acct and time.time() - self._acct[0] < max_age:
            return self._acct[1]
        acct = await self.broker.account()
        self._acct = (time.time(), acct)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        sod = self.db.get("sod") or {}
        if sod.get("day") != day:
            self.db.set("sod", {"day": day, "equity": acct.equity})
        return acct

    def invalidate(self) -> None:
        self._acct = None
        self._realized = None
        self._orders_cache = None

    def start_of_day_equity(self) -> float | None:
        sod = self.db.get("sod") or {}
        return sod.get("equity")

    async def realized_today(self, max_age: float = 15.0) -> float:
        if self._realized and time.time() - self._realized[0] < max_age:
            return self._realized[1]
        try:
            items = await self.broker.income_since(int(utc_day_start() * 1000))
            total = sum(i["amount"] for i in items
                        if i["type"] in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE", "INSURANCE_CLEAR"))
        except ExchangeError as e:
            log.warning("income lookup failed: %s", e)
            closed = self.db.closed_since(utc_day_start())
            total = sum(t["pnl"] or 0 for t in closed)
        self._realized = (time.time(), total)
        return total

    async def open_orders(self, max_age: float = 5.0) -> list:
        if self._orders_cache and time.time() - self._orders_cache[0] < max_age:
            return self._orders_cache[1]
        orders = await self.broker.open_orders()
        self._orders_cache = (time.time(), orders)
        return orders

    def _record(self, action: str, payload: dict) -> None:
        entry = {"ts": time.time(), "action": action, **payload}
        self.actions.append(entry)
        self.db.add_event(self.cycle_id, "action", entry)

    def _live_price(self, symbol: str) -> float | None:
        return self.market.price(symbol)

    async def _fresh_price(self, symbol: str) -> float | None:
        if time.time() - self.market.prices_at > 4:
            try:
                await self.market.refresh_prices()
            except Exception as e:  # noqa: BLE001
                log.warning("price refresh failed: %s", e)
        return self.market.price(symbol)

    # ================================================================ reads
    async def get_portfolio(self) -> dict:
        acct = await self.account()
        try:
            orders = await self.open_orders()
        except ExchangeError as e:
            log.warning("open orders unavailable: %s", e)
            orders = []
        trades = {t["symbol"]: t for t in self.db.open_trades()}
        realized = await self.realized_today()
        sod = self.start_of_day_equity() or acct.equity
        limit = sod * self.s.max_daily_loss_pct / 100
        open_risk = open_risk_usd(list(trades.values()))
        positions = []
        for p in acct.positions:
            t = trades.get(p.symbol)
            prot = [o.to_dict() for o in orders if o.symbol == p.symbol]
            row = {
                "symbol": p.symbol, "side": p.side, "qty": p.qty, "entry": r(p.entry_price), "mark": r(p.mark_price),
                "unrealized_pnl": round(p.unrealized_pnl, 2),
                "pnl_pct_of_margin": round(p.unrealized_pnl / p.margin * 100, 2) if p.margin else None,
                "leverage": p.leverage, "liquidation": r(p.liquidation_price), "margin": round(p.margin, 2),
                "protection": [{"kind": o["kind"], "trigger": r(o["trigger_price"])} for o in prot],
            }
            if t:
                risk_per_unit = abs(t["entry_price"] - (t["initial_stop"] or t["stop_loss"] or t["entry_price"]))
                move = (p.mark_price - t["entry_price"]) * (1 if p.side == "long" else -1)
                row.update({
                    "trade_id": t["id"], "stop_loss": r(t["stop_loss"]), "take_profit": r(t["take_profit"]),
                    "r_now": round(move / risk_per_unit, 2) if risk_per_unit else None,
                    "opened_minutes_ago": round((time.time() - t["opened_at"]) / 60),
                    "setup": t["setup"], "thesis": (t["thesis"] or "")[:300],
                })
            else:
                row["note"] = "position not opened by Tushar.ai (adopted)"
            positions.append(row)
        return {
            "mode": self.s.mode_label,
            "equity": round(acct.equity, 2), "wallet_balance": round(acct.wallet_balance, 2),
            "available": round(acct.available, 2), "unrealized_pnl": round(acct.unrealized_pnl, 2),
            "realized_pnl_today": round(realized, 2),
            "start_of_day_equity": round(sod, 2),
            "daily_loss_limit": round(limit, 2),
            "daily_loss_budget_left": round(limit + min(realized, 0) - open_risk, 2),
            "open_risk_at_stops": round(open_risk, 2),
            "entries_today": len(self.db.trades_since(utc_day_start())),
            "max_entries_per_day": self.s.max_trades_per_day,
            "positions": positions,
            "status": self.status_line(),
        }

    def status_line(self) -> str:
        if self.state.get("kill_switch"):
            return "KILL SWITCH ON: no new entries"
        if self.state.get("paused"):
            return "PAUSED: no new entries"
        h = self.halted_reason()
        return f"HALTED today: {h}" if h else "active"

    async def get_market_overview(self, symbols: list[str] | None = None) -> dict:
        syms = [s.upper() for s in (symbols or self.s.watchlist)][:30]
        return {"as_of_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "rows": await self.market.overview(syms)}

    async def analyze_symbol(self, symbol: str) -> dict:
        symbol = symbol.upper()
        snap = await self.market.snapshot(symbol)
        hits = [h for h in (self.scanner.last.get("hits") or []) if h["symbol"] == symbol]
        if hits:
            snap["scanner_hits"] = [{"setup": h["setup"], "direction": h["direction"]} for h in hits]
        news = self.news.latest(hours=24, symbol=symbol, limit=6)
        if news:
            snap["news_24h"] = [{"title": n["title"], "source": n["source"],
                                 "hours_ago": round((time.time() - n["ts"]) / 3600, 1)} for n in news]
        return snap

    async def scan_market(self) -> dict:
        res = await self.scanner.scan()
        return {"scanned": res["scanned"], "failed": res["failed"],
                "as_of_utc": datetime.fromtimestamp(res["as_of"], tz=timezone.utc).strftime("%H:%M") if res["as_of"] else None,
                "hits": res["hits"][:30],
                "note": "A hit is a chart matching a rule, not a reason to trade by itself."}

    async def get_news(self, hours: float = 12, symbol: str | None = None, limit: int = 25) -> dict:
        items = self.news.latest(hours=hours, symbol=symbol.upper() if symbol else None, limit=limit)
        return {
            "fear_greed": {k: v for k, v in (self.news.fng or {}).items() if k != "history"} or None,
            "fear_greed_7d": (self.news.fng or {}).get("history", [])[-7:],
            "headlines": [{"hours_ago": round((time.time() - n["ts"]) / 3600, 1), "source": n["source"],
                           "title": n["title"], "tags": n.get("tags"), "coins": n.get("symbols"),
                           "kw_sentiment": n.get("sentiment"), "summary": (n.get("summary") or "")[:220]}
                          for n in items],
            "feeds": self.news.status,
        }

    async def run_time_machine(self, symbol: str, side: str, stop_loss: float, take_profit: float,
                               entry: float | None = None, horizon_hours: float = 24) -> dict:
        symbol = symbol.upper()
        price = entry or await self._fresh_price(symbol)
        if not price:
            return {"ok": False, "error": f"no price for {symbol}"}
        key = (symbol, side, round(float(stop_loss), 8), round(float(take_profit), 8), round(float(horizon_hours), 2))
        cached = self._tm_cache.get(key)
        if cached and time.time() - cached[0] < 600:
            return cached[1]
        candles = closed_only(await self.market.klines(symbol, "5m", 3000))
        plan = tmach.Plan(symbol, side, float(price), float(stop_loss), float(take_profit), float(horizon_hours))
        result = await asyncio.to_thread(tmach.run, candles, plan, self.s.tm_replays, 19, 2,
                                         2 * self.s.taker_fee_pct / 100)
        self._tm_cache[key] = (time.time(), result)
        if result.get("ok"):
            sim_id = self.db.insert("sims", {"ts": time.time(), "symbol": symbol, "side": side,
                                             "params": result["plan"], "result": result})
            result["sim_id"] = sim_id
            self.bus.publish("sim", {"id": sim_id, "symbol": symbol, "side": side, "plan": result["plan"],
                                     "verdict": result["verdict"], "mc": result["mc"], "history": result["history"]})
        return result

    async def get_trade_journal(self, limit: int = 20) -> dict:
        rows = self.db.recent_trades(limit)
        all_closed = self.db.recent_trades(500, status="closed")
        return {
            "stats_all_time": trade_stats(all_closed),
            "trades": [{
                "id": t["id"], "symbol": t["symbol"], "side": t["side"], "status": t["status"],
                "entry": r(t["entry_price"]), "exit": r(t["exit_price"]), "stop": r(t["initial_stop"]),
                "target": r(t["take_profit"]), "pnl": round(t["pnl"], 2) if t["pnl"] is not None else None,
                "r": t["r_multiple"], "exit_reason": t["exit_reason"], "setup": t["setup"],
                "confidence": t["confidence"], "thesis": (t["thesis"] or "")[:240], "review": t["review"],
                "opened_utc": datetime.fromtimestamp(t["opened_at"], tz=timezone.utc).strftime("%m-%d %H:%M"),
                "held_minutes": round(((t["closed_at"] or time.time()) - t["opened_at"]) / 60),
            } for t in rows],
        }

    # ================================================================ planning
    async def _risk_context(self, symbol: str, tm_result: dict | None = None) -> RiskContext:
        acct = await self.account(max_age=1.0)
        last_loss = self.db.one(
            "SELECT closed_at FROM trades WHERE symbol=? AND status='closed' AND pnl<0 ORDER BY closed_at DESC LIMIT 1",
            (symbol,))
        return RiskContext(
            account=acct, price=await self._fresh_price(symbol) or 0.0, info=self.market.info(symbol),
            open_trades=self.db.open_trades(), trades_today=len(self.db.trades_since(utc_day_start())),
            realized_today=await self.realized_today(), start_of_day_equity=self.start_of_day_equity() or acct.equity,
            last_loss_at=last_loss["closed_at"] if last_loss else None,
            paused=self.state.get("paused", False), kill_switch=self.state.get("kill_switch", False),
            halted_reason=self.halted_reason(), tm_result=tm_result,
        )

    async def plan_trade(self, symbol: str, side: str, stop_loss: float, take_profit: float,
                         risk_pct: float | None = None, leverage: int | None = None,
                         horizon_hours: float = 24) -> dict:
        """Dry run: size it, check every limit, replay it. Nothing is sent."""
        symbol = symbol.upper()
        tm = await self.run_time_machine(symbol, side, stop_loss, take_profit, None, horizon_hours) \
            if self.s.tm_gate else None
        ctx = await self._risk_context(symbol, tm)
        v = self.risk.evaluate_entry(EntryRequest(symbol, side, stop_loss, take_profit, risk_pct, leverage), ctx)
        out = {"dry_run": True, **v.to_dict()}
        if tm:
            out["time_machine"] = {"verdict": tm.get("verdict"), "notes": tm.get("notes"),
                                   "mc": {k: v_ for k, v_ in (tm.get("mc") or {}).items() if k != "fan"},
                                   "history": tm.get("history"), "error": tm.get("error")}
        return out

    # ================================================================ trading
    async def open_position(self, symbol: str, side: str, stop_loss: float, take_profit: float,
                            risk_pct: float | None = None, leverage: int | None = None,
                            confidence: float | None = None, setup: str = "", thesis: str = "",
                            horizon_hours: float = 24) -> dict:
        symbol, side = symbol.upper(), side.lower()
        async with self._trade_lock:
            tm = await self.run_time_machine(symbol, side, stop_loss, take_profit, None, horizon_hours) \
                if self.s.tm_gate else None
            ctx = await self._risk_context(symbol, tm)
            v = self.risk.evaluate_entry(EntryRequest(symbol, side, stop_loss, take_profit, risk_pct, leverage), ctx)
            verdict = v.to_dict()
            if not v.passed:
                self._record("open_blocked", {"symbol": symbol, "side": side, "blocks": v.blocks})
                self.bus.publish("tool_result", {"cycle_id": self.cycle_id, "name": "risk_gate", "status": "BLOCK",
                                                 "text": f"{symbol} {side} BLOCKED: " + "; ".join(v.blocks)})
                return {"status": "BLOCKED", "blocks": v.blocks, "checks": verdict["checks"], "notes": v.notes,
                        "time_machine_verdict": tm.get("verdict") if tm else None}

            info = self.market.info(symbol)
            try:
                applied_lev = await self.broker.setup_symbol(symbol, v.leverage, self.s.margin_type)
            except ExchangeError as e:
                return {"status": "ERROR", "error": f"could not set leverage: {e}"}
            qty = v.qty
            if applied_lev < v.leverage:
                needed = v.notional / applied_lev
                if needed > ctx.account.available:
                    qty = info.round_qty(float(qty) * ctx.account.available * 0.95 / needed)
                    v.notes.append(f"leverage {applied_lev}x applied by exchange; size reduced to fit margin")
            open_side = "BUY" if side == "long" else "SELL"
            try:
                fill = await self.broker.market_order(symbol, open_side, qty, position=side)
            except ExchangeError as e:
                self._record("open_failed", {"symbol": symbol, "error": str(e)})
                return {"status": "ERROR", "error": f"order rejected: {e}"}
            if not fill.qty:
                return {"status": "ERROR", "error": "order not filled"}
            qty_filled = info.round_qty(fill.qty) or Decimal(str(fill.qty))
            entry = fill.avg_price or ctx.price
            protection = "exchange"
            prot_error = None
            try:
                await self.broker.place_protection(symbol, side, qty_filled, stop=v.stop, target=v.target)
            except ExchangeError as e:
                prot_error = str(e)
                try:  # one retry
                    await asyncio.sleep(0.5)
                    await self.broker.cancel_protection(symbol)
                    await self.broker.place_protection(symbol, side, qty_filled, stop=v.stop, target=v.target)
                    prot_error = None
                except ExchangeError as e2:
                    prot_error = str(e2)
                    protection = "local"
                    self.bus.publish("notice", {"level": "warn", "text":
                                     f"{symbol}: exchange stop/target failed ({e2}). Tushar.ai will enforce them itself."})
            stop_f = float(v.stop)
            risk_usd = abs(entry - stop_f) * float(qty_filled)
            trade_id = self.db.insert("trades", {
                "symbol": symbol, "side": side, "qty": float(qty_filled), "entry_price": entry,
                "stop_loss": stop_f, "take_profit": float(v.target), "initial_stop": stop_f,
                "leverage": applied_lev, "risk_usd": risk_usd,
                "risk_pct": risk_usd / ctx.account.equity * 100 if ctx.account.equity else None,
                "notional": entry * float(qty_filled), "opened_at": time.time(), "status": "open",
                "setup": setup or "discretionary", "confidence": confidence, "thesis": thesis,
                "cycle_id": self.cycle_id, "tm": tmach.brief(tm) if tm else None,
                "extra": {"protection": protection, "protection_error": prot_error, "actor": self.actor,
                          "notes": v.notes, "reward_risk": v.reward_risk},
            })
            self.invalidate()
            trade = self.db.one("SELECT * FROM trades WHERE id=?", (trade_id,))
            self._record("open", {"symbol": symbol, "side": side, "trade_id": trade_id})
            self.bus.publish("trade_open", {"cycle_id": self.cycle_id, "trade": trade})
            return {
                "status": "FILLED", "trade_id": trade_id, "symbol": symbol, "side": side, "qty": float(qty_filled),
                "entry": entry, "stop_loss": stop_f, "take_profit": float(v.target), "leverage": applied_lev,
                "risk_usd": round(risk_usd, 2), "reward_risk": v.reward_risk, "protection": protection,
                "notes": v.notes, "time_machine_verdict": tm.get("verdict") if tm else None,
            }

    async def close_position(self, symbol: str, fraction: float = 1.0, reason: str = "",
                             exit_reason: str | None = None) -> dict:
        symbol = symbol.upper()
        fraction = max(0.05, min(1.0, float(fraction or 1.0)))
        async with self._trade_lock:
            acct = await self.account(max_age=0.5)
            pos = acct.position(symbol)
            if not pos:
                return {"status": "ERROR", "error": f"no open {symbol} position"}
            info = self.market.info(symbol)
            full = fraction >= 0.999
            qty = Decimal(str(pos.qty)) if full or info is None else info.round_qty(pos.qty * fraction)
            if qty <= 0:
                return {"status": "ERROR", "error": "fraction too small for the lot size"}
            if qty >= Decimal(str(pos.qty)):
                full, qty = True, Decimal(str(pos.qty))
            close_side = "SELL" if pos.side == "long" else "BUY"
            if full:
                await self._safe_cancel(symbol)
            try:
                fill = await self.broker.market_order(symbol, close_side, qty, reduce_only=True, position=pos.side)
            except ExchangeError as e:
                return {"status": "ERROR", "error": f"close rejected: {e}"}
            self.invalidate()
            trade = self.db.open_trade_for(symbol)
            exit_price = fill.avg_price or pos.mark_price
            if full:
                if trade:
                    fills = await self.trade_fills(trade)
                    self.finalize_trade(trade, exit_price, exit_reason or ("claude" if self.actor == "claude" else self.actor),
                                        reason, fills=fills, qty_closed=float(qty))
                self._record("close", {"symbol": symbol, "fraction": 1.0, "reason": reason})
                return {"status": "CLOSED", "symbol": symbol, "qty": float(qty), "exit": exit_price,
                        "pnl_estimate": round((exit_price - pos.entry_price) * float(qty) * (1 if pos.side == "long" else -1), 2)}
            # Partial: shrink the protection to the remaining size.
            remaining = Decimal(str(pos.qty)) - qty
            if trade:
                extra = trade.get("extra") or {}
                pnl = (exit_price - trade["entry_price"]) * float(qty) * (1 if pos.side == "long" else -1)
                extra.setdefault("partials", []).append({"ts": time.time(), "qty": float(qty), "price": exit_price,
                                                         "pnl": pnl, "reason": reason})
                self.db.update("trades", trade["id"], {"qty": float(remaining), "extra": extra})
                await self._safe_cancel(symbol)
                try:
                    await self.broker.place_protection(symbol, pos.side, remaining, stop=Decimal(str(trade["stop_loss"])),
                                                       target=Decimal(str(trade["take_profit"])))
                except ExchangeError as e:
                    self.bus.publish("notice", {"level": "warn", "text": f"{symbol}: re-placing protection failed: {e}"})
            self._record("close_partial", {"symbol": symbol, "fraction": fraction, "reason": reason})
            self.bus.publish("trade_close", {"cycle_id": self.cycle_id, "symbol": symbol, "partial": True,
                                             "qty": float(qty), "price": exit_price, "reason": reason})
            return {"status": "PARTIAL", "symbol": symbol, "closed_qty": float(qty), "remaining_qty": float(remaining),
                    "exit": exit_price}

    async def trade_fills(self, trade: dict) -> list[dict] | None:
        """Exchange fills since the trade opened (entry, partials and exit), if available."""
        try:
            fills = await self.broker.fills_since(trade["symbol"], int((trade["opened_at"] - 5) * 1000))
            return fills or None
        except ExchangeError as e:
            log.warning("fills lookup %s: %s", trade["symbol"], e)
            return None

    async def _safe_cancel(self, symbol: str) -> None:
        try:
            await self.broker.cancel_protection(symbol)
        except ExchangeError as e:
            log.warning("cancel protection %s: %s", symbol, e)

    async def update_protection(self, symbol: str, stop_loss: float | None = None, take_profit: float | None = None,
                                reason: str = "") -> dict:
        symbol = symbol.upper()
        async with self._trade_lock:
            acct = await self.account(max_age=0.5)
            pos = acct.position(symbol)
            trade = self.db.open_trade_for(symbol)
            if not pos or not trade:
                return {"status": "ERROR", "error": f"no open {symbol} trade"}
            info = self.market.info(symbol)
            price = await self._fresh_price(symbol) or pos.mark_price
            new_stop = float(info.stop_price(stop_loss, pos.side)) if stop_loss else None
            new_tp = float(info.round_price(take_profit)) if take_profit else None
            problems = self.risk.evaluate_stop_update(pos.side, price, trade["stop_loss"], new_stop, new_tp)
            if problems:
                return {"status": "BLOCKED", "blocks": problems}
            stop_final = new_stop if new_stop is not None else trade["stop_loss"]
            tp_final = new_tp if new_tp is not None else trade["take_profit"]
            await self._safe_cancel(symbol)
            protection = "exchange"
            try:
                await self.broker.place_protection(symbol, pos.side, Decimal(str(pos.qty)),
                                                   stop=Decimal(str(stop_final)) if stop_final else None,
                                                   target=Decimal(str(tp_final)) if tp_final else None)
            except ExchangeError as e:
                protection = "local"
                self.bus.publish("notice", {"level": "warn", "text": f"{symbol}: protection update failed ({e}); enforcing locally"})
            extra = trade.get("extra") or {}
            extra["protection"] = protection
            extra.setdefault("stop_moves", []).append({"ts": time.time(), "stop": stop_final, "target": tp_final,
                                                       "reason": reason})
            self.db.update("trades", trade["id"], {"stop_loss": stop_final, "take_profit": tp_final, "extra": extra})
            self.invalidate()
            self._record("update_protection", {"symbol": symbol, "stop": stop_final, "target": tp_final, "reason": reason})
            self.bus.publish("protection", {"symbol": symbol, "stop_loss": stop_final, "take_profit": tp_final})
            return {"status": "UPDATED", "symbol": symbol, "stop_loss": stop_final, "take_profit": tp_final,
                    "protection": protection}

    def finalize_trade(self, trade: dict, exit_price: float, exit_reason: str, close_reason: str = "",
                       fills: list[dict] | None = None, qty_closed: float | None = None) -> dict:
        direction = 1 if trade["side"] == "long" else -1
        qty = qty_closed or trade["qty"]
        extra = trade.get("extra") or {}
        partial_pnl = sum(p["pnl"] for p in extra.get("partials", []))
        fee_rate = self.s.taker_fee_pct / 100
        close_side = "SELL" if direction == 1 else "BUY"
        closing = [f for f in (fills or []) if f["side"] == close_side and f["qty"] > 0]
        if closing:
            tq = sum(f["qty"] for f in closing)
            exit_price = sum(f["price"] * f["qty"] for f in closing) / tq
            fees = sum(abs(f.get("fee", 0)) for f in fills)
            pnl = sum(f.get("realized_pnl", 0) for f in fills) - fees
        else:
            gross = (exit_price - trade["entry_price"]) * qty * direction + partial_pnl
            fees = (trade["entry_price"] * (trade["qty"] + sum(p["qty"] for p in extra.get("partials", [])))
                    + exit_price * qty) * fee_rate
            pnl = gross - fees
        risk_usd = trade.get("risk_usd") or 0
        r_mult = round(pnl / risk_usd, 2) if risk_usd else None
        changes = {"status": "closed", "closed_at": time.time(), "exit_price": exit_price, "pnl": pnl, "fees": fees,
                   "r_multiple": r_mult, "exit_reason": exit_reason, "close_reason": close_reason}
        self.db.update("trades", trade["id"], changes)
        done = {**trade, **changes}
        self.bus.publish("trade_close", {"cycle_id": self.cycle_id, "trade": done})
        self.invalidate()
        return done

    async def flatten_all(self, reason: str) -> list[dict]:
        acct = await self.account(max_age=0)
        out = []
        for p in acct.positions:
            out.append(await self.close_position(p.symbol, 1.0, reason, exit_reason="kill_switch"))
        return out

    # ================================================================ memory & outlook
    async def set_alert(self, symbol: str, condition: str, price: float, note: str = "") -> dict:
        symbol, condition = symbol.upper(), condition.lower()
        if condition not in ("above", "below"):
            return {"status": "ERROR", "error": "condition must be 'above' or 'below'"}
        if self.market.info(symbol) is None:
            return {"status": "ERROR", "error": f"unknown symbol {symbol}"}
        active = self.db.query("SELECT id FROM alerts WHERE status='active'")
        if len(active) >= 25:
            return {"status": "ERROR", "error": "25 active alerts max: cancel some first"}
        aid = self.db.insert("alerts", {"created_at": time.time(), "symbol": symbol, "condition": condition,
                                        "price": float(price), "note": note[:300], "status": "active",
                                        "cycle_id": self.cycle_id})
        self.bus.publish("alerts", self.active_alerts())
        return {"status": "SET", "alert_id": aid, "symbol": symbol, "condition": condition, "price": price}

    async def cancel_alert(self, alert_id: int) -> dict:
        self.db.update("alerts", int(alert_id), {"status": "cancelled"})
        self.bus.publish("alerts", self.active_alerts())
        return {"status": "CANCELLED", "alert_id": alert_id}

    def active_alerts(self) -> list[dict]:
        return self.db.query("SELECT * FROM alerts WHERE status='active' ORDER BY created_at DESC")

    async def save_memory(self, text: str, kind: str = "lesson", symbol: str | None = None, importance: int = 3) -> dict:
        kind = kind if kind in ("lesson", "rule", "insight", "observation", "plan") else "lesson"
        importance = max(1, min(5, int(importance or 3)))
        mid = self.db.insert("memories", {"ts": time.time(), "kind": kind, "symbol": symbol.upper() if symbol else None,
                                          "text": text.strip()[:600], "importance": importance, "active": 1})
        n = self.db.one("SELECT COUNT(*) AS n FROM memories WHERE active=1")["n"]
        if n > 80:  # keep the memory curated: retire the least important, oldest notes
            self.db.execute("UPDATE memories SET active=0 WHERE id IN (SELECT id FROM memories WHERE active=1 "
                            "ORDER BY importance ASC, ts ASC LIMIT ?)", (n - 80,))
        self.bus.publish("memory", {"id": mid, "kind": kind, "text": text, "importance": importance, "symbol": symbol})
        return {"status": "SAVED", "memory_id": mid}

    async def forget_memory(self, memory_id: int) -> dict:
        self.db.update("memories", int(memory_id), {"active": 0})
        return {"status": "FORGOTTEN", "memory_id": memory_id}

    async def review_trade(self, trade_id: int, lesson: str, grade: str = "") -> dict:
        t = self.db.one("SELECT * FROM trades WHERE id=?", (int(trade_id),))
        if not t:
            return {"status": "ERROR", "error": "unknown trade"}
        review = f"[{grade}] {lesson}" if grade else lesson
        self.db.update("trades", t["id"], {"review": review[:800], "reviewed": 1})
        self.bus.publish("trade_review", {"trade_id": t["id"], "review": review})
        return {"status": "REVIEWED", "trade_id": t["id"]}

    async def publish_outlook(self, regime: str, bias: str, summary: str, watch: list[dict] | None = None,
                              next_check_minutes: int | None = None) -> dict:
        data = {"ts": time.time(), "cycle_id": self.cycle_id, "regime": regime, "bias": bias,
                "summary": summary[:1500], "watch": (watch or [])[:12]}
        self.outlook = data
        self.db.set("outlook", data)
        self.db.insert("outlooks", {"ts": data["ts"], "cycle_id": self.cycle_id, "data": data})
        if next_check_minutes:
            mins = max(self.s.min_cycle_gap_minutes, min(int(next_check_minutes), 240))
            self.requested_next_check = time.time() + mins * 60
            data["next_check_minutes"] = mins
        self.bus.publish("outlook", data)
        return {"status": "PUBLISHED", "next_check_minutes": data.get("next_check_minutes")}
