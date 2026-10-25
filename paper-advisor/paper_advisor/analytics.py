"""Descriptive price indicators for the morning briefing (research inputs, never accounting).

Computed from stored as-traded bars, split-adjusted to the as-of date with OUR recorded splits so the
series is continuous. Dividends are not adjusted out (price-return series). Only bars on or before
the last completed session are used, so no indicator can include a partial or future bar.
Floats are acceptable here: these numbers inform judgment; they never move cash or shares.
"""
from __future__ import annotations

import math
from decimal import Decimal

from . import corporate_actions, prices


def adjusted_series(conn, symbol: str, through: str, limit: int = 300) -> list[dict]:
    bars = prices.history(conn, symbol, through=through, limit=limit)
    splits = [s for s in corporate_actions.for_symbol(conn, symbol) if s["action_type"] == "SPLIT" and s["ex_date"] <= through]
    out = []
    for b in bars:
        f = Decimal(1)
        for s in splits:
            if s["ex_date"] > b["session_date"]:
                f *= Decimal(s["ratio_den"]) / Decimal(s["ratio_num"])
        out.append({"date": b["session_date"], "open": float(Decimal(b["open"]) * f), "high": float(Decimal(b["high"]) * f),
                    "low": float(Decimal(b["low"]) * f), "close": float(Decimal(b["close"]) * f),
                    "volume": float(b["volume"]) / float(f) if f else 0.0})
    return out


def _sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def _ret(xs, n):
    return (xs[-1] / xs[-1 - n] - 1) if len(xs) > n and xs[-1 - n] else None


def _rsi(closes, n=14):
    if len(closes) <= n:
        return None
    gains, losses = [], []
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        ch = b - a
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    ag, al = sum(gains) / n, sum(losses) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def indicators(series: list[dict]) -> dict:
    if not series:
        return {"bars": 0}
    c = [x["close"] for x in series]
    rets = [b / a - 1 for a, b in zip(c[:-1], c[1:]) if a]
    vol20 = None
    if len(rets) >= 20:
        r = rets[-20:]
        mu = sum(r) / 20
        vol20 = math.sqrt(sum((x - mu) ** 2 for x in r) / 19) * math.sqrt(252)
    trs = []
    for prev, cur in zip(series[-15:-1], series[-14:]):
        trs.append(max(cur["high"] - cur["low"], abs(cur["high"] - prev["close"]), abs(cur["low"] - prev["close"])))
    atr = sum(trs) / len(trs) if len(trs) == 14 else None
    window = series[-252:]
    hi = max(x["high"] for x in window)
    lo = min(x["low"] for x in window)
    sma50, sma200 = _sma(c, 50), _sma(c, 200)
    vols = [x["volume"] for x in series]
    v20, v60 = _sma(vols, 20), _sma(vols, 60)
    rnd = lambda x, k=4: None if x is None else round(x, k)
    return {
        "bars": len(series), "last_date": series[-1]["date"], "last_close": round(c[-1], 4),
        "ret_1d": rnd(_ret(c, 1)), "ret_5d": rnd(_ret(c, 5)), "ret_20d": rnd(_ret(c, 20)), "ret_60d": rnd(_ret(c, 60)),
        "ret_252d": rnd(_ret(c, 252)), "sma20": rnd(_sma(c, 20), 2), "sma50": rnd(sma50, 2), "sma200": rnd(sma200, 2),
        "pct_vs_sma50": rnd(c[-1] / sma50 - 1 if sma50 else None), "pct_vs_sma200": rnd(c[-1] / sma200 - 1 if sma200 else None),
        "high_252d": round(hi, 2), "pct_from_252d_high": rnd(c[-1] / hi - 1), "low_252d": round(lo, 2),
        "vol_20d_annualized": rnd(vol20), "atr14_pct": rnd(atr / c[-1] if atr else None), "rsi14": rnd(_rsi(c), 1),
        "volume_ratio_20_60": rnd(v20 / v60 if v20 and v60 else None, 2),
        "window_note": f"{len(window)} bars in 52-week window" + ("" if len(window) >= 250 else " (shorter than a year: history limited)"),
    }
