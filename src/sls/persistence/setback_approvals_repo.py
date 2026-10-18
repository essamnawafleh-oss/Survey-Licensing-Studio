"""Reviewer approvals of setback offset constructions.

An approval records what the reviewer saw: the construction (geometry and diagnostics), the
review items accepted and a fingerprint of everything the construction depends on (boundary,
edge classifications, setback values, rule version, units/CRS, method). The calculation
engine accepts an approval only while the fingerprint still matches, so any change to those
inputs invalidates it. Approvals are never edited or deleted; a new one supersedes the old.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .db import new_id, now_iso

if TYPE_CHECKING:
    from .users import User
    from .workspace import Workspace


class SetbackApprovalRepository:
    def __init__(self, ws: "Workspace") -> None:
        self.ws = ws
        self.db = ws.db

    def create(self, user: "User", case_id: str, construction: dict[str, Any], method: str, note: str,
               rule_version_id: str | None, drawing_id: str | None, drawing_revision_id: str | None,
               approval_id: str = "") -> dict[str, Any]:
        aid = approval_id or new_id()
        ts = now_iso()
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO setback_approvals(id,case_id,method,fingerprint,fingerprint_parts_json,issue_keys_json,"
                "construction_json,rule_version_id,drawing_id,drawing_revision_id,note,approved_by,approved_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (aid, case_id, method, construction["fingerprint"],
                 json.dumps(construction["fingerprint_parts"], sort_keys=True),
                 json.dumps(construction["review_keys"]), json.dumps(construction, ensure_ascii=False),
                 rule_version_id, drawing_id, drawing_revision_id, note, user.username, ts))
            self.ws.audit.record(user, "approve_setback", "setback_approval", aid, case_id,
                                 fingerprint=construction["fingerprint"], review_items=construction["review_keys"],
                                 note=note, drawing_revision_id=drawing_revision_id)
        return self.get(aid)  # type: ignore[return-value]

    def get(self, approval_id: str) -> dict[str, Any] | None:
        r = self.db.query_one("SELECT * FROM setback_approvals WHERE id=?", (approval_id,))
        return self._row(r) if r else None

    def latest(self, case_id: str) -> dict[str, Any] | None:
        r = self.db.query_one("SELECT * FROM setback_approvals WHERE case_id=? ORDER BY approved_at DESC, rowid DESC "
                              "LIMIT 1", (case_id,))
        return self._row(r) if r else None

    def for_inputs(self, case_id: str) -> dict[str, Any] | None:
        """The latest approval in the compact form stored in calculation inputs."""
        a = self.latest(case_id)
        if a is None:
            return None
        return {k: a[k] for k in ("id", "fingerprint", "fingerprint_parts", "issue_keys", "approved_by",
                                  "approved_at", "note")}

    @staticmethod
    def _row(r: Any) -> dict[str, Any]:
        return {"id": r["id"], "case_id": r["case_id"], "method": r["method"], "fingerprint": r["fingerprint"],
                "fingerprint_parts": json.loads(r["fingerprint_parts_json"]),
                "issue_keys": json.loads(r["issue_keys_json"]), "construction": json.loads(r["construction_json"]),
                "rule_version_id": r["rule_version_id"], "drawing_id": r["drawing_id"],
                "drawing_revision_id": r["drawing_revision_id"], "note": r["note"],
                "approved_by": r["approved_by"], "approved_at": r["approved_at"]}
