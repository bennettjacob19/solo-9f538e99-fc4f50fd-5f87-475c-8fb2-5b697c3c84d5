"""Daily schedule arithmetic in the New York time zone.

A "plan date" is the local calendar date on which a backup is scheduled.  The
configured wall-clock time is resolved to an absolute instant as follows:

* normal days  -> the obvious instant;
* spring gap   -> the first valid minute *after* the gap (e.g. 02:30 -> 03:00);
* fall overlap -> the first of the two occurrences.

Resolution is done without relying on private zoneinfo attributes so that it
also works when tzdata is provided by the operating system.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class ScheduleError(ValueError):
    pass


def parse_hhmm(value: str) -> time:
    value = (value or "").strip()
    if not HHMM_RE.match(value):
        raise ScheduleError("备份时刻必须为 HH:MM（24 小时制）")
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _roundtrip(naive: datetime, tz: ZoneInfo) -> datetime:
    """Return the wall clock observed after converting a naive local time to UTC
    and back.  For a non-existent time this differs from ``naive``."""
    aware = naive.replace(tzinfo=tz, fold=0)
    return aware.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)


def is_valid_local(naive: datetime, tz: ZoneInfo) -> bool:
    return _roundtrip(naive, tz) == naive


def is_ambiguous_local(naive: datetime, tz: ZoneInfo) -> bool:
    first = naive.replace(tzinfo=tz, fold=0).utcoffset()
    second = naive.replace(tzinfo=tz, fold=1).utcoffset()
    return first != second


def resolve_wallclock(naive: datetime, tz: ZoneInfo) -> tuple[datetime, bool]:
    """Resolve a naive local wall clock to a real one.

    Returns ``(resolved_naive, shifted)``.  Ambiguous times keep ``fold=0``
    (the first occurrence); non-existent times are moved forward minute by
    minute to the first valid local minute.
    """
    current = naive
    for _ in range(24 * 60 + 5):
        if is_valid_local(current, tz):
            return current, current != naive
        current = current + timedelta(minutes=1)
    raise ScheduleError("无法解析该时刻对应的纽约本地时间")


@dataclass(frozen=True)
class ScheduleSlot:
    plan_date: date
    configured: time
    local: datetime  # resolved naive NY wall clock
    instant: datetime  # aware UTC instant at which the backup becomes due
    shifted: bool
    ambiguous: bool
    fold: int
    tz: ZoneInfo

    @property
    def local_aware(self) -> datetime:
        return self.local.replace(tzinfo=self.tz, fold=self.fold)

    @property
    def note(self) -> Optional[str]:
        if self.shifted:
            return "春季跳时，设定时刻不存在，顺延至跳时后的第一个有效分钟"
        if self.ambiguous:
            return "秋季重复时刻，取第一次出现（EDT）"
        return None


def slot_for(plan_date: date, hhmm: str, tz: ZoneInfo) -> ScheduleSlot:
    configured = parse_hhmm(hhmm)
    naive = datetime.combine(plan_date, configured)
    resolved, shifted = resolve_wallclock(naive, tz)
    ambiguous = is_ambiguous_local(resolved, tz)
    fold = 0  # first occurrence for a repeated time
    aware = resolved.replace(tzinfo=tz, fold=fold)
    instant = aware.astimezone(timezone.utc)
    return ScheduleSlot(
        plan_date=plan_date,
        configured=configured,
        local=resolved,
        instant=instant,
        shifted=shifted,
        ambiguous=ambiguous,
        fold=fold,
        tz=tz,
    )


def latest_due(now_utc: datetime, hhmm: str, tz: ZoneInfo) -> Optional[ScheduleSlot]:
    """The most recent schedule slot whose instant is <= ``now_utc``.

    Looking back only a few days is enough for New York, and deliberately
    ignores all older missed days: callers catch up exactly one plan.
    """
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    now_utc = now_utc.astimezone(timezone.utc)
    local_today = now_utc.astimezone(tz).date()
    best: Optional[ScheduleSlot] = None
    for delta in range(-4, 2):
        candidate = slot_for(local_today + timedelta(days=delta), hhmm, tz)
        if candidate.instant <= now_utc:
            if best is None or candidate.instant > best.instant:
                best = candidate
    return best


def next_due(now_utc: datetime, hhmm: str, tz: ZoneInfo) -> ScheduleSlot:
    now_utc = now_utc.astimezone(timezone.utc)
    local_today = now_utc.astimezone(tz).date()
    for delta in range(-1, 5):
        candidate = slot_for(local_today + timedelta(days=delta), hhmm, tz)
        if candidate.instant > now_utc:
            return candidate
    raise ScheduleError("无法计算下一次备份时刻")


def format_local(dt_utc: datetime, tz: ZoneInfo) -> str:
    return dt_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z (UTC%z)")
