"""Shared fixtures. All data is synthetic (see sls.samples)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sls.core.settings import Settings  # noqa: E402
from sls.persistence.users import User  # noqa: E402
from sls.persistence.workspace import Workspace  # noqa: E402
from sls.samples import write_sample_dxf, write_sample_rules_xlsx  # noqa: E402

ADMIN = User("u-admin", "admin", "Admin", "admin")
SURVEYOR = User("u-surv", "surveyor1", "Surveyor", "surveyor")
REVIEWER = User("u-rev", "reviewer1", "Reviewer", "reviewer")


@pytest.fixture
def sample_dxf(tmp_path: Path) -> Path:
    return write_sample_dxf(tmp_path / "synthetic_survey.dxf")


@pytest.fixture
def sample_xlsx(tmp_path: Path) -> Path:
    return write_sample_rules_xlsx(tmp_path / "synthetic_rules.xlsx")


@pytest.fixture
def workspace(tmp_path: Path):
    ws = Workspace(Settings(data_dir=str(tmp_path / "data")), user=ADMIN)
    yield ws
    ws.close()
