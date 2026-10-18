"""Controlled 2D coordinate transformation from user-entered control points.

Used to bring a regulatory plan into the survey's verified coordinate system. Nothing is
fitted from visual alignment: the user enters corresponding coordinate pairs, reviews
parameters and per-point residuals, and explicitly applies the result, which is stored
as a new drawing revision with the transformation recorded in ``drawing.info``.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass

import numpy as np

from ..core.errors import ValidationError
from . import ops
from .model import Drawing

Pt = tuple[float, float]


@dataclass(frozen=True)
class Similarity:
    """x' = a·x − b·y + tx ;  y' = b·x + a·y + ty  (uniform scale + rotation + translation)."""

    a: float
    b: float
    tx: float
    ty: float
    kind: str = "similarity"  # "translation" | "similarity"

    @property
    def scale(self) -> float:
        return math.hypot(self.a, self.b)

    @property
    def rotation_deg(self) -> float:
        return math.degrees(math.atan2(self.b, self.a))

    def apply(self, x: float, y: float) -> Pt:
        return self.a * x - self.b * y + self.tx, self.b * x + self.a * y + self.ty

    def to_dict(self) -> dict[str, float | str]:
        d = asdict(self)
        d.update(scale=self.scale, rotation_deg=self.rotation_deg)
        return d


@dataclass(frozen=True)
class FitResult:
    transform: Similarity
    residuals: list[float]  # per control point, target units
    rms: float


def fit(src: Sequence[Pt], dst: Sequence[Pt], kind: str = "similarity") -> FitResult:
    if len(src) != len(dst):
        raise ValidationError("Each source point needs a matching target point.")
    if kind == "translation":
        if len(src) < 1:
            raise ValidationError("A translation needs at least one control point pair.")
        tx = sum(d[0] - s[0] for s, d in zip(src, dst)) / len(src)
        ty = sum(d[1] - s[1] for s, d in zip(src, dst)) / len(src)
        t = Similarity(1.0, 0.0, tx, ty, "translation")
    elif kind == "similarity":
        if len(src) < 2:
            raise ValidationError("A similarity transformation needs at least two control point pairs.")
        if len({(round(p[0], 9), round(p[1], 9)) for p in src}) < 2:
            raise ValidationError("Control points must not coincide.")
        # Solve in a centred frame for numerical stability with large grid coordinates.
        sx = sum(p[0] for p in src) / len(src)
        sy = sum(p[1] for p in src) / len(src)
        dx = sum(p[0] for p in dst) / len(dst)
        dy = sum(p[1] for p in dst) / len(dst)
        rows, rhs = [], []
        for (x, y), (u, v) in zip(src, dst):
            x, y, u, v = x - sx, y - sy, u - dx, v - dy
            rows += [[x, -y], [y, x]]
            rhs += [u, v]
        (a, b), *_ = np.linalg.lstsq(np.array(rows), np.array(rhs), rcond=None)
        tx = dx - (a * sx - b * sy)
        ty = dy - (b * sx + a * sy)
        t = Similarity(float(a), float(b), float(tx), float(ty), "similarity")
    else:
        raise ValidationError(f"Unknown transformation kind: {kind}")
    residuals = [math.dist(t.apply(*s), d) for s, d in zip(src, dst)]
    rms = math.sqrt(sum(r * r for r in residuals) / len(residuals))
    return FitResult(t, residuals, rms)


def transform_drawing(drawing: Drawing, t: Similarity) -> Drawing:
    """Return a transformed copy; the original drawing is not modified."""
    out = Drawing.from_dict(drawing.to_dict())
    s, rot = t.scale, t.rotation_deg
    for eid, e in list(out.entities.items()):
        n = ops.scale(e, (0.0, 0.0), s) if abs(s - 1.0) > 1e-15 else e
        n = ops.rotate(n, (0.0, 0.0), rot) if abs(rot) > 1e-15 else n
        out.entities[eid] = ops.translate(n, t.tx, t.ty)
    return out
