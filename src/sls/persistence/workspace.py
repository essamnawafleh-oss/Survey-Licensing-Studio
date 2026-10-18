"""Workspace: the application's persistence facade (database + file store + user)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..core.settings import Settings
from .backup import backup_if_due, create_backup
from .cases import CaseRepository
from .db import Database, new_id, now_iso
from .filestore import FileStore, StoredFile
from .users import AuditLog, User, UserRepository, single_user_mode_user

DB_FILENAME = "sls.sqlite3"


@dataclass(frozen=True)
class FileRecord:
    id: str
    case_id: str | None
    category: str
    original_name: str
    stored_path: str
    sha256: str
    size_bytes: int


class FileRepository:
    def __init__(self, db: Database, store: FileStore) -> None:
        self.db = db
        self.store = store

    def add(self, user: User, source: Path, case_id: str | None, category: str) -> FileRecord:
        stored = self.store.import_file(source, case_id, category)
        return self._register(user, stored, case_id, category)

    def add_bytes(self, user: User, data: bytes, name: str, case_id: str | None, category: str) -> FileRecord:
        stored = self.store.store_bytes(data, name, case_id, category)
        return self._register(user, stored, case_id, category)

    def _register(self, user: User, stored: StoredFile, case_id: str | None, category: str) -> FileRecord:
        existing = self.db.query_one("SELECT * FROM files WHERE stored_path=?", (stored.relative_path,))
        if existing is not None:
            return self._row(existing)
        fid = new_id()
        self.db.execute(
            "INSERT INTO files(id,case_id,category,original_name,stored_path,sha256,size_bytes,imported_at,imported_by)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (fid, case_id, category, stored.original_name, stored.relative_path, stored.sha256,
             stored.size_bytes, now_iso(), user.username),
        )
        return FileRecord(fid, case_id, category, stored.original_name, stored.relative_path,
                          stored.sha256, stored.size_bytes)

    def get(self, file_id: str) -> FileRecord:
        row = self.db.query_one("SELECT * FROM files WHERE id=?", (file_id,))
        if row is None:
            raise KeyError(file_id)
        return self._row(row)

    def path(self, file_id: str) -> Path:
        return self.store.absolute(self.get(file_id).stored_path)

    @staticmethod
    def _row(r) -> FileRecord:  # type: ignore[no-untyped-def]
        return FileRecord(r["id"], r["case_id"], r["category"], r["original_name"], r["stored_path"],
                          r["sha256"], r["size_bytes"])


class Workspace:
    """Opens (and migrates) the local data directory."""

    def __init__(self, settings: Settings, user: User | None = None, auto_backup: bool = True) -> None:
        self.settings = settings
        self.root = settings.data_path
        self.root.mkdir(parents=True, exist_ok=True)
        self.backup_dir = self.root / "backups"
        self.db = Database(self.root / DB_FILENAME, backup_dir=self.backup_dir)
        self.db.migrate()
        self.store = FileStore(self.root)
        self.audit = AuditLog(self.db)
        self.users = UserRepository(self.db)
        self.cases = CaseRepository(self.db, self.audit)
        self.files = FileRepository(self.db, self.store)
        self.user: User = user or single_user_mode_user()
        self._extend()
        if auto_backup:
            backup_if_due(self.db, self.backup_dir, settings.backup_retention)

    def _extend(self) -> None:
        """Attach repositories defined in other modules (kept separate to limit file size)."""
        from .cadastre_repo import CadastreRepository
        from .calculations_repo import CalculationRepository
        from .drawings_repo import DrawingRepository
        from .licenses_repo import LicenseRepository
        from .media_repo import MediaRepository
        from .reports_repo import ReportRepository
        from .rules_repo import RuleRepository

        self.rules = RuleRepository(self)
        self.drawings = DrawingRepository(self)
        self.licenses = LicenseRepository(self)
        self.media = MediaRepository(self)
        self.calculations = CalculationRepository(self)
        self.reports = ReportRepository(self)
        self.cadastre = CadastreRepository(self)
        from .zoning_repo import ZoningRepository

        self.zoning = ZoningRepository(self)

    @property
    def db_path(self) -> Path:
        return self.db.path

    def backup_now(self, reason: str = "manual") -> Path:
        return create_backup(self.db, self.backup_dir, reason)

    def close(self) -> None:
        self.db.close()
