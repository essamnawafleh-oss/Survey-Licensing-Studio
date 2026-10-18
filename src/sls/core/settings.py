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

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir) if self.data_dir else default_data_dir()

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> "Settings":
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in d.items() if k in known}
        nf = values.pop("number_format", None)
        s = cls(**values)  # type: ignore[arg-type]
        if isinstance(nf, dict):
            nf_known = {f.name for f in fields(NumberFormat)}
            s.number_format = NumberFormat(**{k: v for k, v in nf.items() if k in nf_known})
        return s


def settings_path() -> Path:
    override = os.environ.get("SLS_CONFIG")
    return Path(override) if override else default_config_dir() / "settings.json"


def load_settings(path: Path | None = None) -> Settings:
    path = path or settings_path()
    if not path.exists():
        return Settings()
    with path.open(encoding="utf-8") as fh:
        return Settings.from_dict(json.load(fh))


def save_settings(settings: Settings, path: Path | None = None) -> None:
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(settings.to_json(), encoding="utf-8")
    tmp.replace(path)
