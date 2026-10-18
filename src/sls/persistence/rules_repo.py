"""Immutable, versioned land-use rule sets imported from Excel."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..core.errors import RuleImportError, ValidationError
from ..rules.excel_rules import ColumnMapping, LandUseRule, RuleValidation, read_sheet, select_rule, validate
from .db import new_id, now_iso

if TYPE_CHECKING:
    from .users import User
    from .workspace import Workspace


@dataclass(frozen=True)
class RuleSetVersion:
    id: str
    rule_set_id: str
    rule_set_name: str
    version: int
    workbook_name: str
    workbook_sha256: str
    workbook_file_id: str
    sheet_name: str
    header_row: int
    mapping: dict
    imported_at: str
    imported_by: str
    verified: bool
    verified_by: str
    verified_at: str

    @property
    def label(self) -> str:
        state = "✔" if self.verified else "⚠"
        return f"{self.rule_set_name} v{self.version} {state} ({self.workbook_name})"


class RuleRepository:
    def __init__(self, ws: "Workspace") -> None:
        self.ws = ws
        self.db = ws.db

    def validate_workbook(self, path: Path, sheet: str, header_row: int, mapping: ColumnMapping) -> RuleValidation:
        return validate(read_sheet(path, sheet, header_row), mapping)

    def import_version(self, user: "User", rule_set_name: str, path: Path, sheet: str, header_row: int,
                       mapping: ColumnMapping) -> RuleSetVersion:
        user.require("import_rules")
        if not rule_set_name.strip():
            raise ValidationError("Rule set name is required.")
        result = self.validate_workbook(path, sheet, header_row, mapping)
        if not result.ok:
            raise RuleImportError(f"The rule table has {len(result.errors)} validation error(s); nothing was imported.",
                                  errors=[e.__dict__ for e in result.errors])
        with self.db.transaction():
            rs = self.db.query_one("SELECT id FROM rule_sets WHERE name=?", (rule_set_name.strip(),))
            if rs is None:
                rs_id = new_id()
                self.db.execute("INSERT INTO rule_sets(id,name,created_at) VALUES (?,?,?)",
                                (rs_id, rule_set_name.strip(), now_iso()))
            else:
                rs_id = rs["id"]
            version = int(self.db.query_one("SELECT COALESCE(MAX(version),0) FROM rule_set_versions WHERE rule_set_id=?",
                                            (rs_id,))[0]) + 1
            fr = self.ws.files.add(user, Path(path), None, "rule_workbook")
            vid = new_id()
            self.db.execute(
                "INSERT INTO rule_set_versions(id,rule_set_id,version,workbook_file_id,workbook_name,workbook_sha256,"
                "sheet_name,header_row,mapping_json,imported_at,imported_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (vid, rs_id, version, fr.id, Path(path).name, fr.sha256, sheet, header_row,
                 json.dumps(mapping.to_dict(), ensure_ascii=False), now_iso(), user.username),
            )
            for r in result.rules:
                self.db.execute(
                    "INSERT INTO land_use_rules(id,version_id,code,name_ar,name_en,setbacks_json,max_coverage_pct,"
                    "max_floors,max_far,notes,source_row) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (new_id(), vid, r.code, r.name_ar, r.name_en, json.dumps(r.setbacks, sort_keys=True),
                     r.max_coverage_pct, r.max_floors, r.max_far, r.notes, r.source_row),
                )
            self.ws.audit.record(user, "import_rules", "rule_set_version", vid, None, rule_set=rule_set_name,
                                 version=version, workbook=Path(path).name, sha256=fr.sha256, rows=len(result.rules))
        return self.get_version(vid)

    def get_version(self, version_id: str) -> RuleSetVersion:
        r = self.db.query_one(
            "SELECT v.*, s.name AS rs_name FROM rule_set_versions v JOIN rule_sets s ON s.id=v.rule_set_id WHERE v.id=?",
            (version_id,))
        if r is None:
            raise ValidationError(f"Rule version not found: {version_id}")
        return RuleSetVersion(r["id"], r["rule_set_id"], r["rs_name"], r["version"], r["workbook_name"],
                              r["workbook_sha256"], r["workbook_file_id"], r["sheet_name"], r["header_row"],
                              json.loads(r["mapping_json"]), r["imported_at"], r["imported_by"], bool(r["verified"]),
                              r["verified_by"], r["verified_at"])

    def list_versions(self) -> list[RuleSetVersion]:
        rows = self.db.query("SELECT v.id FROM rule_set_versions v JOIN rule_sets s ON s.id=v.rule_set_id "
                             "ORDER BY s.name, v.version DESC")
        return [self.get_version(r["id"]) for r in rows]

    def rules(self, version_id: str) -> list[LandUseRule]:
        rows = self.db.query("SELECT * FROM land_use_rules WHERE version_id=? ORDER BY source_row", (version_id,))
        return [LandUseRule(code=r["code"], setbacks=json.loads(r["setbacks_json"]), source_row=r["source_row"],
                            name_ar=r["name_ar"], name_en=r["name_en"], max_coverage_pct=r["max_coverage_pct"],
                            max_floors=r["max_floors"], max_far=r["max_far"], notes=r["notes"]) for r in rows]

    def rule(self, version_id: str, code: str) -> LandUseRule:
        return select_rule(self.rules(version_id), code)

    def verify_version(self, user: "User", version_id: str) -> None:
        user.require("verify_rules")
        with self.db.transaction():
            self.db.execute("UPDATE rule_set_versions SET verified=1, verified_by=?, verified_at=? WHERE id=?",
                            (user.username, now_iso(), version_id))
            self.ws.audit.record(user, "verify_rules", "rule_set_version", version_id)
