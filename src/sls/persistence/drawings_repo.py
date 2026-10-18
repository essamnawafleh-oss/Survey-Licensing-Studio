"""Drawings (survey / regulatory), immutable drawing revisions and the parcel record."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..cad.model import Drawing
from ..core.errors import ValidationError
from .db import new_id, now_iso

if TYPE_CHECKING:
    from .users import User
    from .workspace import Workspace


@dataclass
class DrawingRecord:
    id: str
    case_id: str
    role: str
    name: str
    source_file_id: str | None
    import_report: dict[str, Any]
    created_at: str


@dataclass
class RevisionRecord:
    id: str
    drawing_id: str
    revision: int
    model_file_id: str
    content_sha256: str
    note: str
    created_at: str
    created_by: str


@dataclass
class ParcelRecord:
    case_id: str
    id: str = ""
    identifier: str = ""
    boundary_entity_id: str = ""
    boundary_points: list[list[float]] = field(default_factory=list)  # verified snapshot (drawing units)
    edge_types: list[str] = field(default_factory=list)
    verified: bool = False
    verified_by: str = ""
    verified_at: str = ""

    def verified_against(self, current_points: list[list[float]]) -> bool:
        """True only if verified AND the boundary has not changed since verification."""
        if not self.verified or len(current_points) != len(self.boundary_points):
            return False
        return all(abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9
                   for a, b in zip(current_points, self.boundary_points))


class DrawingRepository:
    def __init__(self, ws: "Workspace") -> None:
        self.ws = ws
        self.db = ws.db

    def create(self, user: "User", case_id: str, role: str, name: str, drawing: Drawing,
               source_path: Path | None = None, import_report: dict[str, Any] | None = None) -> DrawingRecord:
        user.require("edit_drawing")
        if role not in ("survey", "regulatory"):
            raise ValidationError(f"Invalid drawing role: {role}")
        with self.db.transaction():
            src_id = None
            if source_path is not None:
                # Byte-for-byte copy of the original source file (never modified).
                src_id = self.ws.files.add(user, Path(source_path), case_id, "drawing_source").id
            did = new_id()
            self.db.execute(
                "INSERT INTO drawings(id,case_id,role,name,source_file_id,import_report_json,created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (did, case_id, role, name, src_id, json.dumps(import_report or {}, ensure_ascii=False, default=str),
                 now_iso()),
            )
            self.ws.audit.record(user, "import_drawing", "drawing", did, case_id, role=role, name=name,
                                 source_file_id=src_id)
            self.save_revision(user, did, drawing, "Initial import" if source_path else "Created")
        return self.get(did)

    def get(self, drawing_id: str) -> DrawingRecord:
        r = self.db.query_one("SELECT * FROM drawings WHERE id=?", (drawing_id,))
        if r is None:
            raise ValidationError(f"Drawing not found: {drawing_id}")
        return DrawingRecord(r["id"], r["case_id"], r["role"], r["name"], r["source_file_id"],
                             json.loads(r["import_report_json"]), r["created_at"])

    def for_case(self, case_id: str, role: str | None = None) -> list[DrawingRecord]:
        sql = "SELECT id FROM drawings WHERE case_id=?" + (" AND role=?" if role else "") + " ORDER BY created_at"
        params = (case_id, role) if role else (case_id,)
        return [self.get(r["id"]) for r in self.db.query(sql, params)]

    def save_revision(self, user: "User", drawing_id: str, drawing: Drawing, note: str = "") -> RevisionRecord:
        user.require("edit_drawing")
        latest = self.latest_revision(drawing_id)
        text = drawing.to_json()  # serialised once; its hash is the content hash
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if latest is not None and latest.content_sha256 == content_hash:
            return latest  # unchanged: no new revision
        rec = self.get(drawing_id)
        number = (latest.revision if latest else 0) + 1
        data = text.encode("utf-8")
        with self.db.transaction():
            fr = self.ws.files.add_bytes(user, data, f"drawing_r{number:04d}.slsd.json", rec.case_id,
                                         "drawing_revision")
            rid = new_id()
            self.db.execute(
                "INSERT INTO drawing_revisions(id,drawing_id,revision,model_file_id,content_sha256,note,created_at,"
                "created_by) VALUES (?,?,?,?,?,?,?,?)",
                (rid, drawing_id, number, fr.id, content_hash, note, now_iso(), user.username),
            )
            self.ws.audit.record(user, "save_drawing_revision", "drawing_revision", rid, rec.case_id,
                                 revision=number, sha256=content_hash, note=note)
        return self.revision(rid)

    def revision(self, revision_id: str) -> RevisionRecord:
        r = self.db.query_one("SELECT * FROM drawing_revisions WHERE id=?", (revision_id,))
        if r is None:
            raise ValidationError(f"Drawing revision not found: {revision_id}")
        return RevisionRecord(r["id"], r["drawing_id"], r["revision"], r["model_file_id"], r["content_sha256"],
                              r["note"], r["created_at"], r["created_by"])

    def revisions(self, drawing_id: str) -> list[RevisionRecord]:
        rows = self.db.query("SELECT id FROM drawing_revisions WHERE drawing_id=? ORDER BY revision", (drawing_id,))
        return [self.revision(r["id"]) for r in rows]

    def latest_revision(self, drawing_id: str) -> RevisionRecord | None:
        r = self.db.query_one("SELECT id FROM drawing_revisions WHERE drawing_id=? ORDER BY revision DESC LIMIT 1",
                              (drawing_id,))
        return self.revision(r["id"]) if r else None

    def load(self, revision_id: str) -> Drawing:
        rev = self.revision(revision_id)
        path = self.ws.files.path(rev.model_file_id)
        raw = path.read_bytes()
        # Integrity check on the stored bytes (equivalent to the content hash, without re-serialising).
        if hashlib.sha256(raw).hexdigest() != rev.content_sha256:
            raise ValidationError(f"Drawing revision {rev.revision} failed its integrity check.")
        drawing = Drawing.from_json(raw.decode("utf-8"))
        return drawing

    # -- parcel ----------------------------------------------------------------------

    def parcel(self, case_id: str) -> ParcelRecord:
        r = self.db.query_one("SELECT * FROM parcels WHERE case_id=?", (case_id,))
        if r is None:
            return ParcelRecord(case_id)
        return ParcelRecord(case_id, r["id"], r["identifier"], r["boundary_entity_id"],
                            json.loads(r["boundary_points_json"]), json.loads(r["edge_types_json"]),
                            bool(r["verified"]), r["verified_by"], r["verified_at"])

    def save_parcel(self, user: "User", rec: ParcelRecord, verify: bool = False) -> ParcelRecord:
        user.require("verify_boundary" if verify else "edit_drawing")
        if verify:
            if not rec.boundary_entity_id or len(rec.boundary_points) < 3:
                raise ValidationError("Select a closed property boundary before verifying it.")
            rec.verified, rec.verified_by, rec.verified_at = True, user.username, now_iso()
        was_new = not rec.id
        try:
            return self._save_parcel(user, rec, verify)
        except BaseException:
            if was_new:
                rec.id = ""  # a failed first save can be retried (it inserts again)
            raise

    def _save_parcel(self, user: "User", rec: ParcelRecord, verify: bool) -> ParcelRecord:
        with self.db.transaction():
            if not rec.id:
                rec.id = new_id()
                self.db.execute("INSERT INTO parcels(id,case_id) VALUES (?,?)", (rec.id, rec.case_id))
            self.db.execute(
                "UPDATE parcels SET identifier=?,boundary_entity_id=?,boundary_points_json=?,edge_types_json=?,verified=?,"
                "verified_by=?,verified_at=? WHERE id=?",
                (rec.identifier, rec.boundary_entity_id, json.dumps(rec.boundary_points), json.dumps(rec.edge_types),
                 int(rec.verified), rec.verified_by, rec.verified_at, rec.id),
            )
            self.ws.audit.record(user, "verify_boundary" if verify else "save_parcel", "parcel", rec.id, rec.case_id,
                                 boundary_entity_id=rec.boundary_entity_id, edge_types=rec.edge_types,
                                 verified=rec.verified)
        return rec
