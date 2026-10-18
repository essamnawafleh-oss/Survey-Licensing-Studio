"""Drawing units and coordinate reference system descriptors.

No unit or CRS is ever assumed: values default to ``UNKNOWN`` and calculations refuse
to run until a person has verified them.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass

from .errors import CrsError, UnitsNotVerifiedError


class LengthUnit(enum.Enum):
    UNKNOWN = "unknown"
    MILLIMETER = "mm"
    CENTIMETER = "cm"
    METER = "m"
    KILOMETER = "km"
    INCH = "in"
    FOOT = "ft"

    @property
    def to_meter(self) -> float:
        if self is LengthUnit.UNKNOWN:
            raise UnitsNotVerifiedError("Drawing units are unknown; verify units before measuring.")
        return _TO_METER[self]


_TO_METER = {
    LengthUnit.MILLIMETER: 0.001,
    LengthUnit.CENTIMETER: 0.01,
    LengthUnit.METER: 1.0,
    LengthUnit.KILOMETER: 1000.0,
    LengthUnit.INCH: 0.0254,
    LengthUnit.FOOT: 0.3048,
}

# DXF $INSUNITS codes -> LengthUnit (only codes we can map exactly).
_INSUNITS = {
    1: LengthUnit.INCH,
    2: LengthUnit.FOOT,
    4: LengthUnit.MILLIMETER,
    5: LengthUnit.CENTIMETER,
    6: LengthUnit.METER,
    7: LengthUnit.KILOMETER,
}


def unit_from_insunits(code: int | None) -> LengthUnit:
    """Map a DXF ``$INSUNITS`` code to a unit suggestion. 0/None/unmapped -> UNKNOWN."""
    if code is None:
        return LengthUnit.UNKNOWN
    return _INSUNITS.get(int(code), LengthUnit.UNKNOWN)


def insunits_from_unit(unit: LengthUnit) -> int:
    for code, u in _INSUNITS.items():
        if u is unit:
            return code
    return 0


class CrsKind(enum.Enum):
    UNKNOWN = "unknown"
    PROJECTED = "projected"   # planar metric grid, e.g. a national TM grid
    LOCAL = "local"           # site-local planar coordinates
    GEOGRAPHIC = "geographic"  # lat/long degrees: not valid for metric area


@dataclass(frozen=True)
class DrawingUnits:
    """Units and CRS of one drawing, plus their verification state."""

    unit: LengthUnit = LengthUnit.UNKNOWN
    verified: bool = False
    crs_id: str = ""
    crs_kind: CrsKind = CrsKind.UNKNOWN
    verified_by: str = ""

    def require_metric_planar(self) -> float:
        """Return the factor drawing-unit -> metre, or raise if not usable."""
        if self.unit is LengthUnit.UNKNOWN:
            raise UnitsNotVerifiedError("Drawing units are unknown.")
        if not self.verified:
            raise UnitsNotVerifiedError(
                f"Drawing units ({self.unit.value}) have not been verified by a user."
            )
        if self.crs_kind is CrsKind.GEOGRAPHIC:
            raise CrsError(
                "Geographic (longitude/latitude) coordinates cannot be used for metric "
                "area or length; reproject to a planar CRS first."
            )
        if self.crs_kind is CrsKind.UNKNOWN:
            raise CrsError("Coordinate reference system kind is not set (projected or local).")
        return self.unit.to_meter

    def to_dict(self) -> dict[str, object]:
        return {
            "unit": self.unit.value,
            "verified": self.verified,
            "crs_id": self.crs_id,
            "crs_kind": self.crs_kind.value,
            "verified_by": self.verified_by,
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> "DrawingUnits":
        return cls(
            unit=LengthUnit(str(d.get("unit", "unknown"))),
            verified=bool(d.get("verified", False)),
            crs_id=str(d.get("crs_id", "")),
            crs_kind=CrsKind(str(d.get("crs_kind", "unknown"))),
            verified_by=str(d.get("verified_by", "")),
        )
