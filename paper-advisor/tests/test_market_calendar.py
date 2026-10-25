"""NYSE session calendar.

The 2025-2027 holiday and early-close table was transcribed from the official NYSE page
https://www.nyse.com/markets/hours-calendars (retrieved 2026-10-24). The rule engine must reproduce
it exactly, which is what proves the rules are right for years the official table does not cover.
"""
from datetime import date, datetime, timezone

import pytest

from paper_advisor import market_calendar as cal

UTC = timezone.utc

OFFICIAL_HOLIDAYS = {
    2025: ["2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26", "2025-06-19",
           "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25"],
    2026: ["2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
           "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25"],
    2027: ["2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
           "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"],
}
OFFICIAL_EARLY_CLOSES = {
    2025: ["2025-07-03", "2025-11-28", "2025-12-24"],
    2026: ["2026-11-27", "2026-12-24"],
    2027: ["2027-11-26"],
}


@pytest.mark.parametrize("year", [2025, 2026, 2027])
def test_rule_engine_reproduces_official_nyse_holidays(year):
    rule = sorted(d.isoformat() for d in cal.rule_holidays(year))
    assert rule == OFFICIAL_HOLIDAYS[year]


@pytest.mark.parametrize("year", [2025, 2026, 2027])
def test_rule_engine_reproduces_official_early_closes(year):
    rule = sorted(d.isoformat() for d in cal.rule_early_closes(year))
    assert rule == OFFICIAL_EARLY_CLOSES[year]


def test_official_table_marks_sessions_verified_and_other_years_unverified():
    assert cal.session(date(2026, 10, 26)).verified is True
    assert cal.session(date(2028, 10, 23)).verified is False


def test_known_historical_rules():
    # New Year's Day on a Saturday is NOT observed on the prior Friday (NYSE rule): 2022-01-01.
    assert date(2021, 12, 31) not in cal.rule_holidays(2021)
    assert cal.is_session(date(2021, 12, 31))
    # Sunday holidays move to Monday: New Year's 2023, Juneteenth 2022, Christmas 2022.
    assert date(2023, 1, 2) in cal.rule_holidays(2023)
    assert date(2022, 6, 20) in cal.rule_holidays(2022)
    assert date(2022, 12, 26) in cal.rule_holidays(2022)
    # Juneteenth was first observed by NYSE in 2022.
    assert date(2021, 6, 18) not in cal.rule_holidays(2021)
    # Good Friday 2024 = March 29.
    assert date(2024, 3, 29) in cal.rule_holidays(2024)
    # July 3, 2023 (Monday before a Tuesday July 4) was a 1 p.m. early close.
    assert date(2023, 7, 3) in cal.rule_early_closes(2023)


def test_special_closures_are_closed():
    assert not cal.is_session(date(2025, 1, 9))   # National Day of Mourning, President Carter
    assert not cal.is_session(date(2018, 12, 5))  # National Day of Mourning, President G.H.W. Bush


def test_session_times_in_utc_handle_dst_and_early_close():
    s = cal.session(date(2026, 10, 23))  # EDT (UTC-4)
    assert s.open_utc == datetime(2026, 10, 23, 13, 30, tzinfo=UTC)
    assert s.close_utc == datetime(2026, 10, 23, 20, 0, tzinfo=UTC)
    w = cal.session(date(2026, 11, 27))  # day after Thanksgiving, EST (UTC-5), 1 p.m. close
    assert w.early_close is True
    assert w.open_utc == datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
    assert w.close_utc == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
    assert cal.session(date(2026, 11, 26)) is None  # Thanksgiving
    assert cal.session(date(2026, 10, 24)) is None  # Saturday


def test_next_session_open_strictly_after_timestamp():
    # Saturday night -> Monday open
    sat = datetime(2026, 10, 24, 4, 55, tzinfo=UTC)
    assert cal.next_session_after(sat).date == date(2026, 10, 26)
    # Exactly at the open is NOT eligible for that open (strictly after)
    mon_open = datetime(2026, 10, 26, 13, 30, tzinfo=UTC)
    assert cal.next_session_after(mon_open).date == date(2026, 10, 27)
    # One microsecond before the open is eligible
    before = datetime(2026, 10, 26, 13, 29, 59, 999999, tzinfo=UTC)
    assert cal.next_session_after(before).date == date(2026, 10, 26)
    # Wednesday after the close skips Thanksgiving Thursday -> Friday early close
    wed = datetime(2026, 11, 25, 22, 0, tzinfo=UTC)
    assert cal.next_session_after(wed).date == date(2026, 11, 27)


def test_last_completed_session_respects_finalization_buffer():
    # Friday 2026-10-23 closes 20:00 UTC; with a 60-minute buffer it is complete at 21:00 UTC.
    assert cal.last_completed_session(datetime(2026, 10, 23, 20, 30, tzinfo=UTC), 60).date == date(2026, 10, 22)
    assert cal.last_completed_session(datetime(2026, 10, 23, 21, 0, tzinfo=UTC), 60).date == date(2026, 10, 23)
    assert cal.last_completed_session(datetime(2026, 10, 24, 4, 55, tzinfo=UTC), 60).date == date(2026, 10, 23)


def test_sessions_between_is_inclusive_and_skips_closures():
    got = [s.date.isoformat() for s in cal.sessions_between(date(2026, 11, 24), date(2026, 11, 30))]
    assert got == ["2026-11-24", "2026-11-25", "2026-11-27", "2026-11-30"]


def test_market_status_labels():
    assert cal.market_status(datetime(2026, 10, 24, 4, 55, tzinfo=UTC))["state"] == "closed_weekend"
    assert cal.market_status(datetime(2026, 10, 26, 13, 0, tzinfo=UTC))["state"] == "pre_open"
    assert cal.market_status(datetime(2026, 10, 26, 15, 0, tzinfo=UTC))["state"] == "open"
    assert cal.market_status(datetime(2026, 10, 26, 21, 0, tzinfo=UTC))["state"] == "after_close"
    st = cal.market_status(datetime(2026, 11, 26, 15, 0, tzinfo=UTC))
    assert st["state"] == "closed_holiday" and "Thanksgiving" in st["holiday_name"]


def test_sessions_after_counts_completed_sessions():
    # Sessions strictly after 2026-11-24 up to and including 2026-11-30 -> 25th, 27th, 30th
    assert cal.count_sessions_after(date(2026, 11, 24), date(2026, 11, 30)) == 3
