"""Time Machine: replay a trade 10,000 times before risking a dollar.

Inspired by the day-trader-skill Time Machine, adapted to 24/7 crypto futures:

1. The pool. Every closed 5-minute candle of the last ~10 days becomes four
   ratios to the candle before it (open, high, low, close over previous close),
   filed under its time-of-day slot (288 slots a day).
2. One replay. Start at the entry. For each 5 minutes of the horizon, draw a
   past candle from the same time of day (plus or minus `window` slots) and
   scale it to the current price. Check the stop first, then the target. A
   candle that touches both counts as the stop.
3. Many replays give the odds: target first, stop first, or neither (still
   open at the horizon), plus the spread of where price ends up.
4. The history. The same stop and target distances are re-run from every past
   candle in the data: what really happened, not a simulation.
5. The verdict weighs the target's R: at 2R the target only needs to come first
   in 1 of 3 decided replays (break-even = 1 / (1 + R)).

It is a simulation from the symbol's own recent candles, not a prediction.
Seeded, so the same inputs give the same numbers.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SLOTS = 288
BAR_MS = 300_000


@dataclass
class Plan:
    symbol: str
    side: str          # long | short
    entry: float
    stop: float
    target: float
    horizon_hours: float = 24.0

    def validate(self) -> str | None:
        if self.side not in ("long", "short"):
            return "side must be long or short"
        if min(self.entry, self.stop, self.target) <= 0:
            return "prices must be positive"
        if self.side == "long" and not (self.stop < self.entry < self.target):
            return "for a long, stop < entry < target"
        if self.side == "short" and not (self.target < self.entry < self.stop):
            return "for a short, target < entry < stop"
        if not 0.25 <= self.horizon_hours <= 168:
            return "horizon must be between 0.25 and 168 hours"
        return None

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def target_r(self) -> float:
        return abs(self.target - self.entry) / self.risk


def _ratios(candles: list[dict]):
    o = np.array([c["o"] for c in candles], dtype=float)
    h = np.array([c["h"] for c in candles], dtype=float)
    lo = np.array([c["l"] for c in candles], dtype=float)
    c = np.array([c_["c"] for c_ in candles], dtype=float)
    t = np.array([c_["t"] for c_ in candles], dtype=np.int64)
    pc = c[:-1]
    return (h[1:] / pc, lo[1:] / pc, c[1:] / pc, ((t[1:] // BAR_MS) % SLOTS).astype(int), o, h, lo, c)


def verdict_line(p_target: float, p_stop: float, target_r: float) -> str:
    decided = p_target + p_stop
    if decided <= 0:
        return "Undecided: almost no replay reached the stop or the target in time."
    share = p_target / decided * 100
    breakeven = 100 / (1 + target_r)
    diff = share - breakeven
    if abs(diff) < 5:
        return ("Close to a coin flip" if abs(target_r - 1) < 0.05 else "Close to break-even") + \
            f": target first in {share:.1f}% of decided replays, break-even is {breakeven:.1f}%."
    lean = "target" if diff > 0 else "stop"
    strength = "Strongly toward" if abs(diff) >= 15 else "Leans toward"
    return f"{strength} the {lean}: target first in {share:.1f}% of decided replays vs {breakeven:.1f}% break-even."


def run(candles_5m: list[dict], plan: Plan, replays: int = 10_000, seed: int = 19, window: int = 2,
        fee_rate_roundtrip: float = 0.001, history_step: int = 3) -> dict:
    """Run the Monte Carlo and the history check. `candles_5m` must be closed 5-minute candles."""
    err = plan.validate()
    if err:
        return {"ok": False, "error": err}
    if len(candles_5m) < SLOTS * 3:
        return {"ok": False, "error": f"need at least 3 days of 5m candles, got {len(candles_5m)}"}

    hr, lr, cr, slots, o, h, lo, c = _ratios(candles_5m)
    steps = max(1, int(round(plan.horizon_hours * 12)))
    is_long = plan.side == "long"

    # ----- pool by time-of-day slot (+/- window) -----
    by_slot = [np.flatnonzero(slots == s) for s in range(SLOTS)]
    start_slot = (int(candles_5m[-1]["t"]) // BAR_MS + 1) % SLOTS
    rng = np.random.default_rng(seed)
    idx = np.empty((replays, steps), dtype=np.int64)
    for s in range(steps):
        slot = (start_slot + s) % SLOTS
        cands = np.concatenate([by_slot[(slot + d) % SLOTS] for d in range(-window, window + 1)])
        if cands.size == 0:
            cands = np.arange(cr.size)
        idx[:, s] = cands[rng.integers(0, cands.size, size=replays)]

    c_ratio = cr[idx]
    growth = np.cumprod(c_ratio, axis=1)
    prev = plan.entry * np.concatenate([np.ones((replays, 1)), growth[:, :-1]], axis=1)
    highs = prev * hr[idx]
    lows = prev * lr[idx]
    closes = prev * c_ratio

    if is_long:
        stop_hit, tgt_hit = lows <= plan.stop, highs >= plan.target
    else:
        stop_hit, tgt_hit = highs >= plan.stop, lows <= plan.target
    first_stop = np.where(stop_hit.any(axis=1), stop_hit.argmax(axis=1), steps)
    first_tgt = np.where(tgt_hit.any(axis=1), tgt_hit.argmax(axis=1), steps)
    stop_first = (first_stop <= first_tgt) & (first_stop < steps)
    tgt_first = (first_tgt < first_stop)
    neither = ~(stop_first | tgt_first)

    final = closes[:, -1]
    direction = 1 if is_long else -1
    risk = plan.risk
    target_r = plan.target_r
    r_mult = np.where(tgt_first, target_r, np.where(stop_first, -1.0, (final - plan.entry) * direction / risk))
    fee_r = plan.entry * fee_rate_roundtrip / risk
    green = tgt_first | (neither & ((final - plan.entry) * direction > 0))

    # ----- fan chart bands (downsampled) -----
    pts = np.unique(np.linspace(0, steps - 1, num=min(steps, 48)).astype(int))
    bands = {f"p{q}": [float(x) for x in np.percentile(closes[:, pts], q, axis=0)] for q in (5, 25, 50, 75, 95)}
    hours = [float((i + 1) / 12) for i in pts]
    sample_rows = rng.choice(replays, size=min(30, replays), replace=False)
    samples = [[float(x) for x in closes[i, pts]] for i in sample_rows]

    def pct(mask) -> float:
        return float(mask.mean() * 100)

    ttt = first_tgt[tgt_first]
    tts = first_stop[stop_first]
    mc = {
        "replays": replays, "seed": seed, "steps": steps,
        "target_first_pct": round(pct(tgt_first), 1),
        "stop_first_pct": round(pct(stop_first), 1),
        "neither_pct": round(pct(neither), 1),
        "green_pct": round(pct(green), 1),
        "avg_r": round(float(r_mult.mean()), 3),
        "avg_r_after_fees": round(float(r_mult.mean() - fee_r), 3),
        "fee_r": round(float(fee_r), 3),
        "median_hours_to_target": round(float(np.median(ttt) + 1) / 12, 2) if ttt.size else None,
        "median_hours_to_stop": round(float(np.median(tts) + 1) / 12, 2) if tts.size else None,
        "final_price": {"p5": float(np.percentile(final, 5)), "median": float(np.median(final)),
                        "p95": float(np.percentile(final, 95))},
        "fan": {"hours": hours, **bands, "samples": samples},
    }

    # ----- history: same distances from every past candle -----
    stop_dist = risk / plan.entry
    tgt_dist = abs(plan.target - plan.entry) / plan.entry
    n = len(c)
    starts = np.arange(0, n - steps - 1, history_step)
    hist = {"tested": 0, "target": 0, "stop": 0, "neither": 0, "avg_r": None}
    if starts.size:
        from numpy.lib.stride_tricks import sliding_window_view
        hw = sliding_window_view(h[1:], steps)[starts]
        lw = sliding_window_view(lo[1:], steps)[starts]
        base = c[starts][:, None]
        if is_long:
            s_hit = lw <= base * (1 - stop_dist)
            t_hit = hw >= base * (1 + tgt_dist)
        else:
            s_hit = hw >= base * (1 + stop_dist)
            t_hit = lw <= base * (1 - tgt_dist)
        fs = np.where(s_hit.any(axis=1), s_hit.argmax(axis=1), steps)
        ft = np.where(t_hit.any(axis=1), t_hit.argmax(axis=1), steps)
        hs = (fs <= ft) & (fs < steps)
        ht = ft < fs
        hn = ~(hs | ht)
        end = c[starts + steps]
        hr_mult = np.where(ht, target_r, np.where(hs, -1.0, (end / c[starts] - 1) * direction / stop_dist))
        hist = {"tested": int(starts.size), "target": int(ht.sum()), "stop": int(hs.sum()),
                "neither": int(hn.sum()), "avg_r": round(float(hr_mult.mean()), 3),
                "target_pct": round(float(ht.mean() * 100), 1), "stop_pct": round(float(hs.mean() * 100), 1)}

    lines = [verdict_line(mc["target_first_pct"], mc["stop_first_pct"], target_r)]
    if mc["neither_pct"] > 40:
        lines.append(f"{mc['neither_pct']}% of replays hit neither in {plan.horizon_hours:g}h; "
                     "the price at the horizon decides those.")
    if mc["avg_r_after_fees"] < 0 < mc["avg_r"]:
        lines.append("Fees turn the average R negative at this stop distance.")
    if hist["tested"] and hist.get("avg_r") is not None and (hist["avg_r"] > 0) != (mc["avg_r"] > 0):
        lines.append("The history and the replays disagree on the sign of the average R.")

    return {
        "ok": True,
        "plan": {"symbol": plan.symbol, "side": plan.side, "entry": plan.entry, "stop": plan.stop,
                 "target": plan.target, "horizon_hours": plan.horizon_hours, "target_r": round(target_r, 2),
                 "stop_pct": round(stop_dist * 100, 3), "target_pct": round(tgt_dist * 100, 3)},
        "data": {"candles": len(candles_5m), "days": round(len(candles_5m) / SLOTS, 1)},
        "mc": mc,
        "history": hist,
        "verdict": lines[0],
        "notes": lines[1:],
        "disclaimer": "Simulation from this symbol's own recent 5m candles. Not a prediction.",
    }


def brief(result: dict) -> dict:
    """The result without the chart arrays, for Claude."""
    if not result.get("ok"):
        return result
    mc = {k: v for k, v in result["mc"].items() if k != "fan"}
    return {**{k: v for k, v in result.items() if k != "mc"}, "mc": mc}
