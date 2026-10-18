"""Database backup and restore using SQLite's online backup API."""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.errors import StorageError

if TYPE_CHECKING:
    from .db import Database

BACKUP_PREFIX = "sls_backup_"


def create_backup(db: "Database", backup_dir: Path, reason: str = "manual") -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    safe_reason = "".join(c if c.isalnum() or c in "-_" else "_" for c in reason)
    target = backup_dir / f"{BACKUP_PREFIX}{stamp}_{safe_reason}.sqlite3"
    dest = sqlite3.connect(str(target))
    try:
        db.conn.backup(dest)
    finally:
        dest.close()
    verify_backup(target)
    return target


def verify_backup(path: Path) -> None:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()
    if result != "ok":
        raise StorageError(f"Backup integrity check failed for {path}: {result}")


def list_backups(backup_dir: Path) -> list[Path]:
    if not backup_dir.exists():
        return []
    return sorted(backup_dir.glob(f"{BACKUP_PREFIX}*.sqlite3"))


def prune_backups(backup_dir: Path, keep: int) -> list[Path]:
    backups = list_backups(backup_dir)
    removed = backups[:-keep] if keep > 0 and len(backups) > keep else []
    for p in removed:
        p.unlink()
    return removed


def backup_if_due(db: "Database", backup_dir: Path, keep: int) -> Path | None:
    """Automatic daily backup: create one if none exists for today (UTC)."""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    if any(p.name.startswith(f"{BACKUP_PREFIX}{today}") for p in list_backups(backup_dir)):
        return None
    path = create_backup(db, backup_dir, reason="daily")
    prune_backups(backup_dir, keep)
    return path


def restore_backup(backup: Path, db_path: Path) -> Path:
    """Replace the database at ``db_path`` with ``backup``.

    The application must have closed its connection first. The current database is
    preserved next to it as ``<name>.before-restore-<stamp>`` and is never deleted.
    Returns the path of the preserved copy.
    """
    verify_backup(backup)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    preserved = db_path.with_name(f"{db_path.name}.before-restore-{stamp}")
    if db_path.exists():
        shutil.copy2(db_path, preserved)
    for suffix in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + suffix)
        if side.exists():
            side.unlink()
    shutil.copy2(backup, db_path)
    return preserved
