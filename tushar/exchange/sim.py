"""Simulated market: synthetic but realistic-looking futures prices.

For running Tushar.ai with no network (MARKET_SOURCE=sim) and for tests.
Prices follow a correlated random walk with trending regimes, volatility
clustering and the occasional jump. Everything shown while this source is on
is labelled SIMULATED MARKET in the dashboard. It is a test harness, never a
stand-in for real market data.
"""
from __future__ import annotations

import math
import random
import time
from decimal import Decimal

from .base import SymbolInfo

BAR_MS = 300_000  # 5-minute base bars
INTERVAL_MS = {"1m": 300_000, "3m": 300_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
               "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
               "12h": 43_200_000, "1d": 86_400_000}

# symbol: (start price, daily vol, beta to the market factor, tick, step)
SIM_SYMBOLS = {
    "BTCUSDT": (67_000.0, 0.025, 1.0, "0.10", "0.001"),
    "ETHUSDT": (2_600.0, 0.034, 1.15, "0.01", "0.001"),
    "SOLUSDT": (150.0, 0.048, 1.3, "0.010", "1"),
    "BNBUSDT": (590.0, 0.026, 0.8, "0.010", "0.01"),
    "XRPUSDT": (0.58, 0.04, 1.0, "0.0001", "0.1"),
    "DOGEUSDT": (0.14, 0.055, 1.25, "0.00001", "1"),
    "ADAUSDT": (0.42, 0.045, 1.1, "0.0001", "1"),
    "AVAXUSDT": (28.0, 0.05, 1.25, "0.001", "1"),
    "LINKUSDT": (13.5, 0.045, 1.15, "0.001", "0.01"),
    "SUIUSDT": (1.9, 0.065, 1.4, "0.0001", "0.1"),
    "LTCUSDT": (72.0, 0.035, 0.9, "0.01", "0.001"),
    "TONUSDT": (5.4, 0.04, 0.6, "0.0001", "0.1"),
}


class SimMarket:
    def __init__(self, seed: int = 7, history_bars: int = 10_000, now_ms: int | None = None):
        self.rng = random.Random(seed)
        self.live_rng = random.Random(seed + 1)
        self.history_bars = history_bars
        self.bars: dict[str, list[dict]] = {}
        self.live: dict[str, dict] = {}
        self.funding: dict[str, float] = {}
        self._frozen_now = now_ms
        self._generate()

    # ---------------------------------------------------------------- clock
    def _now_ms(self) -> int:
        return self._frozen_now if self._frozen_now is not None else int(time.time() * 1000)

    # ------------------------------------------------------------ generation
    def _generate(self) -> None:
        now = self._now_ms()
        cur_open = now // BAR_MS * BAR_MS
        start = cur_open - self.history_bars * BAR_MS
        n = self.history_bars
        rng = self.rng
        # Market factor with trending regimes and volatility clustering.
        market = []
        drift, vol_mult = 0.0, 1.0
        for i in range(n):
            if rng.random() < 1 / 400:
                drift = rng.choice([-1, 0, 0, 1]) * rng.uniform(0.00002, 0.00008)
            vol_mult = max(0.4, min(3.0, vol_mult * 0.98 + 0.02 * abs(rng.gauss(1.0, 0.6))))
            hour = ((start + i * BAR_MS) // 3_600_000) % 24
            season = 1.25 if 13 <= hour <= 17 else (0.8 if 2 <= hour <= 6 else 1.0)
            shock = rng.gauss(0, 1) * vol_mult * season
            if rng.random() < 1 / 1500:
                shock += rng.choice([-1, 1]) * rng.uniform(4, 9)
            market.append((drift, shock))
        for sym, (p0, dvol, beta, _, _) in SIM_SYMBOLS.items():
            bvol = dvol / math.sqrt(288)
            price = p0 * math.exp(rng.gauss(0, 0.15))
            bars = []
            idio_drift = 0.0
            for i in range(n):
                if rng.random() < 1 / 600:
                    idio_drift = rng.gauss(0, 0.00012)
                d, shock = market[i]
                ret = beta * d + idio_drift + bvol * (0.75 * beta * shock / max(beta, 0.5) + 0.65 * rng.gauss(0, 1))
                bars.append(self._make_bar(start + i * BAR_MS, price, ret, bvol, p0, rng))
                price = bars[-1]["c"]
            self.bars[sym] = bars
            self.funding[sym] = rng.uniform(-0.0002, 0.0004)
            last = bars[-1]["c"]
            self.live[sym] = {"t": cur_open, "o": last, "h": last, "l": last, "c": last, "v": 0.0,
                              "qv": 0.0, "updated": now, "vol": bvol}

    @staticmethod
    def _make_bar(t: int, price: float, ret: float, bvol: float, p0: float, rng: random.Random) -> dict:
        o = price
        steps = 5
        path = [o]
        p = o
        for _ in range(steps):
            p *= math.exp(ret / steps + rng.gauss(0, bvol * 0.35))
            path.append(p)
        c = path[-1]
        h = max(path) * (1 + abs(rng.gauss(0, bvol * 0.25)))
        lo = min(path) * (1 - abs(rng.gauss(0, bvol * 0.25)))
        base_vol = 2_000_000 / p0
        v = base_vol * (1 + abs(ret) / max(bvol, 1e-9) * 0.8) * math.exp(rng.gauss(0, 0.35))
        return {"t": t, "o": o, "h": h, "l": lo, "c": c, "v": v, "qv": v * c, "ct": t + BAR_MS - 1}

    def _advance(self) -> None:
        """Roll the live bar forward to the current time."""
        now = self._now_ms()
        for sym, lv in self.live.items():
            bvol = lv["vol"]
            elapsed = max(0.0, (now - lv["updated"]) / 1000)
            # Close out finished bars (also catches up after the bot was asleep).
            while now >= lv["t"] + BAR_MS:
                end_elapsed = max(0.0, (lv["t"] + BAR_MS - lv["updated"]) / 1000)
                self._walk(lv, bvol, end_elapsed)
                bar = {k: lv[k] for k in ("t", "o", "h", "l", "c", "v", "qv")}
                bar["ct"] = bar["t"] + BAR_MS - 1
                if bar["v"] == 0:
                    bar["v"] = 1500_000 / max(bar["c"], 1e-9)
                    bar["qv"] = bar["v"] * bar["c"]
                self.bars[sym].append(bar)
                if len(self.bars[sym]) > self.history_bars + 2000:
                    self.bars[sym] = self.bars[sym][-self.history_bars:]
                c = bar["c"]
                lv.update({"t": lv["t"] + BAR_MS, "o": c, "h": c, "l": c, "c": c, "v": 0.0, "qv": 0.0,
                           "updated": lv["t"] + BAR_MS})
                elapsed = max(0.0, (now - lv["updated"]) / 1000)
            self._walk(lv, bvol, elapsed)
            lv["updated"] = now

    def _walk(self, lv: dict, bvol: float, seconds: float) -> None:
        if seconds <= 0:
            return
        sigma = bvol * math.sqrt(seconds / 300)
        c = lv["c"] * math.exp(self.live_rng.gauss(0, sigma))
        lv["c"] = c
        lv["h"] = max(lv["h"], c)
        lv["l"] = min(lv["l"], c)
        dv = (1_200_000 / c) * seconds / 300 * math.exp(self.live_rng.gauss(0, 0.3))
        lv["v"] += dv
        lv["qv"] += dv * c

    # ------------------------------------------------------------- interface
    async def close(self) -> None:
        return None

    def price(self, symbol: str) -> float | None:
        lv = self.live.get(symbol)
        return lv["c"] if lv else None

    async def exchange_info(self, force: bool = False) -> dict[str, SymbolInfo]:
        out = {}
        for sym, (_, _, _, tick, step) in SIM_SYMBOLS.items():
            out[sym] = SymbolInfo(symbol=sym, base=sym[:-4], quote="USDT", tick_size=Decimal(tick),
                                  step_size=Decimal(step), min_qty=Decimal(step), max_qty=Decimal("10000000"),
                                  min_notional=Decimal("5"))
        return out

    async def tickers_24h(self) -> list[dict]:
        self._advance()
        out = []
        for sym, bars in self.bars.items():
            lv = self.live[sym]
            day = bars[-287:]
            first = day[0]["o"]
            hi = max(max(b["h"] for b in day), lv["h"])
            lo = min(min(b["l"] for b in day), lv["l"])
            qv = sum(b["qv"] for b in day) + lv["qv"]
            out.append({"symbol": sym, "last": lv["c"], "change_pct": (lv["c"] / first - 1) * 100,
                        "high": hi, "low": lo, "quote_volume": qv})
        return out

    async def mark_prices(self) -> dict[str, dict]:
        self._advance()
        now = self._now_ms()
        nft = (now // 28_800_000 + 1) * 28_800_000
        return {s: {"mark": lv["c"], "index": lv["c"], "funding_rate": self.funding[s], "next_funding_time": nft}
                for s, lv in self.live.items()}

    async def klines(self, symbol: str, interval: str, limit: int = 300, end_time: int | None = None) -> list[dict]:
        self._advance()
        if symbol not in self.bars:
            raise KeyError(symbol)
        ims = INTERVAL_MS.get(interval, BAR_MS)
        lv = self.live[symbol]
        base = self.bars[symbol] + [dict(lv, ct=lv["t"] + BAR_MS - 1)]
        if end_time is not None:
            base = [b for b in base if b["t"] <= end_time]
        need = limit * (ims // BAR_MS) + ims // BAR_MS
        base = base[-need:]
        out: list[dict] = []
        for b in base:
            bucket = b["t"] // ims * ims
            if out and out[-1]["t"] == bucket:
                agg = out[-1]
                agg["h"] = max(agg["h"], b["h"])
                agg["l"] = min(agg["l"], b["l"])
                agg["c"] = b["c"]
                agg["v"] += b["v"]
                agg["qv"] += b["qv"]
            else:
                out.append({"t": bucket, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"],
                            "qv": b["qv"], "ct": bucket + ims - 1})
        return out[-limit:]

    async def depth(self, symbol: str, limit: int = 20) -> dict:
        p = self.price(symbol) or 1.0
        tick = float(SIM_SYMBOLS[symbol][3])
        r = random.Random(int(p * 1000) % 10_000)
        bias = r.uniform(0.7, 1.3)
        bids = [(p - tick * (i + 1) * 3, r.uniform(0.5, 3) * 20_000 / p * bias) for i in range(limit)]
        asks = [(p + tick * (i + 1) * 3, r.uniform(0.5, 3) * 20_000 / p) for i in range(limit)]
        return {"bids": bids, "asks": asks}

    async def open_interest(self, symbol: str) -> float | None:
        p = self.price(symbol) or 1.0
        return 3e8 / p

    async def open_interest_hist(self, symbol: str, period: str = "1h", limit: int = 24) -> list[dict] | None:
        kl = await self.klines(symbol, "1h", limit)
        oi = 3e8
        out = []
        for k in kl:
            oi *= 1 + (k["c"] / k["o"] - 1) * 0.6
            out.append({"t": k["t"], "oi": oi / k["c"], "oi_value": oi})
        return out

    async def long_short_ratio(self, symbol: str, period: str = "1h") -> dict | None:
        kl = await self.klines(symbol, "1h", 24)
        chg = kl[-1]["c"] / kl[0]["o"] - 1
        long_pct = max(30.0, min(75.0, 52 + chg * 300))
        return {"ratio": long_pct / (100 - long_pct), "long_pct": long_pct, "short_pct": 100 - long_pct}
