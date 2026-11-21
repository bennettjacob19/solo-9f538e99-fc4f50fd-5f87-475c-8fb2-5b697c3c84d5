"""Web layer tests: page rendering, append, settings, clock drill, downloads."""

from __future__ import annotations

import sqlite3
import pytest

from nybackup import backup, source
from nybackup.web import create_app


@pytest.fixture()
def app(tmp_path):
    source_path = str(tmp_path / "jobs.db")
    source.ensure_schema(source_path)
    source.seed(source_path, 3)
    app = create_app(
        {
            "SOURCE_PATH": source_path,
            "STATE_PATH": str(tmp_path / "state.db"),
            "BACKUP_DIR": str(tmp_path / "backups"),
            "AUTO_START_SCHEDULER": False,
            "TESTING": True,
        }
    )
    yield app


def test_index_lists_plans(app):
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "备份计划" in body
    assert "追加样例记录" in body


def test_append_adds_record_to_source(app):
    client = app.test_client()
    before = source.stats(app.config["SOURCE_PATH"])
    response = client.post("/append", data={"title": "测试作业", "site": "皇后区"})
    assert response.status_code == 302
    after = source.stats(app.config["SOURCE_PATH"])
    assert after["jobs"] == before["jobs"] + 1
    assert after["write_seq"] == before["write_seq"] + 1
    jobs = source.recent_jobs(app.config["SOURCE_PATH"], 1)
    assert jobs[0]["title"] == "测试作业"


def test_backup_time_validation(app):
    client = app.test_client()
    store = app.extensions["store"]
    client.post("/settings", data={"backup_time": "25:00"})
    assert store.config()["backup_time"] == "02:30"
    client.post("/settings", data={"backup_time": "23:45"})
    assert store.config()["backup_time"] == "23:45"


def test_clock_jump_and_backup_then_download(app):
    client = app.test_client()
    store = app.extensions["store"]
    service = app.extensions["service"]

    # Jump the drill clock to just after the scheduled moment today.
    target = service.now().astimezone(service.tz).strftime("%Y-%m-%d")
    client.post("/clock/jump", data={"target": f"{target}T23:59"})
    service.tick()

    plans = store.all_plans()
    assert plans, "clock jump should have materialised a plan"
    today_plan = [p for p in plans if p["status"] == "complete"]
    assert today_plan, [dict(p) for p in plans]
    plan = today_plan[-1]

    response = client.get(f"/download/{plan['plan_date']}")
    assert response.status_code == 200
    payload = response.data
    with tempfile_sqlite(payload) as path:
        conn = sqlite3.connect(path)
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            seq = int(conn.execute("SELECT value FROM meta WHERE key='write_seq'").fetchone()[0])
            assert jobs == seq
        finally:
            conn.close()


def test_download_of_unknown_plan_404(app):
    client = app.test_client()
    assert client.get("/download/1999-01-01").status_code == 404
    assert client.get("/download/not-a-date").status_code == 404


def test_partial_artifacts_are_not_downloadable(app):
    client = app.test_client()
    store = app.extensions["store"]
    service = app.extensions["service"]
    plan_date = service.now().astimezone(service.tz).date()
    store.upsert_plan(
        plan_date,
        status="complete",
        due_utc="2026-01-01T07:30:00+00:00",
        configured_time="02:30",
        completed_utc="2026-01-01T07:30:05+00:00",
        completed_real_utc="2026-01-01T07:30:05+00:00",
        filename=backup.backup_filename(plan_date),
        size_bytes=10,
        sha256="deadbeef",
    )
    # A partial file with the right name is still not a complete backup.
    with open(backup.final_path(app.config["BACKUP_DIR"], plan_date) + ".partial", "wb") as fh:
        fh.write(b"x")
    assert client.get(f"/download/{plan_date.isoformat()}").status_code == 404


def test_api_status_reports_plans(app):
    client = app.test_client()
    response = client.get("/api/status")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["backup_time"] == "02:30"
    assert "virtual_now_utc" in payload


class tempfile_sqlite:
    """Write bytes to a temp file and hand back the path."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.path = None

    def __enter__(self):
        import os
        import tempfile

        fd, self.path = tempfile.mkstemp(suffix=".sqlite")
        with os.fdopen(fd, "wb") as fh:
            fh.write(self.payload)
        return self.path

    def __exit__(self, *exc):
        import os

        os.unlink(self.path)
        return False
