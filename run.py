#!/usr/bin/env python3
"""Flask 操作页入口。

Environment variables (all optional):

    SOURCE_DB    path of the job database that is backed up  (data/jobs.db)
    STATE_DB     path of the app's own bookkeeping database  (data/state.db)
    BACKUP_DIR   directory that receives backups             (backups)
    TICK_SECONDS scheduler polling interval                  (1)
    HOST / PORT  bind address                                (0.0.0.0:8000)

Run with ``python run.py`` (or ``flask --app nybackup.web run``).
"""

from __future__ import annotations

import os
import shutil
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from nybackup import source  # noqa: E402
from nybackup.serve import main  # noqa: E402

SAMPLE = os.path.join(BASE_DIR, "sample", "jobs.sqlite")


def _bootstrap_sample() -> None:
    """Copy the shipped sample database into place on first start."""
    target = os.path.abspath(os.environ.get("SOURCE_DB", os.path.join(BASE_DIR, "data", "jobs.db")))
    if os.path.exists(target):
        return
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.exists(SAMPLE):
        shutil.copyfile(SAMPLE, target)
        print(f"[bootstrap] 已复制样例源库 {SAMPLE} -> {target}")
    else:
        source.ensure_schema(target)
        source.seed(target)
        print(f"[bootstrap] 样例源库缺失，已新建并填充 {target}")


if __name__ == "__main__":
    _bootstrap_sample()
    main()
