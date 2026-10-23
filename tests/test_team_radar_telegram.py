"""Multi-agent desk orchestration, liquidation radar, analytics and Telegram commands (all offline)."""
import time

import numpy as np

from tushar.analytics import correlation_matrix, exposure
from tushar.brain.prompts import TEAM, team_section
from tushar.brain.team import TeamBrain, is_team_trigger
from tushar.radar import LiquidationRadar, parse_force_order
from tushar.telegram import TelegramBot


# ------------------------------------------------------------------ radar
def test_parse_force_order():
    msg = {"e": "forceOrder", "E": 1_700_000_000_000, "o": {"s": "BTCUSDT", "S": "SELL", "q": "0.5", "p": "60000",
                                                            "ap": "59990", "z": "0.5", "T": 1_700_000_000_000}}
    ev = parse_force_order(msg)
    assert ev["side"] == "long" and ev["symbol"] == "BTCUSDT" and abs(ev["usd"] - 29_995) < 1e-6
    assert parse_force_order({"o": {"s": "X", "S": "BUY", "q": "1", "p": "2", "ap": "2", "z": "1"}})["side"] == "short"
    assert parse_force_order({"nope": 1}) is None


def test_radar_summary_and_buckets():
    r = LiquidationRadar()
    now = time.time()
    for i, (sym, side, usd) in enumerate([("BTCUSDT", "long", 500_000), ("BTCUSDT", "long", 300_000),
                                          ("ETHUSDT", "short", 100_000)]):
        r.add({"ts": now - i * 60, "symbol": sym, "side": side, "price": 1.0, "qty": usd, "usd": usd})
    r.add({"ts": now - 7200, "symbol": "OLDUSDT", "side": "short", "price": 1.0, "qty": 1, "usd": 9e9})
    s = r.summary(60)
    assert s["longs_liquidated_usd"] == 800_000 and s["shorts_liquidated_usd"] == 100_000
    assert s["dominant"] == "longs" and s["by_symbol"][0]["symbol"] == "BTCUSDT"
    assert r.symbol_summary("ETHUSDT")["shorts_usd"] == 100_000
    assert len(r.buckets(120, 5)) == 24
    assert len(r._pending) >= 2  # big prints queued for the dashboard


# ------------------------------------------------------------------ analytics
def test_correlation_and_beta():
    rng = np.random.default_rng(1)
    btc = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 200)))
    alt = btc ** 2 / 100           # log returns exactly 2x BTC's
    noise = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 200)))
    c = correlation_matrix({"BTCUSDT": list(btc), "ALTUSDT": list(alt), "NOISEUSDT": list(noise)})
    i, j = c["symbols"].index("BTCUSDT"), c["symbols"].index("ALTUSDT")
    assert c["matrix"][i][j] > 0.99 and abs(c["beta"]["ALTUSDT"] - 2.0) < 0.01
    ex = exposure([{"symbol": "ALTUSDT", "side": "long", "qty": 10, "mark_price": 100},
                   {"symbol": "BTCUSDT", "side": "short", "qty": 1, "mark_price": 500}], c["beta"], 10_000)
    assert ex["gross_usd"] == 1500 and ex["net_usd"] == 500 and abs(ex["btc_beta_net_usd"] - 1500) < 5


# ------------------------------------------------------------------ team orchestration
def test_team_triggers():
    assert is_team_trigger("scheduled (every 15 min)") and is_team_trigger("manual run from dashboard")
    assert is_team_trigger("startup")
    assert not is_team_trigger("alert: BTCUSDT above 70000") and not is_team_trigger("position closed: X")


def test_team_section_renders_reports():
    text = team_section({"chartist": {"stance": "bearish", "confidence": 0.6, "headline": "BTC weak",
                                      "summary": "s", "ideas": [{"symbol": "SUIUSDT", "side": "short"}]},
                         "risk": {"error": "timeout"}})
    assert "Chart Analyst: bearish" in text and "SUIUSDT" in text and "unavailable (timeout)" in text


async def test_team_cycle_flow(engine):
    """Phase 1 analysts in parallel, then risk (seeing their reports), then the head trader with team briefing."""
    brain = TeamBrain.__new__(TeamBrain)
    brain.engine = engine
    calls = []

    async def fake_session(prompt, system, names, *, cycle_id, agent="pm", **kw):
        calls.append((agent, prompt, system, names, kw))
        if agent != "pm":
            kw["extra"][0]  # submit_report tool exists
            return {"num_turns": 3, "texts": [], "summary": f"{agent} free text", "model": "haiku"}
        return {"num_turns": 7, "texts": [], "summary": "pm done"}

    brain.session = fake_session
    engine.team_mode_note(1, "team")
    res = await brain.run_cycle("BRIEFING", 1, trigger="scheduled (every 15 min)")
    order = [c[0] for c in calls]
    assert set(order[:3]) == {"chartist", "macro", "flow"} and order[3] == "risk" and order[4] == "pm"
    risk_prompt = calls[3][1]
    assert "Your desk's reports" in risk_prompt and "Chart Analyst" in risk_prompt
    pm_prompt, pm_system = calls[4][1], calls[4][2]
    assert "Risk Officer" in pm_prompt and "# Your desk" in pm_system
    assert all("open_position" not in c[3] for c in calls[:4])      # analysts can't trade
    assert set(calls[0][3]) <= set(TEAM[order[0]]["tools"])
    assert res["num_turns"] == 7 + 4 * 3
    assert {a for a, v in engine.team["agents"].items() if v.get("status") == "done"} >= {"chartist", "risk", "pm"}
    assert len(engine.db.query("SELECT * FROM reports WHERE cycle_id=1")) == 4

    calls.clear()
    await brain.run_cycle("BRIEFING", 2, trigger="alert: BTCUSDT above 1")
    assert [c[0] for c in calls] == ["pm"]                                # events run solo


async def test_mock_team_cycle_and_daily_report(engine):
    res = await engine.run_cycle("scheduled (test)")
    assert res["status"] == "done"
    assert {"chartist", "macro", "flow", "risk"} <= set(engine.team["agents"])
    rep = await engine.make_daily_report()
    assert rep["text"] and engine.db.one("SELECT COUNT(*) AS n FROM daily_reports")["n"] == 1


async def test_new_desk_tools(engine):
    engine.radar.add({"ts": time.time(), "symbol": "BTCUSDT", "side": "long", "price": 1, "qty": 1, "usd": 2e5})
    d = await engine.desk.get_derivatives()
    assert d["most_positive_funding"] and d["liquidations_60m"]["longs_liquidated_usd"] == 200_000
    c = await engine.desk.get_correlations()
    assert "BTCUSDT" in c["symbols"] and "exposure" in c
    liq = await engine.desk.get_liquidations(30)
    assert liq["count"] == 1


# ------------------------------------------------------------------ telegram
async def test_telegram_requires_linked_chat(engine):
    bot = TelegramBot(engine, "TOKEN", "")
    await bot.handle("12345", "/status")
    assert "Your chat id is <code>12345</code>" in bot.sent[-1]
    bot = TelegramBot(engine, "TOKEN", "999")
    await bot.handle("12345", "/pause")
    assert "someone else" in bot.sent[-1] and engine.status()["state"] == "active"


async def test_telegram_commands(engine):
    bot = TelegramBot(engine, "TOKEN", "42")
    await bot.handle("42", "/status")
    assert "Equity" in bot.sent[-1]
    await bot.handle("42", "/pause")
    assert engine.status()["state"] == "paused"
    await bot.handle("42", "/resume")
    assert engine.status()["state"] == "active"
    await bot.handle("42", "/kill")
    assert "confirm" in bot.sent[-1] and engine.status()["state"] == "active"
    await bot.handle("42", "/kill confirm")
    assert engine.status()["state"] == "kill"
    await bot.handle("42", "/pnl")
    assert "All time" in bot.sent[-1]
    await bot.handle("42", "/team")
    assert "Desk" in bot.sent[-1]


def test_telegram_formats_trades(engine):
    bot = TelegramBot(engine, "TOKEN", "42")
    txt = bot.format_event({"type": "trade_close", "data": {"trade": {"symbol": "ETHUSDT", "side": "short", "pnl": -12.5,
                                                                     "r_multiple": -1.0, "exit_price": 1, "exit_reason": "stop_loss"}}})
    assert "CLOSED ETHUSDT" in txt and "-$12.50" in txt and "-1.00R" in txt
    assert bot.format_event({"type": "tick", "data": {}}) is None
