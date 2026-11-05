from __future__ import annotations

import shutil
import time
from pathlib import Path

from .config import Config

BACKUP_INTERVAL_DAYS = 7
BACKUP_KEEP = 1


def backup_path(cfg: Config) -> Path:
    db = cfg.index_path
    return db.with_name(db.name + ".bak")


def maybe_backup_index(cfg: Config, interval_days: int = BACKUP_INTERVAL_DAYS) -> str | None:
    """索引库长期未备份时复制一份 data/index.db.bak（只保留最近 1 份）。

    返回给用户看的提示；无需备份时返回 None。只复制，不修改原库。
    """
    db = cfg.index_path
    bak = backup_path(cfg)
    if not db.exists() or db.stat().st_size == 0:
        return None
    db_mtime = db.stat().st_mtime
    stale = True
    if bak.exists():
        stale = (db_mtime - bak.stat().st_mtime) > interval_days * 86400
    if not stale:
        return None
    try:
        bak.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(db, bak)
    except OSError as exc:
        return f"索引备份失败：{exc}"
    days = (time.time() - db_mtime) / 86400
    return (
        f"索引库已超过 {int(days)} 天未备份，已复制一份到 {bak.name}（保留最近 1 份）。"
    )
