"""Previous-license register.

Editing a record bumps its revision and resets verification to 'unverified', so an
approved calculation can always be traced to the exact record revision it used.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.errors import ValidationError
from ..licensing.reconcile import LicenseRecordInput
from .db import new_id, now_iso

if TYPE_CHECKING:
    from .users import User
    from .workspace import Workspace

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VERIFICATION_STATES = ("unverified", "verified", "disputed")


@dataclass
class LicensedArea:
    floor_key: str
    area_m2: float
    note: str = ""


@dataclass
class LicensedLength:
    kind: str  # wall | fence
    length_m: float
    note: str = ""


@dataclass
class ApprovedViolation:
    description: str
    area_m2: float | None = None


@dataclass
class LicenseDocument:
    id: str
    file_id: str
    original_name: str
    page_refs: str = ""
    include_in_report: bool = True
    page_selection: str = ""
    note: str = ""


@dataclass
class LicenseRecord:
    case_id: str
    license_number: str
    id: str = ""
    issue_date: str = ""
    structure_description: str = ""
    fee_amount: float | None = None
    fee_currency: str = ""
    source_kind: str = "transcribed"
    verification_status: str = "unverified"
    reviewer_notes: str = ""
    verified_by: str = ""
    verified_at: str = ""
    revision: int = 1
    areas: list[LicensedArea] = field(default_factory=list)
    lengths: list[LicensedLength] = field(default_factory=list)
    violations: list[ApprovedViolation] = field(default_factory=list)
    documents: list[LicenseDocument] = field(default_factory=list)

    def to_input(self) -> LicenseRecordInput:
        return LicenseRecordInput(
            self.id, self.license_number, self.revision, self.verification_status,
            tuple((a.floor_key, a.area_m2) for a in self.areas),
            tuple((x.kind, x.length_m) for x in self.lengths), self.issue_date,
        )

    def completeness_issues(self) -> list[str]:
        """Things a reviewer must look at; they are flagged, never filled in."""
        issues = []
        if not self.issue_date:
            issues.append("Issue date is missing.")
        if not self.areas and not self.lengths:
            issues.append("No licensed areas or lengths recorded.")
        if not self.documents:
            issues.append("No source document attached.")
        floors = [a.floor_key for a in self.areas]
        if len(floors) != len(set(floors)):
            issues.append("The same floor appears more than once in this license.")
        return issues


def validate_license(rec: LicenseRecord) -> list[str]:
    errors = []
    if not rec.license_number.strip():
        errors.append("License number is required.")
    if rec.issue_date and not _DATE.match(rec.issue_date):
        errors.append("Issue date must be in YYYY-MM-DD format.")
    if rec.verification_status not in VERIFICATION_STATES:
        errors.append(f"Invalid verification status: {rec.verification_status}")
    for a in rec.areas:
        if not a.floor_key.strip():
            errors.append("Every licensed area needs a floor.")
        if a.area_m2 < 0:
            errors.append(f"Licensed area for floor {a.floor_key} is negative.")
    for x in rec.lengths:
        if x.kind not in ("wall", "fence"):
            errors.append(f"Licensed length kind must be wall or fence, got {x.kind}.")
        if x.length_m < 0:
            errors.append("Licensed length is negative.")
    if rec.fee_amount is not None and rec.fee_amount < 0:
        errors.append("Fee amount is negative.")
    return errors


class LicenseRepository:
    def __init__(self, ws: "Workspace") -> None:
        self.ws = ws
        self.db = ws.db

    def save(self, user: "User", rec: LicenseRecord) -> LicenseRecord:
        user.require("edit_license")
        errors = validate_license(rec)
        if errors:
            raise ValidationError("; ".join(errors), errors=errors)
        ts = now_iso()
        with self.db.transaction():
            if not rec.id:
                rec.id = new_id()
                rec.revision = 1
                rec.verification_status, rec.verified_by, rec.verified_at = "unverified", "", ""
                self.db.execute("INSERT INTO licenses(id,case_id,license_number,created_at,updated_at)"
                                " VALUES (?,?,?,?,?)", (rec.id, rec.case_id, rec.license_number, ts, ts))
                before = None
                action = "create_license"
            else:
                before = asdict(self.get(rec.id))
                rec.revision = int(before["revision"]) + 1
                rec.verification_status, rec.verified_by, rec.verified_at = "unverified", "", ""
                action = "update_license"
            self.db.execute(
                "UPDATE licenses SET license_number=?,issue_date=?,structure_description=?,fee_amount=?,fee_currency=?,"
                "source_kind=?,verification_status=?,reviewer_notes=?,verified_by=?,verified_at=?,revision=?,updated_at=?"
                " WHERE id=?",
                (rec.license_number.strip(), rec.issue_date, rec.structure_description, rec.fee_amount, rec.fee_currency,
                 rec.source_kind, rec.verification_status, rec.reviewer_notes, rec.verified_by, rec.verified_at,
                 rec.revision, ts, rec.id),
            )
            for table in ("license_areas", "license_lengths", "license_violations"):
                self.db.execute(f"DELETE FROM {table} WHERE license_id=?", (rec.id,))
            for a in rec.areas:
                self.db.execute("INSERT INTO license_areas(id,license_id,floor_key,area_m2,note) VALUES (?,?,?,?,?)",
                                (new_id(), rec.id, a.floor_key.strip(), a.area_m2, a.note))
            for x in rec.lengths:
                self.db.execute("INSERT INTO license_lengths(id,license_id,kind,length_m,note) VALUES (?,?,?,?,?)",
                                (new_id(), rec.id, x.kind, x.length_m, x.note))
            for v in rec.violations:
                self.db.execute("INSERT INTO license_violations(id,license_id,description,area_m2) VALUES (?,?,?,?)",
                                (new_id(), rec.id, v.description, v.area_m2))
            after = asdict(rec)
            after.pop("documents", None)
            if before:
                before.pop("documents", None)
            self.ws.audit.record(user, action, "license", rec.id, rec.case_id, before=before, after=after)
        return self.get(rec.id)

    def set_verification(self, user: "User", license_id: str, status: str, reviewer_notes: str = "") -> LicenseRecord:
        user.require("verify_license")
        if status not in VERIFICATION_STATES:
            raise ValidationError(f"Invalid verification status: {status}")
        rec = self.get(license_id)
        with self.db.transaction():
            self.db.execute(
                "UPDATE licenses SET verification_status=?, reviewer_notes=?, verified_by=?, verified_at=?, updated_at=?"
                " WHERE id=?",
                (status, reviewer_notes or rec.reviewer_notes, user.username if status != "unverified" else "",
                 now_iso() if status != "unverified" else "", now_iso(), license_id),
            )
            self.ws.audit.record(user, "license_verification", "license", license_id, rec.case_id,
                                 old=rec.verification_status, new=status, revision=rec.revision, notes=reviewer_notes)
        return self.get(license_id)

    def get(self, license_id: str) -> LicenseRecord:
        r = self.db.query_one("SELECT * FROM licenses WHERE id=?", (license_id,))
        if r is None:
            raise ValidationError(f"License not found: {license_id}")
        rec = LicenseRecord(
            case_id=r["case_id"], license_number=r["license_number"], id=r["id"], issue_date=r["issue_date"],
            structure_description=r["structure_description"], fee_amount=r["fee_amount"], fee_currency=r["fee_currency"],
            source_kind=r["source_kind"], verification_status=r["verification_status"],
            reviewer_notes=r["reviewer_notes"], verified_by=r["verified_by"], verified_at=r["verified_at"],
            revision=r["revision"],
        )
        rec.areas = [LicensedArea(x["floor_key"], x["area_m2"], x["note"]) for x in
                     self.db.query("SELECT * FROM license_areas WHERE license_id=? ORDER BY rowid", (license_id,))]
        rec.lengths = [LicensedLength(x["kind"], x["length_m"], x["note"]) for x in
                       self.db.query("SELECT * FROM license_lengths WHERE license_id=? ORDER BY rowid", (license_id,))]
        rec.violations = [ApprovedViolation(x["description"], x["area_m2"]) for x in
                          self.db.query("SELECT * FROM license_violations WHERE license_id=? ORDER BY rowid",
                                        (license_id,))]
        rec.documents = [
            LicenseDocument(x["id"], x["file_id"], x["original_name"], x["page_refs"], bool(x["include_in_report"]),
                            x["page_selection"], x["note"])
            for x in self.db.query(
                "SELECT d.*, f.original_name FROM license_documents d JOIN files f ON f.id=d.file_id "
                "WHERE d.license_id=? ORDER BY d.rowid", (license_id,))
        ]
        return rec

    def list(self, case_id: str) -> list[LicenseRecord]:
        rows = self.db.query("SELECT id FROM licenses WHERE case_id=? ORDER BY issue_date, license_number", (case_id,))
        return [self.get(r["id"]) for r in rows]

    def inputs(self, case_id: str) -> list[LicenseRecordInput]:
        return [rec.to_input() for rec in self.list(case_id)]

    def attach_document(self, user: "User", license_id: str, source: Path, page_refs: str = "",
                        note: str = "") -> LicenseDocument:
        user.require("edit_license")
        rec = self.get(license_id)
        with self.db.transaction():
            fr = self.ws.files.add(user, Path(source), rec.case_id, "license_doc")
            did = new_id()
            self.db.execute("INSERT INTO license_documents(id,license_id,file_id,page_refs,note) VALUES (?,?,?,?,?)",
                            (did, license_id, fr.id, page_refs, note))
            self.ws.audit.record(user, "attach_license_document", "license", license_id, rec.case_id,
                                 file=fr.original_name, sha256=fr.sha256)
        return next(d for d in self.get(license_id).documents if d.id == did)

    def update_document(self, user: "User", document_id: str, include_in_report: bool, page_selection: str,
                        page_refs: str | None = None) -> None:
        user.require("edit_license")
        from ..reporting.pages import parse_page_selection

        parse_page_selection(page_selection, None)  # validates syntax
        with self.db.transaction():
            self.db.execute("UPDATE license_documents SET include_in_report=?, page_selection=?,"
                            " page_refs=COALESCE(?, page_refs) WHERE id=?",
                            (int(include_in_report), page_selection.strip(), page_refs, document_id))

    def add_ocr_text(self, file_id: str, engine: str, languages: str, text: str) -> str:
        oid = new_id()
        self.db.execute("INSERT INTO ocr_texts(id,file_id,engine,languages,text,reviewed,created_at) VALUES (?,?,?,?,?,0,?)",
                        (oid, file_id, engine, languages, text, now_iso()))
        return oid

    def ocr_texts(self, file_id: str) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT * FROM ocr_texts WHERE file_id=? ORDER BY created_at", (file_id,))]
