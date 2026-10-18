"""Licensing case repository and configurable case fields."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..core.errors import ValidationError
from ..core.units import CrsKind, DrawingUnits, LengthUnit
from .db import Database, new_id, now_iso
from .users import AuditLog, User

CASE_STATUSES = ("draft", "in_progress", "in_review", "closed", "reopened")


@dataclass
class CaseFieldDef:
    key: str
    label_ar: str
    label_en: str
    field_type: str
    choices: list[str] = field(default_factory=list)
    required: bool = False
    sensitive: bool = False
    sort_order: int = 0

    def label(self, lang: str) -> str:
        return self.label_ar if lang == "ar" else self.label_en


@dataclass
class Case:
    id: str = ""
    case_number: str = ""
    status: str = "draft"
    parcel_identifier: str = ""
    survey_date: str = ""
    surveyor: str = ""
    rule_version_id: str | None = None
    land_use_code: str = ""
    units: DrawingUnits = field(default_factory=DrawingUnits)
    notes: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    created_at: str = ""
    created_by: str = ""
    updated_at: str = ""


class CaseRepository:
    def __init__(self, db: Database, audit: AuditLog) -> None:
        self.db = db
        self.audit = audit

    # -- field definitions -------------------------------------------------------

    def field_defs(self) -> list[CaseFieldDef]:
        rows = self.db.query("SELECT * FROM case_field_defs WHERE active=1 ORDER BY sort_order, key")
        return [
            CaseFieldDef(r["key"], r["label_ar"], r["label_en"], r["field_type"],
                         json.loads(r["choices_json"]), bool(r["required"]), bool(r["sensitive"]),
                         r["sort_order"])
            for r in rows
        ]

    def upsert_field_def(self, user: User, fd: CaseFieldDef) -> None:
        user.require("manage_users")
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO case_field_defs(key,label_ar,label_en,field_type,choices_json,required,sensitive,sort_order)"
                " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET label_ar=excluded.label_ar,"
                " label_en=excluded.label_en, field_type=excluded.field_type, choices_json=excluded.choices_json,"
                " required=excluded.required, sensitive=excluded.sensitive, sort_order=excluded.sort_order, active=1",
                (fd.key, fd.label_ar, fd.label_en, fd.field_type, json.dumps(fd.choices, ensure_ascii=False),
                 int(fd.required), int(fd.sensitive), fd.sort_order),
            )
            self.audit.record(user, "upsert_field_def", "case_field_def", fd.key)

    # -- cases ---------------------------------------------------------------------

    def validate(self, case: Case) -> list[str]:
        errors: list[str] = []
        if not case.case_number.strip():
            errors.append("Case number is required.")
        if case.status not in CASE_STATUSES:
            errors.append(f"Invalid case status: {case.status}")
        known = {fd.key: fd for fd in self.field_defs()}
        for key in case.fields:
            if key not in known:
                errors.append(f"Unknown case field: {key}")
        for fd in known.values():
            value = case.fields.get(fd.key, "").strip()
            if fd.required and not value:
                errors.append(f"Field '{fd.label_en}' is required.")
            if value and fd.field_type == "number":
                try:
                    float(value)
                except ValueError:
                    errors.append(f"Field '{fd.label_en}' must be a number.")
            if value and fd.field_type == "choice" and fd.choices and value not in fd.choices:
                errors.append(f"Field '{fd.label_en}' must be one of {fd.choices}.")
        return errors

    def save(self, user: User, case: Case) -> Case:
        user.require("edit_case")
        errors = self.validate(case)
        if errors:
            raise ValidationError("; ".join(errors), errors=errors)
        ts = now_iso()
        with self.db.transaction():
            dup = self.db.query_one(
                "SELECT id FROM cases WHERE case_number=? AND id<>?", (case.case_number.strip(), case.id)
            )
            if dup:
                raise ValidationError(f"Case number '{case.case_number}' already exists.")
            u = case.units
            if not case.id:
                case.id = new_id()
                case.created_at = ts
                case.created_by = user.username
                self.db.execute(
                    "INSERT INTO cases(id,case_number,status,parcel_identifier,survey_date,surveyor,rule_version_id,"
                    "land_use_code,units,units_verified,units_verified_by,crs_id,crs_kind,notes,created_at,created_by,updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (case.id, case.case_number.strip(), case.status, case.parcel_identifier, case.survey_date,
                     case.surveyor, case.rule_version_id, case.land_use_code, u.unit.value, int(u.verified),
                     u.verified_by, u.crs_id, u.crs_kind.value, case.notes, ts, user.username, ts),
                )
                action = "create_case"
            else:
                self.db.execute(
                    "UPDATE cases SET case_number=?,status=?,parcel_identifier=?,survey_date=?,surveyor=?,"
                    "rule_version_id=?,land_use_code=?,units=?,units_verified=?,units_verified_by=?,crs_id=?,"
                    "crs_kind=?,notes=?,updated_at=? WHERE id=?",
                    (case.case_number.strip(), case.status, case.parcel_identifier, case.survey_date, case.surveyor,
                     case.rule_version_id, case.land_use_code, u.unit.value, int(u.verified), u.verified_by,
                     u.crs_id, u.crs_kind.value, case.notes, ts, case.id),
                )
                action = "update_case"
            self.db.execute("DELETE FROM case_field_values WHERE case_id=?", (case.id,))
            for key, value in case.fields.items():
                self.db.execute(
                    "INSERT INTO case_field_values(case_id,field_key,value) VALUES (?,?,?)", (case.id, key, value)
                )
            case.updated_at = ts
            self.audit.record(user, action, "case", case.id, case.id, case_number=case.case_number,
                              status=case.status, land_use_code=case.land_use_code,
                              rule_version_id=case.rule_version_id, units=u.to_dict())
        return case

    def get(self, case_id: str) -> Case:
        r = self.db.query_one("SELECT * FROM cases WHERE id=?", (case_id,))
        if r is None:
            raise ValidationError(f"Case not found: {case_id}")
        values = {row["field_key"]: row["value"] for row in
                  self.db.query("SELECT field_key, value FROM case_field_values WHERE case_id=?", (case_id,))}
        return Case(
            id=r["id"], case_number=r["case_number"], status=r["status"],
            parcel_identifier=r["parcel_identifier"], survey_date=r["survey_date"], surveyor=r["surveyor"],
            rule_version_id=r["rule_version_id"], land_use_code=r["land_use_code"],
            units=DrawingUnits(LengthUnit(r["units"]), bool(r["units_verified"]), r["crs_id"],
                               CrsKind(r["crs_kind"]), r["units_verified_by"]),
            notes=r["notes"], fields=values, created_at=r["created_at"], created_by=r["created_by"],
            updated_at=r["updated_at"],
        )

    def list(self, search: str = "") -> list[Case]:
        like = f"%{search.strip()}%"
        rows = self.db.query(
            "SELECT id FROM cases WHERE case_number LIKE ? OR parcel_identifier LIKE ? OR surveyor LIKE ?"
            " ORDER BY updated_at DESC", (like, like, like),
        )
        return [self.get(r["id"]) for r in rows]

    def set_status(self, user: User, case_id: str, status: str) -> None:
        case = self.get(case_id)
        old = case.status
        case.status = status
        self.save(user, case)
        self.audit.record(user, "case_status", "case", case_id, case_id, old=old, new=status)
