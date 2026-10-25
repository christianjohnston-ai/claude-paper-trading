"""Performance measurement: portfolio vs. keeping cash vs. a passive SPY benchmark.

Honesty rules encoded here:
- Returns are net of modeled costs (fills already include slippage and commission).
- No external cash flows exist in v1 (deposits/withdrawals disabled), so total return =
  equity / starting capital - 1 is also the time-weighted return. Cash-flow-aware returns must be
  implemented before deposits are ever enabled.
- Nothing is annualized before 252 sessions; a few days annualized would be meaningless.
- Every statistic carries its sample size and an explicit insufficient-sample flag. Win rate and
  profit factor are shown from the first closed trade but labeled as not meaningful below
  MIN_TRADES_FOR_STATS; profit factor is undefined (None) when there are no losing trades.
- The SPY benchmark is invested at the same first eligible open as the portfolio could be, with the
  same slippage/commission model and the same dividend treatment (cash on ex-date, not reinvested).
- Provisional snapshots are counted and surfaced, never hidden.
"""
from __future__ import annotations

import json
from decimal import Decimal

from . import accounting, experiments, valuation

MIN_SESSIONS_FOR_STATS = 20
MIN_TRADES_FOR_STATS = 10
ANNUALIZE_AFTER_SESSIONS = 252


def _series(snaps, start_cents):
    out, peak, prev = [], start_cents, start_cents
    for s in snaps:
        e = s["equity_cents"]
        peak = max(peak, e)
        out.append({"date": s["session_date"], "equity_cents": e, "cash_cents": s["cash_cents"], "invested_cents": s["invested_cents"],
                    "daily_return": float(Decimal(e) / Decimal(prev) - 1) if prev else None,
                    "drawdown": float(Decimal(e) / Decimal(peak) - 1) if peak else 0.0,
                    "exposure": float(Decimal(s["invested_cents"]) / Decimal(e)) if e else 0.0,
                    "provisional": bool(s["provisional"]), "provisional_reasons": json.loads(s["provisional_reasons_json"])})
        prev = e
    return out


def compute(conn, exp_id: int) -> dict:
    exp = experiments.get(conn, exp_id)
    start = int(exp["starting_capital_cents"])
    port_snaps = valuation.snapshots(conn, exp_id, "PORTFOLIO")
    bench_snaps = valuation.snapshots(conn, exp_id, "BENCHMARK")
    port = _series(port_snaps, start)
    bench = _series(bench_snaps, start)
    st = accounting.load_state(conn, exp_id, "PORTFOLIO")
    last = port_snaps[-1] if port_snaps else None
    n = len(port)

    def total(series):
        return None if not series else series[-1]["equity_cents"] / start - 1

    trades = st.closed_trades
    wins = [t.realized_cents + t.dividends_cents for t in trades if t.realized_cents + t.dividends_cents > 0]
    losses = [t.realized_cents + t.dividends_cents for t in trades if t.realized_cents + t.dividends_cents < 0]
    avg_equity = sum(p["equity_cents"] for p in port) / n if n else start
    turnover = (last["traded_notional_cents"] / avg_equity) if (last and avg_equity) else 0.0
    port_ret, bench_ret = total(port), total(bench)
    common = min(len(port), len(bench))
    excess = None
    if common:
        excess = port[common - 1]["equity_cents"] / start - bench[common - 1]["equity_cents"] / start
    return {
        "experiment_id": exp_id,
        "starting_capital_cents": start,
        "inception_at": exp["inception_at"],
        "first_investable_session": exp["benchmark_start_session"],
        "sessions_valued": n,
        "as_of_session": last["session_date"] if last else None,
        "cash_cents": last["cash_cents"] if last else st.cash_cents,
        "invested_cents": last["invested_cents"] if last else 0,
        "equity_cents": last["equity_cents"] if last else st.cash_cents,
        "realized_pnl_cents": last["realized_pnl_cents"] if last else 0,
        "unrealized_pnl_cents": last["unrealized_pnl_cents"] if last else 0,
        "dividends_cents": last["dividends_cents"] if last else 0,
        "fees_cents": last["fees_cents"] if last else 0,
        "total_pnl_cents": (last["equity_cents"] - start) if last else 0,
        "total_return": port_ret,
        "spy_benchmark_return": bench_ret,
        "cash_benchmark_return": 0.0,
        "excess_vs_spy": excess,
        "max_drawdown": min((p["drawdown"] for p in port), default=0.0),
        "current_drawdown": port[-1]["drawdown"] if port else 0.0,
        "spy_max_drawdown": min((p["drawdown"] for p in bench), default=0.0),
        "avg_exposure": (sum(p["exposure"] for p in port) / n) if n else 0.0,
        "turnover": turnover,
        "closed_trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": (len(wins) / len(trades)) if trades else None,
        "avg_win_cents": (sum(wins) / len(wins)) if wins else None,
        "avg_loss_cents": (sum(losses) / len(losses)) if losses else None,
        "profit_factor": (sum(wins) / -sum(losses)) if losses else None,
        "provisional_sessions": sum(1 for p in port if p["provisional"]),
        "annualized_return": None,
        # Said in words: the dashboard renders the report that quotes these notes ("6 sessions < 252" read as notation).
        "annualization_note": (f"Not annualized: {n} of the {ANNUALIZE_AFTER_SESSIONS} sessions needed"
                               if n < ANNUALIZE_AFTER_SESSIONS else ""),
        "insufficient_sample": n < MIN_SESSIONS_FOR_STATS or len(trades) < MIN_TRADES_FOR_STATS,
        "sample_note": (f"{n} valued session{'' if n == 1 else 's'} and {len(trades)} closed trade{'' if len(trades) == 1 else 's'}: "
                        "far too few to infer skill; treat every number as a description of what happened, not evidence of an edge"),
        "series": port,
        "benchmark_series": bench,
        "trades": [{"symbol": t.symbol, "opened_at": t.opened_at, "closed_at": t.closed_at, "entry_session": t.entry_session,
                    "exit_session": t.exit_session, "cost_cents": t.cost_cents, "proceeds_cents": t.proceeds_cents,
                    "realized_cents": t.realized_cents, "dividends_cents": t.dividends_cents, "fees_cents": t.fees_cents,
                    "buy_fill_ids": t.buy_fill_ids, "sell_fill_ids": t.sell_fill_ids} for t in trades],
    }
