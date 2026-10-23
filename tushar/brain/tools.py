"""Tool catalogue: the desk actions exposed to Claude, with JSON schemas."""
from __future__ import annotations

from typing import Any

SIDE = {"type": "string", "enum": ["long", "short"]}
SYMBOL = {"type": "string", "description": "USDT perpetual symbol, e.g. BTCUSDT"}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


# name -> (description, schema, desk method, kind) ; kind: read | trade | memory
TOOLS: dict[str, tuple[str, dict, str, str]] = {
    "get_portfolio": (
        "Account equity, balance, today's realized PnL, daily loss budget left, and every open position with "
        "its stop, target, R so far, thesis and exchange protection orders.",
        _obj({}), "get_portfolio", "read"),
    "get_market_overview": (
        "One row per symbol: price, 1h/4h/24h change, 1h and 4h trend, RSI, ATR%, ADX, funding, volume. "
        "Defaults to the watchlist.",
        _obj({"symbols": {"type": "array", "items": {"type": "string"}, "description": "optional symbols"}}),
        "get_market_overview", "read"),
    "analyze_symbol": (
        "Deep multi-timeframe read of one symbol: 15m/1h/4h/1d indicators (EMAs, RSI, MACD, ATR, ADX, Bollinger, "
        "volume ratio, trend, support/resistance), recent candles, funding, open interest change, long/short "
        "accounts, order book imbalance, scanner hits and news for that coin.",
        _obj({"symbol": SYMBOL}, ["symbol"]), "analyze_symbol", "read"),
    "scan_market": (
        "Scan the most traded USDT perpetuals for setups on closed 1h candles: breakouts/breakdowns, flags, "
        "trend pullbacks, EMA50 reclaims/losses, RSI bounces/rejections, squeezes, volume spikes, funding extremes.",
        _obj({}), "scan_market", "read"),
    "get_news": (
        "Latest crypto + macro headlines (tagged: macro, regulation, etf, hack, listing, liquidations, "
        "institutional...) and the Fear & Greed index. Optionally filter by coin.",
        _obj({"hours": {"type": "number", "description": "lookback hours (default 12)"},
              "symbol": {"type": "string", "description": "optional coin filter, e.g. SOLUSDT"}}),
        "get_news", "read"),
    "run_time_machine": (
        "Replay a trade 10,000 times from the coin's own recent 5m candles (same time of day) and re-run the same "
        "stop/target distances on the real history. Returns odds of target first / stop first / neither, average R "
        "(before and after fees), where price ends up, and a verdict that weighs the target's R.",
        _obj({"symbol": SYMBOL, "side": SIDE, "stop_loss": {"type": "number"}, "take_profit": {"type": "number"},
              "entry": {"type": "number", "description": "optional; defaults to the live price"},
              "horizon_hours": {"type": "number", "description": "replay length, default 24"}},
             ["symbol", "side", "stop_loss", "take_profit"]),
        "run_time_machine", "read"),
    "plan_trade": (
        "DRY RUN of an entry at the live price: sizes it from the loss at the stop, checks every risk limit "
        "(PASS/BLOCK with reasons) and runs the Time Machine. Nothing is sent to the exchange. Always do this "
        "before open_position.",
        _obj({"symbol": SYMBOL, "side": SIDE, "stop_loss": {"type": "number"}, "take_profit": {"type": "number"},
              "risk_pct": {"type": "number", "description": "% of equity lost if the stop hits (capped by the limit)"},
              "leverage": {"type": "integer"},
              "horizon_hours": {"type": "number", "description": "time machine horizon, default 24"}},
             ["symbol", "side", "stop_loss", "take_profit"]),
        "plan_trade", "read"),
    "open_position": (
        "Open a futures position at market with a reduce-only stop loss and take profit on the exchange. "
        "Passes the risk gate first; a BLOCK is final.",
        _obj({"symbol": SYMBOL, "side": SIDE, "stop_loss": {"type": "number"}, "take_profit": {"type": "number"},
              "risk_pct": {"type": "number", "description": "% of equity lost if the stop hits"},
              "leverage": {"type": "integer"},
              "confidence": {"type": "number", "description": "0-1, your conviction"},
              "setup": {"type": "string", "description": "short label, e.g. trend_pullback_long, breakout_20"},
              "thesis": {"type": "string", "description": "why, what invalidates it, what you expect"},
              "horizon_hours": {"type": "number", "description": "expected holding time, default 24"}},
             ["symbol", "side", "stop_loss", "take_profit", "thesis"]),
        "open_position", "trade"),
    "close_position": (
        "Close all or part of a position at market (fraction 0.05-1.0). The remaining part keeps its stop/target.",
        _obj({"symbol": SYMBOL, "fraction": {"type": "number", "description": "default 1.0 (all)"},
              "reason": {"type": "string"}}, ["symbol", "reason"]),
        "close_position", "trade"),
    "update_protection": (
        "Move the stop loss and/or take profit of an open position (stops can only be tightened).",
        _obj({"symbol": SYMBOL, "stop_loss": {"type": "number"}, "take_profit": {"type": "number"},
              "reason": {"type": "string"}}, ["symbol", "reason"]),
        "update_protection", "trade"),
    "set_alert": (
        "Wake Tushar.ai for an extra cycle when a symbol trades above/below a price.",
        _obj({"symbol": SYMBOL, "condition": {"type": "string", "enum": ["above", "below"]},
              "price": {"type": "number"}, "note": {"type": "string", "description": "what to do when it hits"}},
             ["symbol", "condition", "price"]),
        "set_alert", "memory"),
    "cancel_alert": ("Cancel a price alert by id.", _obj({"alert_id": {"type": "integer"}}, ["alert_id"]),
                     "cancel_alert", "memory"),
    "get_trade_journal": (
        "Your recent trades (thesis, result, R, reviews) and all-time stats by setup.",
        _obj({"limit": {"type": "integer", "description": "default 20"}}), "get_trade_journal", "read"),
    "review_trade": (
        "Write the post-trade review for a closed trade: what happened vs the thesis and the lesson.",
        _obj({"trade_id": {"type": "integer"}, "lesson": {"type": "string"},
              "grade": {"type": "string", "description": "A-F for decision quality (not the outcome)"}},
             ["trade_id", "lesson"]),
        "review_trade", "memory"),
    "save_memory": (
        "Save a durable lesson, rule, insight or plan to your long-term memory. It appears in every future briefing.",
        _obj({"text": {"type": "string"},
              "kind": {"type": "string", "enum": ["lesson", "rule", "insight", "observation", "plan"]},
              "symbol": {"type": "string"}, "importance": {"type": "integer", "description": "1-5"}}, ["text"]),
        "save_memory", "memory"),
    "forget_memory": ("Retire an outdated memory by id.", _obj({"memory_id": {"type": "integer"}}, ["memory_id"]),
                      "forget_memory", "memory"),
    "publish_outlook": (
        "REQUIRED at the end of every cycle. Publishes your market view to the dashboard and sets when you want "
        "the next cycle.",
        _obj({"regime": {"type": "string", "description": "e.g. trending up, choppy range, risk-off selloff"},
              "bias": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
              "summary": {"type": "string", "description": "what you see, what you did and why (3-6 sentences)"},
              "watch": {"type": "array", "items": {"type": "object", "properties": {
                  "symbol": {"type": "string"}, "bias": {"type": "string"}, "note": {"type": "string"}}},
                  "description": "coins you are watching, with levels"},
              "next_check_minutes": {"type": "integer", "description": "when to run the next cycle"}},
             ["regime", "bias", "summary"]),
        "publish_outlook", "memory"),
}

READ_ONLY = [n for n, t in TOOLS.items() if t[3] == "read"]


def tool_names(include_trading: bool = True) -> list[str]:
    return [n for n, t in TOOLS.items() if include_trading or t[3] == "read"]


async def call_tool(desk, name: str, args: dict[str, Any]) -> Any:
    if name not in TOOLS:
        raise ValueError(f"unknown tool {name}")
    method = getattr(desk, TOOLS[name][2])
    schema_props = TOOLS[name][1]["properties"]
    clean = {k: v for k, v in (args or {}).items() if k in schema_props and v is not None}
    return await method(**clean)
