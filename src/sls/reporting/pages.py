"""Page-selection parsing for attaching parts of multipage documents."""
from __future__ import annotations

from ..core.errors import ValidationError


def parse_page_selection(selection: str, total: int | None) -> list[int] | None:
    """Parse '1,3-5' into 0-based page indices.

    Empty selection means all pages (returns ``None``). With ``total`` given, pages
    outside 1..total raise ``ValidationError`` instead of being dropped.
    """
    text = (selection or "").strip()
    if not text:
        return None
    pages: list[int] = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                start, end = int(a), int(b)
            else:
                start = end = int(part)
        except ValueError as exc:
            raise ValidationError(f"Invalid page selection '{selection}'.") from exc
        if start < 1 or end < start:
            raise ValidationError(f"Invalid page range '{part}'.")
        if total is not None and end > total:
            raise ValidationError(f"Page {end} does not exist (document has {total} pages).")
        for p in range(start, end + 1):
            if p - 1 not in pages:
                pages.append(p - 1)
    return pages
