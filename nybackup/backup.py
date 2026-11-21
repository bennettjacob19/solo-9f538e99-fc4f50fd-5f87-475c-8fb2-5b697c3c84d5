"""Snapshot + atomic publication of a backup file.

Requirements implemented here:

* the source keeps accepting writes while a backup runs;
* the published ``.sqlite`` file is a self-contained database that can be
  opened on its own (no ``-wal`` / ``-shm`` side cars, no locking);
* the content comes from one instant (a single consistent snapshot);
* an incomplete file is never offered for download;
* a fully written file survives a restart and is reused as-is.

``VACUUM INTO`` is used for the snapshot.  SQLite runs it inside a single read
transaction, so the output is a point-in-time copy, and it writes a compacted
standalone database with ``journal_mode=delete``.

The snapshot is written to a hidden ``.<name>.<uuid>.tmp`` file and only
renamed to its public name after it has been opened independently, verified
and fsync'd.  ``os.replace`` on the same filesystem is atomic, so a reader can
never observe a half-written backup under the public name.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from . import source

TMP_SUFFIX = ".tmp"
PARTIAL_SUFFIX = ".partial"
BACKUP_RE = re.compile(r"^jobs-(\d{4}-\d{2}-\d{2})\.sqlite$")

_write_lock = threading.Lock()


def backup_filename(plan_date: date) -> str:
    return f"jobs-{plan_date.isoformat()}.sqlite"


def plan_date_from_filename(name: str) -> Optional[date]:
    match = BACKUP_RE.match(name)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


@dataclass
class SnapshotResult:
    filename: str
    path: str
    size: int
    sha256: str
    jobs: int
    events: int
    write_seq: int
    completed_real_utc: datetime


class BackupError(RuntimeError):
    pass


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def final_path(backup_dir: str, plan_date: date) -> str:
    return os.path.join(backup_dir, backup_filename(plan_date))


def _fsync_file(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(tmp_path: str, destination: str) -> None:
    """Durably move a finished snapshot to its public name."""
    _fsync_file(tmp_path)
    os.replace(tmp_path, destination)
    _fsync_dir(os.path.dirname(os.path.abspath(destination)))


def take_snapshot(
    source_path: str,
    backup_dir: str,
    plan_date: date,
    *,
    before_publish=None,
) -> SnapshotResult:
    """Create a standalone copy of ``source_path`` and publish it atomically."""
    os.makedirs(backup_dir, exist_ok=True)
    final = final_path(backup_dir, plan_date)
    if os.path.exists(final) and os.path.getsize(final) > 0:
        raise FileExistsError(final)

    with _write_lock:
        token = f"{os.getpid()}-{threading.get_ident()}-{datetime.now(timezone.utc).timestamp():.6f}"
        tmp_name = os.path.join(backup_dir, f".{backup_filename(plan_date)}.{token}{TMP_SUFFIX}")
        sidecars = [tmp_name + s for s in ("-journal", "-wal", "-shm")]

        # A read-only connection never checkpoints the source; the result is
        # still the single consistent view of the WAL at one instant.
        conn = sqlite3.connect(
            f"file:{os.path.abspath(source_path)}?mode=ro",
            uri=True,
            timeout=30,
            isolation_level=None,
        )
        try:
            conn.execute("PRAGMA busy_timeout = 60000")
            conn.execute("VACUUM INTO ?", (tmp_name,))
        finally:
            conn.close()

        try:
            info = source.verify(tmp_name)
            if info["integrity"] != "ok":
                raise BackupError(f"备份文件完整性检查失败: {info['integrity']}")
            if info["foreign_key_violations"] != 0:
                raise BackupError("备份文件存在外键不一致，疑似非同一时点")
            # ``write_seq`` is maintained by a trigger on every job insert, so a
            # mismatch means the copy mixed pages from different instants.
            # A foreign/older schema without the trigger is simply not checked.
            if info["managed"] and info["write_seq"] != info["jobs"]:
                raise BackupError("快照内写入计数与作业数不一致，疑似非同一时点")
            digest = sha256_of(tmp_name)
            if before_publish is not None:
                before_publish(tmp_name)
            publish(tmp_name, final)
        except BaseException:
            for leftover in [tmp_name] + sidecars:
                try:
                    os.remove(leftover)
                except FileNotFoundError:
                    pass
            raise

    return SnapshotResult(
        filename=os.path.basename(final),
        path=final,
        size=os.path.getsize(final),
        sha256=digest,
        jobs=info["jobs"],
        events=info["events"],
        write_seq=info["write_seq"],
        completed_real_utc=file_mtime_utc(final),
    )


def inspect_file(path: str) -> dict:
    """Open an SQLite file in isolation and report its contents."""
    return source.verify(path)


def file_mtime_utc(path: str) -> datetime:
    return datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)


def cleanup_leftovers(backup_dir: str) -> list[str]:
    """Remove temp/partial files from interrupted runs.

    Completed backups have no ``.tmp``/``.partial`` suffix, so nothing served
    for download can be removed here.
    """
    removed = []
    if not os.path.isdir(backup_dir):
        return removed
    for name in os.listdir(backup_dir):
        if not (name.endswith(TMP_SUFFIX) or name.endswith(PARTIAL_SUFFIX)):
            continue
        try:
            os.remove(os.path.join(backup_dir, name))
            removed.append(name)
        except OSError:
            pass
    return removed


def list_backup_files(backup_dir: str) -> dict[str, str]:
    """Map plan-date ISO strings to complete backup file paths."""
    result: dict[str, str] = {}
    if not os.path.isdir(backup_dir):
        return result
    for name in sorted(os.listdir(backup_dir)):
        d = plan_date_from_filename(name)
        if d is None:
            continue
        path = os.path.join(backup_dir, name)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            result[d.isoformat()] = path
    return result
