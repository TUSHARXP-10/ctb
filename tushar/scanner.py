"""Chart scanner: finds setups across the most traded USDT perpetuals.

A hit is a chart that matches a rule, not a signal to trade. Claude decides.
All rules read CLOSED 1-hour candles only (the forming candle is ignored).
"""
from __future__ import annotations

import asyncio
import logging
import time

from . import indicators as ind
from .market import MarketService, closed_only, r

log = logging.getLogger("tushar.scanner")

# setup -> (direction, base score, plain description)
SETUPS = {
    "breakout_20": ("long", 3, "closed above the 20-bar high on rising volume"),
    "breakdown_20": ("short", 3, "closed below the 20-bar low on rising volume"),
    "bull_flag": ("long", 3, "strong push up, then a tight 5-bar pause"),
    "bear_flag": ("short", 3, "strong drop, then a tight 5-bar pause"),
    "trend_pullback_long": ("long", 3, "uptrend; dipped to the 20/50 EMA and closed back above the 20"),
    "trend_pullback_short": ("short", 3, "downtrend; bounced to the 20/50 EMA and closed back below the 20"),
    "ema50_reclaim": ("long", 2, "closed back above the 50 EMA"),
    "ema50_loss": ("short", 2, "closed back below the 50 EMA"),
    "oversold_bounce": ("long", 2, "RSI was under 30 and the last candle closed green"),
    "overbought_reject": ("short", 2, "RSI was over 70 and the last candle closed red"),
    "oversold": ("watch", 1, "RSI under 25"),
    "overbought": ("watch", 1, "RSI over 75"),
    "squeeze": ("watch", 1, "Bollinger width at a 100-bar low: a big move is loading"),
    "volume_spike": ("move", 2, "volume over 3x average with a big candle"),
    "crowded_longs": ("short", 1, "funding above 0.05% per 8h: longs are paying up"),
    "crowded_shorts": ("long", 1, "funding below -0.03% per 8h: shorts are paying up"),
}


def detect(candles: list[dict], funding_rate: float | None = None) -> list[dict]:
    """Run every rule on one symbol's closed 1h candles."""
    if len(candles) < 60:
        return []
    o = [c["o"] for c in candles]
    h = [c["h"] for c in candles]
    lo = [c["l"] for c in candles]
    c = [x["c"] for x in candles]
    v = [x["v"] for x in candles]
    e20, e50, e200 = ind.ema(c, 20), ind.ema(c, 50), ind.ema(c, 200)
    rs = ind.rsi(c, 14)
    _, bu, bd = ind.bollinger(c, 20, 2.0)
    vol_avg = sum(v[-21:-1]) / 20
    vol_ratio = v[-1] / vol_avg if vol_avg else 0
    price = c[-1]
    hits: list[str] = []

    if price > max(h[-21:-1]) and vol_ratio > 1.2:
        hits.append("breakout_20")
    if price < min(lo[-21:-1]) and vol_ratio > 1.2:
        hits.append("breakdown_20")

    impulse = ind.pct(c[-6], c[-16])
    flag_range = (max(h[-5:]) - min(lo[-5:])) / price * 100
    if impulse > 4 and flag_range < impulse * 0.4 and c[-1] > c[-16]:
        hits.append("bull_flag")
    if impulse < -4 and flag_range < -impulse * 0.4 and c[-1] < c[-16]:
        hits.append("bear_flag")

    if e20[-1] and e50[-1] and e200[-1]:
        touched_long = any(lo[i] <= max(e20[i], e50[i]) * 1.002 for i in range(-3, 0))
        touched_short = any(h[i] >= min(e20[i], e50[i]) * 0.998 for i in range(-3, 0))
        if e50[-1] > e200[-1] and price > e200[-1] and touched_long and price > e20[-1] and 40 <= (rs[-1] or 0) <= 60:
            hits.append("trend_pullback_long")
        if e50[-1] < e200[-1] and price < e200[-1] and touched_short and price < e20[-1] and 40 <= (rs[-1] or 100) <= 60:
            hits.append("trend_pullback_short")
    if e50[-2] and e50[-1]:
        if c[-2] < e50[-2] and c[-1] > e50[-1]:
            hits.append("ema50_reclaim")
        if c[-2] > e50[-2] and c[-1] < e50[-1]:
            hits.append("ema50_loss")
    if rs[-2] is not None and rs[-1] is not None:
        if rs[-2] < 30 and c[-1] > o[-1]:
            hits.append("oversold_bounce")
        if rs[-2] > 70 and c[-1] < o[-1]:
            hits.append("overbought_reject")
        if rs[-1] < 25:
            hits.append("oversold")
        if rs[-1] > 75:
            hits.append("overbought")
    widths = [(u - d) / ((u + d) / 2) for u, d in zip(bu[-100:], bd[-100:]) if u is not None and d is not None]
    if len(widths) >= 60 and widths[-1] <= min(widths) * 1.0001:
        hits.append("squeeze")
    if vol_ratio > 3 and abs(ind.pct(c[-1], o[-1])) > 1.5:
        hits.append("volume_spike")
    if funding_rate is not None:
        if funding_rate > 0.0005:
            hits.append("crowded_longs")
        elif funding_rate < -0.0003:
            hits.append("crowded_shorts")

    out = []
    trend = "up" if e50[-1] and e200[-1] and e50[-1] > e200[-1] else "down"
    for name in hits:
        direction, score, desc = SETUPS[name]
        if direction == "move":
            direction = "long" if c[-1] > o[-1] else "short"
        aligned = (direction == "long" and trend == "up") or (direction == "short" and trend == "down")
        out.append({
            "setup": name, "direction": direction, "description": desc,
            "score": score + (1 if aligned else 0) + (1 if vol_ratio > 2 else 0),
            "trend_1h": trend,
        })
    return out


class Scanner:
    def __init__(self, market: MarketService, settings):
        self.market = market
        self.settings = settings
        self.last: dict = {"as_of": None, "hits": [], "scanned": 0, "failed": 0}
        self._lock = asyncio.Lock()

    async def scan(self, max_age: float = 300) -> dict:
        async with self._lock:
            if self.last["as_of"] and time.time() - self.last["as_of"] < max_age:
                return self.last
            universe = self.market.universe()
            sem = asyncio.Semaphore(6)
            failed = 0

            async def one(sym: str) -> list[dict]:
                nonlocal failed
                async with sem:
                    try:
                        candles = closed_only(await self.market.klines(sym, "1h", 220))
                    except Exception as e:  # noqa: BLE001
                        failed += 1
                        log.debug("scan %s failed: %s", sym, e)
                        return []
                fr = (self.market.marks.get(sym) or {}).get("funding_rate")
                found = detect(candles, fr)
                t = self.market.tickers.get(sym, {})
                for f in found:
                    f.update({"symbol": sym, "price": r(candles[-1]["c"]),
                              "chg_24h": r(t.get("change_pct"), 3),
                              "vol_24h_musd": r(t.get("quote_volume", 0) / 1e6, 4)})
                return found

            results = await asyncio.gather(*(one(s) for s in universe))
            hits = [h for group in results for h in group]
            hits.sort(key=lambda x: (x["score"], x.get("vol_24h_musd") or 0), reverse=True)
            self.last = {"as_of": time.time(), "hits": hits, "scanned": len(universe), "failed": failed}
            return self.last
