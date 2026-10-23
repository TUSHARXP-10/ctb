"""Technical indicators in plain Python.

A candle is a dict with keys t (open time, ms), o, h, l, c, v (base volume),
qv (quote volume). Indicator functions return lists aligned with their input,
with None where there is not enough data yet.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

Num = Optional[float]


def sma(values: Sequence[float], period: int) -> list[Num]:
    out: list[Num] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    s = sum(values[:period])
    out[period - 1] = s / period
    for i in range(period, len(values)):
        s += values[i] - values[i - period]
        out[i] = s / period
    return out


def ema(values: Sequence[float], period: int) -> list[Num]:
    """Exponential moving average, seeded with the SMA of the first `period` values."""
    out: list[Num] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes: Sequence[float], period: int = 14) -> list[Num]:
    """Wilder's RSI."""
    out: list[Num] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / period, losses / period
    out[period] = _rsi_value(avg_g, avg_l)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
        out[i] = _rsi_value(avg_g, avg_l)
    return out


def _rsi_value(avg_g: float, avg_l: float) -> float:
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    rs = avg_g / avg_l
    return 100 - 100 / (1 + rs)


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    tr = []
    for i in range(len(closes)):
        if i == 0:
            tr.append(highs[0] - lows[0])
        else:
            pc = closes[i - 1]
            tr.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))
    return tr


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> list[Num]:
    """Wilder's ATR."""
    tr = true_range(highs, lows, closes)
    out: list[Num] = [None] * len(tr)
    if len(tr) < period:
        return out
    prev = sum(tr[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(tr)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9):
    ef, es = ema(closes, fast), ema(closes, slow)
    line: list[Num] = [a - b if a is not None and b is not None else None for a, b in zip(ef, es)]
    start = next((i for i, v in enumerate(line) if v is not None), len(line))
    sig_part = ema([v for v in line[start:]], signal) if start < len(line) else []
    sig: list[Num] = [None] * start + sig_part
    hist: list[Num] = [a - b if a is not None and b is not None else None for a, b in zip(line, sig)]
    return line, sig, hist


def bollinger(closes: Sequence[float], period: int = 20, mult: float = 2.0):
    mid = sma(closes, period)
    upper: list[Num] = [None] * len(closes)
    lower: list[Num] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        m = mid[i]
        sd = math.sqrt(sum((x - m) ** 2 for x in window) / period)
        upper[i], lower[i] = m + mult * sd, m - mult * sd
    return mid, upper, lower


def adx(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int = 14) -> list[Num]:
    n = len(closes)
    out: list[Num] = [None] * n
    if n < period * 2 + 1:
        return out
    tr = true_range(highs, lows, closes)
    pdm = [0.0] * n
    ndm = [0.0] * n
    for i in range(1, n):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        pdm[i] = up if up > down and up > 0 else 0.0
        ndm[i] = down if down > up and down > 0 else 0.0
    atr_s = sum(tr[1:period + 1])
    p_s = sum(pdm[1:period + 1])
    n_s = sum(ndm[1:period + 1])
    dx: list[float] = []
    for i in range(period + 1, n):
        atr_s = atr_s - atr_s / period + tr[i]
        p_s = p_s - p_s / period + pdm[i]
        n_s = n_s - n_s / period + ndm[i]
        pdi = 100 * p_s / atr_s if atr_s else 0.0
        ndi = 100 * n_s / atr_s if atr_s else 0.0
        dx.append(100 * abs(pdi - ndi) / (pdi + ndi) if pdi + ndi else 0.0)
        if len(dx) == period:
            out[i] = sum(dx) / period
        elif len(dx) > period:
            out[i] = (out[i - 1] * (period - 1) + dx[-1]) / period  # type: ignore[operator]
    return out


def pivots(highs: Sequence[float], lows: Sequence[float], left: int = 3, right: int = 3):
    """Swing highs and lows (fractals). Returns two lists of (index, price)."""
    ph, pl = [], []
    for i in range(left, len(highs) - right):
        h = highs[i]
        if all(h >= highs[j] for j in range(i - left, i + right + 1)):
            ph.append((i, h))
        lo = lows[i]
        if all(lo <= lows[j] for j in range(i - left, i + right + 1)):
            pl.append((i, lo))
    return ph, pl


def last(values: Sequence[Num]) -> Num:
    for v in reversed(values):
        if v is not None:
            return v
    return None


def pct(a: float, b: float) -> float:
    """Percent change from b to a."""
    return (a / b - 1) * 100 if b else 0.0


def summarize(candles: list[dict]) -> dict:
    """The numbers Claude sees for one timeframe."""
    if len(candles) < 30:
        return {"bars": len(candles), "note": "not enough data"}
    o = [c["o"] for c in candles]
    h = [c["h"] for c in candles]
    lo = [c["l"] for c in candles]
    c = [c_["c"] for c_ in candles]
    v = [c_["v"] for c_ in candles]
    price = c[-1]
    e20, e50, e200 = last(ema(c, 20)), last(ema(c, 50)), last(ema(c, 200))
    r = rsi(c, 14)
    a = last(atr(h, lo, c, 14)) or 0.0
    _, _, hist = macd(c)
    mid, up, dn = bollinger(c, 20, 2.0)
    ax = last(adx(h, lo, c, 14))
    vol_avg = sum(v[-21:-1]) / 20 if len(v) > 21 else None
    e50_series = ema(c, 50)
    e50_prev = e50_series[-6] if len(e50_series) > 6 else None

    trend = "range"
    if e50 is not None and e50_prev is not None:
        slope_up, slope_dn = e50 > e50_prev, e50 < e50_prev
        above_200 = e200 is None or price > e200
        below_200 = e200 is None or price < e200
        if price > e50 and slope_up and above_200:
            trend = "uptrend"
        elif price < e50 and slope_dn and below_200:
            trend = "downtrend"

    ph, pl = pivots(h, lo)
    resist = sorted({round(p, 10) for _, p in ph if p > price})[:3]
    support = sorted({round(p, 10) for _, p in pl if p < price}, reverse=True)[:3]

    bb_u, bb_d, bb_m = up[-1], dn[-1], mid[-1]
    pctb = (price - bb_d) / (bb_u - bb_d) if bb_u is not None and bb_d is not None and bb_u != bb_d else None
    width = (bb_u - bb_d) / bb_m * 100 if bb_u is not None and bb_m else None

    return {
        "price": price,
        "change_1": round(pct(price, c[-2]), 3),
        "change_6": round(pct(price, c[-7]), 3) if len(c) > 7 else None,
        "change_24": round(pct(price, c[-25]), 3) if len(c) > 25 else None,
        "ema20": e20, "ema50": e50, "ema200": e200,
        "rsi": round(r[-1], 1) if r[-1] is not None else None,
        "rsi_prev": round(r[-2], 1) if len(r) > 1 and r[-2] is not None else None,
        "macd_hist": hist[-1],
        "macd_hist_prev": hist[-2] if len(hist) > 1 else None,
        "atr": a,
        "atr_pct": round(a / price * 100, 3) if price else None,
        "adx": round(ax, 1) if ax is not None else None,
        "bb_pctb": round(pctb, 2) if pctb is not None else None,
        "bb_width_pct": round(width, 2) if width is not None else None,
        "vol_ratio": round(v[-1] / vol_avg, 2) if vol_avg else None,
        "high_20": max(h[-21:-1]),
        "low_20": min(lo[-21:-1]),
        "trend": trend,
        "resistance": resist,
        "support": support,
        "last_open": o[-1],
    }
