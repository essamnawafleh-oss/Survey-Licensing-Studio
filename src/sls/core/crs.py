"""Coordinate reference systems via the PROJ database (pyproj, fully offline).

The default system for this department is Jordan TM (JTM) as defined in the authority's GIS
(.prj "Jordan_JTM" = ESRI:102158): Transverse Mercator, central meridian 37°E, scale factor
0.9998, false easting 500 000 m, false northing −3 000 000 m, International 1924 ellipsoid.
EPSG:3066 has the same projection, so grid coordinates are identical; only the datum used
for latitude/longitude differs. Parameters come from the PROJ database, never typed by hand.

Survey and licensing calculations use planar *grid* coordinates. Geographic coordinates are
only displayed for orientation; the datum transformation to WGS 84 has an accuracy of metres
(reported by PROJ) and is never used for any measurement.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

JTM_ESRI = "ESRI:102158"  # "Jordan_JTM" as exported by the authority's ArcGIS (.prj), datum D_Jordan
JTM_EPSG = "EPSG:3066"    # "ED50 / Jordan TM": identical projection and grid, different datum label
JTM = JTM_ESRI

# Presets offered in the UI. Codes must exist in the PROJ database.
PRESETS: tuple[tuple[str, str], ...] = (
    (JTM_ESRI, "Jordan_JTM (JTM — GIS definition)"),
    (JTM_EPSG, "ED50 / Jordan TM (JTM)"),
    ("EPSG:32636", "WGS 84 / UTM zone 36N"),
    ("EPSG:32637", "WGS 84 / UTM zone 37N"),
)


@dataclass(frozen=True)
class CrsInfo:
    code: str
    name: str
    kind: str          # "projected" | "geographic" | other PROJ type name
    unit: str          # axis unit name, e.g. "metre"
    unit_to_m: float | None
    area_of_use: tuple[float, float, float, float] | None  # west, south, east, north (degrees)
    area_name: str


class CrsUnavailable(Exception):
    pass


def _pyproj():  # type: ignore[no-untyped-def]
    try:
        import pyproj
    except ImportError as exc:  # pragma: no cover - pyproj is a hard dependency
        raise CrsUnavailable("pyproj is not installed.") from exc
    return pyproj


@lru_cache(maxsize=32)
def describe(code: str) -> CrsInfo:
    """Look up a CRS by code (e.g. 'EPSG:3066'). Raises CrsUnavailable if unknown."""
    pyproj = _pyproj()
    try:
        crs = pyproj.CRS.from_user_input(code)
    except pyproj.exceptions.CRSError as exc:
        raise CrsUnavailable(f"Unknown coordinate reference system: {code}") from exc
    axis = crs.axis_info[0] if crs.axis_info else None
    unit = axis.unit_name if axis else ""
    factor = axis.unit_conversion_factor if axis and crs.is_projected else None
    aou = crs.area_of_use
    kind = "projected" if crs.is_projected else ("geographic" if crs.is_geographic else crs.type_name)
    return CrsInfo(code, crs.name, kind, unit, factor,
                   (aou.west, aou.south, aou.east, aou.north) if aou else None, aou.name if aou else "")


@lru_cache(maxsize=8)
def _to_wgs84(code: str):  # type: ignore[no-untyped-def]
    pyproj = _pyproj()
    return pyproj.Transformer.from_crs(code, "EPSG:4326", always_xy=True)


def to_lonlat(code: str, x: float, y: float) -> tuple[float, float]:
    return _to_wgs84(code).transform(x, y)


def wgs84_accuracy_m(code: str) -> float | None:
    """Accuracy (m) of the datum transformation PROJ selected, if known."""
    acc = _to_wgs84(code).accuracy
    return acc if acc is not None and acc >= 0 else None


def scale_factor(code: str, x: float, y: float) -> float | None:
    """Point scale factor of the projection at a grid position (areas scale by its square)."""
    pyproj = _pyproj()
    crs = pyproj.CRS.from_user_input(code)
    if not crs.is_projected:
        return None
    lon, lat = to_lonlat(code, x, y)
    proj = pyproj.Proj(crs)
    f = proj.get_factors(lon, lat)
    return float(f.meridional_scale) if math.isfinite(f.meridional_scale) else None


def from_wkt(wkt: str) -> str:
    """Identify a .prj WKT as an authority code (e.g. 'ESRI:102158'), or return the WKT itself."""
    pyproj = _pyproj()
    try:
        crs = pyproj.CRS.from_wkt(wkt)
    except pyproj.exceptions.CRSError as exc:
        raise CrsUnavailable(f"Unreadable coordinate system definition: {exc}") from exc
    auth = crs.to_authority(min_confidence=90)
    return f"{auth[0]}:{auth[1]}" if auth else crs.to_wkt()


@lru_cache(maxsize=64)
def same_grid(a: str, b: str) -> bool:
    """True when two systems give identical planar coordinates (same projection, parameters,
    ellipsoid and unit), even if their datum names differ (e.g. ESRI:102158 vs EPSG:3066)."""
    if not a or not b:
        return False
    if a == b:
        return True
    pyproj = _pyproj()
    try:
        ca, cb = pyproj.CRS.from_user_input(a), pyproj.CRS.from_user_input(b)
    except pyproj.exceptions.CRSError:
        return False
    if not (ca.is_projected and cb.is_projected):
        return False
    oa, ob = ca.coordinate_operation, cb.coordinate_operation
    if oa.method_name != ob.method_name:
        return False
    pa = {p.name: round(p.value, 9) for p in oa.params}
    pb = {p.name: round(p.value, 9) for p in ob.params}
    ea, eb = ca.ellipsoid, cb.ellipsoid
    return (pa == pb and abs(ea.semi_major_metre - eb.semi_major_metre) < 1e-6
            and abs(ea.inverse_flattening - eb.inverse_flattening) < 1e-9
            and ca.axis_info[0].unit_conversion_factor == cb.axis_info[0].unit_conversion_factor)


@dataclass(frozen=True)
class ExtentCheck:
    ok: bool
    lonlat_bounds: tuple[float, float, float, float] | None  # west, south, east, north
    message: str


def check_extents(code: str, extents: tuple[float, float, float, float], unit_to_m: float = 1.0) -> ExtentCheck:
    """Do the drawing extents, read as coordinates in ``code``, fall inside its area of use?

    A failed check means the drawing is probably in another system (or in local
    coordinates, or in other units). Nothing is moved; the result is only reported.
    """
    info = describe(code)
    if info.kind != "projected":
        return ExtentCheck(False, None, f"{code} is not a projected system; metric areas are not possible.")
    f = unit_to_m / (info.unit_to_m or 1.0)
    corners = [(extents[0] * f, extents[1] * f), (extents[2] * f, extents[3] * f),
               (extents[0] * f, extents[3] * f), (extents[2] * f, extents[1] * f)]
    try:
        pts = [to_lonlat(code, x, y) for x, y in corners]
    except Exception as exc:  # noqa: BLE001 - PROJ errors are reported, not raised
        return ExtentCheck(False, None, f"Coordinates cannot be converted with {code}: {exc}")
    if any(not (math.isfinite(lo) and math.isfinite(la)) for lo, la in pts):
        return ExtentCheck(False, None, f"Coordinates are outside the valid range of {code}.")
    lons = [p[0] for p in pts]
    lats = [p[1] for p in pts]
    bounds = (min(lons), min(lats), max(lons), max(lats))
    aou = info.area_of_use
    if aou is None:
        return ExtentCheck(True, bounds, "The system has no published area of use; extents were not checked.")
    inside = bounds[0] >= aou[0] - 0.05 and bounds[2] <= aou[2] + 0.05 and bounds[1] >= aou[1] - 0.05 \
        and bounds[3] <= aou[3] + 0.05
    where = f"lat {bounds[1]:.4f}…{bounds[3]:.4f}, lon {bounds[0]:.4f}…{bounds[2]:.4f}"
    if inside:
        return ExtentCheck(True, bounds, f"Coordinates lie inside the area of use of {info.name} ({info.area_name}): "
                                         f"{where}.")
    return ExtentCheck(False, bounds, f"Coordinates do NOT lie inside the area of use of {info.name} "
                                      f"({info.area_name}): {where}. The drawing is probably in another system, "
                                      "in local coordinates, or in different units.")
