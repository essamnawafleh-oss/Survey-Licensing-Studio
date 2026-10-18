"""Diagnostic log and uncaught-exception handling.

The log is a rotating text file outside the installation folder:
Windows ``%LOCALAPPDATA%\\SurveyLicensingStudio\\logs\\sls.log``, elsewhere
``~/.local/share/SurveyLicensingStudio/logs/sls.log`` (override: ``SLS_LOG_DIR``).
It records start-up details (versions, paths, frozen or not) and every uncaught exception, so a
problem in the windowed executable (which has no console) can still be diagnosed. The log never
contains survey geometry or document contents.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import sys
import threading
import traceback
from collections.abc import Callable
from pathlib import Path

from .settings import default_data_dir

LOGGER = logging.getLogger("sls")
_configured: Path | None = None


def log_dir() -> Path:
    override = os.environ.get("SLS_LOG_DIR")
    if override:
        return Path(override)
    base = default_data_dir()
    return (base.parent if sys.platform.startswith("win") else base) / "logs"


def setup_logging(directory: Path | None = None, level: int = logging.INFO) -> Path:
    """Configure the rotating file log once; returns the log file path."""
    global _configured
    if _configured is not None:
        return _configured
    d = directory or log_dir()
    path = d / "sls.log"
    try:
        d.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5,
                                                                        encoding="utf-8")
    except OSError:  # unwritable log folder: never prevent start-up
        handler = logging.StreamHandler(sys.stderr) if sys.stderr else logging.NullHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(level)
    _configured = path
    return path


def startup_report() -> dict[str, str]:
    from .. import __version__

    info = {"version": __version__, "python": sys.version.split()[0], "platform": platform.platform(),
            "frozen": str(bool(getattr(sys, "frozen", False))), "executable": sys.executable,
            "bundle_dir": str(getattr(sys, "_MEIPASS", ""))}
    for mod in ("PySide6", "shapely", "pyproj", "ezdxf", "reportlab", "pypdf"):
        try:
            info[mod] = __import__(mod).__version__
        except Exception as exc:  # noqa: BLE001 - reported, not fatal
            info[mod] = f"unavailable ({exc})"
    try:
        import pyproj

        info["proj_data"] = pyproj.datadir.get_data_dir()
    except Exception as exc:  # noqa: BLE001
        info["proj_data"] = f"unavailable ({exc})"
    return info


def log_startup() -> None:
    for k, v in startup_report().items():
        LOGGER.info("startup %s = %s", k, v)


def install_exception_hooks(show: Callable[[str], None] | None = None) -> None:
    """Log uncaught exceptions (main and worker threads) and optionally show a message."""
    def handle(exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        LOGGER.error("uncaught exception\n%s", text)
        if show is not None:
            try:
                show(f"{exc_type.__name__}: {exc}")
            except Exception:  # noqa: BLE001 - the error is already logged
                pass

    sys.excepthook = handle
    threading.excepthook = lambda a: handle(a.exc_type, a.exc_value, a.exc_traceback)  # type: ignore[arg-type]
