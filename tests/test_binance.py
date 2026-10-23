import hashlib
import hmac
import json
from decimal import Decimal
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from tushar.exchange.binance import BinanceAPIError, BinanceFutures

SECRET = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"

EXCHANGE_INFO = {"symbols": [{
    "symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING", "contractType": "PERPETUAL",
    "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "120"},
        {"filterType": "MIN_NOTIONAL", "notional": "100"},
    ]}]}


def client(handler) -> BinanceFutures:
    b = BinanceFutures("https://demo-fapi.binance.com", "KEY", SECRET)
    b._client = httpx.AsyncClient(base_url=b.base_url, transport=httpx.MockTransport(handler))
    return b


async def test_signature_and_api_key_header():
    seen = {}

    def handler(req: httpx.Request):
        seen["req"] = req
        return httpx.Response(200, json={"totalWalletBalance": "1", "totalMarginBalance": "1",
                                         "availableBalance": "1", "totalUnrealizedProfit": "0"})

    b = client(handler)
    await b._request("GET", "/fapi/v2/account", signed=True)
    req = seen["req"]
    assert req.headers["X-MBX-APIKEY"] == "KEY"
    qs = urlsplit(str(req.url)).query
    body, sig = qs.rsplit("&signature=", 1)
    assert sig == hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    params = dict(parse_qsl(body))
    assert "timestamp" in params and params["recvWindow"] == "6000"


def test_known_binance_signature_vector():
    # Example from Binance's API docs.
    q = "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"
    assert hmac.new(SECRET.encode(), q.encode(), hashlib.sha256).hexdigest() == \
        "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"


async def test_exchange_info_filters_and_rounding():
    b = client(lambda req: httpx.Response(200, json=EXCHANGE_INFO))
    info = (await b.exchange_info())["BTCUSDT"]
    assert info.tick_size == Decimal("0.10") and info.step_size == Decimal("0.001")
    assert info.max_qty == Decimal("120") and info.min_notional == Decimal("100")
    assert info.round_qty(0.12345) == Decimal("0.123")
    assert info.stop_price(50_000.07, "long") == Decimal("50000.0")
    assert info.stop_price(50_000.01, "short") == Decimal("50000.1")


async def test_protection_uses_algo_api():
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path, dict(parse_qsl(req.url.query.decode()))))
        return httpx.Response(200, json={"algoId": 777, "algoStatus": "NEW"})

    b = client(handler)
    orders = await b.place_protection("BTCUSDT", "long", Decimal("0.010"), stop=Decimal("49000.0"),
                                      target=Decimal("52000.0"))
    assert [o.kind for o in orders] == ["SL", "TP"]
    method, path, p = calls[0]
    assert (method, path) == ("POST", "/fapi/v1/algoOrder")
    assert p["algoType"] == "CONDITIONAL" and p["type"] == "STOP_MARKET" and p["side"] == "SELL"
    assert p["triggerPrice"] == "49000" and p["quantity"] == "0.01" and p["reduceOnly"] == "true"
    assert calls[1][2]["type"] == "TAKE_PROFIT_MARKET"


async def test_protection_falls_back_to_classic_order():
    def handler(req: httpx.Request):
        if req.url.path == "/fapi/v1/algoOrder":
            return httpx.Response(404, text="not found")
        p = dict(parse_qsl(req.url.query.decode()))
        assert p["stopPrice"] == "51000" and p["closePosition"] == "true"
        return httpx.Response(200, json={"orderId": 5, "status": "NEW"})

    b = client(handler)
    orders = await b.place_protection("BTCUSDT", "short", Decimal("0.01"), stop=Decimal("51000"))
    assert orders[0].id == "5" and not orders[0].algo
    assert b.algo_supported is False


async def test_open_orders_merges_algo_and_classic():
    def handler(req: httpx.Request):
        if req.url.path == "/fapi/v1/openAlgoOrders":
            return httpx.Response(200, json=[{"algoId": 1, "symbol": "BTCUSDT", "side": "SELL",
                                              "orderType": "STOP_MARKET", "quantity": "0.01", "triggerPrice": "49000"}])
        return httpx.Response(200, json=[{"orderId": 2, "symbol": "BTCUSDT", "side": "SELL",
                                          "type": "TAKE_PROFIT_MARKET", "origQty": "0", "stopPrice": "52000",
                                          "closePosition": True}])

    orders = await client(handler).open_orders()
    assert {(o.kind, o.algo) for o in orders} == {("SL", True), ("TP", False)}


async def test_clock_drift_retry():
    state = {"n": 0}

    def handler(req: httpx.Request):
        if req.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1_800_000_000_000})
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(400, json={"code": -1021, "msg": "Timestamp outside recvWindow"})
        return httpx.Response(200, json=[])

    b = client(handler)
    assert await b._request("GET", "/fapi/v1/openOrders", signed=True) == []
    assert state["n"] == 2


async def test_api_error_carries_code():
    b = client(lambda req: httpx.Response(400, json={"code": -2019, "msg": "Margin is insufficient."}))
    with pytest.raises(BinanceAPIError) as e:
        await b._request("POST", "/fapi/v1/order", {"symbol": "BTCUSDT"}, signed=True)
    assert e.value.code == -2019 and "Margin" in str(e.value)


async def test_klines_paginates():
    def handler(req: httpx.Request):
        p = dict(parse_qsl(req.url.query.decode()))
        end = int(p.get("endTime", 10_000_000_000))
        n = int(p["limit"])
        start = end - n * 60_000
        rows = [[t, "1", "2", "0.5", "1.5", "10", t + 59_999, "15", 1, "0", "0", "0"]
                for t in range(start + 1, end, 60_000)][:n]
        return httpx.Response(200, content=json.dumps(rows))

    rows = await client(handler).klines("BTCUSDT", "1m", 2000)
    assert len(rows) == 2000
    assert all(rows[i]["t"] < rows[i + 1]["t"] for i in range(len(rows) - 1))
