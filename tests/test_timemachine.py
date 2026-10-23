import math
import random

from tushar import timemachine as tm


def walk(n=3000, drift=0.0, vol=0.002, seed=1, start=100.0):
    rng = random.Random(seed)
    out, p = [], start
    for i in range(n):
        o = p
        p *= math.exp(drift + rng.gauss(0, vol))
        h, lo = max(o, p) * (1 + abs(rng.gauss(0, vol / 3))), min(o, p) * (1 - abs(rng.gauss(0, vol / 3)))
        out.append({"t": 1_700_000_000_000 + i * 300_000, "o": o, "h": h, "l": lo, "c": p, "v": 1.0, "qv": 1.0})
    return out


def test_validation():
    c = walk()
    assert not tm.run(c, tm.Plan("X", "long", 100, 101, 105))["ok"]
    assert not tm.run(c, tm.Plan("X", "short", 100, 99, 95))["ok"]
    assert "3 days" in tm.run(c[:100], tm.Plan("X", "long", 100, 99, 102))["error"]


def test_deterministic_with_seed():
    c = walk()
    p = c[-1]["c"]
    plan = tm.Plan("X", "long", p, p * 0.99, p * 1.02, 12)
    a = tm.run(c, plan, replays=2000, seed=19)
    b = tm.run(c, plan, replays=2000, seed=19)
    assert a["mc"]["target_first_pct"] == b["mc"]["target_first_pct"]
    assert a["mc"]["avg_r"] == b["mc"]["avg_r"]
    total = a["mc"]["target_first_pct"] + a["mc"]["stop_first_pct"] + a["mc"]["neither_pct"]
    assert abs(total - 100) < 0.3


def test_drift_shows_up_in_odds():
    up = walk(drift=0.0006)
    p = up[-1]["c"]
    long_res = tm.run(up, tm.Plan("X", "long", p, p * 0.98, p * 1.02, 24), replays=2000)
    short_res = tm.run(up, tm.Plan("X", "short", p, p * 1.02, p * 0.98, 24), replays=2000)
    assert long_res["mc"]["target_first_pct"] > short_res["mc"]["target_first_pct"]
    assert long_res["history"]["target"] > long_res["history"]["stop"]


def test_fan_chart_shape():
    c = walk()
    p = c[-1]["c"]
    res = tm.run(c, tm.Plan("X", "long", p, p * 0.99, p * 1.02, 6), replays=1000)
    fan = res["mc"]["fan"]
    assert len(fan["hours"]) == len(fan["p50"]) == len(fan["p5"])
    assert all(a <= b for a, b in zip(fan["p5"], fan["p95"]))
    assert "fan" not in tm.brief(res)["mc"]


def test_verdict_thresholds():
    assert tm.verdict_line(50, 50, 1.0).startswith("Close to a coin flip")
    assert tm.verdict_line(33, 67, 2.0).startswith("Close to break-even")
    assert tm.verdict_line(45, 55, 2.0).startswith("Leans toward the target")
    assert tm.verdict_line(70, 30, 1.0).startswith("Strongly toward the target")
    assert tm.verdict_line(20, 80, 1.0).startswith("Strongly toward the stop")
    assert tm.verdict_line(0, 0, 1.0).startswith("Undecided")
