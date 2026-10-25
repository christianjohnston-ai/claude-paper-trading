"""NYSE regular-session calendar (holidays, early closes, session open/close instants).

Why this module exists: every timing guarantee in the simulator (a fill may only use a session that
opened strictly after the decision; a daily bar is only final after its session closed) depends on
knowing exactly when sessions open and close. A wrong holiday would let an order "fill" on a day the
market was shut, or let a bar be treated as final before it was.

Sources:
- OFFICIAL_* tables for 2025-2027 were transcribed from https://www.nyse.com/markets/hours-calendars
  (retrieved 2026-10-24). Sessions in those years are marked verified=True.
- Other years use the NYSE observance rules below and are marked verified=False so the UI can say
  "calendar rule-derived, not verified against an official NYSE publication". tests/
  test_market_calendar.py proves the rules reproduce the official 2025-2027 table exactly.
- Unscheduled closures (national days of mourning, storms) cannot be predicted. Known ones live in
  SPECIAL_CLOSURES; new ones are added at runtime from the `calendar_overrides` table via
  set_extra_closures(). Until someone records such a closure, an order targeting that day simply
  stays pending because no bar exists (see broker_sim), which is the safe failure.

Times: regular session 09:30-16:00 America/New_York; early close 13:00. All returned instants are
timezone-aware UTC datetimes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
OPEN_TIME = time(9, 30)
CLOSE_TIME = time(16, 0)
EARLY_CLOSE_TIME = time(13, 0)
OFFICIAL_SOURCE = "https://www.nyse.com/markets/hours-calendars (retrieved 2026-10-24)"

OFFICIAL_HOLIDAYS: dict[int, dict[date, str]] = {
    2025: {
        date(2025, 1, 1): "New Year's Day", date(2025, 1, 20): "Martin Luther King, Jr. Day",
        date(2025, 2, 17): "Washington's Birthday", date(2025, 4, 18): "Good Friday",
        date(2025, 5, 26): "Memorial Day", date(2025, 6, 19): "Juneteenth National Independence Day",
        date(2025, 7, 4): "Independence Day", date(2025, 9, 1): "Labor Day",
        date(2025, 11, 27): "Thanksgiving Day", date(2025, 12, 25): "Christmas Day",
    },
    2026: {
        date(2026, 1, 1): "New Year's Day", date(2026, 1, 19): "Martin Luther King, Jr. Day",
        date(2026, 2, 16): "Washington's Birthday", date(2026, 4, 3): "Good Friday",
        date(2026, 5, 25): "Memorial Day", date(2026, 6, 19): "Juneteenth National Independence Day",
        date(2026, 7, 3): "Independence Day (observed)", date(2026, 9, 7): "Labor Day",
        date(2026, 11, 26): "Thanksgiving Day", date(2026, 12, 25): "Christmas Day",
    },
    2027: {
        date(2027, 1, 1): "New Year's Day", date(2027, 1, 18): "Martin Luther King, Jr. Day",
        date(2027, 2, 15): "Washington's Birthday", date(2027, 3, 26): "Good Friday",
        date(2027, 5, 31): "Memorial Day", date(2027, 6, 18): "Juneteenth National Independence Day (observed)",
        date(2027, 7, 5): "Independence Day (observed)", date(2027, 9, 6): "Labor Day",
        date(2027, 11, 25): "Thanksgiving Day", date(2027, 12, 24): "Christmas Day (observed)",
    },
}
OFFICIAL_EARLY_CLOSES: dict[int, dict[date, time]] = {
    2025: {date(2025, 7, 3): EARLY_CLOSE_TIME, date(2025, 11, 28): EARLY_CLOSE_TIME, date(2025, 12, 24): EARLY_CLOSE_TIME},
    2026: {date(2026, 11, 27): EARLY_CLOSE_TIME, date(2026, 12, 24): EARLY_CLOSE_TIME},
    2027: {date(2027, 11, 26): EARLY_CLOSE_TIME},
}
SPECIAL_CLOSURES: dict[date, str] = {
    date(2012, 10, 29): "Hurricane Sandy",
    date(2012, 10, 30): "Hurricane Sandy",
    date(2018, 12, 5): "National Day of Mourning (President George H.W. Bush)",
    date(2025, 1, 9): "National Day of Mourning (President Jimmy Carter)",
}
_EXTRA_CLOSURES: dict[date, str] = {}


def set_extra_closures(closures: dict[date, str]) -> None:
    """Install runtime closures recorded by the operator (calendar_overrides table)."""
    _EXTRA_CLOSURES.clear()
    _EXTRA_CLOSURES.update(closures)


@dataclass(frozen=True)
class Session:
    date: date
    open_utc: datetime
    close_utc: datetime
    early_close: bool
    verified: bool

    @property
    def open_ny(self) -> datetime:
        return self.open_utc.astimezone(NY)

    @property
    def close_ny(self) -> datetime:
        return self.close_utc.astimezone(NY)


def _easter(year: int) -> date:
    # Anonymous Gregorian algorithm (Meeus/Jones/Butcher).
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def rule_holidays(year: int) -> dict[date, str]:
    h: dict[date, str] = {}
    ny = date(year, 1, 1)
    # NYSE does not close on Friday Dec 31 when Jan 1 falls on a Saturday (year-end books stay open).
    if ny.weekday() == 6:
        h[ny + timedelta(days=1)] = "New Year's Day (observed)"
    elif ny.weekday() != 5:
        h[ny] = "New Year's Day"
    h[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King, Jr. Day"
    h[_nth_weekday(year, 2, 0, 3)] = "Washington's Birthday"
    h[_easter(year) - timedelta(days=2)] = "Good Friday"
    h[_last_weekday(year, 5, 0)] = "Memorial Day"
    if year >= 2022:
        j = date(year, 6, 19)
        h[_observed(j)] = "Juneteenth National Independence Day" + (" (observed)" if _observed(j) != j else "")
    i = date(year, 7, 4)
    h[_observed(i)] = "Independence Day" + (" (observed)" if _observed(i) != i else "")
    h[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    h[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving Day"
    x = date(year, 12, 25)
    h[_observed(x)] = "Christmas Day" + (" (observed)" if _observed(x) != x else "")
    return h


def rule_early_closes(year: int) -> dict[date, time]:
    hol = rule_holidays(year)
    out: dict[date, time] = {}
    out[_nth_weekday(year, 11, 3, 4) + timedelta(days=1)] = EARLY_CLOSE_TIME  # day after Thanksgiving
    for d in (date(year, 7, 3), date(year, 12, 24)):
        # July 3 / Dec 24 close early only when they are Mon-Thu trading days (a Friday Dec 24 or
        # July 3 is the observed holiday itself when the 25th/4th falls on a Saturday).
        if d.weekday() <= 3 and d not in hol:
            out[d] = EARLY_CLOSE_TIME
    return out


def holidays(year: int) -> dict[date, str]:
    return dict(OFFICIAL_HOLIDAYS.get(year) or rule_holidays(year))


def early_closes(year: int) -> dict[date, time]:
    return dict(OFFICIAL_EARLY_CLOSES[year] if year in OFFICIAL_EARLY_CLOSES else rule_early_closes(year))


def closure_reason(d: date) -> str | None:
    if d.weekday() >= 5:
        return "Weekend"
    if d in _EXTRA_CLOSURES:
        return _EXTRA_CLOSURES[d]
    if d in SPECIAL_CLOSURES:
        return SPECIAL_CLOSURES[d]
    return holidays(d.year).get(d)


def session(d: date) -> Session | None:
    if closure_reason(d) is not None:
        return None
    close_t = early_closes(d.year).get(d, CLOSE_TIME)
    return Session(
        date=d,
        open_utc=datetime.combine(d, OPEN_TIME, tzinfo=NY).astimezone(UTC),
        close_utc=datetime.combine(d, close_t, tzinfo=NY).astimezone(UTC),
        early_close=close_t != CLOSE_TIME,
        verified=d.year in OFFICIAL_HOLIDAYS,
    )


def is_session(d: date) -> bool:
    return session(d) is not None


def _require_aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return ts.astimezone(UTC)


def ny_date(ts: datetime) -> date:
    return _require_aware(ts).astimezone(NY).date()


def next_session_after(ts: datetime) -> Session:
    """First session whose OPEN is strictly after ts. This is the earliest session an order decided
    at ts may fill in (market-on-open). Exactly-at-the-open is not eligible: the decision could have
    seen the opening print."""
    ts = _require_aware(ts)
    d = ny_date(ts)
    for _ in range(30):
        s = session(d)
        if s is not None and s.open_utc > ts:
            return s
        d += timedelta(days=1)
    raise RuntimeError("no session found within 30 days; calendar is inconsistent")


def last_completed_session(ts: datetime, finalization_minutes: int) -> Session | None:
    """Latest session whose close + finalization buffer is at or before ts. Daily bars for that
    session are treated as final; anything later is an in-progress or not-yet-settled bar."""
    ts = _require_aware(ts)
    d = ny_date(ts)
    for _ in range(30):
        s = session(d)
        if s is not None and s.close_utc + timedelta(minutes=finalization_minutes) <= ts:
            return s
        d -= timedelta(days=1)
    return None


def previous_session(d: date) -> Session | None:
    x = d - timedelta(days=1)
    for _ in range(30):
        s = session(x)
        if s is not None:
            return s
        x -= timedelta(days=1)
    return None


def following_session(d: date) -> Session:
    x = d + timedelta(days=1)
    for _ in range(30):
        s = session(x)
        if s is not None:
            return s
        x += timedelta(days=1)
    raise RuntimeError("no following session within 30 days")


def sessions_between(start: date, end: date) -> list[Session]:
    out, d = [], start
    while d <= end:
        s = session(d)
        if s is not None:
            out.append(s)
        d += timedelta(days=1)
    return out


def count_sessions_after(after: date, through: date) -> int:
    """Number of sessions with after < date <= through."""
    if through <= after:
        return 0
    return len(sessions_between(after + timedelta(days=1), through))


def state_words(status: dict) -> str:
    """market_status()'s state as the owner reads it, in the dashboard and in the run reports alike ("closed weekend"
    and "pre open" were the stored codes with their underscores swapped for spaces)."""
    st = status["state"]
    if st == "closed_holiday":
        return f"closed ({status.get('holiday_name') or 'holiday'})"
    return {"pre_open": "before the open", "open": "open (regular session)", "after_close": "after the close",
            "closed_weekend": "closed (weekend)"}.get(st, st.replace("_", " "))


def market_status(ts: datetime) -> dict:
    ts = _require_aware(ts)
    d = ny_date(ts)
    s = session(d)
    nxt = next_session_after(ts)
    base = {
        "now_utc": ts.isoformat(),
        "ny_time": ts.astimezone(NY).strftime("%Y-%m-%d %H:%M %Z"),
        "next_session_date": nxt.date.isoformat(),
        "next_session_open_utc": nxt.open_utc.isoformat(),
        "next_session_early_close": nxt.early_close,
        "calendar_verified": nxt.verified,
        "calendar_source": OFFICIAL_SOURCE if nxt.verified else "NYSE observance rules (not verified for this year)",
    }
    if s is None:
        reason = closure_reason(d)
        state = "closed_weekend" if reason == "Weekend" else "closed_holiday"
        return {**base, "state": state, "session_date": None, "holiday_name": None if reason == "Weekend" else reason}
    if ts < s.open_utc:
        state = "pre_open"
    elif ts < s.close_utc:
        state = "open"
    else:
        state = "after_close"
    return {**base, "state": state, "session_date": d.isoformat(), "holiday_name": None,
            "session_open_utc": s.open_utc.isoformat(), "session_close_utc": s.close_utc.isoformat(),
            "early_close": s.early_close}
