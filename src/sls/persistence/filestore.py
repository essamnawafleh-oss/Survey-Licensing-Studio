"""Controlled local file store.

Files are copied into ``<root>/cases/<case_id>/<category>/`` (or ``<root>/shared/...``)
with a content-hash prefix. Existing files are never overwritten and source files are
never modified.
"""
from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..core.errors import StorageError

CATEGORIES = (
    "drawing_source", "drawing_revision", "license_doc", "photo", "attachment", "report",
    "rule_workbook", "cadastre", "gis",
)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name: str) -> str:
    cleaned = _UNSAFE.sub("_", name).strip(" .")
    return cleaned[:120] or "file"


@dataclass(frozen=True)
class StoredFile:
    relative_path: str
    sha256: str
    size_bytes: int
    original_name: str


class FileStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def absolute(self, relative_path: str) -> Path:
        p = (self.root / relative_path).resolve()
        if self.root.resolve() not in p.parents:
            raise StorageError(f"Path escapes the data directory: {relative_path}")
        return p

    def _target_dir(self, case_id: str | None, category: str) -> Path:
        if category not in CATEGORIES:
            raise StorageError(f"Unknown file category: {category}")
        base = self.root / ("cases/" + case_id if case_id else "shared")
        d = base / category
        d.mkdir(parents=True, exist_ok=True)
        return d

    def import_file(self, source: Path, case_id: str | None, category: str) -> StoredFile:
        source = Path(source)
        if not source.is_file():
            raise StorageError(f"Source file not found: {source}")
        digest = sha256_file(source)
        return self._place(case_id, category, source.name, digest,
                           lambda target: shutil.copy2(source, target))

    def store_bytes(self, data: bytes, name: str, case_id: str | None, category: str) -> StoredFile:
        digest = sha256_bytes(data)
        return self._place(case_id, category, name, digest, lambda target: target.write_bytes(data))

    def _place(self, case_id, category, name, digest, writer) -> StoredFile:  # type: ignore[no-untyped-def]
        target_dir = self._target_dir(case_id, category)
        base = f"{digest[:12]}_{safe_name(name)}"
        target = target_dir / base
        if target.exists():
            if sha256_file(target) == digest:
                # Identical content already stored: reuse it, nothing is overwritten.
                return self._stored(target, digest, name)
            stem, suffix = target.stem, target.suffix
            n = 1
            while target.exists():
                target = target_dir / f"{stem}_{n}{suffix}"
                n += 1
        tmp = target.with_name(target.name + ".partial")
        writer(tmp)
        if sha256_file(tmp) != digest:
            tmp.unlink(missing_ok=True)
            raise StorageError(f"Copy verification failed for {name}")
        tmp.rename(target)
        return self._stored(target, digest, name)

    def _stored(self, target: Path, digest: str, name: str) -> StoredFile:
        return StoredFile(
            relative_path=target.relative_to(self.root).as_posix(),
            sha256=digest,
            size_bytes=target.stat().st_size,
            original_name=name,
        )

    def verify(self, relative_path: str, expected_sha256: str) -> bool:
        p = self.absolute(relative_path)
        return p.is_file() and sha256_file(p) == expected_sha256
