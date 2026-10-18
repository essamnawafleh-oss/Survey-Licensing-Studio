"""Report export revisions (draft / reviewed / approved). Earlier revisions are never removed."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..core.errors import ValidationError
from .db import new_id

if TYPE_CHECKING:
    from .users import User
    from .workspace import Workspace

REPORT_STATUSES = ("draft", "reviewed", "approved")
_STATUS_PERMISSION = {"draft": "generate_report", "reviewed": "review_report", "approved": "approve_report"}


@dataclass
class ReportExport:
    id: str
    case_id: str
    revision: int
    status: str
    calculation_id: str | None
    file_id: str
    manifest: dict[str, Any]
    options: dict[str, Any]
    generated_at: str
    generated_by: str


class ReportRepository:
    def __init__(self, ws: "Workspace") -> None:
        self.ws = ws
        self.db = ws.db

    def next_revision(self, case_id: str) -> int:
        return int(self.db.query_one("SELECT COALESCE(MAX(revision),0) FROM report_exports WHERE case_id=?",
                                     (case_id,))[0]) + 1

    def save(self, user: "User", case_id: str, revision: int, status: str, calculation_id: str | None,
             pdf_bytes: bytes, file_name: str, manifest: dict[str, Any], options: dict[str, Any],
             generated_at: str) -> ReportExport:
        if status not in REPORT_STATUSES:
            raise ValidationError(f"Invalid report status: {status}")
        user.require(_STATUS_PERMISSION[status])
        with self.db.transaction():
            fr = self.ws.files.add_bytes(user, pdf_bytes, file_name, case_id, "report")
            rid = new_id()
            self.db.execute(
                "INSERT INTO report_exports(id,case_id,revision,status,calculation_id,file_id,manifest_json,options_json,"
                "generated_at,generated_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rid, case_id, revision, status, calculation_id, fr.id,
                 json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                 json.dumps(options, ensure_ascii=False, sort_keys=True), generated_at, user.username),
            )
            self.ws.audit.record(user, f"report_{status}", "report_export", rid, case_id, revision=revision,
                                 sha256=fr.sha256, calculation_id=calculation_id)
        return self.get(rid)

    def get(self, report_id: str) -> ReportExport:
        r = self.db.query_one("SELECT * FROM report_exports WHERE id=?", (report_id,))
        if r is None:
            raise ValidationError(f"Report not found: {report_id}")
        return ReportExport(r["id"], r["case_id"], r["revision"], r["status"], r["calculation_id"], r["file_id"],
                            json.loads(r["manifest_json"]), json.loads(r["options_json"]), r["generated_at"],
                            r["generated_by"])

    def list(self, case_id: str) -> list[ReportExport]:
        rows = self.db.query("SELECT id FROM report_exports WHERE case_id=? ORDER BY revision DESC", (case_id,))
        return [self.get(r["id"]) for r in rows]
