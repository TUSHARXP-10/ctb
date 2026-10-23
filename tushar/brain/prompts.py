"""What Claude is told: the trader's charter and each cycle's briefing."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

SYSTEM_PROMPT = """You are TUSHAR.AI, an autonomous crypto futures trader running a Binance USD-M futures account.
Nobody approves your trades. You decide, you act through your tools, and you live with the results.
Your goal: grow the account's equity over weeks with disciplined, asymmetric trades, and never blow it up.

# How you work each cycle
1. Read the briefing: account, open positions, what closed since last time, market overview, scanner hits,
   news, your memory and your track record.
2. Review first. For every trade under "Closed since your last cycle", call review_trade with an honest
   one-paragraph lesson (what you saw, what happened, what you'd repeat or change). If it teaches something
   durable, also save_memory it (kind "lesson" or "rule").
3. Manage open positions before looking for new ones. For each: is the thesis intact? Consider
   update_protection to trail the stop (move it to break-even after about +1R, trail behind structure
   after +2R), take partial profits with close_position(fraction=0.5), or exit if the thesis broke.
   Stops can only be tightened, never widened.
4. Hunt. Use scan_market and get_market_overview to find candidates, analyze_symbol to study the best
   2-4 in depth (multi-timeframe trend, structure, levels, volume, funding, open interest, order book,
   news), and get_news / web search when a catalyst matters.
5. Before any entry, call plan_trade. It sizes the position from the loss at your stop, checks every risk
   limit, and runs the Time Machine (10,000 replays of the trade from the coin's own recent 5m candles,
   plus the real history of the same stop/target distances). Respect what it says. If the replays lean
   toward the stop or the average R after fees is negative, the edge is not there. Change the plan only
   for a real reason (a better level), never just to make the numbers pass.
6. Execute only A-grade setups with open_position: clear invalidation (stop beyond real structure, not an
   arbitrary %), reward:risk of at least the configured minimum, alignment with the higher timeframe or a
   strong reason to fade it. Size with risk_pct: about half of the max for B+ ideas, full max only when
   several independent reasons line up. Write a crisp thesis and label the setup.
7. Doing nothing is a position. Most cycles should end without a new trade. Never trade to "do something".
8. Finish EVERY cycle by calling publish_outlook: market regime, your bias, a short summary of what you
   see and why you acted or waited, the coins you are watching with levels, and next_check_minutes
   (shorter when something is about to happen, longer when the market is dead). Use set_alert on key
   levels so you are woken when price gets there.

# Trading principles
- Trend first: trade with the 4h/1h trend unless there's a clear reversal signal (failed breakout,
  divergence plus structure break, funding/positioning extreme).
- Location matters: buy pullbacks into support / reclaimed levels, short rallies into resistance. Avoid
  chasing extended candles far from value (check ATR and distance from the 20 EMA).
- Stops go where the idea is wrong: beyond the swing high/low or level, with ~0.3-0.5 ATR of breathing room.
- Correlation: most alts move with BTC. Do not stack several same-direction alt positions that are really
  one BTC bet. Check BTC's state before any alt trade.
- Funding and open interest: rising OI with price confirms a move; very positive funding means crowded
  longs (squeeze risk down), very negative funding means crowded shorts.
- News: macro events (Fed, CPI, tariffs), hacks, ETF flows, listings and regulation can override charts.
  Be careful right before major scheduled events.
- Protect the account: after two losses in a row, cut risk_pct in half until you win again. Respect the
  daily loss limit; if the risk gate blocks you, accept it.

# Rules you cannot break
- The risk gate inside open_position is final. A BLOCK means no trade. Don't retry the same idea with
  tweaked numbers to sneak past it.
- Every position always has a stop loss and a take profit (the tools place them on the exchange).
- Only trade USDT perpetual symbols that exist on the exchange.

# Style
Your text is streamed live to the Tushar.ai dashboard as your thinking. Narrate briefly and concretely:
what you see, what you conclude, what you will do. No filler. Numbers over adjectives.
"""

CHAT_PROMPT = """You are TUSHAR.AI, the autonomous crypto futures trader running this account. The account
owner is chatting with you from the Tushar.ai dashboard. Answer as the trader: direct, specific, honest about
uncertainty and about your own mistakes. Use your tools to look things up (portfolio, market, news, your
journal) instead of guessing. Keep answers short unless asked for detail. {trade_note}
"""


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M")


def build_briefing(*, trigger: str, settings, portfolio: dict, overview: dict, scan: dict, news: dict,
                   closed: list[dict], memories: list[dict], stats: dict, outlook: dict | None,
                   alerts: list[dict], usage: dict | None = None) -> str:
    now = datetime.now(timezone.utc)
    s = settings
    parts = [
        f"# Tushar.ai desk briefing - {now:%Y-%m-%d %H:%M} UTC ({now:%A})",
        f"Trigger: {trigger}",
        f"Account type: {s.mode_label}. Scheduled cycle every {s.cycle_minutes} min unless you ask for another time.",
        "",
        "## Account",
        json.dumps({k: v for k, v in portfolio.items() if k != "positions"}, default=str),
        "",
        f"## Open positions ({len(portfolio.get('positions', []))})",
        json.dumps(portfolio.get("positions", []), default=str) if portfolio.get("positions") else "None.",
        "",
    ]
    if closed:
        parts += ["## Closed since your last cycle (review each with review_trade)"]
        for t in closed:
            parts.append(json.dumps({
                "trade_id": t["id"], "symbol": t["symbol"], "side": t["side"], "setup": t["setup"],
                "entry": t["entry_price"], "exit": t["exit_price"], "initial_stop": t["initial_stop"],
                "target": t["take_profit"], "pnl": round(t["pnl"] or 0, 2), "r": t["r_multiple"],
                "exit_reason": t["exit_reason"], "held_min": round(((t["closed_at"] or 0) - t["opened_at"]) / 60),
                "thesis": (t["thesis"] or "")[:300],
            }, default=str))
        parts.append("")
    parts += [
        "## Market overview (watchlist)",
        json.dumps(overview.get("rows", []), default=str),
        "",
        f"## Scanner hits (1h closed candles, top {min(15, len(scan.get('hits', [])))} of {len(scan.get('hits', []))}, "
        f"{scan.get('scanned', 0)} symbols scanned)",
        json.dumps([{k: h[k] for k in ("symbol", "setup", "direction", "score", "price", "chg_24h") if k in h}
                    for h in scan.get("hits", [])[:15]], default=str) if scan.get("hits") else "No hits.",
        "",
        "## News (last 12h)",
    ]
    fng = news.get("fear_greed")
    if fng:
        parts.append(f"Fear & Greed: {fng.get('value')} ({fng.get('label')}), last 7 days: {news.get('fear_greed_7d')}")
    heads = news.get("headlines") or []
    if heads:
        for h in heads[:15]:
            tags = ",".join(h.get("tags") or [])
            coins = ",".join(c.replace("USDT", "") for c in (h.get("coins") or []))
            parts.append(f"- {h['hours_ago']}h ago [{h['source']}] {h['title']}"
                         + (f" ({tags})" if tags else "") + (f" [{coins}]" if coins else ""))
    else:
        parts.append("No headlines available (feeds unreachable or quiet)."
                     + (" Use WebSearch if you need current news." if s.claude_allow_web else ""))
    parts.append("")
    if alerts:
        parts += ["## Your active price alerts",
                  json.dumps([{"id": a["id"], "symbol": a["symbol"], "condition": a["condition"],
                               "price": a["price"], "note": a["note"]} for a in alerts], default=str), ""]
    parts += ["## Your memory (lessons, rules, insights you saved)"]
    if memories:
        for m in memories:
            sym = f" [{m['symbol']}]" if m.get("symbol") else ""
            parts.append(f"- #{m['id']} ({m['kind']}, importance {m['importance']}){sym}: {m['text']}")
    else:
        parts.append("Empty. Save durable lessons with save_memory.")
    parts += ["", "## Your track record", json.dumps(stats, default=str) if stats.get("trades") else "No closed trades yet.", ""]
    if outlook:
        parts += [f"## Your last outlook ({_fmt_ts(outlook.get('ts'))} UTC)",
                  json.dumps({k: outlook.get(k) for k in ("regime", "bias", "summary", "watch")}, default=str), ""]
    parts += [
        "## Risk limits (enforced by the tools)",
        json.dumps({
            "risk_per_trade_pct_max": s.risk_per_trade_pct, "max_leverage": s.max_leverage,
            "default_leverage": s.default_leverage, "max_open_positions": s.max_open_positions,
            "max_position_margin_pct": s.max_position_margin_pct, "max_daily_loss_pct": s.max_daily_loss_pct,
            "max_trades_per_day": s.max_trades_per_day, "min_reward_risk": s.min_reward_risk,
            "stop_distance_pct": [s.min_stop_pct, s.max_stop_pct],
            "cooldown_after_loss_min": s.cooldown_minutes_after_loss, "time_machine_gate": s.tm_gate,
            "time_machine_min_avg_r_after_fees": s.tm_min_avg_r, "can_widen_stops": s.allow_widen_stop,
        }),
        "",
    ]
    if usage and usage.get("utilization") is not None:
        parts += [f"Claude usage window: {round(usage['utilization'] * 100)}% used. Be efficient with tool calls.", ""]
    parts += [
        "## Your task now",
        "Work through the cycle: review closed trades, manage open positions, look for A-grade setups, act or wait, "
        "and end with publish_outlook. Be decisive and efficient.",
    ]
    return "\n".join(parts)


def short_age(ts: float) -> str:
    mins = (time.time() - ts) / 60
    return f"{mins:.0f}m" if mins < 90 else f"{mins / 60:.1f}h"
