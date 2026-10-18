"""Previous-license reconciliation: licensed vs measured quantities.

Only *verified* records contribute. Conflicts are flagged and block the affected
result instead of being resolved by assumption (TD-08).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

TOLERANCE = 0.005  # m² / m: values within this are treated as equal


@dataclass(frozen=True)
class LicenseRecordInput:
    id: str
    license_number: str
    revision: int
    status: str  # unverified | verified | disputed
    areas: tuple[tuple[str, float], ...] = ()     # (floor_key, m²)
    lengths: tuple[tuple[str, float], ...] = ()   # (wall|fence, m)
    issue_date: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "LicenseRecordInput":
        return cls(d["id"], d["license_number"], int(d["revision"]), d["status"],
                   tuple((str(a), float(b)) for a, b in d.get("areas", ())),
                   tuple((str(a), float(b)) for a, b in d.get("lengths", ())), d.get("issue_date", ""))


@dataclass
class QuantityLine:
    """One reconciled quantity (a floor area, or a wall/fence length)."""

    key: str
    unit: str
    measured: float | None
    licensed: float
    remaining: float | None
    license_numbers: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Reconciliation:
    areas: list[QuantityLine]
    lengths: list[QuantityLine]
    total_measured_area: float
    total_licensed_area: float
    total_remaining_area: float | None
    total_remaining_reason: str
    excluded_records: list[str]
    warnings: list[str]
    assumptions: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _signature(rec: LicenseRecordInput) -> tuple:
    return (tuple(sorted(rec.areas)), tuple(sorted(rec.lengths)), rec.issue_date)


def reconcile(measured_areas: Mapping[str, float], measured_lengths: Mapping[str, float],
              records: Sequence[LicenseRecordInput]) -> Reconciliation:
    warnings: list[str] = []
    assumptions: list[str] = []
    excluded: list[str] = []

    verified: list[LicenseRecordInput] = []
    for rec in records:
        if rec.status == "verified":
            verified.append(rec)
        elif rec.status == "disputed":
            excluded.append(rec.id)
            warnings.append(f"License {rec.license_number} is marked disputed and is excluded.")
        else:
            excluded.append(rec.id)
            warnings.append(f"License {rec.license_number} is not verified and is excluded from the calculation.")

    if not records:
        warnings.append("No previous license records have been entered. The licensed quantities are shown as 0; "
                        "confirm that no earlier licenses exist.")
    elif not verified:
        warnings.append("No verified previous license records: licensed quantities are 0 pending verification.")

    # Duplicate license numbers among verified records.
    by_number: dict[str, list[LicenseRecordInput]] = {}
    for rec in verified:
        by_number.setdefault(rec.license_number.strip(), []).append(rec)
    conflicted: dict[str, dict[str, list[str]]] = {"areas": {}, "lengths": {}}
    unique: list[LicenseRecordInput] = []
    for number, recs in by_number.items():
        if len(recs) == 1:
            unique.append(recs[0])
            continue
        if len({_signature(r) for r in recs}) == 1:
            warnings.append(f"License {number} was entered {len(recs)} times with identical values; counted once.")
            unique.append(recs[0])
        else:
            msg = f"License {number} has {len(recs)} verified entries with different values."
            for attr in ("areas", "lengths"):
                for k in {k for r in recs for k, _ in getattr(r, attr)}:
                    conflicted[attr].setdefault(k, []).append(msg)
            warnings.append(f"Conflicting verified entries for license {number}; affected quantities are blocked.")

    def build(measured: Mapping[str, float], attr: str, unit: str) -> list[QuantityLine]:
        licensed: dict[str, float] = {}
        numbers: dict[str, list[str]] = {}
        for rec in unique:
            for key, value in getattr(rec, attr):
                licensed[key] = licensed.get(key, 0.0) + value
                numbers.setdefault(key, []).append(rec.license_number)
        blocked_keys = conflicted[attr]
        keys = list(measured) + sorted(k for k in set(licensed) | set(blocked_keys) if k not in measured)
        lines: list[QuantityLine] = []
        for key in keys:
            m = measured.get(key)
            lic = licensed.get(key, 0.0)
            line = QuantityLine(key, unit, m, lic, None, numbers.get(key, []))
            line.conflicts.extend(blocked_keys.get(key, []))
            if len(line.license_numbers) > 1:
                line.warnings.append("Several licenses cover this item; their licensed portions are assumed "
                                     "not to overlap.")
            if m is None:
                line.conflicts.append(f"Licensed item '{key}' does not exist in the measured drawing.")
            elif lic > m + TOLERANCE:
                line.conflicts.append(f"Licensed quantity {lic:.2f} {unit} exceeds measured {m:.2f} {unit}.")
            if not line.conflicts and m is not None:
                line.remaining = m - lic
            lines.append(line)
        return lines

    areas = build(measured_areas, "areas", "m²")
    lengths = build(measured_lengths, "lengths", "m")
    if any(len(line.license_numbers) > 1 for line in areas + lengths):
        assumptions.append("Where several verified licenses cover the same floor or element, their quantities "
                           "are summed on the assumption that the licensed portions do not overlap.")

    total_measured = sum(v for v in measured_areas.values())
    total_licensed = sum(line.licensed for line in areas)
    blocked = [line.key for line in areas if line.remaining is None]
    if blocked:
        total_remaining = None
        reason = "Blocked by conflicts on: " + ", ".join(blocked)
    else:
        total_remaining = sum(line.remaining for line in areas if line.remaining is not None)
        reason = ""
    return Reconciliation(areas, lengths, total_measured, total_licensed, total_remaining, reason,
                          excluded, warnings, assumptions)
