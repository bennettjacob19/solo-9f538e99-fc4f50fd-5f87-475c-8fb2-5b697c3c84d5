"""The shipped sample database must be usable as-is."""

import os
import sqlite3

from nybackup import source

SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sample", "jobs.sqlite")


def test_sample_exists_and_is_standalone():
    assert os.path.exists(SAMPLE)
    assert not os.path.exists(SAMPLE + "-wal")
    assert not os.path.exists(SAMPLE + "-shm")
    info = source.verify(SAMPLE)
    assert info["integrity"] == "ok"
    assert info["foreign_key_violations"] == 0
    assert info["jobs"] >= 10
    assert info["write_seq"] == info["jobs"]

    conn = sqlite3.connect(f"file:{SAMPLE}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        conn.close()
