"""Risk manager: sizes every entry to its max loss and gates it PASS / BLOCK.

Inspired by day-trader-skill's trader-budget + trader-risk: the size comes from
the loss at the stop, every limit is checked, and a BLOCK is final. Getting out
is always allowed: exits are never blocked by the entry rules.
"""
from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from .exchange.base import Account, SymbolInfo


@dataclass
class EntryRequest:
    symbol: str
    side: str                 # long | short
    stop_loss: float
    take_profit: float
    risk_pct: float | None = None
    leverage: int | None = None


@dataclass
class RiskContext:
    account: Account
    price: float                       # live mark price (the market entry)
    info: SymbolInfo | None
    open_trades: list[dict]            # DB rows of open trades
    trades_today: int
    realized_today: float              # realized PnL since 00:00 UTC (negative = loss)
    start_of_day_equity: float
    last_loss_at: float | None = None  # last losing exit on this symbol
    paused: bool = False
    kill_switch: bool = False
    halted_reason: str | None = None
    tm_result: dict | None = None


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


@dataclass
class Verdict:
    passed: bool
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    qty: Decimal = Decimal(0)
    entry: float = 0.0
    stop: Decimal | None = None
    target: Decimal | None = None
    leverage: int = 1
    notional: float = 0.0
    margin: float = 0.0
    risk_usd: float = 0.0
    risk_pct: float = 0.0
    reward_risk: float = 0.0

    @property
    def blocks(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if not c.ok]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["qty"] = float(self.qty)
        d["stop"] = float(self.stop) if self.stop is not None else None
        d["target"] = float(self.target) if self.target is not None else None
        d["status"] = "PASS" if self.passed else "BLOCK"
        d["blocks"] = self.blocks
        return d


def open_risk_usd(trades: list[dict], price_of=None) -> float:
    """Money lost if every open trade hit its current stop (0 for stops locked in profit)."""
    total = 0.0
    for t in trades:
        stop, entry, qty = t.get("stop_loss"), t.get("entry_price"), t.get("qty") or 0
        if not stop or not entry:
            total += t.get("risk_usd") or 0
            continue
        loss = (entry - stop) * qty if t["side"] == "long" else (stop - entry) * qty
        total += max(loss, 0.0)
    return total


class RiskManager:
    def __init__(self, settings):
        self.s = settings

    # ------------------------------------------------------------------
    def evaluate_entry(self, req: EntryRequest, ctx: RiskContext) -> Verdict:
        s = self.s
        v = Verdict(passed=False, entry=ctx.price)
        checks = v.checks

        def check(name: str, ok: bool, detail: str) -> bool:
            checks.append(Check(name, bool(ok), detail))
            return bool(ok)

        # --- state ---
        check("kill_switch", not ctx.kill_switch, "kill switch is ON" if ctx.kill_switch else "off")
        check("paused", not ctx.paused, "trading is paused" if ctx.paused else "running")
        check("daily_halt", not ctx.halted_reason, ctx.halted_reason or "not halted")

        # --- format / symbol ---
        info = ctx.info
        side_ok = req.side in ("long", "short")
        check("format", side_ok and req.stop_loss > 0 and req.take_profit > 0,
              "side must be long/short with positive stop_loss and take_profit" if not side_ok
              or req.stop_loss <= 0 or req.take_profit <= 0 else "ok")
        check("symbol", info is not None and info.tradable,
              f"{req.symbol} is not a tradable USDT perpetual" if not (info and info.tradable) else "ok")
        check("blocked", req.symbol not in s.blocked_symbols,
              f"{req.symbol} is on the blocked list" if req.symbol in s.blocked_symbols else "ok")
        price = ctx.price
        if not price or price <= 0 or info is None or not side_ok:
            check("price", False, "no live price")
            return v

        # --- stop / target geometry against the LIVE price ---
        long_ = req.side == "long"
        stop = info.stop_price(req.stop_loss, req.side)
        target = info.round_price(req.take_profit)
        v.stop, v.target = stop, target
        stop_f, target_f = float(stop), float(target)
        geo_ok = (stop_f < price < target_f) if long_ else (target_f < price < stop_f)
        if not check("stop_side", geo_ok,
                     (f"for a {req.side} at {price:g}, need " +
                      ("stop < price < target" if long_ else "target < price < stop") +
                      f" (stop {stop_f:g}, target {target_f:g})") if not geo_ok else "ok"):
            return v
        stop_dist = abs(price - stop_f)
        stop_pct = stop_dist / price * 100
        check("stop_distance", s.min_stop_pct <= stop_pct <= s.max_stop_pct,
              f"stop is {stop_pct:.2f}% away; allowed {s.min_stop_pct}%..{s.max_stop_pct}%")
        rr = abs(target_f - price) / stop_dist
        v.reward_risk = round(rr, 2)
        check("reward_risk", rr >= s.min_reward_risk - 1e-9, f"reward:risk {rr:.2f} (min {s.min_reward_risk})")

        # --- portfolio limits ---
        open_syms = {t["symbol"] for t in ctx.open_trades} | {p.symbol for p in ctx.account.positions}
        check("duplicate", req.symbol not in open_syms,
              f"already have a {req.symbol} position (close or manage it instead)" if req.symbol in open_syms else "ok")
        n_open = len(open_syms)
        check("max_positions", n_open < s.max_open_positions, f"{n_open} open, max {s.max_open_positions}")
        check("trades_today", ctx.trades_today < s.max_trades_per_day,
              f"{ctx.trades_today} entries today, max {s.max_trades_per_day}")
        if ctx.last_loss_at and s.cooldown_minutes_after_loss:
            mins = (time.time() - ctx.last_loss_at) / 60
            check("cooldown", mins >= s.cooldown_minutes_after_loss,
                  f"lost on {req.symbol} {mins:.0f} min ago; cooldown {s.cooldown_minutes_after_loss} min")

        # --- sizing (trader-budget style: size from the loss at the stop) ---
        equity = ctx.account.equity
        risk_pct = min(req.risk_pct or s.risk_per_trade_pct, s.risk_per_trade_pct)
        risk_pct = max(risk_pct, 0.05)
        risk_budget = equity * risk_pct / 100
        lev = int(max(1, min(req.leverage or s.default_leverage, s.max_leverage, info.max_leverage)))
        # Keep the liquidation price well beyond the stop (isolated margin ~ 100/lev % away).
        max_lev_for_stop = max(1, math.floor(70 / stop_pct)) if stop_pct > 0 else lev
        if lev > max_lev_for_stop:
            v.notes.append(f"leverage lowered {lev}x -> {max_lev_for_stop}x so liquidation sits beyond the stop")
            lev = max_lev_for_stop
        qty_raw = risk_budget / stop_dist
        cap_margin = equity * s.max_position_margin_pct / 100 * lev
        cap_avail = max(ctx.account.available, 0) * 0.95 * lev
        notional = min(qty_raw * price, cap_margin, cap_avail)
        if notional < qty_raw * price * 0.999:
            which = "max position margin" if cap_margin <= cap_avail else "available margin"
            v.notes.append(f"size capped by {which}")
        qty = info.round_qty(notional / price)
        if qty > info.max_qty:
            qty = info.round_qty(float(info.max_qty))
        notional = float(qty) * price
        v.qty, v.leverage, v.notional = qty, lev, notional
        v.margin = notional / lev
        v.risk_usd = float(qty) * stop_dist
        v.risk_pct = v.risk_usd / equity * 100 if equity else 0.0
        check("size", qty >= info.min_qty and qty > 0 and notional >= float(info.min_notional),
              f"qty {qty} (${notional:,.2f}); exchange minimum {info.min_qty} and ${info.min_notional}")
        check("margin", v.margin <= max(ctx.account.available, 0),
              f"needs ${v.margin:,.2f} margin, ${ctx.account.available:,.2f} available")

        # --- daily loss budget (counts the full loss at every open stop) ---
        sod = ctx.start_of_day_equity or equity
        limit = sod * s.max_daily_loss_pct / 100
        at_risk = -min(ctx.realized_today, 0) + open_risk_usd(ctx.open_trades) + v.risk_usd
        check("daily_loss", at_risk <= limit + 1e-9,
              f"today's losses + open risk + this trade = ${at_risk:,.2f}; daily limit ${limit:,.2f}")

        # --- time machine gate ---
        if s.tm_gate and ctx.tm_result is not None:
            tm = ctx.tm_result
            if tm.get("ok"):
                avg = tm["mc"]["avg_r_after_fees"]
                check("time_machine", avg >= s.tm_min_avg_r,
                      f"{tm['verdict']} avg R after fees {avg:+.2f} (min {s.tm_min_avg_r:+.2f})")
            else:
                v.notes.append(f"time machine skipped: {tm.get('error')}")

        v.passed = all(c.ok for c in checks)
        return v

    # ------------------------------------------------------------------
    def evaluate_stop_update(self, side: str, price: float, current_stop: float | None,
                             new_stop: float | None, new_target: float | None) -> list[str]:
        """Problems with moving a stop/target (empty list = fine)."""
        problems = []
        long_ = side == "long"
        if new_stop is not None:
            if (long_ and new_stop >= price) or (not long_ and new_stop <= price):
                problems.append(f"new stop {new_stop:g} is on the wrong side of the price {price:g}")
            if current_stop and not self.s.allow_widen_stop:
                widened = new_stop < current_stop if long_ else new_stop > current_stop
                if widened:
                    problems.append(f"stops can only be tightened (current {current_stop:g}, new {new_stop:g})")
        if new_target is not None:
            if (long_ and new_target <= price) or (not long_ and new_target >= price):
                problems.append(f"new target {new_target:g} is on the wrong side of the price {price:g}")
        return problems
