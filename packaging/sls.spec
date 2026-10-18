# PyInstaller spec for Survey & Licensing Studio (one-folder, windowed).
# Build on Windows:  pyinstaller packaging\sls.spec --noconfirm
# The bundled fonts are SIL OFL 1.1; their licence files are shipped alongside.
from pathlib import Path

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(SRC)],
    datas=[
        (str(SRC / "sls" / "resources" / "fonts"), "sls/resources/fonts"),
        (str(SRC / "sls" / "resources" / "i18n"), "sls/resources/i18n"),
    ],
    hiddenimports=["PySide6.QtPdf", "PySide6.QtPdfWidgets", "bidi.algorithm", "xlrd"],
    excludes=["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="SurveyLicensingStudio", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="SurveyLicensingStudio")
