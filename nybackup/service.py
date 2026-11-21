"""Scheduler: decide which plan is due and run it exactly once.

Rules implemented here
----------------------
* The **drill clock** (``virtual = real UTC + offset``) is the authority for
  "has the scheduled moment passed" and for times shown on the page.
* If several plans became due while the service was not running, only the
  **most recent** one is executed.  It keeps its original New York plan date;
  the older ones are recorded as ``skipped``.
* A plan counts as done only after its snapshot has been atomically published
  under ``jobs-YYYY-MM-DD.sqlite`` and verified.  Bookkeeping stores the file's
  production time as the actual completion time.
* On restart, a fully written backup file is adopted as-is (same bytes, same
  mtime).  Leftover temp files are removed and the affected plan is retried
  under the catch-up rule.

Only one backup runs at a time; a tick is guarded and the snapshot writer is
additionally guarded in :mod:`nybackup.backup`.
"""

from __future__ import annotations

import os
import threading
import traceback
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from . import backup
from .schedule import (
    ScheduleError,
    ScheduleSlot,
    latest_due,
    next_due,
    parse_hhmm,
    slot_for,
)
from .store import Store, iso_utc, parse_utc

TZ_NAME = "America/New_York"
MAX_ATTEMPTS = 5
RETRY_BACKOFF_REAL_SECONDS = 5.0
STALE_RUNNING_REAL_SECONDS = 900.0


class Service:
    def __init__(
        self,
        store: Store,
        source_path: str,
        backup_dir: str,
        *,
        tick_seconds: float = 1.0,
        catchup_window_days: int = 31,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self.store = store
        self.source_path = os.path.abspath(source_path)
        self.backup_dir = os.path.abspath(backup_dir)
        self.tz = ZoneInfo(store.config().get("timezone", TZ_NAME))
        self.tick_seconds = tick_seconds
        self.catchup_window_days = catchup_window_days
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._tick_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._last_eval_cache: Optional[datetime] = None
        self._last_eval_persisted: Optional[datetime] = None
        # Persisting the watermark every tick would write to disk each second;
        # every 15 s is plenty and a restart only widens the catch-up window.
        self._last_eval_persist_seconds = 15.0

    # -- clock ------------------------------------------------------------
    def real_now(self) -> datetime:
        """Wall clock (UTC).  Injectable for tests."""
        return self._clock().astimezone(timezone.utc)

    def now(self) -> datetime:
        """Drill clock: wall clock plus the configured offset."""
        offset = float(self.store.config().get("clock_offset_seconds", 0.0) or 0.0)
        return self.real_now() + timedelta(seconds=offset)

    def settings(self) -> dict:
        cfg = self.store.config()
        return {
            "backup_time": cfg.get("backup_time", "02:30"),
            "timezone": cfg.get("timezone", TZ_NAME),
            "clock_offset_seconds": float(cfg.get("clock_offset_seconds", 0.0) or 0.0),
        }

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="nybackup-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=15)

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                self.store.log(
                    "error",
                    "调度循环异常:\n" + traceback.format_exc(),
                    virtual_now=self.now(),
                    real_now=self.real_now(),
                )
            self._wake.wait(self.tick_seconds)
            self._wake.clear()

    # -- reaching a target time -------------------------------------------
    def _local_day_floor(self, when: datetime) -> datetime:
        local_today = when.astimezone(self.tz).date()
        floor = datetime.combine(local_today, datetime.min.time()).replace(tzinfo=self.tz)
        return floor.astimezone(timezone.utc)

    def _last_eval(self) -> datetime:
        if self._last_eval_cache is not None:
            return self._last_eval_cache
        raw = self.store.config().get("last_eval_virtual_utc")
        parsed = parse_utc(raw) if isinstance(raw, str) else None
        if parsed is not None:
            self._last_eval_cache = parsed
            return parsed
        # First ever start: look back to the beginning of today in New York so
        # that a backup time which already passed today is caught up once.
        self._last_eval_cache = self._local_day_floor(self.now())
        return self._last_eval_cache

    def _set_last_eval(self, when: datetime) -> None:
        """Remember the evaluation watermark.

        The in-memory value always tracks every tick; it is persisted only
        periodically, which keeps the once-per-second scheduler from writing to
        disk constantly.  After a restart the watermark may therefore be a few
        seconds old, which only widens the catch-up window harmlessly.
        """
        self._last_eval_cache = when
        real_now = self.real_now()
        if self._last_eval_persisted is None or (
            real_now - self._last_eval_persisted
        ).total_seconds() >= self._last_eval_persist_seconds:
            self._last_eval_persisted = real_now
            self.store.set_config({"last_eval_virtual_utc": iso_utc(when)})

    # -- plan materialisation --------------------------------------------
    def _slots_between(
        self, start_exclusive: datetime, end_inclusive: datetime
    ) -> list[ScheduleSlot]:
        hhmm = self.settings()["backup_time"]
        try:
            parse_hhmm(hhmm)
        except ScheduleError:
            return []
        if end_inclusive <= start_exclusive:
            return []
        span = end_inclusive - start_exclusive
        slots: list[ScheduleSlot] = []
        if span <= timedelta(days=self.catchup_window_days):
            first = start_exclusive.astimezone(self.tz).date() - timedelta(days=1)
            last = end_inclusive.astimezone(self.tz).date() + timedelta(days=1)
            day = first
            while day <= last:
                slot = slot_for(day, hhmm, self.tz)
                if start_exclusive < slot.instant <= end_inclusive:
                    slots.append(slot)
                day += timedelta(days=1)
        else:
            # A very long gap (clock jump / long outage): do not fabricate a row
            # per missed day, just find the latest due slot.
            slot = latest_due(end_inclusive, hhmm, self.tz)
            if slot is not None and start_exclusive < slot.instant <= end_inclusive:
                slots.append(slot)
        return slots

    def materialise(
        self, start_exclusive: datetime, end_inclusive: datetime
    ) -> Optional[ScheduleSlot]:
        """Create/adjust plan rows for every slot that became due in the gap.

        Only the newest becomes ``pending``; older missed plans are recorded as
        ``skipped`` so the catch-up rule is visible on the page.
        """
        slots = self._slots_between(start_exclusive, end_inclusive)
        if not slots:
            return None
        latest = slots[-1]
        real_now = self.real_now()
        for slot in slots:
            existing = self.store.get_plan(slot.plan_date)
            is_latest = slot is latest
            if existing is None:
                self.store.upsert_plan(
                    slot.plan_date,
                    status="pending" if is_latest else "skipped",
                    due_utc=iso_utc(slot.instant),
                    configured_time=slot.configured.strftime("%H:%M"),
                    shifted=int(slot.shifted),
                    ambiguous=int(slot.ambiguous),
                    error=(
                        None
                        if is_latest
                        else "停机期间错过；按规则仅补最近一次到期备份"
                    ),
                    _real_now=real_now,
                )
                if not is_latest:
                    self.store.log(
                        "warn",
                        f"计划 {slot.plan_date.isoformat()} 在停机期间错过，按规则跳过",
                        virtual_now=end_inclusive,
                        real_now=real_now,
                    )
            elif not is_latest and existing["status"] in ("pending", "failed", "running"):
                self.store.mark_skipped(
                    slot.plan_date,
                    "停机期间错过；按规则仅补最近一次到期备份",
                    real_now=real_now,
                )
                self.store.log(
                    "warn",
                    f"计划 {slot.plan_date.isoformat()} 被更新的到期计划取代，标记为跳过",
                    virtual_now=end_inclusive,
                    real_now=real_now,
                )
            elif is_latest and existing["status"] == "skipped":
                # The clock was moved back onto a previously skipped plan and it
                # is the newest due plan again: re-arm it.
                self.store.upsert_plan(
                    slot.plan_date,
                    status="pending",
                    error=None,
                    due_utc=iso_utc(slot.instant),
                    configured_time=slot.configured.strftime("%H:%M"),
                    shifted=int(slot.shifted),
                    ambiguous=int(slot.ambiguous),
                    _real_now=real_now,
                )
                self.store.log(
                    "info",
                    f"计划 {slot.plan_date.isoformat()} 重新排队（演练时钟回拨）",
                    virtual_now=end_inclusive,
                    real_now=real_now,
                )
            elif is_latest and existing["status"] in ("pending", "failed"):
                # Not run yet: follow the current schedule so that changing the
                # backup time takes effect for a plan that has not fired.
                self.store.upsert_plan(
                    slot.plan_date,
                    due_utc=iso_utc(slot.instant),
                    configured_time=slot.configured.strftime("%H:%M"),
                    shifted=int(slot.shifted),
                    ambiguous=int(slot.ambiguous),
                    _real_now=real_now,
                )
        return latest

    # -- tick -------------------------------------------------------------
    def tick(self) -> dict:
        if not self._tick_lock.acquire(blocking=False):
            return {"action": "busy"}
        try:
            return self._tick()
        finally:
            self._tick_lock.release()

    def _tick(self) -> dict:
        settings = self.settings()
        try:
            parse_hhmm(settings["backup_time"])
        except ScheduleError as exc:
            self.store.log(
                "error", str(exc), virtual_now=self.now(), real_now=self.real_now()
            )
            return {"action": "error", "message": str(exc)}

        virtual_now = self.now()
        last_eval = self._last_eval()
        if virtual_now < last_eval:
            # The drill clock was moved backwards.  Do not replay the gap
            # between then and now, but do treat the current local day as the
            # window so a drill can be restarted on any date (e.g. jump back to
            # a DST change and then step across it).
            floor = self._local_day_floor(virtual_now)
            self._set_last_eval(virtual_now)
            self.materialise(floor, virtual_now)
        else:
            self.materialise(last_eval, virtual_now)
            self._set_last_eval(virtual_now)

        row = self._pick_work(virtual_now)
        if row is None:
            return {"action": "idle", "virtual_now": iso_utc(virtual_now)}
        return self._execute(row, virtual_now)

    def _pick_work(self, virtual_now: datetime):
        plans = self.store.all_plans()
        overdue = []
        for plan in plans:
            due = parse_utc(plan["due_utc"])
            if due is None or due > virtual_now or plan["status"] == "skipped":
                continue
            overdue.append(plan)
        if not overdue:
            return None
        overdue.sort(key=lambda p: (p["due_utc"], p["plan_date"]))
        newest = overdue[-1]
        real_now = self.real_now()
        # Older unfinished plans are superseded by the newest one.
        for older in overdue[:-1]:
            if older["status"] in ("pending", "running", "failed"):
                self.store.mark_skipped(
                    date.fromisoformat(older["plan_date"]),
                    "已有更新的到期计划；按规则仅补最近一次",
                    real_now=real_now,
                )
                self.store.log(
                    "warn",
                    f"计划 {older['plan_date']} 被更新的到期计划取代，标记为跳过",
                    virtual_now=virtual_now,
                    real_now=real_now,
                )
        if newest["status"] in ("complete", "running"):
            if newest["status"] == "running":
                # A "running" row whose worker is gone (e.g. a crash inside a
                # handler before the failure was recorded) is retried.
                started = parse_utc(newest["started_real_utc"])
                if started is not None and (
                    real_now - started
                ).total_seconds() > STALE_RUNNING_REAL_SECONDS:
                    self.store.upsert_plan(
                        date.fromisoformat(newest["plan_date"]),
                        status="pending",
                        _real_now=real_now,
                    )
                    self.store.log(
                        "warn",
                        f"计划 {newest['plan_date']} 的运行状态已超时，按补跑规则重新排队",
                        virtual_now=virtual_now,
                        real_now=real_now,
                    )
                    return self.store.get_plan(date.fromisoformat(newest["plan_date"]))
            return None
        if newest["status"] == "failed":
            if newest["attempts"] >= MAX_ATTEMPTS:
                return None
            updated = parse_utc(newest["updated_utc"])
            if updated is not None and (
                real_now - updated
            ).total_seconds() < RETRY_BACKOFF_REAL_SECONDS:
                return None
        return newest

    # -- execution --------------------------------------------------------
    def _execute(self, row, virtual_now: datetime) -> dict:
        if not self._run_lock.acquire(blocking=False):
            return {"action": "busy"}
        try:
            return self._execute_locked(row, virtual_now)
        finally:
            self._run_lock.release()

    def _execute_locked(self, row, virtual_now: datetime) -> dict:
        plan_date = date.fromisoformat(row["plan_date"])
        real_now = self.real_now()
        final = backup.final_path(self.backup_dir, plan_date)

        # A complete file on disk wins: reuse it, never overwrite.
        if os.path.exists(final) and os.path.getsize(final) > 0:
            adopted = self._adopt_existing(plan_date, final, virtual_now=virtual_now)
            if adopted is not None:
                return adopted
            return {"action": "failed", "message": "已有同名备份文件但无法校验"}

        settings = self.settings()
        due = parse_utc(row["due_utc"]) or slot_for(
            plan_date, settings["backup_time"], self.tz
        ).instant
        attempts = self.store.begin_attempt(
            plan_date,
            due_utc=due,
            configured_time=row["configured_time"] or settings["backup_time"],
            shifted=bool(row["shifted"]),
            ambiguous=bool(row["ambiguous"]),
            partial_path=backup.final_path(self.backup_dir, plan_date)
            + backup.PARTIAL_SUFFIX,
            virtual_now=virtual_now,
            real_now=real_now,
        )
        self.store.log(
            "info",
            f"开始备份计划 {plan_date.isoformat()}（第 {attempts} 次尝试）",
            virtual_now=virtual_now,
            real_now=real_now,
        )
        try:
            result = backup.take_snapshot(self.source_path, self.backup_dir, plan_date)
        except FileExistsError:
            adopted = self._adopt_existing(plan_date, final, virtual_now=self.now())
            return adopted or {"action": "failed", "message": "备份文件已存在"}
        except Exception as exc:  # noqa: BLE001 - surface any failure to the page
            failed_at = self.now()
            self.store.finish_attempt(
                plan_date,
                status="failed",
                virtual_now=failed_at,
                real_now=self.real_now(),
                error=f"{type(exc).__name__}: {exc}",
            )
            self.store.log(
                "error",
                f"备份计划 {plan_date.isoformat()} 失败: {type(exc).__name__}: {exc}",
                virtual_now=failed_at,
                real_now=self.real_now(),
            )
            return {
                "action": "failed",
                "plan_date": plan_date.isoformat(),
                "message": str(exc),
            }

        completed_virtual = self.now()
        self.store.finish_attempt(
            plan_date,
            status="complete",
            virtual_now=completed_virtual,
            real_now=self.real_now(),
            completed_virtual=completed_virtual,
            completed_real=result.completed_real_utc,
            filename=result.filename,
            size_bytes=result.size,
            sha256=result.sha256,
        )
        self.store.log(
            "info",
            f"备份计划 {plan_date.isoformat()} 完成: {result.filename} "
            f"({result.size} 字节, jobs={result.jobs}, write_seq={result.write_seq})",
            virtual_now=completed_virtual,
            real_now=self.real_now(),
        )
        return {
            "action": "complete",
            "plan_date": plan_date.isoformat(),
            "filename": result.filename,
            "size": result.size,
            "sha256": result.sha256,
            "jobs": result.jobs,
        }

    def _adopt_existing(
        self, plan_date: date, path: str, *, virtual_now: datetime
    ) -> Optional[dict]:
        """Register an already-published file without rewriting it."""
        real_now = self.real_now()
        try:
            info = backup.inspect_file(path)
        except Exception as exc:
            self.store.log(
                "warn",
                f"备份文件 {os.path.basename(path)} 无法打开，将移出并重新生成: {exc}",
                virtual_now=virtual_now,
                real_now=real_now,
            )
            self._quarantine(path)
            return None
        inconsistent = info["managed"] and info["write_seq"] != info["jobs"]
        if info["integrity"] != "ok" or inconsistent:
            self.store.log(
                "warn",
                f"备份文件 {os.path.basename(path)} 校验失败，将移出并重新生成",
                virtual_now=virtual_now,
                real_now=real_now,
            )
            self._quarantine(path)
            return None

        digest = backup.sha256_of(path)
        produced = backup.file_mtime_utc(path)
        existing = self.store.get_plan(plan_date)
        if existing is None:
            # Adoption can happen after the state DB was lost/rebuilt: create
            # the plan row first so the completion can be recorded.
            settings = self.settings()
            try:
                slot = slot_for(plan_date, settings["backup_time"], self.tz)
            except ScheduleError:
                slot = None
            self.store.upsert_plan(
                plan_date,
                status="pending",
                due_utc=iso_utc(slot.instant) if slot else iso_utc(produced),
                configured_time=(
                    slot.configured.strftime("%H:%M")
                    if slot
                    else settings["backup_time"]
                ),
                shifted=int(slot.shifted) if slot else 0,
                ambiguous=int(slot.ambiguous) if slot else 0,
                _real_now=real_now,
            )
            existing = self.store.get_plan(plan_date)
        # Preserve a known completion time across restarts; otherwise rebuild
        # the drill-clock value from the file's production time.
        if existing is not None and existing["completed_utc"]:
            completed_virtual = parse_utc(existing["completed_utc"]) or virtual_now
        else:
            completed_virtual = produced + timedelta(
                seconds=float(self.store.config().get("clock_offset_seconds", 0.0) or 0.0)
            )
        self.store.finish_attempt(
            plan_date,
            status="complete",
            virtual_now=completed_virtual,
            real_now=real_now,
            completed_virtual=completed_virtual,
            completed_real=produced,
            filename=os.path.basename(path),
            size_bytes=os.path.getsize(path),
            sha256=digest,
        )
        self.store.log(
            "info",
            f"沿用已完整落盘的备份 {os.path.basename(path)}（计划 {plan_date.isoformat()}）",
            virtual_now=virtual_now,
            real_now=real_now,
        )
        return {
            "action": "adopted",
            "plan_date": plan_date.isoformat(),
            "filename": os.path.basename(path),
            "size": os.path.getsize(path),
        }

    @staticmethod
    def _quarantine(path: str) -> None:
        try:
            os.replace(path, path + ".corrupt")
        except OSError:
            pass

    # -- operations -------------------------------------------------------
    def run_plan_now(self, plan_date: date) -> dict:
        settings = self.settings()
        try:
            slot = slot_for(plan_date, settings["backup_time"], self.tz)
        except ScheduleError as exc:
            return {"action": "error", "message": str(exc)}
        row = self.store.get_plan(plan_date)
        if row is None:
            self.store.upsert_plan(
                plan_date,
                status="pending",
                due_utc=iso_utc(slot.instant),
                configured_time=slot.configured.strftime("%H:%M"),
                shifted=int(slot.shifted),
                ambiguous=int(slot.ambiguous),
                _real_now=self.real_now(),
            )
            row = self.store.get_plan(plan_date)
        if row["status"] == "complete":
            return {
                "action": "none",
                "message": "该计划已有完整备份，沿用原文件",
                "filename": row["filename"],
            }
        if not self._run_lock.acquire(blocking=False):
            return {"action": "busy", "message": "已有备份正在进行"}
        try:
            return self._execute_locked(row, self.now())
        finally:
            self._run_lock.release()

    def recover(self) -> None:
        """Start-up housekeeping: drop temp files, adopt intact backups."""
        virtual_now = self.now()
        real_now = self.real_now()
        removed = backup.cleanup_leftovers(self.backup_dir)
        if removed:
            self.store.log(
                "warn",
                "清理未完成的备份残件: " + ", ".join(removed),
                virtual_now=virtual_now,
                real_now=real_now,
            )
        for row in self.store.plans_with_status("running"):
            self.store.upsert_plan(
                date.fromisoformat(row["plan_date"]),
                status="pending",
                _real_now=real_now,
            )
            self.store.log(
                "warn",
                f"计划 {row['plan_date']} 上次未完成，按补跑规则重新排队",
                virtual_now=virtual_now,
                real_now=real_now,
            )
        for iso_date, path in backup.list_backup_files(self.backup_dir).items():
            plan_date = date.fromisoformat(iso_date)
            row = self.store.get_plan(plan_date)
            if row is None or row["status"] != "complete":
                self._adopt_existing(plan_date, path, virtual_now=virtual_now)

    # -- views ------------------------------------------------------------
    def status_snapshot(self) -> dict:
        settings = self.settings()
        virtual_now = self.now()
        try:
            upcoming = next_due(virtual_now, settings["backup_time"], self.tz)
        except ScheduleError:
            upcoming = None
        return {
            "settings": settings,
            "virtual_now": virtual_now,
            "real_now": self.real_now(),
            "next_due": upcoming,
        }
