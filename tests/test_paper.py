import pytest

from tushar.exchange.base import ExchangeError
from tushar.exchange.paper import SLIPPAGE, PaperBroker


class Prices:
    def __init__(self):
        self.p = {"BTCUSDT": 50_000.0}

    def __call__(self, s):
        return self.p.get(s)


@pytest.fixture
def pb():
    prices = Prices()
    b = PaperBroker(prices, 10_000, 0.05)
    b.prices = prices
    return b


async def test_open_close_pnl_and_fees(pb):
    await pb.setup_symbol("BTCUSDT", 5)
    f = await pb.market_order("BTCUSDT", "BUY", 0.1)
    assert abs(f.avg_price - 50_000 * (1 + SLIPPAGE)) < 1e-6
    acct = await pb.account()
    assert len(acct.positions) == 1 and acct.positions[0].side == "long"
    assert abs(acct.positions[0].margin - 0.1 * f.avg_price / 5) < 1e-6
    pb.prices.p["BTCUSDT"] = 51_000
    f2 = await pb.market_order("BTCUSDT", "SELL", 0.1, reduce_only=True)
    expected = (51_000 * (1 - SLIPPAGE) - f.avg_price) * 0.1
    assert abs(f2.realized_pnl - expected) < 1e-6
    acct = await pb.account()
    assert acct.positions == []
    fees = 0.1 * f.avg_price * 0.0005 + 0.1 * f2.avg_price * 0.0005
    assert abs(acct.wallet_balance - (10_000 + expected - fees)) < 1e-6


async def test_reduce_only_without_position(pb):
    with pytest.raises(ExchangeError):
        await pb.market_order("BTCUSDT", "SELL", 0.1, reduce_only=True)


async def test_insufficient_margin(pb):
    await pb.setup_symbol("BTCUSDT", 1)
    with pytest.raises(ExchangeError):
        await pb.market_order("BTCUSDT", "BUY", 1.0)  # $50k notional at 1x on $10k


async def test_stop_and_target_fire(pb):
    await pb.setup_symbol("BTCUSDT", 5)
    await pb.market_order("BTCUSDT", "SELL", 0.1)
    await pb.place_protection("BTCUSDT", "short", 0.1, stop=51_000, target=48_000)
    assert len(await pb.open_orders()) == 2
    pb.prices.p["BTCUSDT"] = 50_500
    assert pb.on_tick() == []
    pb.prices.p["BTCUSDT"] = 47_900
    ev = pb.on_tick()
    assert ev and ev[0]["kind"] == "TP" and ev[0]["pnl"] > 0
    assert await pb.open_orders() == []          # the stop leg is gone with the position
    assert (await pb.account()).positions == []


async def test_liquidation(pb):
    await pb.setup_symbol("BTCUSDT", 20)
    await pb.market_order("BTCUSDT", "BUY", 0.5)
    before = pb.s["wallet"]
    pb.prices.p["BTCUSDT"] = 40_000
    ev = pb.on_tick()
    assert ev[0]["kind"] == "LIQUIDATION"
    assert pb.s["wallet"] < before
    assert (await pb.account()).positions == []


async def test_state_roundtrip(pb):
    saved = {}
    b = PaperBroker(pb.prices, 10_000, 0.05, save_fn=lambda s: saved.update(s))
    await b.setup_symbol("BTCUSDT", 3)
    await b.market_order("BTCUSDT", "BUY", 0.05)
    b2 = PaperBroker(pb.prices, 10_000, 0.05, state=saved)
    assert (await b2.account()).positions[0].qty == 0.05
