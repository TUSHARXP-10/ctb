"""Binance USD-M futures REST client (demo, testnet or live).

Public market data and signed account/trading calls. Stop-loss and
take-profit orders go to the Algo Order API (`/fapi/v1/algoOrder`), which
Binance made mandatory for conditional orders on 2025-12-09; the classic
`/fapi/v1/order` endpoint is used as a fallback for hosts without it.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from .base import Account, ExchangeError, Fill, Order, Position, SymbolInfo, dec, fmt

log = logging.getLogger("tushar.binance")

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "12h": 43_200_000, "1d": 86_400_000,
}


class BinanceAPIError(ExchangeError):
    pass


def parse_kline(k: list) -> dict:
    return {
        "t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]),
        "v": float(k[5]), "ct": int(k[6]), "qv": float(k[7]),
    }


class BinanceFutures:
    def __init__(self, base_url: str, api_key: str = "", api_secret: str = "",
                 public_fallback_url: str | None = None, recv_window: int = 6000):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret.encode() if api_secret else b""
        self.recv_window = recv_window
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=15.0,
                                         headers={"User-Agent": "tushar.ai/1.0"})
        self._public_fallback = (
            httpx.AsyncClient(base_url=public_fallback_url, timeout=10.0)
            if public_fallback_url and public_fallback_url.rstrip("/") != self.base_url else None
        )
        self._time_offset = 0
        self._symbols: dict[str, SymbolInfo] = {}
        self._symbols_at = 0.0
        self._setup_cache: dict[str, tuple[int, str]] = {}
        self._unavailable: dict[str, float] = {}   # optional endpoints that failed recently
        self.hedge_mode = False
        self.algo_supported = True

    async def close(self) -> None:
        await self._client.aclose()
        if self._public_fallback:
            await self._public_fallback.aclose()

    # ------------------------------------------------------------------ http
    async def _request(self, method: str, path: str, params: dict | None = None, signed: bool = False,
                       client: httpx.AsyncClient | None = None, retries: int = 2) -> Any:
        client = client or self._client
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {}
        for attempt in range(retries + 1):
            query = dict(params)
            if signed:
                if not self.api_key or not self.api_secret:
                    raise BinanceAPIError("Binance API key/secret not configured")
                query["timestamp"] = int(time.time() * 1000) + self._time_offset
                query["recvWindow"] = self.recv_window
                qs = urlencode(query)
                sig = hmac.new(self.api_secret, qs.encode(), hashlib.sha256).hexdigest()
                qs = f"{qs}&signature={sig}"
                headers = {"X-MBX-APIKEY": self.api_key}
            else:
                qs = urlencode(query)
                if self.api_key:
                    headers = {"X-MBX-APIKEY": self.api_key}
            url = f"{path}?{qs}" if qs else path
            try:
                resp = await client.request(method, url, headers=headers)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if attempt < retries and method == "GET":
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise BinanceAPIError(f"network error on {path}: {e!r}") from e
            if resp.status_code == 200:
                return resp.json()
            code, msg = None, resp.text[:300]
            try:
                body = resp.json()
                code, msg = body.get("code"), body.get("msg", msg)
            except ValueError:
                pass
            if code == -1021 and attempt < retries:   # clock drift
                await self.sync_time()
                continue
            if resp.status_code in (429, 418):
                raise BinanceAPIError(f"rate limited by Binance on {path}: {msg}", code, resp.status_code)
            if resp.status_code >= 500 and attempt < retries and method == "GET":
                await asyncio.sleep(1.0)
                continue
            raise BinanceAPIError(f"{path}: {msg}", code, resp.status_code)
        raise BinanceAPIError(f"{path}: retries exhausted")

    async def sync_time(self) -> None:
        data = await self._request("GET", "/fapi/v1/time", retries=1)
        self._time_offset = int(data["serverTime"]) - int(time.time() * 1000)

    async def _optional(self, key: str, path: str, params: dict) -> Any:
        """Endpoints that may not exist on demo/testnet: try the main host, then public prod."""
        if time.time() - self._unavailable.get(key, 0) < 1800:
            return None
        for client in (self._client, self._public_fallback):
            if client is None:
                continue
            try:
                return await self._request("GET", path, params, client=client, retries=0)
            except ExchangeError:
                continue
        self._unavailable[key] = time.time()
        return None

    # ------------------------------------------------------------ market data
    async def exchange_info(self, force: bool = False) -> dict[str, SymbolInfo]:
        if self._symbols and not force and time.time() - self._symbols_at < 3600:
            return self._symbols
        data = await self._request("GET", "/fapi/v1/exchangeInfo")
        out: dict[str, SymbolInfo] = {}
        for s in data.get("symbols", []):
            f = {x["filterType"]: x for x in s.get("filters", [])}
            lot = f.get("MARKET_LOT_SIZE") or f.get("LOT_SIZE") or {}
            lot_limit = f.get("LOT_SIZE") or lot
            step = max(dec(lot.get("stepSize", "0.001")), dec(lot_limit.get("stepSize", "0.001")))
            out[s["symbol"]] = SymbolInfo(
                symbol=s["symbol"], base=s.get("baseAsset", ""), quote=s.get("quoteAsset", ""),
                tick_size=dec(f.get("PRICE_FILTER", {}).get("tickSize", "0.01")),
                step_size=step,
                min_qty=max(dec(lot.get("minQty", "0")), dec(lot_limit.get("minQty", "0"))),
                max_qty=min(dec(lot.get("maxQty", "1e12")), dec(lot_limit.get("maxQty", "1e12"))),
                min_notional=dec(f.get("MIN_NOTIONAL", {}).get("notional", "5")),
                status=s.get("status", "TRADING"),
                contract_type=s.get("contractType", "PERPETUAL"),
            )
        self._symbols, self._symbols_at = out, time.time()
        return out

    async def tickers_24h(self) -> list[dict]:
        data = await self._request("GET", "/fapi/v1/ticker/24hr")
        return [{
            "symbol": t["symbol"], "last": float(t["lastPrice"]),
            "change_pct": float(t["priceChangePercent"]), "high": float(t["highPrice"]),
            "low": float(t["lowPrice"]), "quote_volume": float(t["quoteVolume"]),
        } for t in data]

    async def mark_prices(self) -> dict[str, dict]:
        data = await self._request("GET", "/fapi/v1/premiumIndex")
        if isinstance(data, dict):
            data = [data]
        return {d["symbol"]: {
            "mark": float(d["markPrice"]), "index": float(d.get("indexPrice") or 0),
            "funding_rate": float(d.get("lastFundingRate") or 0),
            "next_funding_time": int(d.get("nextFundingTime") or 0),
        } for d in data}

    async def klines(self, symbol: str, interval: str, limit: int = 300, end_time: int | None = None) -> list[dict]:
        out: list[dict] = []
        remaining = limit
        end = end_time
        while remaining > 0:
            batch = min(remaining, 1500)
            data = await self._request("GET", "/fapi/v1/klines",
                                       {"symbol": symbol, "interval": interval, "limit": batch, "endTime": end})
            if not data:
                break
            out = [parse_kline(k) for k in data] + out
            remaining -= len(data)
            if len(data) < batch:
                break
            end = int(data[0][0]) - 1
        return out

    async def depth(self, symbol: str, limit: int = 20) -> dict:
        d = await self._request("GET", "/fapi/v1/depth", {"symbol": symbol, "limit": limit})
        return {"bids": [(float(p), float(q)) for p, q in d["bids"]],
                "asks": [(float(p), float(q)) for p, q in d["asks"]]}

    async def open_interest(self, symbol: str) -> float | None:
        d = await self._optional("oi", "/fapi/v1/openInterest", {"symbol": symbol})
        return float(d["openInterest"]) if d else None

    async def open_interest_hist(self, symbol: str, period: str = "1h", limit: int = 24) -> list[dict] | None:
        d = await self._optional("oih", "/futures/data/openInterestHist",
                                 {"symbol": symbol, "period": period, "limit": limit})
        if not d:
            return None
        return [{"t": int(x["timestamp"]), "oi": float(x["sumOpenInterest"]),
                 "oi_value": float(x["sumOpenInterestValue"])} for x in d]

    async def long_short_ratio(self, symbol: str, period: str = "1h") -> dict | None:
        d = await self._optional("lsr", "/futures/data/globalLongShortAccountRatio",
                                 {"symbol": symbol, "period": period, "limit": 1})
        if not d:
            return None
        x = d[-1]
        return {"ratio": float(x["longShortRatio"]), "long_pct": float(x["longAccount"]) * 100,
                "short_pct": float(x["shortAccount"]) * 100}

    # -------------------------------------------------------------- account
    async def prepare(self) -> None:
        """Clock sync and one-way position mode (hedge mode is supported if it can't be changed)."""
        await self.sync_time()
        try:
            d = await self._request("GET", "/fapi/v1/positionSide/dual", signed=True)
            if d.get("dualSidePosition"):
                try:
                    await self._request("POST", "/fapi/v1/positionSide/dual",
                                        {"dualSidePosition": "false"}, signed=True)
                    self.hedge_mode = False
                except BinanceAPIError as e:
                    log.warning("account is in hedge mode and could not switch to one-way: %s", e)
                    self.hedge_mode = True
        except BinanceAPIError as e:
            log.warning("could not read position mode: %s", e)

    async def setup_symbol(self, symbol: str, leverage: int, margin_type: str = "ISOLATED") -> int:
        """Set margin type and leverage. Returns the leverage actually applied."""
        cached = self._setup_cache.get(symbol)
        if cached == (leverage, margin_type):
            return leverage
        try:
            await self._request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type},
                                signed=True)
        except BinanceAPIError as e:
            if e.code not in (-4046, -4048, -4047):  # already set / position open / open orders
                log.warning("marginType %s: %s", symbol, e)
        lev = leverage
        while lev >= 1:
            try:
                d = await self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": lev},
                                        signed=True)
                lev = int(d.get("leverage", lev))
                break
            except BinanceAPIError as e:
                if e.code in (-4028, -4161) and lev > 1:   # leverage not valid for this symbol/notional
                    lev = max(1, lev // 2)
                    continue
                raise
        self._setup_cache[symbol] = (lev, margin_type)
        return lev

    async def account(self) -> Account:
        try:
            acc = await self._request("GET", "/fapi/v2/account", signed=True)
        except BinanceAPIError as e:
            if e.status not in (404, 410):
                raise
            acc = await self._request("GET", "/fapi/v3/account", signed=True)
        positions = await self.positions()
        return Account(
            wallet_balance=float(acc.get("totalWalletBalance", 0)),
            equity=float(acc.get("totalMarginBalance", 0)),
            available=float(acc.get("availableBalance", 0)),
            unrealized_pnl=float(acc.get("totalUnrealizedProfit", 0)),
            positions=positions,
        )

    async def positions(self) -> list[Position]:
        try:
            data = await self._request("GET", "/fapi/v2/positionRisk", signed=True)
        except BinanceAPIError:
            data = await self._request("GET", "/fapi/v3/positionRisk", signed=True)
        out = []
        for p in data:
            amt = float(p.get("positionAmt", 0))
            if amt == 0:
                continue
            mark = float(p.get("markPrice", 0))
            margin = float(p.get("isolatedMargin") or p.get("isolatedWallet") or p.get("initialMargin") or 0)
            lev = int(float(p.get("leverage", 0) or 0))
            if not margin and lev:
                margin = abs(amt) * mark / lev
            out.append(Position(
                symbol=p["symbol"], side="long" if amt > 0 else "short", qty=abs(amt),
                entry_price=float(p.get("entryPrice", 0)), mark_price=mark,
                unrealized_pnl=float(p.get("unRealizedProfit", p.get("unrealizedProfit", 0))),
                leverage=lev or 1, liquidation_price=float(p.get("liquidationPrice", 0) or 0),
                margin=margin, position_side=p.get("positionSide", "BOTH"),
            ))
        return out

    async def open_orders(self, symbol: str | None = None) -> list[Order]:
        orders: list[Order] = []
        try:
            data = await self._request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}, signed=True)
            items = data if isinstance(data, list) else (data.get("orders") or data.get("rows") or [])
            for o in items:
                orders.append(Order(
                    id=str(o.get("algoId") or o.get("clientAlgoId")), symbol=o.get("symbol", ""),
                    side=o.get("side", ""), type=o.get("orderType") or o.get("type") or "",
                    qty=float(o.get("quantity") or 0), trigger_price=float(o.get("triggerPrice") or 0),
                    price=float(o.get("price") or 0), reduce_only=bool(o.get("reduceOnly", True)),
                    algo=True, status=o.get("algoStatus") or o.get("status") or "NEW",
                ))
        except BinanceAPIError as e:
            if e.status == 404:
                self.algo_supported = False
            else:
                raise  # unknown state: callers must not assume the stops are missing
        data = await self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol}, signed=True)
        for o in data:
            orders.append(Order(
                id=str(o["orderId"]), symbol=o["symbol"], side=o["side"], type=o.get("type", ""),
                qty=float(o.get("origQty") or 0), trigger_price=float(o.get("stopPrice") or 0),
                price=float(o.get("price") or 0),
                reduce_only=bool(o.get("reduceOnly") or o.get("closePosition")), algo=False,
                status=o.get("status", "NEW"),
            ))
        return orders

    # -------------------------------------------------------------- trading
    def _pos_side(self, side: str) -> str | None:
        return ("LONG" if side == "long" else "SHORT") if self.hedge_mode else None

    async def market_order(self, symbol: str, side: str, qty: Decimal | float, reduce_only: bool = False,
                           position: str | None = None) -> Fill:
        """side is BUY/SELL. `position` (long/short) is needed in hedge mode."""
        params: dict[str, Any] = {
            "symbol": symbol, "side": side, "type": "MARKET", "quantity": fmt(dec(qty)),
            "newOrderRespType": "RESULT",
        }
        if self.hedge_mode:
            params["positionSide"] = self._pos_side(position or ("long" if side == "BUY" else "short"))
        elif reduce_only:
            params["reduceOnly"] = "true"
        d = await self._request("POST", "/fapi/v1/order", params, signed=True)
        executed = float(d.get("executedQty") or 0)
        avg = float(d.get("avgPrice") or 0)
        if executed == 0 or avg == 0:
            # Some hosts answer before the fill lands: look the order up.
            await asyncio.sleep(0.4)
            q = await self._request("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": d["orderId"]},
                                    signed=True)
            executed = float(q.get("executedQty") or executed)
            avg = float(q.get("avgPrice") or avg)
        return Fill(symbol=symbol, side=side, qty=executed, avg_price=avg, order_id=str(d.get("orderId")))

    async def place_protection(self, symbol: str, position_side: str, qty: Decimal | float,
                               stop: Decimal | None = None, target: Decimal | None = None) -> list[Order]:
        """Reduce-only STOP_MARKET / TAKE_PROFIT_MARKET for an open position."""
        close_side = "SELL" if position_side == "long" else "BUY"
        placed = []
        for kind, price in (("STOP_MARKET", stop), ("TAKE_PROFIT_MARKET", target)):
            if price is None:
                continue
            placed.append(await self._place_conditional(symbol, close_side, kind, dec(price), dec(qty),
                                                        position_side))
        return placed

    async def _place_conditional(self, symbol: str, side: str, kind: str, trigger: Decimal, qty: Decimal,
                                 position_side: str) -> Order:
        algo_err: BinanceAPIError | None = None
        if self.algo_supported:
            params: dict[str, Any] = {
                "algoType": "CONDITIONAL", "symbol": symbol, "side": side, "type": kind,
                "triggerPrice": fmt(trigger), "quantity": fmt(qty), "workingType": "MARK_PRICE",
            }
            if self.hedge_mode:
                params["positionSide"] = self._pos_side(position_side)
            else:
                params["reduceOnly"] = "true"
            try:
                d = await self._request("POST", "/fapi/v1/algoOrder", params, signed=True)
                return Order(id=str(d.get("algoId") or d.get("clientAlgoId")), symbol=symbol, side=side,
                             type=kind, qty=float(qty), trigger_price=float(trigger), algo=True,
                             status=d.get("algoStatus", "NEW"))
            except BinanceAPIError as e:
                algo_err = e
                if e.status == 404:
                    self.algo_supported = False
        # Fallback: classic conditional order (older hosts).
        params = {"symbol": symbol, "side": side, "type": kind, "stopPrice": fmt(trigger),
                  "closePosition": "true", "workingType": "MARK_PRICE"}
        if self.hedge_mode:
            params["positionSide"] = self._pos_side(position_side)
        try:
            d = await self._request("POST", "/fapi/v1/order", params, signed=True)
        except BinanceAPIError as e:
            if algo_err is not None:
                raise BinanceAPIError(f"could not place {kind}: algo API said '{algo_err}', "
                                      f"classic API said '{e}'", algo_err.code, algo_err.status) from e
            raise
        return Order(id=str(d["orderId"]), symbol=symbol, side=side, type=kind, qty=float(qty),
                     trigger_price=float(trigger), algo=False, status=d.get("status", "NEW"))

    async def cancel_protection(self, symbol: str) -> int:
        """Cancel every open conditional and regular order on a symbol."""
        n = 0
        for o in await self.open_orders(symbol):
            if not o.algo:
                continue
            try:
                await self._request("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": o.id},
                                    signed=True)
                n += 1
            except BinanceAPIError as e:
                log.warning("cancel algo order %s %s: %s", symbol, o.id, e)
        try:
            await self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}, signed=True)
        except BinanceAPIError as e:
            log.debug("allOpenOrders %s: %s", symbol, e)
        return n

    async def income_since(self, start_ms: int) -> list[dict]:
        data = await self._request("GET", "/fapi/v1/income", {"startTime": start_ms, "limit": 1000}, signed=True)
        return [{"symbol": d.get("symbol", ""), "type": d.get("incomeType", ""), "amount": float(d.get("income", 0)),
                 "t": int(d.get("time", 0))} for d in data]

    async def fills_since(self, symbol: str, start_ms: int) -> list[dict]:
        data = await self._request("GET", "/fapi/v1/userTrades", {"symbol": symbol, "startTime": start_ms,
                                                                  "limit": 500}, signed=True)
        return [{"side": d["side"], "price": float(d["price"]), "qty": float(d["qty"]),
                 "realized_pnl": float(d.get("realizedPnl", 0)), "fee": float(d.get("commission", 0)),
                 "t": int(d["time"]), "order_id": str(d.get("orderId"))} for d in data]
