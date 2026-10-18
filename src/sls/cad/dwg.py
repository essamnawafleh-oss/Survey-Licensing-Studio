"""DWG interoperability through the external ODA File Converter (see TD-04).

ezdxf cannot read or write DWG itself. This adapter converts DWG -> DXF with the
separately installed, free ODA File Converter and then runs the normal DXF import. The
original DWG is preserved by the caller. STATUS: not verified (no converter and no real
DWG fixtures in the build environment); see tests/test_dwg_acceptance.py.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from ..core.errors import DwgUnavailableError
from .dxf_io import ImportReport, export_dxf, import_dxf
from .model import Drawing

_DEFAULT_NAMES = ("ODAFileConverter.exe", "ODAFileConverter")


def find_converter(configured: str = "") -> Path | None:
    if configured:
        p = Path(configured)
        return p if p.is_file() else None
    for name in _DEFAULT_NAMES:
        found = shutil.which(name)
        if found:
            return Path(found)
    if sys.platform.startswith("win"):
        for base in (Path("C:/Program Files/ODA"), Path("C:/Program Files (x86)/ODA")):
            if base.is_dir():
                hits = sorted(base.glob("ODAFileConverter*/ODAFileConverter.exe"))
                if hits:
                    return hits[-1]
    return None


def _configure(converter: Path) -> None:
    import ezdxf

    key = "win_exec_path" if sys.platform.startswith("win") else "unix_exec_path"
    ezdxf.options.set("odafc-addon", key, str(converter))


def dwg_status(configured: str = "") -> str:
    conv = find_converter(configured)
    if conv is None:
        return ("DWG support unavailable: ODA File Converter is not installed or not configured "
                "(Settings → DWG converter path).")
    return f"DWG via ODA File Converter at {conv} (unverified integration)."


def import_dwg(path: Path, configured_converter: str = "") -> tuple[Drawing, ImportReport]:
    conv = find_converter(configured_converter)
    if conv is None:
        raise DwgUnavailableError(dwg_status(configured_converter))
    _configure(conv)
    from ezdxf.addons import odafc

    with tempfile.TemporaryDirectory() as tmp:
        dxf_path = Path(tmp) / (Path(path).stem + ".dxf")
        try:
            odafc.convert(str(path), str(dxf_path), version="R2018", replace=True)
        except odafc.ODAFCError as exc:
            raise DwgUnavailableError(f"ODA File Converter failed for {Path(path).name}: {exc}") from exc
        drawing, report = import_dxf(dxf_path)
    report.source_name = Path(path).name
    report.warnings.insert(0, "Converted from DWG to DXF with ODA File Converter before import; the original DWG "
                              "file is kept unchanged in the case store.")
    drawing.info["source_name"] = Path(path).name
    drawing.info["converted_from"] = "DWG"
    return drawing, report


def export_dwg(drawing: Drawing, path: Path, configured_converter: str = "", version: str = "R2018") -> list[str]:
    conv = find_converter(configured_converter)
    if conv is None:
        raise DwgUnavailableError(dwg_status(configured_converter))
    _configure(conv)
    from ezdxf.addons import odafc

    with tempfile.TemporaryDirectory() as tmp:
        dxf_path = Path(tmp) / (Path(path).stem + ".dxf")
        warnings = export_dxf(drawing, dxf_path)
        try:
            odafc.convert(str(dxf_path), str(path), version=version, replace=True)
        except odafc.ODAFCError as exc:
            raise DwgUnavailableError(f"ODA File Converter failed writing {Path(path).name}: {exc}") from exc
    return warnings
