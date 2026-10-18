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
        with db.lock:
            db.conn.backup(dest)
        dest.execute("PRAGMA journal_mode=DELETE")  # a backup is a single self-contained file
    finally:
        dest.close()
    verify_backup(target)
    return target


def _ro(path: Path) -> sqlite3.Connection:
    # immutable: read without locks or -wal/-shm sidecar files (backups are closed, finished files)
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True)


def verify_backup(path: Path) -> None:
    conn = _ro(path)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        raise StorageError(f"{path.name} is not a readable SQLite database: {exc}") from exc
    finally:
        conn.close()
    if result != "ok":
        raise StorageError(f"Backup integrity check failed for {path}: {result}")


def backup_schema_version(path: Path) -> int:
    """Schema version recorded in a backup; refuses files that are not application databases."""
    conn = _ro(path)
    try:
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
                        ).fetchone() is None:
            raise StorageError(f"{path.name} is not a Survey & Licensing Studio database (no schema history).")
        return int(conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0])
    finally:
        conn.close()


def list_backups(backup_dir: Path) -> list[Path]:
    if not backup_dir.exists():
        return []
    return sorted(backup_dir.glob(f"{BACKUP_PREFIX}*.sqlite3"))


def prune_backups(backup_dir: Path, keep: int) -> list[Path]:
    """Remove old *daily* backups beyond ``keep``. Manual and pre-migration/pre-restore backups are kept."""
    backups = [p for p in list_backups(backup_dir) if p.stem.endswith("_daily")]
    removed = backups[:-keep] if keep > 0 and len(backups) > keep else []
    for p in removed:
        p.unlink()
        for suffix in ("-wal", "-shm"):  # sidecars left by older versions' verification
            p.with_name(p.name + suffix).unlink(missing_ok=True)
    return removed


def backup_if_due(db: "Database", backup_dir: Path, keep: int) -> Path | None:
    """Automatic daily backup: create one if none exists for today (UTC)."""
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    if any(p.name.startswith(f"{BACKUP_PREFIX}{today}") and p.stem.endswith("_daily")
           for p in list_backups(backup_dir)):
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
    from .schema import MIGRATIONS

    verify_backup(backup)
    version = backup_schema_version(backup)
    latest = max(m[0] for m in MIGRATIONS)
    if version > latest:
        raise StorageError(f"{backup.name} was made by a newer version of the application (schema {version}; this "
                           f"version supports up to {latest}). Install the newer version to restore it.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    preserved = db_path.with_name(f"{db_path.name}.before-restore-{stamp}")
    n = 1
    while preserved.exists():  # never overwrite an earlier preserved copy
        preserved = db_path.with_name(f"{db_path.name}.before-restore-{stamp}-{n}")
        n += 1
    if db_path.exists():
        # The SQLite backup API reads the database *with* its -wal file, so transactions committed
        # before an unclean shutdown are kept in the preserved copy.
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(preserved))
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
            src.close()
    for suffix in ("-wal", "-shm"):
        side = db_path.with_name(db_path.name + suffix)
        if side.exists():
            side.unlink()
    shutil.copy2(backup, db_path)
    return preserved
