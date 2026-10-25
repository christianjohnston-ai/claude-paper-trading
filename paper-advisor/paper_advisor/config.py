"""Versioned, prospective experiment configuration (risk limits and simulation assumptions).

A configuration change never edits an existing version: it creates version N+1 whose
effective_from is the moment it was made. Every decision, risk check and order records the
config_version_id that applied, so earlier results keep the rules they were made under.

Commission and slippage values are SIMULATION ASSUMPTIONS chosen for conservatism, not verified
broker economics (the brief's default: $0 commission, 10 bps adverse slippage per side).

SYNC WARNING: DEFAULTS mirrors the "Default configuration" table in README.md and the risk settings
form in dashboard/pages.py. Add new keys to all three.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from zoneinfo import ZoneInfo

from . import clock
from . import money as m

DEFAULTS: dict = {
    "min_order_usd": "5.00",
    "max_position_pct": "0.25",
    "min_cash_reserve_pct": "0.10",
    "max_open_positions": 5,
    "max_new_orders_per_session": 3,
    "commission_per_order_usd": "0.00",
    "commission_bps": "0",
    "slippage_bps": "10",
    "qty_decimals": 6,
    "bar_finalization_minutes": 60,
    "decision_open_buffer_seconds": 120,
    "session_data_wait_days": 5,
    "require_exit_rule_on_buy": True,
    "max_stop_loss_pct": "0.50",
    "benchmark_symbol": "SPY",
    "display_timezone": "America/Los_Angeles",
    "market_timezone": "America/New_York",
    "lock_ttl_minutes": 180,
    "backup_keep": 60,
    "deposits_withdrawals_enabled": False,
    "order_type": "MOO_NEXT_ELIGIBLE_SESSION",
    "rule_evaluation": "CLOSE_CONFIRMED_NEXT_OPEN",
}

EDITABLE_KEYS = ("min_order_usd", "max_position_pct", "min_cash_reserve_pct", "max_open_positions",
                 "max_new_orders_per_session", "commission_per_order_usd", "commission_bps", "slippage_bps",
                 "require_exit_rule_on_buy", "max_stop_loss_pct")


class ConfigError(ValueError):
    pass


def _dec_in(params, key, lo, hi, lo_inclusive=True, hi_inclusive=True) -> str:
    try:
        v = Decimal(str(params[key]))
    except Exception as exc:
        raise ConfigError(f"{key} must be a decimal number") from exc
    if not v.is_finite() or (v < lo if lo_inclusive else v <= lo) or (v > hi if hi_inclusive else v >= hi):
        raise ConfigError(f"{key}={params[key]} is out of range")
    return str(v)


def _int_in(params, key, lo, hi) -> int:
    v = params[key]
    if isinstance(v, bool) or not isinstance(v, int):
        try:
            v = int(str(v))
        except Exception as exc:
            raise ConfigError(f"{key} must be an integer") from exc
    if v < lo or v > hi:
        raise ConfigError(f"{key}={v} is out of range [{lo}, {hi}]")
    return int(v)


def _bool(params, key) -> bool:
    v = params[key]
    if isinstance(v, bool):
        return v
    if str(v).lower() in ("true", "1", "yes", "on"):
        return True
    if str(v).lower() in ("false", "0", "no", "off"):
        return False
    raise ConfigError(f"{key} must be true/false")


def normalize(params: dict) -> dict:
    unknown = set(params) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"unknown config keys: {sorted(unknown)}")
    p = {**DEFAULTS, **params}
    out = dict(p)
    try:
        if m.parse_usd(p["min_order_usd"]) < 1:
            raise ConfigError("min_order_usd must be at least $0.01")
        out["min_order_usd"] = m.cents_to_str(m.parse_usd(p["min_order_usd"]))
        out["commission_per_order_usd"] = m.cents_to_str(m.parse_usd(p["commission_per_order_usd"]))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    out["max_position_pct"] = _dec_in(p, "max_position_pct", Decimal(0), Decimal(1), lo_inclusive=False)
    out["min_cash_reserve_pct"] = _dec_in(p, "min_cash_reserve_pct", Decimal(0), Decimal(1), hi_inclusive=False)
    out["max_open_positions"] = _int_in(p, "max_open_positions", 1, 100)
    out["max_new_orders_per_session"] = _int_in(p, "max_new_orders_per_session", 1, 50)
    out["commission_bps"] = _dec_in(p, "commission_bps", Decimal(0), Decimal(1000))
    out["slippage_bps"] = _dec_in(p, "slippage_bps", Decimal(0), Decimal(1000))
    out["qty_decimals"] = _int_in(p, "qty_decimals", 6, 6)  # fixed: the schema stores micro-shares
    out["bar_finalization_minutes"] = _int_in(p, "bar_finalization_minutes", 15, 600)
    out["decision_open_buffer_seconds"] = _int_in(p, "decision_open_buffer_seconds", 0, 3600)
    out["session_data_wait_days"] = _int_in(p, "session_data_wait_days", 1, 30)
    out["require_exit_rule_on_buy"] = _bool(p, "require_exit_rule_on_buy")
    out["max_stop_loss_pct"] = _dec_in(p, "max_stop_loss_pct", Decimal(0), Decimal(1), lo_inclusive=False, hi_inclusive=False)
    out["lock_ttl_minutes"] = _int_in(p, "lock_ttl_minutes", 10, 1440)
    out["backup_keep"] = _int_in(p, "backup_keep", 1, 10000)
    if _bool(p, "deposits_withdrawals_enabled"):
        raise ConfigError("deposits/withdrawals are disabled in v1 (returns are not cash-flow-weighted yet)")
    out["deposits_withdrawals_enabled"] = False
    if not str(p["benchmark_symbol"]).strip():
        raise ConfigError("benchmark_symbol is required")
    for tz_key in ("display_timezone", "market_timezone"):
        try:
            ZoneInfo(str(p[tz_key]))
        except Exception as exc:
            raise ConfigError(f"{tz_key} is not a valid timezone") from exc
    if p["market_timezone"] != "America/New_York":
        raise ConfigError("market_timezone must be America/New_York (NYSE sessions)")
    if p["order_type"] != DEFAULTS["order_type"] or p["rule_evaluation"] != DEFAULTS["rule_evaluation"]:
        raise ConfigError("order_type and rule_evaluation are fixed in v1")
    return out


def min_fillable_order_cents(params: dict) -> int:
    """The smallest BUY notional that can actually fill under these limits.

    broker_sim pays commission out of the order (budget = notional - per-order - ceil(notional*bps/1e4)) and
    expires a BUY whose cost is below the minimum order; with prices under $10,000 the cost equals the budget.
    So the smallest fillable notional n* solves n - ceil(n*bps/1e4) >= min_order + per_order, i.e.
    n* = ceil((min_order + per_order) / (1 - bps/1e4)). With the default $0 commission it is the minimum order.
    SYNC WARNING: mirrors broker_sim._fill (budget, below_minimum_at_fill) and submission's min_order check;
    dashboard/static/app.js repeats this arithmetic exactly (integers) for the live warning in the risk form.
    """
    p = normalize(params)
    floor_cents = m.parse_usd(p["min_order_usd"]) + m.parse_usd(p["commission_per_order_usd"])
    bps = Decimal(p["commission_bps"])
    with localcontext() as ctx:
        ctx.prec = 50
        return int((Decimal(floor_cents) / (1 - bps / Decimal(10_000))).to_integral_value(rounding=ROUND_CEILING))


def min_tradable_capital_cents(params: dict) -> int:
    """The smallest equity with which ANY buy can pass the validator and fill (empty account, nothing pending).

    A buy needs n* <= floor(equity * max_position_pct) and equity - n* >= ceil(equity * min_cash_reserve_pct)
    (submission._buy_checks); both sides only grow with equity, so the answer is
    max(ceil(n* / max_position_pct), ceil(n* / (1 - min_cash_reserve_pct))). Defaults: $5 / 0.25 = $20.00.
    Below it an experiment could never trade while its benchmark invests, which would make every
    comparison meaningless; experiments.create refuses it and the dashboard checks it while typing.
    """
    p = normalize(params)
    n_star = Decimal(min_fillable_order_cents(p))
    with localcontext() as ctx:
        ctx.prec = 50
        by_position = (n_star / Decimal(p["max_position_pct"])).to_integral_value(rounding=ROUND_CEILING)
        by_reserve = (n_star / (1 - Decimal(p["min_cash_reserve_pct"]))).to_integral_value(rounding=ROUND_CEILING)
    return int(max(by_position, by_reserve))


def tradability_fixes(params: dict, equity_cents: int) -> dict:
    """The one-field changes that would let an account of this equity place a buy again ({} when it already can).

    Offered next to a "no buy can pass" warning (Rule 19: the screen computes the fix). Each value is the nearest
    one for that field alone, and a field is offered only if it binds:
      cap  = floor(equity * max_position_pct), room = equity - ceil(equity * min_cash_reserve_pct), n* = smallest fillable buy
      min_order_usd        largest M with n*(M) <= min(cap, room): M = floor(min(cap, room) * (1 - bps/1e4)) - per_order
      max_position_pct     smallest P (2 decimals) with floor(equity * P) >= n*  (only if the reserve leaves room for n*)
      min_cash_reserve_pct largest R (2 decimals) with equity - ceil(equity * R) >= n*  (only if the cap allows n*)
    SYNC WARNING: dashboard/static/app.js tradabilityFixes() repeats this arithmetic exactly (BigInt).
    """
    p = normalize(params)
    n_star = min_fillable_order_cents(p)
    pos, res = Decimal(p["max_position_pct"]), Decimal(p["min_cash_reserve_pct"])
    bps = Decimal(p["commission_bps"])
    with localcontext() as ctx:
        ctx.prec = 50
        cap = int((Decimal(equity_cents) * pos).to_integral_value(rounding=ROUND_FLOOR))
        room = equity_cents - int((Decimal(equity_cents) * res).to_integral_value(rounding=ROUND_CEILING))
        if cap >= n_star and room >= n_star:
            return {}
        out = {}
        m_max = int((Decimal(min(cap, room)) * (1 - bps / Decimal(10_000))).to_integral_value(rounding=ROUND_FLOOR)) \
            - m.parse_usd(p["commission_per_order_usd"])
        if m_max >= 1:
            out["min_order_usd"] = m.cents_to_str(m_max)
        if cap < n_star <= room:
            p_min = (Decimal(n_star) * 100 / Decimal(equity_cents)).to_integral_value(rounding=ROUND_CEILING) / 100
            if p_min <= 1:
                out["max_position_pct"] = f"{p_min:.2f}"
        if room < n_star <= cap:
            r_max = (Decimal(equity_cents - n_star) * 100 / Decimal(equity_cents)).to_integral_value(rounding=ROUND_FLOOR) / 100
            if r_max >= 0:
                out["min_cash_reserve_pct"] = f"{r_max:.2f}"
    return out


@dataclass(frozen=True)
class RiskConfig:
    min_order_cents: int
    max_position_pct: Decimal
    min_cash_reserve_pct: Decimal
    max_open_positions: int
    max_new_orders_per_session: int
    commission_per_order_cents: int
    commission_bps: Decimal
    slippage_bps: Decimal
    qty_decimals: int
    bar_finalization_minutes: int
    decision_open_buffer_seconds: int
    session_data_wait_days: int
    require_exit_rule_on_buy: bool
    max_stop_loss_pct: Decimal
    benchmark_symbol: str
    display_timezone: str
    market_timezone: str
    lock_ttl_minutes: int
    backup_keep: int
    deposits_withdrawals_enabled: bool
    params: dict

    @classmethod
    def from_params(cls, params: dict) -> "RiskConfig":
        p = normalize(params)
        return cls(
            min_order_cents=m.parse_usd(p["min_order_usd"]), max_position_pct=Decimal(p["max_position_pct"]),
            min_cash_reserve_pct=Decimal(p["min_cash_reserve_pct"]), max_open_positions=p["max_open_positions"],
            max_new_orders_per_session=p["max_new_orders_per_session"],
            commission_per_order_cents=m.parse_usd(p["commission_per_order_usd"]), commission_bps=Decimal(p["commission_bps"]),
            slippage_bps=Decimal(p["slippage_bps"]), qty_decimals=p["qty_decimals"],
            bar_finalization_minutes=p["bar_finalization_minutes"], decision_open_buffer_seconds=p["decision_open_buffer_seconds"],
            session_data_wait_days=p["session_data_wait_days"], require_exit_rule_on_buy=p["require_exit_rule_on_buy"],
            max_stop_loss_pct=Decimal(p["max_stop_loss_pct"]), benchmark_symbol=p["benchmark_symbol"],
            display_timezone=p["display_timezone"], market_timezone=p["market_timezone"],
            lock_ttl_minutes=p["lock_ttl_minutes"], backup_keep=p["backup_keep"],
            deposits_withdrawals_enabled=p["deposits_withdrawals_enabled"], params=p)


def _sha(params: dict) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()


def insert_version(conn, experiment_id: int, params: dict, *, effective_from: str, created_by: str, reason: str, now: str) -> int:
    p = normalize(params)
    version = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM config_versions WHERE experiment_id=?",
                           (experiment_id,)).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO config_versions(experiment_id, version, params_json, params_sha256, effective_from, created_at, created_by, reason)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (experiment_id, version, json.dumps(p, sort_keys=True), _sha(p), effective_from, now, created_by, reason))
    return int(cur.lastrowid)


def create_version(conn, experiment_id: int, overrides: dict, *, created_by: str, reason: str, now: str) -> int:
    """New version = current params + overrides, effective immediately (prospectively)."""
    if not reason or not reason.strip():
        raise ConfigError("a reason is required for every configuration change")
    bad = set(overrides) - set(EDITABLE_KEYS)
    if bad:
        raise ConfigError(f"not editable after creation: {sorted(bad)} (create a new experiment instead)")
    row = current_version(conn, experiment_id, now)
    params = {**json.loads(row["params_json"]), **overrides}
    return insert_version(conn, experiment_id, params, effective_from=now, created_by=created_by, reason=reason, now=now)


def current_version(conn, experiment_id: int, at_iso: str | None = None):
    at_iso = at_iso or clock.now_iso()
    row = conn.execute("SELECT * FROM config_versions WHERE experiment_id=? AND effective_from<=? ORDER BY version DESC LIMIT 1",
                       (experiment_id, at_iso)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM config_versions WHERE experiment_id=? ORDER BY version LIMIT 1", (experiment_id,)).fetchone()
    if row is None:
        raise ConfigError(f"experiment {experiment_id} has no configuration")
    return row


def current(conn, experiment_id: int, at_iso: str | None = None) -> tuple[int, RiskConfig]:
    row = current_version(conn, experiment_id, at_iso)
    return int(row["id"]), RiskConfig.from_params(json.loads(row["params_json"]))


def by_id(conn, version_id: int) -> RiskConfig:
    row = conn.execute("SELECT * FROM config_versions WHERE id=?", (version_id,)).fetchone()
    return RiskConfig.from_params(json.loads(row["params_json"]))
