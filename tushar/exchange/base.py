"""Shared exchange types: symbol filters, positions, orders, fills."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Optional


class ExchangeError(Exception):
    """Any problem talking to the exchange."""

    def __init__(self, message: str, code: int | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


def dec(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def fmt(d: Decimal) -> str:
    """Decimal to a plain string Binance accepts (no exponent, no trailing zeros)."""
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


@dataclass
class SymbolInfo:
    symbol: str
    base: str
    quote: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal
    status: str = "TRADING"
    contract_type: str = "PERPETUAL"
    max_leverage: int = 125

    def round_qty(self, qty: float) -> Decimal:
        """Floor to the lot step (never round a size up)."""
        q = (dec(qty) / self.step_size).to_integral_value(ROUND_DOWN) * self.step_size
        return max(q, Decimal(0))

    def round_price(self, price: float, mode: str = "nearest") -> Decimal:
        rounding = {"nearest": ROUND_HALF_UP, "down": ROUND_FLOOR, "up": ROUND_CEILING}[mode]
        return (dec(price) / self.tick_size).to_integral_value(rounding) * self.tick_size

    def stop_price(self, price: float, side: str) -> Decimal:
        """Round a stop AWAY from the entry: down for a long, up for a short."""
        return self.round_price(price, "down" if side == "long" else "up")

    @property
    def tradable(self) -> bool:
        return self.status == "TRADING" and self.quote == "USDT" and self.contract_type == "PERPETUAL"


@dataclass
class Position:
    symbol: str
    side: str            # long | short
    qty: float           # always positive
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    leverage: int = 1
    liquidation_price: float = 0.0
    margin: float = 0.0
    position_side: str = "BOTH"   # BOTH in one-way mode, LONG/SHORT in hedge mode

    @property
    def notional(self) -> float:
        return abs(self.qty * self.mark_price)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["notional"] = self.notional
        d["pnl_pct"] = (self.unrealized_pnl / self.margin * 100) if self.margin else None
        return d


@dataclass
class Account:
    wallet_balance: float
    equity: float
    available: float
    unrealized_pnl: float
    positions: list[Position] = field(default_factory=list)

    def position(self, symbol: str) -> Optional[Position]:
        return next((p for p in self.positions if p.symbol == symbol), None)

    def to_dict(self) -> dict:
        return {
            "wallet_balance": self.wallet_balance,
            "equity": self.equity,
            "available": self.available,
            "unrealized_pnl": self.unrealized_pnl,
            "positions": [p.to_dict() for p in self.positions],
        }


@dataclass
class Order:
    id: str
    symbol: str
    side: str             # BUY | SELL
    type: str             # STOP_MARKET | TAKE_PROFIT_MARKET | LIMIT ...
    qty: float
    trigger_price: float = 0.0
    price: float = 0.0
    reduce_only: bool = True
    algo: bool = False
    status: str = "NEW"

    @property
    def kind(self) -> str:
        t = self.type.upper()
        if t.startswith("STOP") or t.startswith("TRAILING"):
            return "SL"
        if t.startswith("TAKE_PROFIT"):
            return "TP"
        return "ORDER"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind"] = self.kind
        return d


@dataclass
class Fill:
    symbol: str
    side: str        # BUY | SELL
    qty: float
    avg_price: float
    fee: float = 0.0
    order_id: str = ""
    realized_pnl: float = 0.0
