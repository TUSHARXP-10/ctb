"""End-to-end: simulated market + paper account + mock brain, through the real desk and engine."""
import time

from tushar.brain.cli import extract_json


async def test_mock_cycle_opens_a_protected_trade(engine):
    res = await engine.run_cycle("test")
    assert res["status"] == "done", res
    trades = engine.db.open_trades()
    cycle = engine.db.one("SELECT * FROM cycles WHERE id=?", (res["cycle_id"],))
    assert cycle["status"] == "done"
    assert engine.desk.outlook and engine.desk.outlook["summary"]
    if trades:  # the sim seed normally produces a setup; if it does, it must be fully protected
        t = trades[0]
        orders = await engine.broker.open_orders(t["symbol"])
        assert {o.kind for o in orders} == {"SL", "TP"}
        assert t["tm"]["ok"] and t["risk_usd"] > 0


async def test_open_close_and_journal(engine):
    d = engine.desk
    price = engine.market.price("ETHUSDT")
    res = await d.open_position("ETHUSDT", "long", price * 0.98, price * 1.05, risk_pct=1.0, leverage=5,
                                setup="test", thesis="unit test")
    if res["status"] == "BLOCKED":  # the time machine may veto on this seed; test the gate then
        assert any("time_machine" in b for b in res["blocks"])
        engine.s.tm_gate = False
        res = await d.open_position("ETHUSDT", "long", price * 0.98, price * 1.05, risk_pct=1.0, leverage=5,
                                    setup="test", thesis="unit test")
    assert res["status"] == "FILLED", res
    pf = await d.get_portfolio()
    assert pf["positions"][0]["symbol"] == "ETHUSDT"
    assert pf["positions"][0]["stop_loss"] is not None
    blocked = await d.update_protection("ETHUSDT", stop_loss=price * 0.95, reason="widen")
    assert blocked["status"] == "BLOCKED"
    ok = await d.update_protection("ETHUSDT", stop_loss=price * 0.99, reason="tighten")
    assert ok["status"] == "UPDATED"
    part = await d.close_position("ETHUSDT", 0.5, "take half")
    assert part["status"] == "PARTIAL"
    assert len(await engine.broker.open_orders("ETHUSDT")) == 2
    done = await d.close_position("ETHUSDT", 1.0, "done")
    assert done["status"] == "CLOSED"
    t = engine.db.recent_trades(1)[0]
    assert t["status"] == "closed" and t["exit_reason"] == "claude" and t["pnl"] is not None
    journal = await d.get_trade_journal()
    assert journal["stats_all_time"]["trades"] == 1


async def test_stop_hit_is_reconciled(engine):
    engine.s.tm_gate = False
    d = engine.desk
    price = engine.market.price("BTCUSDT")
    res = await d.open_position("BTCUSDT", "long", price * 0.985, price * 1.04, thesis="t")
    assert res["status"] == "FILLED", res
    engine.source.live["BTCUSDT"]["c"] = price * 0.97      # gap through the stop
    await engine.market.refresh_prices()
    await engine.monitor_once()
    t = engine.db.recent_trades(1)[0]
    assert t["status"] == "closed" and t["exit_reason"] == "stop_loss" and t["pnl"] < 0
    assert t["r_multiple"] < -0.9
    assert any("position closed" in r for r in engine._wake_reasons)
    assert await engine.broker.open_orders("BTCUSDT") == []


async def test_local_guard_when_exchange_stop_missing(engine):
    engine.s.tm_gate = False
    d = engine.desk
    price = engine.market.price("SOLUSDT")
    res = await d.open_position("SOLUSDT", "short", price * 1.02, price * 0.95, thesis="t")
    assert res["status"] == "FILLED", res
    await engine.broker.cancel_protection("SOLUSDT")        # simulate a lost exchange stop
    engine._last_protect_check = time.time()                 # don't re-place it this tick
    d.invalidate()
    engine.source.live["SOLUSDT"]["c"] = price * 1.03
    await engine.market.refresh_prices()
    await engine.monitor_once()
    t = engine.db.recent_trades(1)[0]
    assert t["status"] == "closed" and t["exit_reason"] == "stop_loss"


async def test_kill_switch_flattens_and_blocks(engine):
    engine.s.tm_gate = False
    d = engine.desk
    p = engine.market.price("BNBUSDT")
    assert (await d.open_position("BNBUSDT", "long", p * 0.98, p * 1.05, thesis="t"))["status"] == "FILLED"
    closed = await engine.kill()
    assert closed and closed[0]["status"] == "CLOSED"
    assert (await d.account(max_age=0)).positions == []
    res = await d.open_position("BNBUSDT", "long", p * 0.98, p * 1.05, thesis="t")
    assert res["status"] == "BLOCKED" and any("kill_switch" in b for b in res["blocks"])
    engine.reset_kill()
    assert engine.status()["state"] == "active"


async def test_alert_wakes_claude(engine):
    p = engine.market.price("XRPUSDT")
    await engine.desk.set_alert("XRPUSDT", "above", p * 1.01, "breakout")
    engine.source.live["XRPUSDT"]["c"] = p * 1.02
    await engine.market.refresh_prices()
    await engine.monitor_once()
    assert engine.desk.active_alerts() == []
    assert any("alert: XRPUSDT" in r for r in engine._wake_reasons)


async def test_memory_review_outlook(engine):
    d = engine.desk
    m = await d.save_memory("Don't short into RSI < 20 on 1h", "rule", "BTCUSDT", 5)
    assert m["status"] == "SAVED"
    assert engine.db.memories()[0]["importance"] == 5
    o = await d.publish_outlook("range", "neutral", "waiting", [{"symbol": "BTCUSDT", "note": "x"}], 30)
    assert o["next_check_minutes"] == 30
    assert d.requested_next_check > time.time() + 25 * 60


async def test_briefing_contains_sections(engine):
    text = await engine.build_briefing("test", [])
    for section in ("## Account", "## Market overview", "## Scanner hits", "## News", "## Risk limits", "## Your task now"):
        assert section in text


async def test_settings_are_clamped_and_persisted(engine):
    changed = engine.update_settings({"max_leverage": 500, "risk_per_trade_pct": "0.5", "nonsense": 1,
                                      "watchlist": "btcusdt, ethusdt"})
    assert engine.s.max_leverage == 50 and engine.s.risk_per_trade_pct == 0.5
    assert engine.s.watchlist == ["BTCUSDT", "ETHUSDT"]
    assert "nonsense" not in changed
    assert engine.db.get_settings()["max_leverage"] == 50


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Here you go:\n```json\n{"a": {"b": 2}}\n```') == {"a": {"b": 2}}
    assert extract_json('blah {"x": [1, {"y": 2}]} trailing') == {"x": [1, {"y": 2}]}
    assert extract_json("no json") is None


async def test_pending_entry_fires_through_risk_gate(engine):
    engine.s.tm_gate = False
    d = engine.desk
    price = engine.market.price("ETHUSDT")
    bad = await d.place_pending_entry("ETHUSDT", "long", price * 0.99, price * 1.0, price * 1.05, "t")
    assert bad["status"] == "ERROR"                                     # stop above entry
    far = await d.place_pending_entry("ETHUSDT", "long", price * 0.5, price * 0.45, price * 0.6, "t")
    assert far["status"] == "ERROR"                                     # too far from price
    res = await d.place_pending_entry("ETHUSDT", "long", price * 0.99, price * 0.975, price * 1.04, "dip buy",
                                      setup="pullback")
    assert res["status"] == "PENDING" and "below" in res["triggers_when_price"] and res["type"] == "limit"
    assert (await d.place_pending_entry("ETHUSDT", "long", price * 0.98, price * 0.97, price * 1.04, "x"))["status"] == "ERROR"
    await engine.market.refresh_prices()
    await engine.monitor_once()
    assert d.pending_entries() and not engine.db.open_trades()          # not there yet
    engine.source.live["ETHUSDT"]["c"] = price * 0.985
    await engine.market.refresh_prices()
    await engine.monitor_once()
    assert not d.pending_entries()
    t = engine.db.open_trades()[0]
    assert t["symbol"] == "ETHUSDT" and t["side"] == "long" and "pending entry" in t["thesis"]
    assert any("pending entry" in r for r in engine._wake_reasons)
    row = engine.db.one("SELECT status FROM pending_entries WHERE id=?", (res["pending_id"],))
    assert row["status"] == "filled"


async def test_pending_entry_expires(engine):
    d = engine.desk
    price = engine.market.price("BNBUSDT")
    res = await d.place_pending_entry("BNBUSDT", "short", price * 1.01, price * 1.03, price * 0.97, "fade", expires_hours=1)
    engine.db.update("pending_entries", res["pending_id"], {"expires_at": time.time() - 1})
    await engine.monitor_once()
    assert engine.db.one("SELECT status FROM pending_entries WHERE id=?", (res["pending_id"],))["status"] == "expired"
