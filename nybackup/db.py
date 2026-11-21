from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator


def connect(path: str, *, readonly: bool = False, timeout: float = 30.0) -> sqlite3.Connection:
    """Open a SQLite connection with sane defaults.

    WAL keeps readers (backup) and writers (the jobs page) from blocking each
    other.  ``synchronous=FULL`` makes a committed transaction durable, which is
    what the backup snapshot relies on.
    """
    if readonly:
        uri = f"file:{os.path.abspath(path)}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=timeout, isolation_level=None)
    else:
        conn = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = %d" % int(timeout * 1000))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A write transaction that serialises concurrent appenders."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
