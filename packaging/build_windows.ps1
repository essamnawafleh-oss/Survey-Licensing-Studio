# Build the Windows distribution folder (run from the repository root in PowerShell).
# Requires Python 3.12+ (64-bit) on Windows.
$ErrorActionPreference = "Stop"
python -m venv .venv-build
.\.venv-build\Scripts\python -m pip install --upgrade pip
.\.venv-build\Scripts\python -m pip install -r requirements.txt pyinstaller==6.*
.\.venv-build\Scripts\python -m pytest -q          # tests must pass before packaging
.\.venv-build\Scripts\pyinstaller packaging\sls.spec --noconfirm
Write-Host "Built: dist\SurveyLicensingStudio\SurveyLicensingStudio.exe"
