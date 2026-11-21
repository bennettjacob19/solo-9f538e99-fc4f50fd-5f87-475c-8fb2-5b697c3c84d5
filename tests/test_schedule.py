"""DST + plan arithmetic tests."""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from nybackup.schedule import (
    is_ambiguous_local,
    is_valid_local,
    latest_due,
    next_due,
    parse_hhmm,
    resolve_wallclock,
    slot_for,
)

NY = ZoneInfo("America/New_York")


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_parse_hhmm():
    assert parse_hhmm("02:30").strftime("%H:%M") == "02:30"
    for bad in ["2:30", "24:00", "02:60", "", "abc", " 02:30x"]:
        with pytest.raises(ValueError):
            parse_hhmm(bad)


def test_normal_day():
    slot = slot_for(date(2026, 11, 21), "02:30", NY)
    assert slot.local == datetime(2026, 11, 21, 2, 30)
    assert slot.instant == utc(2026, 11, 21, 7, 30)  # EST = UTC-5
    assert not slot.shifted


def test_spring_gap_shifts_to_first_valid_minute():
    # 2026-03-08: 02:00 -> 03:00 EDT
    assert not is_valid_local(datetime(2026, 3, 8, 2, 30), NY)
    slot = slot_for(date(2026, 3, 8), "02:30", NY)
    assert slot.local == datetime(2026, 3, 8, 3, 0)
    assert slot.instant == utc(2026, 3, 8, 7, 0)
    assert slot.shifted

    # The instant of the gap start also moves to the first valid minute.
    slot = slot_for(date(2026, 3, 8), "02:00", NY)
    assert slot.local == datetime(2026, 3, 8, 3, 0)

    # Just before the gap is untouched.
    slot = slot_for(date(2026, 3, 8), "01:59", NY)
    assert slot.local == datetime(2026, 3, 8, 1, 59)
    assert not slot.shifted

    # 03:00 exists normally.
    slot = slot_for(date(2026, 3, 8), "03:00", NY)
    assert slot.local == datetime(2026, 3, 8, 3, 0)
    assert not slot.shifted


def test_fall_overlap_takes_first_occurrence():
    # 2026-11-01: 01:00-02:00 repeats.  The first one is EDT (UTC-4).
    naive = datetime(2026, 11, 1, 1, 30)
    assert is_ambiguous_local(naive, NY)
    slot = slot_for(date(2026, 11, 1), "01:30", NY)
    assert slot.instant == utc(2026, 11, 1, 5, 30)  # first (EDT)
    assert slot.ambiguous and not slot.shifted
    assert slot.instant != utc(2026, 11, 1, 6, 30)  # second (EST)

    # A time after the overlap is unambiguous off EST.
    slot = slot_for(date(2026, 11, 1), "02:30", NY)
    assert slot.instant == utc(2026, 11, 1, 7, 30)
    assert not slot.ambiguous


def test_latest_due_picks_most_recent_slot():
    # The day after a DST change, 10:00 UTC = 05:00 EST
    now = utc(2026, 3, 9, 10, 0)
    slot = latest_due(now, "02:30", NY)
    assert slot.plan_date == date(2026, 3, 9)
    # 02:30 on Mar 9 is EDT (UTC-4) -> 06:30 UTC
    assert slot.instant == utc(2026, 3, 9, 6, 30)

    # Before that day's time: falls back to the previous day.
    slot = latest_due(utc(2026, 3, 9, 5, 0), "02:30", NY)
    assert slot.plan_date == date(2026, 3, 8)
    assert slot.instant == utc(2026, 3, 8, 7, 0)  # shifted from the gap


def test_next_due():
    slot = next_due(utc(2026, 11, 21, 8, 0), "02:30", NY)
    assert slot.plan_date == date(2026, 11, 22)
    assert slot.instant == utc(2026, 11, 22, 7, 30)


def test_resolve_returns_same_for_valid():
    naive = datetime(2026, 6, 1, 12, 0)
    resolved, shifted = resolve_wallclock(naive, NY)
    assert resolved == naive and not shifted
