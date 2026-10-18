"""DWG interoperability through an external converter (see TD-04 and TD-21).

ezdxf cannot read DWG itself. A DWG file is converted to DXF by an external tool and then
goes through the normal DXF import (with its full entity accounting). The original DWG is
preserved by the caller. Two converters are supported, in order of preference:

1. **ODA File Converter** (free download from the Open Design Alliance, ODA licence terms):
   best fidelity, also writes DWG.
2. **LibreDWG** ``dwg2dxf`` (GNU, GPLv3, run as a separate program): read-only here. Older
   releases mis-decode some AutoCAD 2018 objects; every converter error message is kept in
   the import report and damaged entities are counted, never hidden.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..core.errors import DwgUnavailableError
from .dxf_io import ImportReport, export_dxf, import_dxf
from .model import Drawing

_ODA_NAMES = ("ODAFileConverter.exe", "ODAFileConverter")
_LIBREDWG_NAMES = ("dwg2dxf.exe", "dwg2dxf")


def _bundled_dir() -> Path:
    """Folder for converters shipped with the application (frozen build or source tree)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))
    return base / "tools"


def find_oda(configured: str = "") -> Path | None:
    if configured:
        p = Path(configured)
        return p if p.is_file() and "oda" in p.name.lower() else None
    for name in _ODA_NAMES:
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


def find_libredwg(configured: str = "") -> Path | None:
    if configured:
        p = Path(configured)
        return p if p.is_file() else None
    for name in _LIBREDWG_NAMES:
        bundled = _bundled_dir() / "libredwg" / name
        if bundled.is_file():
            return bundled
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


# Backwards-compatible name used by tests and settings.
find_converter = find_oda


@dataclass(frozen=True)
class Converter:
    kind: str  # "oda" | "libredwg"
    path: Path

    @property
    def label(self) -> str:
        return "ODA File Converter" if self.kind == "oda" else f"LibreDWG {libredwg_version(self.path)}".strip()


def choose_converter(oda_path: str = "", libredwg_path: str = "") -> Converter | None:
    oda = find_oda(oda_path)
    if oda is not None:
        return Converter("oda", oda)
    lib = find_libredwg(libredwg_path)
    return Converter("libredwg", lib) if lib is not None else None


def libredwg_version(exe: Path) -> str:
    try:
        out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=30,
                             env=_libredwg_env(exe), check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r"(\d+\.\d+(?:\.\d+)*)", out.stdout + out.stderr)
    return m.group(1) if m else ""


def _libredwg_env(exe: Path) -> dict[str, str]:
    env = dict(os.environ)
    libdir = exe.parent.parent / "lib"  # conda-style layout: bin/dwg2dxf + lib/libredwg.so
    if libdir.is_dir():
        env["LD_LIBRARY_PATH"] = f"{libdir}{os.pathsep}{env.get('LD_LIBRARY_PATH', '')}"
    return env


def dwg_status(oda_path: str = "", libredwg_path: str = "") -> str:
    conv = choose_converter(oda_path, libredwg_path)
    if conv is None:
        return ("DWG support unavailable: install ODA File Converter or LibreDWG and set its path in Settings.")
    return f"DWG import via {conv.label} at {conv.path}."


def _convert_oda(exe: Path, src: Path, dst: Path, version: str = "R2018") -> list[str]:
    import ezdxf
    from ezdxf.addons import odafc

    key = "win_exec_path" if sys.platform.startswith("win") else "unix_exec_path"
    ezdxf.options.set("odafc-addon", key, str(exe))
    try:
        odafc.convert(str(src), str(dst), version=version, replace=True)
    except odafc.ODAFCError as exc:
        raise DwgUnavailableError(f"ODA File Converter failed for {src.name}: {exc}") from exc
    return []


def _convert_libredwg(exe: Path, src: Path, dst: Path) -> list[str]:
    try:
        res = subprocess.run([str(exe), "-y", "-o", str(dst), str(src)], capture_output=True, text=True,
                             timeout=1800, env=_libredwg_env(exe), check=False)
    except subprocess.TimeoutExpired as exc:
        raise DwgUnavailableError(f"LibreDWG timed out converting {src.name}.") from exc
    messages = [ln.strip() for ln in (res.stderr + res.stdout).splitlines() if ln.strip()]
    if res.returncode != 0 or not dst.is_file() or dst.stat().st_size == 0:
        tail = "; ".join(messages[-5:])
        raise DwgUnavailableError(f"LibreDWG could not convert {src.name} (exit {res.returncode}): {tail}")
    return messages


def import_dwg(path: Path, oda_path: str = "", libredwg_path: str = "") -> tuple[Drawing, ImportReport]:
    conv = choose_converter(oda_path, libredwg_path)
    if conv is None:
        raise DwgUnavailableError(dwg_status(oda_path, libredwg_path))
    path = Path(path)
    with tempfile.TemporaryDirectory() as tmp:
        dxf_path = Path(tmp) / (path.stem + ".dxf")
        if conv.kind == "oda":
            messages = _convert_oda(conv.path, path, dxf_path)
        else:
            messages = _convert_libredwg(conv.path, path, dxf_path)
        drawing, report = import_dxf(dxf_path)
    errors = [m for m in messages if m.upper().startswith("ERROR")]
    report.source_name = path.name
    report.converter = conv.label
    report.converter_messages = messages[:200]
    report.warnings.insert(0, f"Converted from DWG to DXF with {conv.label}; the original DWG file is kept unchanged.")
    if errors:
        report.warnings.insert(1, f"The converter reported {len(errors)} read error(s): some entities may be missing "
                                  "or damaged. Compare with the source in AutoCAD before relying on them.")
    drawing.info["source_name"] = path.name
    drawing.info["converted_from"] = "DWG"
    drawing.info["converter"] = conv.label
    return drawing, report


def export_dwg(drawing: Drawing, path: Path, oda_path: str = "", version: str = "R2018") -> list[str]:
    """Write DWG (ODA File Converter only; LibreDWG is not used for writing)."""
    oda = find_oda(oda_path)
    if oda is None:
        raise DwgUnavailableError("Writing DWG requires ODA File Converter.")
    with tempfile.TemporaryDirectory() as tmp:
        dxf_path = Path(tmp) / (Path(path).stem + ".dxf")
        warnings = export_dxf(drawing, dxf_path)
        _convert_oda(oda, dxf_path, Path(path), version)
    return warnings
