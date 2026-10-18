"""Architecture rules and command-line administration."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_engine_layers_do_not_import_qt():
    code = ("import sys; import sls.geometry.setbacks, sls.geometry.areas, sls.engine.run, sls.rules.excel_rules, "
            "sls.licensing.reconcile, sls.cad.dxf_io, sls.cad.ops, sls.reporting.report, sls.services.report_service; "
            "print(any(m.startswith('PySide6') for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT,
                         env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, check=True)
    assert out.stdout.strip() == "False"


def test_cli_backup_list_and_restore(tmp_path):
    cfg = tmp_path / "settings.json"
    cfg.write_text('{"data_dir": "%s"}' % (tmp_path / "data").as_posix(), encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "SLS_CONFIG": str(cfg)}

    def run(*args: str) -> str:
        return subprocess.run([sys.executable, "-m", "sls", *args], capture_output=True, text=True, env=env,
                              check=True).stdout

    backup = Path(run("backup").strip())
    assert backup.is_file()
    assert str(backup) in run("list-backups")
    out = run("restore", str(backup))
    assert "Previous database preserved" in out
