"""Stored calculations: immutable revisions with full input snapshots."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..core.errors import ValidationError
from ..engine.inputs import CalculationInputs
from ..engine.run import ENGINE_VERSION, result_hash, run_calculation
from .db import new_id, now_iso

if TYPE_CHECKING:
    from .users import User
    from .workspace import Workspace


@dataclass
class CalculationRecord:
    id: str
    case_id: str
    revision: int
    engine_version: str
    drawing_revision_id: str | None
    rule_version_id: str | None
    input_hash: str
    result_hash: str
    inputs: CalculationInputs
    results: dict[str, Any]
    status: str
    created_at: str
    created_by: str


class CalculationRepository:
    def __init__(self, ws: "Workspace") -> None:
        self.ws = ws
        self.db = ws.db

    def save(self, user: "User", case_id: str, inputs: CalculationInputs, results: dict[str, Any]) -> CalculationRecord:
        user.require("run_calculation")
        with self.db.transaction():
            rev = int(self.db.query_one("SELECT COALESCE(MAX(revision),0) FROM calculations WHERE case_id=?",
                                        (case_id,))[0]) + 1
            cid = new_id()
            rhash = result_hash(results)
            self.db.execute(
                "INSERT INTO calculations(id,case_id,revision,engine_version,drawing_revision_id,rule_version_id,"
                "input_hash,result_hash,inputs_json,results_json,status,created_at,created_by)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, case_id, rev, ENGINE_VERSION, inputs.drawing_revision_id, inputs.rule_version_id,
                 inputs.input_hash(), rhash, inputs.canonical_json(),
                 json.dumps(results, ensure_ascii=False, sort_keys=True),
                 "error" if results.get("status") == "error" else "ok", now_iso(), user.username),
            )
            for lic in inputs.licenses:
                self.db.execute("INSERT INTO calculation_licenses(calculation_id,license_id,license_revision)"
                                " VALUES (?,?,?)", (cid, lic["id"], lic["revision"]))
            self.ws.audit.record(user, "run_calculation", "calculation", cid, case_id, revision=rev,
                                 input_hash=inputs.input_hash(), result_hash=rhash, status=results.get("status"))
        return self.get(cid)

    def get(self, calc_id: str) -> CalculationRecord:
        r = self.db.query_one("SELECT * FROM calculations WHERE id=?", (calc_id,))
        if r is None:
            raise ValidationError(f"Calculation not found: {calc_id}")
        return CalculationRecord(r["id"], r["case_id"], r["revision"], r["engine_version"], r["drawing_revision_id"],
                                 r["rule_version_id"], r["input_hash"], r["result_hash"],
                                 CalculationInputs.from_dict(json.loads(r["inputs_json"])),
                                 json.loads(r["results_json"]), r["status"], r["created_at"], r["created_by"])

    def list(self, case_id: str) -> list[CalculationRecord]:
        rows = self.db.query("SELECT id FROM calculations WHERE case_id=? ORDER BY revision DESC", (case_id,))
        return [self.get(r["id"]) for r in rows]

    def latest(self, case_id: str) -> CalculationRecord | None:
        r = self.db.query_one("SELECT id FROM calculations WHERE case_id=? ORDER BY revision DESC LIMIT 1", (case_id,))
        return self.get(r["id"]) if r else None

    def reproduce(self, calc_id: str) -> tuple[bool, str]:
        """Re-run a stored calculation from its input snapshot and compare result hashes."""
        rec = self.get(calc_id)
        if rec.inputs.input_hash() != rec.input_hash:
            return False, "stored inputs do not match their recorded hash"
        new_hash = result_hash(run_calculation(rec.inputs))
        return new_hash == rec.result_hash, new_hash
