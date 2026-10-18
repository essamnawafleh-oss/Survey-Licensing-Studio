"""Every translation key used in the code exists in both Arabic and English catalogues."""
from __future__ import annotations

import re
from pathlib import Path

from sls.cad.model import ENTITY_KINDS, LAYER_ROLES
from sls.core.i18n import catalogue
from sls.core.units import CrsKind, LengthUnit
from sls.persistence.cases import CASE_STATUSES
from sls.persistence.media_repo import NOTE_SECTIONS
from sls.persistence.users import ROLES
from sls.ui.tools import TOOLS

SRC = Path(__file__).resolve().parent.parent / "src" / "sls"
LITERAL = re.compile(r"""\btr\(\s*["']([a-z_]+\.[A-Za-z0-9_.]+)["']""")


def used_keys() -> set[str]:
    keys: set[str] = set()
    for path in SRC.rglob("*.py"):
        keys |= {k for k in LITERAL.findall(path.read_text(encoding="utf-8")) if not k.endswith(".")}
    keys |= {f"role.{r}" for r in LAYER_ROLES}
    keys |= {f"kind_entity.{k}" for k in ENTITY_KINDS}
    keys |= {f"tool_name.{t}" for t in TOOLS}
    keys |= {f"unit_name.{u.value}" for u in LengthUnit}
    keys |= {f"crs_kind.{k.value}" for k in CrsKind}
    keys |= {f"case_status.{s}" for s in CASE_STATUSES}
    keys |= {f"note.{s}" for s in NOTE_SECTIONS}
    keys |= {f"users.role.{r}" for r in ROLES}
    keys |= {f"source.{s}" for s in ("survey", "regulatory", "derived", "user")}
    keys |= {f"calc.status.{s}" for s in ("ok", "partial", "error")}
    keys |= {f"lic.source.{s}" for s in ("transcribed", "ocr_reviewed", "imported")}
    keys |= {f"lic.mark.{s}" for s in ("verified", "disputed", "unverified")}
    keys |= {f"verif.{s}" for s in ("verified", "disputed", "unverified")}
    keys |= {f"status.{s}" for s in ("draft", "reviewed", "approved")}
    keys |= {f"check.{s}" for s in ("setbacks", "coverage", "floors", "far", "pass", "fail", "not_evaluated")}
    keys |= {f"setback.{s}" for s in ("ok", "multipart", "empty")}
    keys |= {f"layer.btn.{b}" for b in ("new", "rename", "duplicate", "delete", "up", "down", "current", "props")}
    keys |= {f"rules.field.{f}" for f in ("code", "front", "side", "rear", "name_ar", "name_en", "max_coverage_pct",
                                           "max_floors", "max_far", "notes")}
    keys |= {f"lic_issue.{k}" for k in ("no_date", "no_quantities", "no_document", "duplicate_floor")}
    keys |= {f"lic.{k}" for k in ("attach", "open", "doc_options", "ocr")}
    keys |= {f"kind.{k}" for k in ("wall", "fence")}
    keys |= {f"rules.st.{k}" for k in ("numeric", "conditional", "no_setbacks")}
    keys |= {f"rules.st_hint.{k}" for k in ("conditional", "no_setbacks")}
    keys |= {f"act.{a}" for a in ("new_case", "open_case", "save", "import_drawing", "new_drawing", "export_dxf",
                                  "demo_case", "backup", "restore", "settings", "users", "exit", "undo", "redo",
                                  "erase", "select_all", "zoom_extents", "grid", "osnap", "grid_snap", "ortho",
                                  "lineweights", "case_details", "rules", "boundary", "calculate", "results",
                                  "licenses", "media", "report", "regulatory_check", "regulatory_transform",
                                  "case_fields", "setback_override", "limitations", "about")}
    return keys


def test_all_keys_present_in_both_languages():
    keys = used_keys()
    for lang in ("ar", "en"):
        missing = sorted(k for k in keys if k not in catalogue(lang))
        assert not missing, f"{lang}: {missing}"


def test_catalogues_have_same_keys_and_placeholders():
    ar, en = catalogue("ar"), catalogue("en")
    assert set(ar) == set(en)
    ph = re.compile(r"{(\w+)}")
    for k in en:
        assert set(ph.findall(en[k])) == set(ph.findall(ar[k])), k
