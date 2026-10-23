import time
from decimal import Decimal

from tushar.exchange.base import Account, Position, SymbolInfo
from tushar.risk import EntryRequest, RiskContext, RiskManager, open_risk_usd

from .conftest import make_settings

BTC = SymbolInfo("BTCUSDT", "BTC", "USDT", Decimal("0.10"), Decimal("0.001"), Decimal("0.001"), Decimal("1000"),
                 Decimal("100"))


def ctx(**over):
    base = dict(account=Account(10_000, 10_000, 10_000, 0, []), price=50_000.0, info=BTC, open_trades=[],
                trades_today=0, realized_today=0.0, start_of_day_equity=10_000.0)
    base.update(over)
    return RiskContext(**base)


def rm(tmp_path, **over):
    return RiskManager(make_settings(tmp_path, **over))


def test_sizes_from_loss_at_stop(tmp_path):
    v = rm(tmp_path, risk_per_trade_pct=1.0).evaluate_entry(EntryRequest("BTCUSDT", "long", 49_000, 52_500), ctx())
    assert v.passed, v.blocks
    # $100 risk / $1000 stop distance = 0.1 BTC
    assert v.qty == Decimal("0.100")
    assert abs(v.risk_usd - 100) < 1e-6
    assert v.reward_risk == 2.5


def test_risk_pct_is_capped(tmp_path):
    v = rm(tmp_path, risk_per_trade_pct=1.0).evaluate_entry(
        EntryRequest("BTCUSDT", "long", 49_000, 52_500, risk_pct=9.0), ctx())
    assert v.qty == Decimal("0.100")


def test_wrong_side_stop_blocks(tmp_path):
    v = rm(tmp_path).evaluate_entry(EntryRequest("BTCUSDT", "long", 51_000, 53_000), ctx())
    assert not v.passed and any("stop_side" in b for b in v.blocks)
    v = rm(tmp_path).evaluate_entry(EntryRequest("BTCUSDT", "short", 49_000, 47_000), ctx())
    assert not v.passed


def test_reward_risk_minimum(tmp_path):
    v = rm(tmp_path, min_reward_risk=2.0).evaluate_entry(EntryRequest("BTCUSDT", "long", 49_000, 51_500), ctx())
    assert not v.passed and any("reward_risk" in b for b in v.blocks)


def test_stop_distance_limits(tmp_path):
    v = rm(tmp_path, min_stop_pct=0.5).evaluate_entry(EntryRequest("BTCUSDT", "long", 49_900, 51_000), ctx())
    assert any("stop_distance" in b for b in v.blocks)


def test_duplicate_and_max_positions(tmp_path):
    pos = Position("BTCUSDT", "long", 0.1, 50_000, 50_000, 0, 5, 0, 1000)
    v = rm(tmp_path).evaluate_entry(EntryRequest("BTCUSDT", "long", 49_000, 52_500),
                                    ctx(account=Account(10_000, 10_000, 9000, 0, [pos])))
    assert any("duplicate" in b for b in v.blocks)
    trades = [{"symbol": f"X{i}USDT", "side": "long", "entry_price": 1, "stop_loss": 0.99, "qty": 1} for i in range(4)]
    v = rm(tmp_path, max_open_positions=4).evaluate_entry(EntryRequest("BTCUSDT", "long", 49_000, 52_500),
                                                          ctx(open_trades=trades))
    assert any("max_positions" in b for b in v.blocks)


def test_daily_loss_budget(tmp_path):
    v = rm(tmp_path, max_daily_loss_pct=2.0, risk_per_trade_pct=1.5).evaluate_entry(
        EntryRequest("BTCUSDT", "long", 49_000, 52_500), ctx(realized_today=-100.0))
    assert any("daily_loss" in b for b in v.blocks)


def test_kill_switch_and_pause_block(tmp_path):
    v = rm(tmp_path).evaluate_entry(EntryRequest("BTCUSDT", "long", 49_000, 52_500), ctx(kill_switch=True))
    assert any("kill_switch" in b for b in v.blocks)
    v = rm(tmp_path).evaluate_entry(EntryRequest("BTCUSDT", "long", 49_000, 52_500), ctx(paused=True))
    assert any("paused" in b for b in v.blocks)


def test_cooldown_after_loss(tmp_path):
    v = rm(tmp_path, cooldown_minutes_after_loss=30).evaluate_entry(
        EntryRequest("BTCUSDT", "long", 49_000, 52_500), ctx(last_loss_at=time.time() - 300))
    assert any("cooldown" in b for b in v.blocks)


def test_leverage_lowered_for_wide_stop(tmp_path):
    v = rm(tmp_path, max_leverage=20, max_stop_pct=15).evaluate_entry(
        EntryRequest("BTCUSDT", "long", 45_000, 60_000, leverage=20), ctx())
    # 10% stop -> at most 7x so liquidation stays beyond the stop
    assert v.leverage == 7
    assert any("leverage lowered" in n for n in v.notes)


def test_size_capped_by_margin(tmp_path):
    v = rm(tmp_path, max_position_margin_pct=10, default_leverage=2, risk_per_trade_pct=5).evaluate_entry(
        EntryRequest("BTCUSDT", "long", 49_900, 51_000, leverage=2), ctx())
    # margin cap: 10% of 10k * 2x = $2000 notional = 0.04 BTC
    assert v.qty == Decimal("0.040")
    assert any("capped" in n for n in v.notes)


def test_too_small_blocks(tmp_path):
    v = rm(tmp_path, risk_per_trade_pct=0.1).evaluate_entry(
        EntryRequest("BTCUSDT", "long", 40_000, 70_000), ctx(account=Account(100, 100, 100, 0, [])))
    assert any("size" in b for b in v.blocks)


def test_time_machine_gate(tmp_path):
    tm = {"ok": True, "verdict": "Strongly toward the stop", "mc": {"avg_r_after_fees": -0.6}}
    v = rm(tmp_path, tm_gate=True, tm_min_avg_r=-0.15).evaluate_entry(
        EntryRequest("BTCUSDT", "long", 49_000, 52_500), ctx(tm_result=tm))
    assert any("time_machine" in b for b in v.blocks)


def test_stop_update_rules(tmp_path):
    r = rm(tmp_path, allow_widen_stop=False)
    assert r.evaluate_stop_update("long", 50_000, 49_000, 49_500, None) == []
    assert r.evaluate_stop_update("long", 50_000, 49_000, 48_000, None)  # widening
    assert r.evaluate_stop_update("long", 50_000, 49_000, 50_500, None)  # above price
    assert r.evaluate_stop_update("short", 50_000, 51_000, 50_500, 48_000) == []
    assert r.evaluate_stop_update("short", 50_000, 51_000, None, 50_500)  # target wrong side


def test_open_risk_ignores_locked_profit():
    trades = [{"side": "long", "entry_price": 100, "stop_loss": 95, "qty": 2},
              {"side": "short", "entry_price": 100, "stop_loss": 98, "qty": 5}]  # stop in profit
    assert open_risk_usd(trades) == 10
