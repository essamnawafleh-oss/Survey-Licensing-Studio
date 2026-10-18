"""In-memory drawing model: ordered layers and typed entities with stable IDs.

Coordinates are stored in drawing units (as surveyed). Entity ``meta`` carries
provenance and classification (``source``: "survey" | "regulatory" | "derived" | "user").
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from ..core.units import DrawingUnits

INVALID_LAYER_CHARS = '<>/\\":;?*|=`'
ENTITY_KINDS = ("line", "polyline", "circle", "arc", "text", "point", "dimension")

# Layer roles drive the calculation engine; they are assigned explicitly by the user.
LAYER_ROLES = (
    "unassigned", "building", "wall", "fence", "road", "parcel", "property_boundary",
    "neighbor_parcel", "setback", "setback_violation", "dimension", "annotation",
    "licensed_area", "reference", "well", "drainage", "obstacle",
)

DEFAULT_LAYERS: list[dict[str, Any]] = [
    {"name": "BUILDINGS", "color": "#FFD54F", "role": "building", "floor_key": "G"},
    {"name": "WALLS", "color": "#E0E0E0", "role": "wall", "lineweight": 0.35},
    {"name": "EXTERNAL_FENCES", "color": "#A1887F", "role": "fence", "linetype": "DASHED"},
    {"name": "ROADS", "color": "#90A4AE", "role": "road"},
    {"name": "LAND_PARCELS", "color": "#81C784", "role": "neighbor_parcel"},
    {"name": "PROPERTY_BOUNDARY", "color": "#4FC3F7", "role": "property_boundary", "lineweight": 0.5},
    {"name": "SETBACK_BOUNDARY", "color": "#BA68C8", "role": "setback", "linetype": "DASHED", "derived": True},
    {"name": "SETBACK_VIOLATIONS", "color": "#EF5350", "role": "setback_violation", "derived": True,
     "transparency": 0.5},
    {"name": "DIMENSIONS", "color": "#80CBC4", "role": "dimension"},
    {"name": "LABELS", "color": "#FFFFFF", "role": "annotation"},
    {"name": "LICENSED_AREAS", "color": "#66BB6A", "role": "licensed_area", "transparency": 0.6},
    {"name": "REFERENCE", "color": "#757575", "role": "reference"},
]


def new_entity_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Layer:
    name: str
    color: str = "#FFFFFF"
    linetype: str = "CONTINUOUS"
    lineweight: float = 0.25  # mm
    visible: bool = True
    locked: bool = False
    transparency: float = 0.0  # 0 = opaque, 1 = invisible
    role: str = "unassigned"
    floor_key: str = ""
    source_name: str = ""
    derived: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Layer":
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Entity:
    kind: str
    layer: str
    data: dict[str, Any]
    id: str = field(default_factory=new_entity_id)
    meta: dict[str, Any] = field(default_factory=dict)
    color: str | None = None  # None = by layer

    def clone(self, new_id: bool = False) -> "Entity":
        e = copy.deepcopy(self)
        if new_id:
            e.id = new_entity_id()
        return e

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "layer": self.layer, "data": self.data,
                "meta": self.meta, "color": self.color}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Entity":
        return cls(d["kind"], d["layer"], d["data"], d["id"], d.get("meta", {}), d.get("color"))

    # -- geometry helpers (drawing units) -----------------------------------------

    def vertices(self) -> list[tuple[float, float]]:
        d = self.data
        if self.kind == "line":
            return [tuple(d["p1"]), tuple(d["p2"])]
        if self.kind == "polyline":
            return [(p[0], p[1]) for p in d["points"]]
        if self.kind in ("circle", "arc"):
            return [tuple(d["center"])]
        if self.kind in ("text", "point"):
            return [tuple(d["position"])]
        if self.kind == "dimension":
            return [tuple(d["p1"]), tuple(d["p2"])]
        return []

    def flattened(self, segments_per_circle: int = 128) -> list[tuple[float, float]]:
        """Point sequence approximating the entity outline (arcs/bulges sampled)."""
        d = self.data
        if self.kind == "line" or self.kind == "dimension":
            return [tuple(d["p1"]), tuple(d["p2"])]
        if self.kind == "polyline":
            return flatten_bulge_polyline(d["points"], d.get("closed", False), segments_per_circle)
        if self.kind == "circle":
            cx, cy = d["center"]
            r = d["radius"]
            n = segments_per_circle
            return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n))
                    for i in range(n + 1)]
        if self.kind == "arc":
            return arc_points(d["center"], d["radius"], d["start_angle"], d["end_angle"], segments_per_circle)
        return self.vertices()

    def is_closed_shape(self) -> bool:
        return self.kind == "circle" or (self.kind == "polyline" and bool(self.data.get("closed")))

    def has_curves(self) -> bool:
        if self.kind in ("circle", "arc"):
            return True
        if self.kind == "polyline":
            return any(len(p) > 2 and abs(p[2]) > 1e-12 for p in self.data["points"])
        return False


def arc_points(center, radius, start_deg, end_deg, segments_per_circle=128):  # type: ignore[no-untyped-def]
    sweep = (end_deg - start_deg) % 360.0 or 360.0
    n = max(2, int(math.ceil(segments_per_circle * sweep / 360.0)))
    cx, cy = center
    return [(cx + radius * math.cos(math.radians(start_deg + sweep * i / n)),
             cy + radius * math.sin(math.radians(start_deg + sweep * i / n))) for i in range(n + 1)]


def flatten_bulge_polyline(points, closed, segments_per_circle=128):  # type: ignore[no-untyped-def]
    """Expand DXF bulge arcs into points. Point items are (x, y) or (x, y, bulge)."""
    pts = [(p[0], p[1], p[2] if len(p) > 2 else 0.0) for p in points]
    if not pts:
        return []
    out: list[tuple[float, float]] = [(pts[0][0], pts[0][1])]
    n = len(pts)
    count = n if closed else n - 1
    for i in range(count):
        x1, y1, b = pts[i]
        x2, y2, _ = pts[(i + 1) % n]
        if abs(b) < 1e-12:
            out.append((x2, y2))
            continue
        theta = 4 * math.atan(b)  # included angle, signed
        chord = math.hypot(x2 - x1, y2 - y1)
        if chord == 0:
            continue
        r = chord / (2 * math.sin(abs(theta) / 2))
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        ux, uy = (x2 - x1) / chord, (y2 - y1) / chord
        # Centre lies left of the chord for a CCW (b > 0) arc under 180°, right for CW;
        # arcs over 180° put it on the opposite side.
        sign = 1 if b > 0 else -1
        d = math.sqrt(max(r * r - (chord / 2) ** 2, 0.0))
        if abs(theta) > math.pi:
            d = -d
        cx, cy = mx - uy * d * sign, my + ux * d * sign
        a1 = math.atan2(y1 - cy, x1 - cx)
        steps = max(2, int(math.ceil(segments_per_circle * abs(theta) / (2 * math.pi))))
        for k in range(1, steps + 1):
            a = a1 + theta * k / steps
            out.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        out[-1] = (x2, y2)
    return out


class Drawing:
    def __init__(self, units: DrawingUnits | None = None) -> None:
        self.layers: list[Layer] = []
        self.entities: dict[str, Entity] = {}
        self.units = units or DrawingUnits()
        self.active_layer: str = "0"
        self.info: dict[str, Any] = {}
        self.revision_note = ""

    # -- layers -----------------------------------------------------------------

    @classmethod
    def with_default_layers(cls, units: DrawingUnits | None = None) -> "Drawing":
        d = cls(units)
        d.layers.append(Layer("0"))
        for spec in DEFAULT_LAYERS:
            d.layers.append(Layer(**spec))
        d.active_layer = "BUILDINGS"
        return d

    def layer(self, name: str) -> Layer:
        for layer in self.layers:
            if layer.name == name:
                return layer
        key = name.casefold()
        for layer in self.layers:
            if layer.name.casefold() == key:
                return layer
        raise KeyError(name)

    def has_layer(self, name: str) -> bool:
        # DXF layer names are case-insensitive, so uniqueness is checked the same way.
        key = name.casefold()
        return any(layer.name.casefold() == key for layer in self.layers)

    @staticmethod
    def check_layer_name(name: str) -> None:
        if not name.strip():
            raise ValueError("Layer name is required.")
        bad = sorted(set(name) & set(INVALID_LAYER_CHARS))
        if bad:
            raise ValueError(f"Layer name '{name}' contains characters not allowed in DXF/DWG: {' '.join(bad)}")

    def add_layer(self, layer: Layer, index: int | None = None) -> Layer:
        self.check_layer_name(layer.name)
        if self.has_layer(layer.name):
            raise ValueError(f"Layer '{layer.name}' already exists.")
        if index is None:
            self.layers.append(layer)
        else:
            self.layers.insert(index, layer)
        return layer

    def ensure_layer(self, name: str, **props: Any) -> Layer:
        if self.has_layer(name):
            return self.layer(name)
        return self.add_layer(Layer(name, **props))

    def rename_layer(self, old: str, new: str) -> None:
        if old == "0":
            raise ValueError("Layer '0' cannot be renamed.")
        self.check_layer_name(new)
        if self.has_layer(new) and new.casefold() != old.casefold():
            raise ValueError(f"Layer '{new}' already exists.")
        key = old.casefold()  # layer names are case-insensitive (DXF)
        self.layer(old).name = new
        for e in self.entities.values():
            if e.layer.casefold() == key:
                e.layer = new
        if self.active_layer.casefold() == key:
            self.active_layer = new

    def remove_layer(self, name: str) -> None:
        if name == "0":
            raise ValueError("Layer '0' cannot be deleted.")
        if any(e.layer.casefold() == name.casefold() for e in self.entities.values()):
            raise ValueError(f"Layer '{name}' still contains entities; move or erase them first.")
        self.layers.remove(self.layer(name))
        if self.active_layer == name:
            self.active_layer = "0"

    def duplicate_layer(self, name: str, with_entities: bool = True) -> Layer:
        src = self.layer(name)
        new_name = f"{name}_COPY"
        n = 2
        while self.has_layer(new_name):
            new_name = f"{name}_COPY{n}"
            n += 1
        dup = Layer.from_dict({**src.to_dict(), "name": new_name})
        self.add_layer(dup, self.layers.index(src) + 1)
        if with_entities:
            for e in [e for e in self.entities.values() if e.layer == name]:
                c = e.clone(new_id=True)
                c.layer = new_name
                self.entities[c.id] = c
        return dup

    def move_layer(self, name: str, new_index: int) -> None:
        layer = self.layer(name)
        self.layers.remove(layer)
        self.layers.insert(max(0, min(new_index, len(self.layers))), layer)

    def layers_by_role(self, role: str) -> list[Layer]:
        return [layer for layer in self.layers if layer.role == role]

    # -- entities -------------------------------------------------------------------

    def add(self, entity: Entity) -> Entity:
        if entity.kind not in ENTITY_KINDS:
            raise ValueError(f"Unsupported entity kind: {entity.kind}")
        if not self.has_layer(entity.layer):
            self.add_layer(Layer(entity.layer))
        else:
            entity.layer = self.layer(entity.layer).name  # same spelling as the layer table (case)
        self.entities[entity.id] = entity
        return entity

    def entities_on(self, layer_names: set[str] | list[str]) -> list[Entity]:
        names = set(layer_names)
        return [e for e in self.entities.values() if e.layer in names]

    def extents(self, only_visible: bool = True) -> tuple[float, float, float, float] | None:
        visible = {layer.name for layer in self.layers if layer.visible or not only_visible}
        xs: list[float] = []
        ys: list[float] = []
        for e in self.entities.values():
            if e.layer not in visible:
                continue
            if e.kind in ("circle", "arc"):
                cx, cy = e.data["center"]
                r = e.data["radius"]
                xs += [cx - r, cx + r]
                ys += [cy - r, cy + r]
            else:
                for x, y in e.flattened(32):
                    xs.append(x)
                    ys.append(y)
        if not xs:
            return None
        return min(xs), min(ys), max(xs), max(ys)

    # -- serialisation ------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "sls-drawing",
            "version": 1,
            "units": self.units.to_dict(),
            "active_layer": self.active_layer,
            "info": self.info,
            "layers": [layer.to_dict() for layer in self.layers],
            "entities": [e.to_dict() for e in self.entities.values()],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def content_hash(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    def source_content_hash(self) -> str:
        """Hash of non-derived content (generated setback/violation geometry excluded)."""
        d = self.to_dict()
        d["entities"] = [e for e in d["entities"] if e["meta"].get("source") != "derived"]
        d.pop("active_layer", None)
        payload = json.dumps(d, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Drawing":
        if d.get("format") != "sls-drawing":
            raise ValueError("Not a Survey & Licensing Studio drawing file.")
        dr = cls(DrawingUnits.from_dict(d.get("units", {})))
        dr.layers = [Layer.from_dict(x) for x in d["layers"]]
        for ed in d["entities"]:
            e = Entity.from_dict(ed)
            dr.entities[e.id] = e
        dr.active_layer = d.get("active_layer", "0")
        dr.info = d.get("info", {})
        return dr

    @classmethod
    def from_json(cls, text: str) -> "Drawing":
        return cls.from_dict(json.loads(text))
