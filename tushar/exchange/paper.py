"""Paper broker: a simulated futures account that trades on real (or simulated) prices.

Used when TRADING_MODE=paper, or with MARKET_SOURCE=sim for offline runs and
tests. Isolated margin, taker fees, small slippage, reduce-only stop-loss and
take-profit triggers on the mark price, and liquidation.
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Callable, Optional

from .base import Account, ExchangeError, Fill, Order, Position

SLIPPAGE = 0.0002          # 0.02 % against you on every market fill
MAINT_MARGIN_RATE = 0.005  # rough maintenance margin for the liquidation estimate


class PaperBroker:
    hedge_mode = False
    algo_supported = True

    def __init__(self, price_fn: Callable[[str], Optional[float]], start_balance: float = 10_000.0,
                 fee_pct: float = 0.05, state: dict | None = None,
                 save_fn: Callable[[dict], None] | None = None):
        self.price_fn = price_fn
        self.fee_rate = fee_pct / 100
        self.save_fn = save_fn
        self.s = state or {
            "wallet": float(start_balance), "positions": {}, "orders": [], "income": [], "fills": {},
            "leverage": {}, "next_id": 1, "start_balance": float(start_balance),
        }

    # ------------------------------------------------------------- helpers
    def _save(self) -> None:
        if self.save_fn:
            self.save_fn(self.s)

    def _price(self, symbol: str) -> float:
        p = self.price_fn(symbol)
        if not p:
            raise ExchangeError(f"no price for {symbol}")
        return float(p)

    def _id(self) -> str:
        i = self.s["next_id"]
        self.s["next_id"] = i + 1
        return f"P{i}"

    def _income(self, symbol: str, typ: str, amount: float) -> None:
        self.s["income"].append({"symbol": symbol, "type": typ, "amount": amount, "t": int(time.time() * 1000)})
        self.s["income"] = self.s["income"][-2000:]

    def _fill_log(self, symbol: str, side: str, price: float, qty: float, pnl: float, fee: float, oid: str) -> None:
        self.s["fills"].setdefault(symbol, []).append({
            "side": side, "price": price, "qty": qty, "realized_pnl": pnl, "fee": fee,
            "t": int(time.time() * 1000), "order_id": oid})
        self.s["fills"][symbol] = self.s["fills"][symbol][-300:]

    # ------------------------------------------------------------- interface
    async def prepare(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def setup_symbol(self, symbol: str, leverage: int, margin_type: str = "ISOLATED") -> int:
        self.s["leverage"][symbol] = int(leverage)
        self._save()
        return int(leverage)

    def _position_obj(self, symbol: str, p: dict) -> Position:
        try:
            mark = self._price(symbol)
        except ExchangeError:
            mark = p["entry"]
        direction = 1 if p["side"] == "long" else -1
        upnl = (mark - p["entry"]) * p["qty"] * direction
        lev = p["leverage"]
        liq = p["entry"] * (1 - 1 / lev + MAINT_MARGIN_RATE) if direction == 1 else p["entry"] * (1 + 1 / lev - MAINT_MARGIN_RATE)
        return Position(symbol=symbol, side=p["side"], qty=p["qty"], entry_price=p["entry"], mark_price=mark,
                        unrealized_pnl=upnl, leverage=lev, liquidation_price=max(liq, 0.0), margin=p["margin"])

    async def positions(self) -> list[Position]:
        return [self._position_obj(s, p) for s, p in self.s["positions"].items()]

    async def account(self) -> Account:
        pos = await self.positions()
        upnl = sum(p.unrealized_pnl for p in pos)
        used = sum(p.margin for p in pos)
        wallet = self.s["wallet"]
        return Account(wallet_balance=wallet, equity=wallet + upnl, available=max(wallet - used, 0.0),
                       unrealized_pnl=upnl, positions=pos)

    async def open_orders(self, symbol: str | None = None) -> list[Order]:
        return [Order(id=o["id"], symbol=o["symbol"], side=o["side"], type=o["type"], qty=o["qty"],
                      trigger_price=o["trigger"], algo=True)
                for o in self.s["orders"] if symbol is None or o["symbol"] == symbol]

    async def market_order(self, symbol: str, side: str, qty: Decimal | float, reduce_only: bool = False,
                           position: str | None = None) -> Fill:
        fill = self._execute(symbol, side, float(qty), reduce_only)
        self._save()
        return fill

    def _execute(self, symbol: str, side: str, qty: float, reduce_only: bool,
                 price: float | None = None, slip: bool = True) -> Fill:
        if qty <= 0:
            raise ExchangeError("quantity must be positive", code=-4003)
        px = price if price is not None else self._price(symbol)
        if slip:
            px = px * (1 + SLIPPAGE) if side == "BUY" else px * (1 - SLIPPAGE)
        pos = self.s["positions"].get(symbol)
        oid = self._id()
        direction = 1 if side == "BUY" else -1
        realized = 0.0
        fee = 0.0
        filled = 0.0

        if pos and (pos["side"] == "long") != (direction == 1):
            # Reduce (or close, or flip) the existing position.
            close_qty = min(qty, pos["qty"])
            pdir = 1 if pos["side"] == "long" else -1
            realized = (px - pos["entry"]) * close_qty * pdir
            f = close_qty * px * self.fee_rate
            fee += f
            frac = close_qty / pos["qty"]
            pos["margin"] -= pos["margin"] * frac
            pos["qty"] = round(pos["qty"] - close_qty, 12)
            self.s["wallet"] += realized - f
            self._income(symbol, "REALIZED_PNL", realized)
            self._income(symbol, "COMMISSION", -f)
            filled += close_qty
            if pos["qty"] <= 1e-12:
                del self.s["positions"][symbol]
                self.s["orders"] = [o for o in self.s["orders"] if o["symbol"] != symbol]
            qty -= close_qty
            if qty > 1e-12 and not reduce_only:
                self._open(symbol, direction, qty, px)
                fee_open = qty * px * self.fee_rate
                fee += fee_open
                filled += qty
        else:
            if reduce_only:
                raise ExchangeError("ReduceOnly Order is rejected: no position to reduce", code=-2022)
            fee_open = qty * px * self.fee_rate
            needed = qty * px / self.s["leverage"].get(symbol, 5) + fee_open
            acct_avail = self.s["wallet"] - sum(p["margin"] for p in self.s["positions"].values())
            if needed > acct_avail + 1e-9:
                raise ExchangeError(f"Margin is insufficient (need {needed:.2f}, have {acct_avail:.2f})", code=-2019)
            self._open(symbol, direction, qty, px)
            fee += fee_open
            filled += qty
        self._fill_log(symbol, side, px, filled, realized, fee, oid)
        return Fill(symbol=symbol, side=side, qty=filled, avg_price=px, fee=fee, order_id=oid, realized_pnl=realized)

    def _open(self, symbol: str, direction: int, qty: float, px: float) -> None:
        lev = self.s["leverage"].get(symbol, 5)
        f = qty * px * self.fee_rate
        self.s["wallet"] -= f
        self._income(symbol, "COMMISSION", -f)
        pos = self.s["positions"].get(symbol)
        if pos:
            total = pos["qty"] + qty
            pos["entry"] = (pos["entry"] * pos["qty"] + px * qty) / total
            pos["qty"] = total
            pos["margin"] += qty * px / lev
        else:
            self.s["positions"][symbol] = {"side": "long" if direction == 1 else "short", "qty": qty,
                                           "entry": px, "leverage": lev, "margin": qty * px / lev,
                                           "opened": time.time()}

    async def place_protection(self, symbol: str, position_side: str, qty: Decimal | float,
                               stop: Decimal | None = None, target: Decimal | None = None) -> list[Order]:
        close_side = "SELL" if position_side == "long" else "BUY"
        out = []
        for typ, price in (("STOP_MARKET", stop), ("TAKE_PROFIT_MARKET", target)):
            if price is None:
                continue
            o = {"id": self._id(), "symbol": symbol, "side": close_side, "type": typ, "qty": float(qty),
                 "trigger": float(price)}
            self.s["orders"].append(o)
            out.append(Order(id=o["id"], symbol=symbol, side=close_side, type=typ, qty=float(qty),
                             trigger_price=float(price), algo=True))
        self._save()
        return out

    async def cancel_protection(self, symbol: str) -> int:
        before = len(self.s["orders"])
        self.s["orders"] = [o for o in self.s["orders"] if o["symbol"] != symbol]
        self._save()
        return before - len(self.s["orders"])

    async def income_since(self, start_ms: int) -> list[dict]:
        return [i for i in self.s["income"] if i["t"] >= start_ms]

    async def fills_since(self, symbol: str, start_ms: int) -> list[dict]:
        return [f for f in self.s["fills"].get(symbol, []) if f["t"] >= start_ms]

    # ------------------------------------------------------------- simulation
    def on_tick(self) -> list[dict]:
        """Fire stop/target orders and liquidations against the current mark price."""
        events = []
        for symbol in list(self.s["positions"]):
            pos = self.s["positions"].get(symbol)
            if not pos:
                continue
            try:
                mark = self._price(symbol)
            except ExchangeError:
                continue
            is_long = pos["side"] == "long"
            # Liquidation check first.
            p = self._position_obj(symbol, pos)
            if (is_long and mark <= p.liquidation_price) or (not is_long and mark >= p.liquidation_price):
                lost = pos["margin"]
                self.s["wallet"] -= lost
                self._income(symbol, "INSURANCE_CLEAR", -lost)
                self._fill_log(symbol, "SELL" if is_long else "BUY", mark, pos["qty"], -lost, 0.0, self._id())
                del self.s["positions"][symbol]
                self.s["orders"] = [o for o in self.s["orders"] if o["symbol"] != symbol]
                events.append({"symbol": symbol, "kind": "LIQUIDATION", "price": mark})
                continue
            for o in sorted([o for o in self.s["orders"] if o["symbol"] == symbol],
                            key=lambda o: 0 if o["type"].startswith("STOP") else 1):
                hit = False
                if o["type"].startswith("STOP"):
                    hit = mark <= o["trigger"] if is_long else mark >= o["trigger"]
                elif o["type"].startswith("TAKE_PROFIT"):
                    hit = mark >= o["trigger"] if is_long else mark <= o["trigger"]
                if hit and symbol in self.s["positions"]:
                    qty = min(o["qty"], self.s["positions"][symbol]["qty"])
                    fill = self._execute(symbol, o["side"], qty, True, price=mark)
                    self.s["orders"] = [x for x in self.s["orders"] if x["id"] != o["id"]]
                    events.append({"symbol": symbol, "kind": "SL" if o["type"].startswith("STOP") else "TP",
                                   "price": fill.avg_price, "pnl": fill.realized_pnl})
                    break
        if events:
            self._save()
        return events
