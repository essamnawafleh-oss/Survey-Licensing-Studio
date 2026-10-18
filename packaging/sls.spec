# PyInstaller spec for Survey & Licensing Studio (one-folder, windowed).
# Build on Windows:  pyinstaller packaging\sls.spec --noconfirm
# The bundled fonts are SIL OFL 1.1; their licence files are shipped alongside.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(SRC)],
    datas=[
        (str(SRC / "sls" / "resources" / "fonts"), "sls/resources/fonts"),
        (str(SRC / "sls" / "resources" / "i18n"), "sls/resources/i18n"),
        *collect_data_files("pyproj"),  # PROJ database (proj.db) for EPSG:3066 / JTM, offline
        *([(str(ROOT / "tools"), "tools")] if (ROOT / "tools").is_dir() else []),  # optional bundled LibreDWG
    ],
    hiddenimports=["PySide6.QtPdf", "PySide6.QtPdfWidgets", "bidi.algorithm", "xlrd"],
    excludes=["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
ICON = str(ROOT / "packaging" / "sls.ico")
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="SurveyLicensingStudio", console=False, icon=ICON)
# Console twin for administration and release checks (backup, restore, create-admin, self-test):
# the windowed executable has no console, so their output would be invisible.
exe_console = EXE(pyz, a.scripts, [], exclude_binaries=True, name="SurveyLicensingStudio-console", console=True,
                  icon=ICON)
coll = COLLECT(exe, exe_console, a.binaries, a.datas, name="SurveyLicensingStudio")
