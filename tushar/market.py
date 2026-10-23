"""Market data service: cached prices, candles, and the snapshots Claude reads."""
from __future__ import annotations

import asyncio
import logging
import time
from math import floor, log10
from typing import Any

from . import indicators as ind
from .exchange.base import SymbolInfo

log = logging.getLogger("tushar.market")

KLINE_TTL = {"1m": 10, "3m": 15, "5m": 20, "15m": 40, "30m": 60, "1h": 90, "2h": 120, "4h": 240,
             "6h": 300, "12h": 600, "1d": 900}


def r(x: Any, sig: int = 6) -> Any:
    """Round to `sig` significant digits to keep prompts short."""
    if not isinstance(x, float):
        return x
    if x == 0:
        return 0.0
    return round(x, max(0, sig - 1 - int(floor(log10(abs(x))))))


def closed_only(candles: list[dict], now_ms: int | None = None) -> list[dict]:
    now_ms = now_ms or int(time.time() * 1000)
    return [c for c in candles if c.get("ct", 0) < now_ms]


class MarketService:
    def __init__(self, source, settings):
        self.source = source
        self.settings = settings
        self.symbols: dict[str, SymbolInfo] = {}
        self.tickers: dict[str, dict] = {}
        self.marks: dict[str, dict] = {}
        self.prices_at = 0.0
        self.tickers_at = 0.0
        self._kcache: dict[tuple, tuple[float, list[dict]]] = {}
        self._locks: dict[tuple, asyncio.Lock] = {}
        self.history: dict[str, list[tuple[float, float]]] = {}   # recent marks for move detection

    # ------------------------------------------------------------- refresh
    async def refresh_symbols(self, force: bool = False) -> None:
        self.symbols = await self.source.exchange_info(force=force)

    async def refresh_prices(self) -> dict[str, dict]:
        marks = await self.source.mark_prices()
        self.marks.update(marks)
        now = time.time()
        self.prices_at = now
        for s in self._tracked():
            m = marks.get(s)
            if not m:
                continue
            h = self.history.setdefault(s, [])
            h.append((now, m["mark"]))
            while h and now - h[0][0] > 3600:
                h.pop(0)
        return marks

    async def refresh_tickers(self) -> None:
        for t in await self.source.tickers_24h():
            self.tickers[t["symbol"]] = t
        self.tickers_at = time.time()

    def _tracked(self) -> list[str]:
        return list(dict.fromkeys(self.settings.watchlist))

    # ------------------------------------------------------------- lookups
    def price(self, symbol: str) -> float | None:
        m = self.marks.get(symbol)
        if m and m.get("mark"):
            return m["mark"]
        t = self.tickers.get(symbol)
        return t["last"] if t else None

    def move_pct(self, symbol: str, minutes: int = 15) -> float | None:
        h = self.history.get(symbol) or []
        if len(h) < 2:
            return None
        cutoff = time.time() - minutes * 60
        base = next((p for t, p in h if t >= cutoff), None)
        return ind.pct(h[-1][1], base) if base else None

    def info(self, symbol: str) -> SymbolInfo | None:
        return self.symbols.get(symbol)

    def universe(self, top_n: int | None = None) -> list[str]:
        """Most traded USDT perpetuals, watchlist first."""
        blocked = set(self.settings.blocked_symbols)
        top_n = top_n or self.settings.scan_top_n
        ranked = sorted(
            (t for s, t in self.tickers.items()
             if s in self.symbols and self.symbols[s].tradable and s not in blocked),
            key=lambda t: t["quote_volume"], reverse=True)
        out = [s for s in self.settings.watchlist if s in self.symbols and s not in blocked]
        for t in ranked:
            if len(out) >= top_n:
                break
            if t["symbol"] not in out:
                out.append(t["symbol"])
        return out

    async def klines(self, symbol: str, interval: str, limit: int = 300) -> list[dict]:
        key = (symbol, interval, limit)
        cached = self._kcache.get(key)
        ttl = KLINE_TTL.get(interval, 60)
        if cached and time.time() - cached[0] < ttl:
            return cached[1]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._kcache.get(key)
            if cached and time.time() - cached[0] < ttl:
                return cached[1]
            data = await self.source.klines(symbol, interval, limit)
            self._kcache[key] = (time.time(), data)
            if len(self._kcache) > 600:
                for k in sorted(self._kcache, key=lambda k: self._kcache[k][0])[:200]:
                    self._kcache.pop(k, None)
            return data

    # ------------------------------------------------------------- snapshots
    async def overview(self, symbols: list[str]) -> list[dict]:
        """One compact row per symbol for the cycle briefing."""
        async def row(sym: str) -> dict | None:
            try:
                h1 = await self.klines(sym, "1h", 220)
                h4 = await self.klines(sym, "4h", 220)
            except Exception as e:  # noqa: BLE001
                return {"symbol": sym, "error": str(e)[:120]}
            s1, s4 = ind.summarize(h1), ind.summarize(h4)
            t = self.tickers.get(sym, {})
            m = self.marks.get(sym, {})
            price = self.price(sym) or s1.get("price")
            return {
                "symbol": sym, "price": r(price),
                "chg_1h": s1.get("change_1"), "chg_4h": s4.get("change_1"),
                "chg_24h": r(t.get("change_pct"), 3),
                "trend_1h": s1.get("trend"), "trend_4h": s4.get("trend"),
                "rsi_1h": s1.get("rsi"), "rsi_4h": s4.get("rsi"),
                "atr_pct_1h": s1.get("atr_pct"),
                "adx_4h": s4.get("adx"),
                "funding_pct": r(m.get("funding_rate", 0) * 100, 3) if m else None,
                "vol_24h_musd": r(t.get("quote_volume", 0) / 1e6, 4) if t else None,
            }
        rows = await asyncio.gather(*(row(s) for s in symbols))
        return [x for x in rows if x]

    async def snapshot(self, symbol: str) -> dict:
        """Deep multi-timeframe read of one symbol for analyze_symbol."""
        if symbol not in self.symbols:
            raise ValueError(f"{symbol} is not a tradable USD-M perpetual here")
        tfs = {"15m": 200, "1h": 300, "4h": 250, "1d": 120}
        kl = dict(zip(tfs, await asyncio.gather(*(self.klines(symbol, tf, n) for tf, n in tfs.items()))))
        out: dict[str, Any] = {"symbol": symbol, "price": r(self.price(symbol) or kl["15m"][-1]["c"])}
        for tf, candles in kl.items():
            s = ind.summarize(candles)
            out[tf] = {k: (r(v) if isinstance(v, float) else ([r(x) for x in v] if isinstance(v, list) else v))
                       for k, v in s.items()}
        # Recent candles so Claude can read the price action itself.
        for tf, n in (("15m", 12), ("1h", 12), ("4h", 8)):
            out[f"candles_{tf}"] = [
                [time.strftime("%m-%d %H:%M", time.gmtime(c["t"] / 1000)), r(c["o"]), r(c["h"]), r(c["l"]),
                 r(c["c"]), r(c["qv"] / 1e6, 4)] for c in kl[tf][-n:]]
        out["candles_format"] = "[utc open time, open, high, low, close, quote volume $M]"
        m = self.marks.get(symbol, {})
        t = self.tickers.get(symbol, {})
        out["funding_pct"] = r(m.get("funding_rate", 0) * 100, 4) if m else None
        out["mark_vs_index_pct"] = r(ind.pct(m["mark"], m["index"]), 3) if m.get("index") else None
        out["24h"] = {"change_pct": r(t.get("change_pct"), 3), "high": r(t.get("high")), "low": r(t.get("low")),
                      "quote_volume_musd": r(t.get("quote_volume", 0) / 1e6, 4)} if t else None
        oi_hist, lsr, depth = await asyncio.gather(
            self._safe(self.source.open_interest_hist(symbol, "1h", 25)),
            self._safe(self.source.long_short_ratio(symbol, "1h")),
            self._safe(self.source.depth(symbol, 50)),
        )
        if oi_hist and len(oi_hist) > 1:
            out["open_interest"] = {
                "usd_m": r(oi_hist[-1]["oi_value"] / 1e6, 5),
                "change_4h_pct": r(ind.pct(oi_hist[-1]["oi"], oi_hist[-5]["oi"]), 3) if len(oi_hist) > 5 else None,
                "change_24h_pct": r(ind.pct(oi_hist[-1]["oi"], oi_hist[0]["oi"]), 3),
            }
        if lsr:
            out["long_short_accounts"] = {k: r(v, 4) for k, v in lsr.items()}
        if depth and depth["bids"] and depth["asks"]:
            price = out["price"]
            band = price * 0.005
            bid_v = sum(q * p for p, q in depth["bids"] if p >= price - band)
            ask_v = sum(q * p for p, q in depth["asks"] if p <= price + band)
            spread = depth["asks"][0][0] - depth["bids"][0][0]
            out["order_book"] = {
                "spread_pct": r(spread / price * 100, 3),
                "bid_usd_within_0_5pct": r(bid_v, 4), "ask_usd_within_0_5pct": r(ask_v, 4),
                "imbalance": r((bid_v - ask_v) / (bid_v + ask_v), 3) if bid_v + ask_v else None,
            }
        info = self.symbols[symbol]
        out["tick_size"] = str(info.tick_size)
        out["step_size"] = str(info.step_size)
        return out

    @staticmethod
    async def _safe(coro):
        try:
            return await coro
        except Exception as e:  # noqa: BLE001
            log.debug("optional market data failed: %s", e)
            return None
