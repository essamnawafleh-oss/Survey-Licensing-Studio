"""Application settings stored as JSON in a local, configurable location."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_DIR_NAME = "SurveyLicensingStudio"


def default_config_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_DIR_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_DIR_NAME


def default_data_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_DIR_NAME / "data"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_DIR_NAME


@dataclass
class NumberFormat:
    area_decimals: int = 2
    length_decimals: int = 2
    arabic_indic_digits: bool = False

    def area(self, value: float) -> str:
        return self._digits(f"{value:,.{self.area_decimals}f}")

    def length(self, value: float) -> str:
        return self._digits(f"{value:,.{self.length_decimals}f}")

    def _digits(self, text: str) -> str:
        if not self.arabic_indic_digits:
            return text
        table = str.maketrans("0123456789,.", "٠١٢٣٤٥٦٧٨٩٬٫")
        return text.translate(table)


@dataclass
class Settings:
    data_dir: str = ""
    language: str = "ar"  # "ar" or "en"
    number_format: NumberFormat = field(default_factory=NumberFormat)
    access_control_enabled: bool = False
    backup_retention: int = 30
    oda_converter_path: str = ""
    libredwg_path: str = ""
    default_crs: str = "ESRI:102158"  # Jordan TM (JTM) as defined in the authority's GIS
    tesseract_path: str = ""
    sliver_area_tolerance_m2: float = 0.01
    # "offset_corner": surveyor workflow (inward offsets, trim/extend, sharp corners; TD-26).
    # "distance_zone": minimum-distance zones with arcs at reflex corners (TD-06).
    setback_method: str = "offset_corner"

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir) if self.data_dir else default_data_dir()

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, d: dict[str, object], problems: list[str] | None = None) -> "Settings":
        """Build settings from stored values; invalid values are replaced by defaults and listed in ``problems``."""
        problems = problems if problems is not None else []
        defaults = cls()
        values: dict[str, object] = {}
        for f in fields(cls):
            if f.name not in d or f.name == "number_format":
                continue
            v = _checked(f.name, d[f.name], getattr(defaults, f.name), problems)
            if v is not _INVALID:
                values[f.name] = v
        s = cls(**values)  # type: ignore[arg-type]
        nf = d.get("number_format")
        if isinstance(nf, dict):
            nd = NumberFormat()
            s.number_format = NumberFormat(**{f.name: v for f in fields(NumberFormat) if f.name in nf
                                              and (v := _checked(f.name, nf[f.name], getattr(nd, f.name), problems))
                                              is not _INVALID})
        if s.language not in ("ar", "en"):
            problems.append(f"language '{s.language}' is not supported; using 'ar'.")
            s.language = "ar"
        if s.setback_method not in ("offset_corner", "distance_zone"):
            problems.append(f"setback_method '{s.setback_method}' is unknown; using 'offset_corner'.")
            s.setback_method = "offset_corner"
        return s


def settings_path() -> Path:
    override = os.environ.get("SLS_CONFIG")
    return Path(override) if override else default_config_dir() / "settings.json"


_INVALID = object()


def _checked(name: str, value: object, default: object, problems: list[str]) -> object:
    """``value`` if it has the default's type (ints accepted for floats), else _INVALID (and a problem note)."""
    if isinstance(default, bool):
        ok = isinstance(value, bool)
    elif isinstance(default, int):
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif isinstance(default, float):
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        value = float(value) if ok else value  # type: ignore[arg-type]
    else:
        ok = isinstance(value, type(default))
    if not ok:
        problems.append(f"{name}: invalid value {value!r}; the default {default!r} is used.")
        return _INVALID
    return value


def load_settings_checked(path: Path | None = None) -> tuple[Settings, list[str]]:
    """Load settings; a damaged file never prevents start-up.

    Unreadable JSON is moved aside as ``settings.json.bad-<time>`` (kept for inspection) and defaults
    are used; individual invalid values fall back to their defaults. The problems are returned so the
    application can tell the user.
    """
    path = path or settings_path()
    if not path.exists():
        return Settings(), []
    problems: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))  # tolerate a BOM (Notepad)
        if not isinstance(data, dict):
            raise ValueError("the file does not contain a settings object")
    except (OSError, ValueError) as exc:
        from datetime import datetime

        aside = path.with_name(f"{path.name}.bad-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        try:
            path.replace(aside)
            where = f"; the file was moved to {aside.name}"
        except OSError:
            where = ""
        return Settings(), [f"The settings file could not be read ({exc}); default settings are used{where}."]
    return Settings.from_dict(data, problems), problems


def load_settings(path: Path | None = None) -> Settings:
    return load_settings_checked(path)[0]


def save_settings(settings: Settings, path: Path | None = None) -> None:
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(settings.to_json(), encoding="utf-8")
    tmp.replace(path)
