"""Minimal key-based translation catalogue (Arabic / English).

Catalogues are JSON files in ``resources/i18n`` so non-developers can edit wording.
Lookup falls back to English, then to the key itself (which tests flag as missing).
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

LANGUAGES = ("ar", "en")
_I18N_DIR = Path(__file__).resolve().parent.parent / "resources" / "i18n"

_current = "en"


@lru_cache(maxsize=None)
def catalogue(lang: str) -> dict[str, str]:
    path = _I18N_DIR / f"{lang}.json"
    with path.open(encoding="utf-8") as fh:
        data: dict[str, str] = json.load(fh)
    return data


def set_language(lang: str) -> None:
    global _current
    if lang not in LANGUAGES:
        raise ValueError(f"Unsupported language: {lang}")
    _current = lang


def current_language() -> str:
    return _current


def is_rtl(lang: str | None = None) -> bool:
    return (lang or _current) == "ar"


def tr(key: str, lang: str | None = None, **kwargs: object) -> str:
    lang = lang or _current
    text = catalogue(lang).get(key) or catalogue("en").get(key) or key
    return text.format(**kwargs) if kwargs else text
