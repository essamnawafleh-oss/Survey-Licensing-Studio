"""SQLite connection management, transactions and migrations."""
from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .schema import DEFAULT_CASE_FIELDS, MIGRATIONS


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


class Database:
    """Thin wrapper around one SQLite connection.

    The application is single-process; one connection with explicit transactions is
    sufficient and keeps behaviour deterministic.
    """

    def __init__(self, path: Path, backup_dir: Path | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.backup_dir = backup_dir
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._depth = 0

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Nestable transaction (inner levels use savepoints)."""
        if self._depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        else:
            self.conn.execute(f"SAVEPOINT sp{self._depth}")
        self._depth += 1
        try:
            yield self.conn
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("ROLLBACK")
            else:
                self.conn.execute(f"ROLLBACK TO sp{self._depth}")
                self.conn.execute(f"RELEASE sp{self._depth}")
            raise
        else:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute(f"RELEASE sp{self._depth}")

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    # -- migrations ---------------------------------------------------------------

    def schema_version(self) -> int:
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, description TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        row = self.conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)

    def migrate(self) -> list[int]:
        """Apply pending migrations. Backs up a non-empty database first."""
        current = self.schema_version()
        pending = [m for m in MIGRATIONS if m[0] > current]
        if not pending:
            return []
        if current > 0 and self.backup_dir is not None:
            from .backup import create_backup

            create_backup(self, self.backup_dir, reason=f"pre-migration-v{current}")
        applied: list[int] = []
        for version, description, sql in pending:
            # executescript would commit implicitly; run statements inside one transaction.
            with self.transaction():
                for statement in _split_sql(sql):
                    self.conn.execute(statement)
                if version == 1:
                    self._seed_defaults()
                self.conn.execute(
                    "INSERT INTO schema_migrations(version, description, applied_at) VALUES (?,?,?)",
                    (version, description, now_iso()),
                )
            applied.append(version)
        return applied

    def _seed_defaults(self) -> None:
        for order, f in enumerate(DEFAULT_CASE_FIELDS):
            self.conn.execute(
                "INSERT INTO case_field_defs(key,label_ar,label_en,field_type,sensitive,sort_order)"
                " VALUES (?,?,?,?,?,?)",
                (f["key"], f["label_ar"], f["label_en"], f["field_type"], f.get("sensitive", 0), order),
            )


def _split_sql(script: str) -> list[str]:
    """Split a migration script on ';' line ends (our scripts contain no triggers)."""
    lines = [ln for ln in script.splitlines() if not ln.strip().startswith("--")]
    parts = "\n".join(lines).split(";")
    return [p.strip() for p in parts if p.strip()]
