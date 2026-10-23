"""Portfolio analytics: correlations, BTC beta and real exposure."""
from __future__ import annotations

import numpy as np


def log_returns(closes: list[float]) -> np.ndarray:
    a = np.asarray(closes, dtype=float)
    return np.diff(np.log(a)) if a.size > 1 else np.array([])


def correlation_matrix(closes: dict[str, list[float]], base: str = "BTCUSDT") -> dict:
    """Pearson correlation of hourly log returns, plus each symbol's beta to `base`."""
    syms = [s for s, c in closes.items() if len(c) > 10]
    if len(syms) < 2:
        return {"symbols": syms, "matrix": [], "beta": {}, "bars": 0}
    n = min(len(closes[s]) for s in syms)
    rets = np.vstack([log_returns(closes[s][-n:]) for s in syms])
    rets = np.nan_to_num(rets)
    with np.errstate(invalid="ignore", divide="ignore"):
        corr = np.corrcoef(rets)
    corr = np.nan_to_num(corr)
    beta = {}
    if base in syms:
        b = rets[syms.index(base)]
        var = float(np.var(b))
        for i, s in enumerate(syms):
            beta[s] = round(float(np.cov(rets[i], b, bias=True)[0, 1] / var), 2) if var > 0 else None
    vol = {s: round(float(np.std(rets[i]) * np.sqrt(24 * 365) * 100), 1) for i, s in enumerate(syms)}
    return {"symbols": syms, "matrix": [[round(float(x), 2) for x in row] for row in corr],
            "beta": beta, "annual_vol_pct": vol, "bars": n - 1}


def exposure(positions: list[dict], beta: dict[str, float | None], equity: float) -> dict:
    """Gross, net and BTC-beta-weighted exposure of the open book."""
    gross = net = beta_net = 0.0
    rows = []
    for p in positions:
        notional = abs(p["qty"] * p["mark_price"])
        signed = notional if p["side"] == "long" else -notional
        b = beta.get(p["symbol"])
        b = 1.0 if b is None else b
        gross += notional
        net += signed
        beta_net += signed * b
        rows.append({"symbol": p["symbol"], "side": p["side"], "notional": round(notional, 2), "beta": b})
    eq = equity or 1.0
    return {
        "gross_usd": round(gross, 2), "net_usd": round(net, 2), "btc_beta_net_usd": round(beta_net, 2),
        "gross_x_equity": round(gross / eq, 2), "btc_beta_net_x_equity": round(beta_net / eq, 2),
        "positions": rows,
        "note": ("Several same-direction alt positions are mostly one BTC bet; use btc_beta_net to judge real risk."),
    }


def crowded_pairs(matrix: dict, threshold: float = 0.8) -> list[dict]:
    syms, m = matrix.get("symbols", []), matrix.get("matrix", [])
    out = []
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            if m and m[i][j] >= threshold:
                out.append({"a": syms[i], "b": syms[j], "corr": m[i][j]})
    return sorted(out, key=lambda x: x["corr"], reverse=True)[:10]
