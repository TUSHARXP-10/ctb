from tushar import indicators as ind


def test_ema_of_constant_is_constant():
    out = ind.ema([5.0] * 30, 10)
    assert out[8] is None and out[9] == 5.0 and out[-1] == 5.0


def test_sma_window():
    assert ind.sma([1, 2, 3, 4, 5], 3)[2:] == [2, 3, 4]


def test_rsi_extremes():
    up = [float(i) for i in range(1, 40)]
    assert ind.rsi(up, 14)[-1] == 100.0
    flat = [10.0] * 40
    assert ind.rsi(flat, 14)[-1] == 50.0
    down = list(reversed(up))
    assert ind.rsi(down, 14)[-1] < 1


def test_rsi_wilder_known_value():
    # Classic Wilder example (stockcharts): first RSI on these closes is ~70.53
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28,
              46.28, 46.00, 46.03, 46.41, 46.22, 45.64]
    r = ind.rsi(closes, 14)
    assert abs(r[14] - 70.53) < 0.1


def test_atr_constant_range():
    h = [11.0] * 30
    lo = [9.0] * 30
    c = [10.0] * 30
    assert abs(ind.atr(h, lo, c, 14)[-1] - 2.0) < 1e-9


def test_macd_flat_is_zero():
    line, sig, hist = ind.macd([100.0] * 60)
    assert abs(line[-1]) < 1e-9 and abs(sig[-1]) < 1e-9 and abs(hist[-1]) < 1e-9


def test_summarize_trend_detection():
    candles = []
    p = 100.0
    for i in range(250):
        p *= 1.002
        candles.append({"t": i * 3_600_000, "o": p / 1.001, "h": p * 1.003, "l": p / 1.004, "c": p, "v": 100 + i, "qv": 1})
    s = ind.summarize(candles)
    assert s["trend"] == "uptrend"
    assert s["rsi"] > 60
    assert s["ema20"] > s["ema50"] > s["ema200"]


def test_pivots():
    highs = [1, 2, 3, 9, 3, 2, 1, 2, 3, 2, 1]
    lows = [h - 0.5 for h in highs]
    ph, pl = ind.pivots(highs, lows, 3, 3)
    assert (3, 9) in ph
