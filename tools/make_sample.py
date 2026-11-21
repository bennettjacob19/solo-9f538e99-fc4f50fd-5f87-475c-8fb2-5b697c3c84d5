#!/usr/bin/env python3
"""Create the sample source database and seed it with demo job records.

Run once (or with --force) to (re)create ``sample/jobs.sqlite``.  The docker
entrypoint copies this file into ``data/jobs.db`` the first time the container
starts.

    python tools/make_sample.py [--out sample/jobs.sqlite] [--count 12] [--force]
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nybackup import source  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="sample/jobs.sqlite")
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    path = os.path.abspath(args.out)
    if os.path.exists(path):
        if not args.force:
            print(f"{path} 已存在；使用 --force 覆盖。", file=sys.stderr)
            return 1
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except FileNotFoundError:
                pass

    source.ensure_schema(path)
    inserted = source.seed(path, args.count, force=True)

    # Compact to a standalone file: no -wal/-shm side cars ship with the
    # sample, so it can be copied on its own.
    tmp = path + ".compact"
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        conn.execute("VACUUM INTO ?", (tmp,))
    finally:
        conn.close()
    os.replace(tmp, path)

    stats = source.stats(path)
    print(f"已写入 {inserted} 条作业 -> {path}")
    print(f"jobs={stats['jobs']} events={stats['events']} write_seq={stats['write_seq']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
