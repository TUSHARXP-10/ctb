"""Liquidation radar: every forced liquidation on Binance USD-M futures, live.

Listens to the public `!forceOrder@arr` stream (no key needed). A SELL
liquidation order means a LONG was wiped out; a BUY means a SHORT was. Big
liquidation cascades mark local tops/bottoms and fuel squeezes, so the
team's flow analyst and the head trader both read this.

With MARKET_SOURCE=sim the radar invents liquidations from the simulated
price moves (labelled sim) so the pipeline can be tested offline.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque

log = logging.getLogger("tushar.radar")

STREAM_URL = "wss://fstream.binance.com/ws/!forceOrder@arr"
BIG_USD = 100_000  # prints at or above this are pushed to the dashboard one by one


def parse_force_order(msg: dict) -> dict | None:
    """Binance forceOrder event -> liquidation record."""
    o = msg.get("o") if isinstance(msg, dict) else None
    if not o:
        return None
    try:
        price = float(o.get("ap") or o.get("p") or 0)
        qty = float(o.get("z") or o.get("q") or 0)
    except (TypeError, ValueError):
        return None
    if price <= 0 or qty <= 0:
        return None
    return {
        "ts": int(o.get("T") or msg.get("E") or time.time() * 1000) / 1000,
        "symbol": o.get("s", ""),
        "side": "long" if o.get("S") == "SELL" else "short",   # which side got liquidated
        "price": price, "qty": qty, "usd": price * qty,
    }


class LiquidationRadar:
    def __init__(self, bus=None, url: str = STREAM_URL, keep_seconds: int = 6 * 3600):
        self.bus = bus
        self.url = url
        self.keep = keep_seconds
        self.events: deque[dict] = deque(maxlen=50_000)
        self.status = "starting"
        self.connected_at: float | None = None
        self._pending: list[dict] = []

    def add(self, ev: dict) -> None:
        self.events.append(ev)
        cutoff = time.time() - self.keep
        while self.events and self.events[0]["ts"] < cutoff:
            self.events.popleft()
        if ev["usd"] >= BIG_USD:
            self._pending.append(ev)

    # ------------------------------------------------------------- views
    def summary(self, minutes: float = 60, top: int = 8) -> dict:
        cutoff = time.time() - minutes * 60
        evs = [e for e in self.events if e["ts"] >= cutoff]
        longs = sum(e["usd"] for e in evs if e["side"] == "long")
        shorts = sum(e["usd"] for e in evs if e["side"] == "short")
        by_sym: dict[str, dict] = {}
        for e in evs:
            b = by_sym.setdefault(e["symbol"], {"symbol": e["symbol"], "longs_usd": 0.0, "shorts_usd": 0.0, "count": 0})
            b["longs_usd" if e["side"] == "long" else "shorts_usd"] += e["usd"]
            b["count"] += 1
        ranked = sorted(by_sym.values(), key=lambda b: b["longs_usd"] + b["shorts_usd"], reverse=True)[:top]
        for b in ranked:
            b["longs_usd"], b["shorts_usd"] = round(b["longs_usd"]), round(b["shorts_usd"])
        biggest = sorted(evs, key=lambda e: e["usd"], reverse=True)[:top]
        dominant = "longs" if longs > shorts * 1.5 else "shorts" if shorts > longs * 1.5 else "balanced"
        return {
            "window_minutes": minutes, "status": self.status, "count": len(evs),
            "total_usd": round(longs + shorts), "longs_liquidated_usd": round(longs),
            "shorts_liquidated_usd": round(shorts), "dominant": dominant,
            "by_symbol": ranked,
            "biggest": [{"symbol": e["symbol"], "side": e["side"], "usd": round(e["usd"]), "price": e["price"],
                         "minutes_ago": round((time.time() - e["ts"]) / 60, 1)} for e in biggest],
        }

    def symbol_summary(self, symbol: str, minutes: float = 60) -> dict:
        cutoff = time.time() - minutes * 60
        evs = [e for e in self.events if e["ts"] >= cutoff and e["symbol"] == symbol]
        return {"longs_usd": round(sum(e["usd"] for e in evs if e["side"] == "long")),
                "shorts_usd": round(sum(e["usd"] for e in evs if e["side"] == "short")), "count": len(evs)}

    def recent(self, n: int = 40) -> list[dict]:
        return [dict(e) for e in list(self.events)[-n:]][::-1]

    def buckets(self, minutes: int = 120, size: int = 5) -> list[dict]:
        """Liquidations per `size`-minute bucket, for the dashboard bar chart."""
        now = time.time()
        n = minutes // size
        out = [{"t": now - (n - i) * size * 60, "longs": 0.0, "shorts": 0.0} for i in range(n)]
        start = now - minutes * 60
        for e in self.events:
            if e["ts"] < start:
                continue
            i = min(n - 1, int((e["ts"] - start) // (size * 60)))
            out[i]["longs" if e["side"] == "long" else "shorts"] += e["usd"]
        return out

    # ------------------------------------------------------------- runners
    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(2)
            if self._pending and self.bus:
                batch, self._pending = self._pending[-20:], []
                self.bus.publish("liquidations", {"events": batch, "summary": self.summary(60, 5)})

    async def run(self) -> None:
        """Connect to Binance and keep listening, reconnecting with backoff."""
        try:
            import websockets
        except ImportError:
            self.status = "disabled (pip install websockets)"
            return
        flusher = asyncio.create_task(self._flush_loop())
        delay = 2
        try:
            while True:
                try:
                    async with websockets.connect(self.url, open_timeout=15, ping_interval=20,
                                                  ping_timeout=20, close_timeout=5) as ws:
                        self.status = "live"
                        self.connected_at = time.time()
                        delay = 2
                        async for raw in ws:
                            try:
                                ev = parse_force_order(json.loads(raw))
                            except ValueError:
                                continue
                            if ev:
                                self.add(ev)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    self.status = f"reconnecting ({type(e).__name__})"
                    log.debug("liquidation stream: %s", e)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120)
        finally:
            flusher.cancel()

    async def run_sim(self, market) -> None:
        """Synthetic liquidations from simulated price moves (offline test mode)."""
        self.status = "sim"
        rng = random.Random(11)
        flusher = asyncio.create_task(self._flush_loop())
        try:
            while True:
                await asyncio.sleep(1.5)
                for sym in list(market.symbols)[:12]:
                    mv = market.move_pct(sym, 5) or 0.0
                    p = market.price(sym)
                    if not p:
                        continue
                    intensity = 0.08 + min(abs(mv), 3) * 0.4
                    if rng.random() < intensity:
                        side = "long" if (mv < 0 if rng.random() < 0.8 else mv > 0) else "short"
                        usd = rng.lognormvariate(9.5, 1.3) * (3 if sym == "BTCUSDT" else 1)
                        self.add({"ts": time.time(), "symbol": sym, "side": side, "price": p, "qty": usd / p,
                                  "usd": usd, "sim": True})
        finally:
            flusher.cancel()
