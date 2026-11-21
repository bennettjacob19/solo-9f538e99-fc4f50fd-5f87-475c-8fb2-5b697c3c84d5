"""End-to-end backup behaviour tests using a controllable clock."""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone

import pytest

from nybackup import backup, source
from nybackup.schedule import slot_for
from nybackup.service import Service
from nybackup.store import Store

NY = "America/New_York"


class Clock:
    def __init__(self, start: datetime):
        self.value = start
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self.value

    def set(self, value: datetime) -> None:
        with self._lock:
            self.value = value

    def advance(self, **kwargs) -> None:
        with self._lock:
            self.value = self.value + timedelta(**kwargs)


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


@pytest.fixture()
def env(tmp_path):
    src = str(tmp_path / "jobs.db")
    state = str(tmp_path / "state.db")
    out = str(tmp_path / "backups")
    os.makedirs(out, exist_ok=True)
    source.ensure_schema(src)
    source.seed(src, 5)
    clock = Clock(utc(2026, 11, 20, 8, 0))
    store = Store(state)
    service = Service(store, src, out, tick_seconds=0.01, clock=clock)
    yield {
        "src": src,
        "state": state,
        "out": out,
        "clock": clock,
        "store": store,
        "service": service,
    }
    service.stop()
    store.close()


def read_backup(path: str) -> dict:
    conn = sqlite3.connect(path)
    try:
        jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM job_events").fetchone()[0]
        seq = conn.execute("SELECT value FROM meta WHERE key='write_seq'").fetchone()[0]
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        return {
            "jobs": jobs,
            "events": events,
            "write_seq": int(seq),
            "journal": journal,
            "integrity": integrity,
        }
    finally:
        conn.close()


def test_backup_created_and_downloadable(env):
    svc = env["service"]
    # 2026-11-20 08:00 UTC = 03:00 EST, after 02:30
    svc.tick()
    row = env["store"].get_plan(date(2026, 11, 20))
    assert row is not None and row["status"] == "complete"
    path = backup.final_path(env["out"], date(2026, 11, 20))
    assert os.path.exists(path)
    info = read_backup(path)
    assert info["integrity"] == "ok"
    assert info["jobs"] == info["events"] == info["write_seq"] == 5
    assert info["journal"] == "delete"
    # No WAL side cars next to the backup.
    assert not os.path.exists(path + "-wal")
    assert not os.path.exists(path + "-shm")
    # Plan date is the New York date of the scheduled moment.
    assert row["completed_utc"]
    assert row["sha256"] == backup.sha256_of(path)


def test_backup_is_standalone_and_openable_in_isolation(env):
    svc = env["service"]
    svc.tick()
    path = backup.final_path(env["out"], date(2026, 11, 20))
    # Move it away from its directory: it must still open.
    isolated = str(env["out"]) + "-isolated"
    os.makedirs(isolated, exist_ok=True)
    target = os.path.join(isolated, "copy.sqlite")
    with open(path, "rb") as src, open(target, "wb") as dst:
        dst.write(src.read())
    info = read_backup(target)
    assert info["integrity"] == "ok" and info["jobs"] == 5


def test_writes_during_backup_captured_consistently(env):
    """Concurrent appends must not corrupt the snapshot or block each other."""
    import time

    svc = env["service"]
    stop = threading.Event()
    appended = []
    started = threading.Event()

    def writer():
        while not stop.is_set():
            appended.append(source.append_record(env["src"])["write_seq"])
            started.set()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        # Make sure the source is actively being written while we snapshot.
        assert started.wait(5)
        for _ in range(3):
            # Run the newest unfinished plan manually so we don't depend on time.
            result = svc.run_plan_now(date(2026, 11, 20))
            assert result["action"] in ("complete", "adopted", "none")
            path = backup.final_path(env["out"], date(2026, 11, 20))
            info = read_backup(path)
            assert info["jobs"] == info["write_seq"] == info["events"]
            assert info["integrity"] == "ok"
            # The snapshot is a consistent prefix of the writes: no torn state,
            # and it can never contain more rows than the source had.
            current = source.stats(env["src"])
            assert info["write_seq"] <= current["write_seq"]
            assert appended
            env["store"].delete_plan(date(2026, 11, 20))
            os.remove(path)
            time.sleep(0.01)
    finally:
        stop.set()
        thread.join()


def test_catch_up_runs_only_most_recent_missed_plan(env):
    store, svc, clock = env["store"], env["service"], env["clock"]
    # First tick establishes a baseline for "today" (2026-11-20, already past).
    svc.tick()
    runs = [r["plan_date"] for r in store.all_plans() if r["status"] == "complete"]
    assert runs == ["2026-11-20"]

    # Service is "down" for four days, then restarts on 2026-11-24 03:10 EST.
    clock.set(utc(2026, 11, 24, 8, 10))
    svc.tick()
    plans = {p["plan_date"]: p for p in store.all_plans()}
    assert plans["2026-11-24"]["status"] == "complete"
    for missed in ("2026-11-21", "2026-11-22", "2026-11-23"):
        assert plans[missed]["status"] == "skipped"
        assert not os.path.exists(backup.final_path(env["out"], date.fromisoformat(missed)))
    # The catch-up keeps the original plan date of the most recent due slot.
    assert os.path.exists(backup.final_path(env["out"], date(2026, 11, 24)))
    assert plans["2026-11-24"]["due_utc"].startswith("2026-11-24T07:30")


def test_spring_forward_shift_is_recorded(env):
    store, svc, clock = env["store"], env["service"], env["clock"]
    clock.set(utc(2026, 3, 8, 7, 5))  # 03:05 EDT, shifted slot was 07:00 UTC
    svc.tick()
    row = store.get_plan(date(2026, 3, 8))
    assert row["status"] == "complete"
    assert row["shifted"] == 1
    assert row["configured_time"] == "02:30"
    assert row["due_utc"].startswith("2026-03-08T07:00")


def test_fall_back_uses_first_occurrence(env):
    store, svc, clock = env["store"], env["service"], env["clock"]
    store.set_config({"backup_time": "01:30"})
    clock.set(utc(2026, 11, 1, 5, 40))  # 01:40 EDT, first pass of 01:30
    svc.tick()
    row = store.get_plan(date(2026, 11, 1))
    assert row["status"] == "complete"
    assert row["ambiguous"] == 1
    assert row["due_utc"].startswith("2026-11-01T05:30")

    # One hour later (second 01:30 EST) nothing new runs.
    clock.set(utc(2026, 11, 1, 6, 40))
    svc.tick()
    assert len(store.all_plans()) == 1


def test_restart_adopts_complete_backup_without_rewriting(env):
    store, svc = env["store"], env["service"]
    svc.tick()
    path = backup.final_path(env["out"], date(2026, 11, 20))
    before_mtime = os.path.getmtime(path)
    before_hash = backup.sha256_of(path)
    store.close()

    # Simulate a restart: new Store + Service on the same data.
    store2 = Store(env["state"])
    svc2 = Service(store2, env["src"], env["out"], tick_seconds=0.01, clock=env["clock"])
    try:
        svc2.recover()
        svc2.tick()
        row = store2.get_plan(date(2026, 11, 20))
        assert row["status"] == "complete"
        assert os.path.getmtime(path) == before_mtime
        assert backup.sha256_of(path) == before_hash
    finally:
        store2.close()


def test_partial_file_is_discarded_and_never_served(env):
    store, svc = env["store"], env["service"]
    part = backup.final_path(env["out"], date(2026, 11, 20)) + backup.PARTIAL_SUFFIX
    with open(part, "wb") as fh:
        fh.write(b"not a database")
    tmp = os.path.join(env["out"], ".jobs-2026-11-20.sqlite.deadbeef.tmp")
    with open(tmp, "wb") as fh:
        fh.write(b"partial")

    svc.recover()
    assert not os.path.exists(part)
    assert not os.path.exists(tmp)
    assert backup.list_backup_files(env["out"]) == {}

    svc.tick()
    row = store.get_plan(date(2026, 11, 20))
    assert row["status"] == "complete"
    assert backup.list_backup_files(env["out"]) == {"2026-11-20": backup.final_path(env["out"], date(2026, 11, 20))}


def test_crash_before_publish_leaves_no_downloadable_file(env, monkeypatch):
    svc = env["service"]

    def boom(tmp_path):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(backup, "publish", boom)
    result = svc.run_plan_now(date(2026, 11, 20))
    assert result["action"] == "failed"
    assert backup.list_backup_files(env["out"]) == {}
    leftovers = [n for n in os.listdir(env["out"]) if n.endswith(".tmp")]
    assert leftovers == []


def test_configured_time_change_applies_to_future_plans(env):
    store, svc, clock = env["store"], env["service"], env["clock"]
    svc.tick()  # 2026-11-20 done at 02:30
    store.set_config({"backup_time": "09:15"})
    # Same evening: 09:15 EST is 14:15 UTC, still in the future at 13:00 UTC.
    clock.set(utc(2026, 11, 20, 13, 0))
    svc.tick()
    row = store.get_plan(date(2026, 11, 20))
    # The day's plan already succeeded at 02:30; no duplicate backup.
    assert row["status"] == "complete"
    assert row["configured_time"] == "02:30"

    clock.set(utc(2026, 11, 20, 14, 20))
    svc.tick()
    plans = {p["plan_date"]: p for p in store.all_plans()}
    assert plans["2026-11-20"]["status"] == "complete"
    assert len([p for p in plans.values() if p["status"] == "complete"]) == 1
    assert os.path.exists(backup.final_path(env["out"], date(2026, 11, 20)))


def test_restart_after_long_outage_skips_intermediate_days(env):
    """A new process must not fabricate a backup for every missed day."""
    store, svc, clock = env["store"], env["service"], env["clock"]
    svc.tick()  # 2026-11-20 complete
    store.close()

    clock.set(utc(2026, 11, 27, 9, 0))  # 04:00 EST on the 27th
    store2 = Store(env["state"])
    svc2 = Service(store2, env["src"], env["out"], tick_seconds=0.01, clock=clock)
    try:
        svc2.recover()
        svc2.tick()
        plans = {p["plan_date"]: p for p in store2.all_plans()}
        complete = sorted(d for d, p in plans.items() if p["status"] == "complete")
        skipped = sorted(d for d, p in plans.items() if p["status"] == "skipped")
        assert complete == ["2026-11-20", "2026-11-27"]
        assert skipped == [
            "2026-11-21",
            "2026-11-22",
            "2026-11-23",
            "2026-11-24",
            "2026-11-25",
            "2026-11-26",
        ]
        # Only two backups exist on disk.
        assert sorted(backup.list_backup_files(env["out"])) == complete
    finally:
        store2.close()


def test_crashed_running_plan_is_retried_after_restart(env):
    store, svc = env["store"], env["service"]
    plan_date = date(2026, 11, 20)
    # Simulate: a run started (status running) but the process died before the
    # snapshot was published.
    store.begin_attempt(
        plan_date,
        due_utc=slot_for(plan_date, "02:30", svc.tz).instant,
        configured_time="02:30",
        shifted=False,
        ambiguous=False,
        partial_path=backup.backup_filename(plan_date) + ".partial",
        virtual_now=utc(2026, 11, 20, 7, 30),
        real_now=utc(2026, 11, 20, 7, 30),
    )
    with open(os.path.join(env["out"], ".jobs-2026-11-20.sqlite.crashed.tmp"), "wb") as fh:
        fh.write(b"half-written")
    store.close()

    store2 = Store(env["state"])
    svc2 = Service(store2, env["src"], env["out"], tick_seconds=0.01, clock=env["clock"])
    try:
        svc2.recover()
        assert store2.get_plan(plan_date)["status"] == "pending"
        assert [n for n in os.listdir(env["out"]) if n.endswith(".tmp")] == []
        svc2.tick()
        row = store2.get_plan(plan_date)
        assert row["status"] == "complete"
        assert row["attempts"] == 2  # one failed before, one succeeded now
    finally:
        store2.close()


def test_completed_backup_survives_restart_and_is_reused(env):
    svc, store = env["service"], env["store"]
    svc.tick()
    path = backup.final_path(env["out"], date(2026, 11, 20))
    original = backup.sha256_of(path)
    store.close()

    store2 = Store(env["state"])
    svc2 = Service(store2, env["src"], env["out"], tick_seconds=0.01, clock=env["clock"])
    try:
        svc2.recover()
        svc2.tick()
        # A manual re-run must not overwrite the complete file.
        result = svc2.run_plan_now(date(2026, 11, 20))
        assert result["action"] == "none"
        assert backup.sha256_of(path) == original
    finally:
        store2.close()


def test_adoption_rebuilds_missing_plan_row(env):
    """If the state DB is lost, complete files are re-registered on startup."""
    store, svc = env["store"], env["service"]
    svc.tick()
    path = backup.final_path(env["out"], date(2026, 11, 20))
    original = backup.sha256_of(path)
    mtime = os.path.getmtime(path)
    store.clear_plans()
    store.close()

    store2 = Store(env["state"])
    svc2 = Service(store2, env["src"], env["out"], tick_seconds=0.01, clock=env["clock"])
    try:
        svc2.recover()
        row = store2.get_plan(date(2026, 11, 20))
        assert row is not None and row["status"] == "complete"
        assert row["filename"] == "jobs-2026-11-20.sqlite"
        assert os.path.getmtime(path) == mtime
        assert backup.sha256_of(path) == original
    finally:
        store2.close()


def test_moving_backup_time_later_takes_effect_before_it_fires(env):
    store, svc, clock = env["store"], env["service"], env["clock"]
    clock.set(utc(2026, 11, 21, 4, 0))  # 2026-11-20 23:00 EST
    svc.tick()  # baseline for 11-20 (its 02:30 already ran below)
    store.set_config({"backup_time": "04:15"})
    clock.set(utc(2026, 11, 21, 9, 20))  # 04:20 EST on the 21st
    svc.tick()
    plans = {p["plan_date"]: p for p in store.all_plans()}
    # The 21st's plan was still pending when the time changed, so it follows 04:15.
    assert plans["2026-11-21"]["status"] == "complete"
    assert plans["2026-11-21"]["configured_time"] == "04:15"
    assert plans["2026-11-21"]["due_utc"].startswith("2026-11-21T09:15")
