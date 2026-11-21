"""The sample job store that gets backed up.

The database uses WAL and full synchronisation so that a consistent snapshot
can be taken while the web page keeps appending records.
"""

from __future__ import annotations

import os
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from .db import connect, transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    site        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',
    priority    INTEGER NOT NULL DEFAULT 3,
    note        TEXT NOT NULL DEFAULT '',
    created_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL REFERENCES jobs(id),
    ts_utc     TEXT NOT NULL,
    message    TEXT NOT NULL
);

-- Monotonic write counter, bumped by a trigger in the same transaction as
-- each append.  Any writer (web page, scripts, tests) therefore keeps
-- ``write_seq`` equal to the number of job rows, which lets a backup verify
-- that its contents really come from one point in time.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Baseline: the counter starts at the number of existing rows.  OR IGNORE
-- keeps it untouched on re-runs of this schema script.
INSERT OR IGNORE INTO meta(key, value)
SELECT 'write_seq', CAST((SELECT COUNT(*) FROM jobs) AS TEXT);

CREATE TRIGGER IF NOT EXISTS jobs_seq_after_insert
AFTER INSERT ON jobs
BEGIN
    UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'write_seq';
END;
"""

SITES = ["曼哈顿中城", "布鲁克林", "皇后区", "布朗克斯", "斯塔滕岛"]
TITLES = [
    "冷库压缩机巡检",
    "地铁站厅照明更换",
    "写字楼消防阀门测试",
    "便利店冷柜除霜",
    "仓库叉车电池保养",
    "屋顶空调滤网清洗",
    "后厨排烟管道清理",
]
STATUSES = ["queued", "in_progress", "done", "blocked"]


def ensure_schema(path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def _write_seq(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT value FROM meta WHERE key='write_seq'").fetchone()[0])


def seed(path: str, count: int = 12, force: bool = False) -> int:
    """Insert a handful of demo records into an empty (or forced) store."""
    ensure_schema(path)
    conn = connect(path)
    try:
        existing = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        if existing and not force:
            return 0
        rng = random.Random(20261121)
        now = datetime.now(timezone.utc)
        with transaction(conn):
            for _ in range(count):
                created = now - timedelta(hours=rng.randint(1, 240))
                cur = conn.execute(
                    "INSERT INTO jobs(title, site, status, priority, note, created_utc) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        rng.choice(TITLES),
                        rng.choice(SITES),
                        rng.choice(STATUSES),
                        rng.randint(1, 5),
                        "样例数据",
                        created.isoformat(timespec="seconds"),
                    ),
                )
                job_id = cur.lastrowid
                conn.execute(
                    "INSERT INTO job_events(job_id, ts_utc, message) VALUES(?,?,?)",
                    (job_id, created.isoformat(timespec="seconds"), "作业单已创建（样例）"),
                )
        return count
    finally:
        conn.close()


def append_record(
    path: str,
    title: Optional[str] = None,
    site: Optional[str] = None,
    *,
    source: str = "web",
) -> dict:
    """Append one job plus its initial event and bump the counter atomically."""
    ensure_schema(path)
    conn = connect(path)
    try:
        now = datetime.now(timezone.utc)
        title = (title or "").strip() or f"网页追加作业 {now.strftime('%m-%d %H:%M:%S')}"
        site = (site or "").strip() or SITES[int(now.timestamp()) % len(SITES)]
        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO jobs(title, site, status, priority, note, created_utc) "
                "VALUES(?,?,?,?,?,?)",
                (
                    title[:200],
                    site[:80],
                    "queued",
                    3,
                    f"由{source}追加",
                    now.isoformat(timespec="seconds"),
                ),
            )
            job_id = cur.lastrowid
            conn.execute(
                "INSERT INTO job_events(job_id, ts_utc, message) VALUES(?,?,?)",
                (job_id, now.isoformat(timespec="microseconds"), f"通过{source}追加"),
            )
            seq = _write_seq(conn)
        return {
            "id": job_id,
            "title": title,
            "site": site,
            "write_seq": seq,
            "created_utc": now.isoformat(),
        }
    finally:
        conn.close()


def recent_jobs(path: str, limit: int = 15) -> list[sqlite3.Row]:
    if not os.path.exists(path):
        return []
    conn = connect(path, readonly=True)
    try:
        return list(
            conn.execute(
                "SELECT id, title, site, status, priority, created_utc "
                "FROM jobs ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        )
    finally:
        conn.close()


def stats(path: str) -> dict:
    if not os.path.exists(path):
        return {"jobs": 0, "events": 0, "write_seq": 0, "size": 0}
    conn = connect(path, readonly=True)
    try:
        jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM job_events").fetchone()[0]
        seq = int(conn.execute("SELECT value FROM meta WHERE key='write_seq'").fetchone()[0])
    finally:
        conn.close()
    return {"jobs": jobs, "events": events, "write_seq": seq, "size": os.path.getsize(path)}


def verify(path: str) -> dict:
    """Open a database independently and assert it is usable."""
    conn = connect(path, readonly=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        triggers = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        jobs = (
            conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            if "jobs" in tables
            else 0
        )
        events = (
            conn.execute("SELECT COUNT(*) FROM job_events").fetchone()[0]
            if "job_events" in tables
            else 0
        )
        seq = 0
        if "meta" in tables:
            row = conn.execute("SELECT value FROM meta WHERE key='write_seq'").fetchone()
            if row is not None:
                seq = int(row[0])
        return {
            "integrity": integrity,
            "foreign_key_violations": len(fk),
            "jobs": jobs,
            "events": events,
            "write_seq": seq,
            # The counter is only authoritative when our trigger maintains it.
            "managed": "jobs_seq_after_insert" in triggers,
        }
    finally:
        conn.close()
