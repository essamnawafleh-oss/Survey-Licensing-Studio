"""Arabic/English text for ReportLab: shaping, bidi, wrapping and font runs.

ReportLab has no Arabic shaping. We (1) wrap lines in *logical* order by measured width,
(2) reshape each line into presentation forms (arabic-reshaper), (3) reorder it visually
(python-bidi), then (4) draw it as runs, using Noto Naskh Arabic for Arabic-script
characters and Noto Sans for everything else. Wrapping before reordering keeps
multi-line right-to-left paragraphs in the correct line order.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path

import arabic_reshaper
# The compiled `bidi.get_display` (python-bidi 0.6) implements the paired-bracket rule N0, so
# mixed Arabic/Latin text with brackets is ordered correctly, but it does not mirror glyphs
# (rule L4); `_mirror_brackets` does that. (The pure-Python algorithm mirrors but lacks N0.)
from bidi import get_display
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Flowable

FONT_DIR = Path(__file__).resolve().parent.parent / "resources" / "fonts"
AR, AR_BOLD, LAT, LAT_BOLD = "SLS-Naskh", "SLS-Naskh-Bold", "SLS-Sans", "SLS-Sans-Bold"
_registered = False
_coverage: dict[str, set[int]] = {}


def register_fonts() -> None:
    global _registered
    if _registered:
        return
    for name, file in ((AR, "NotoNaskhArabic-Regular.ttf"), (AR_BOLD, "NotoNaskhArabic-Bold.ttf"),
                       (LAT, "NotoSans-Regular.ttf"), (LAT_BOLD, "NotoSans-Bold.ttf")):
        font = TTFont(name, str(FONT_DIR / file))
        pdfmetrics.registerFont(font)
        _coverage[name] = set(font.face.charToGlyph)
    _registered = True


def is_arabic_char(ch: str) -> bool:
    cp = ord(ch)
    return (0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F or 0x08A0 <= cp <= 0x08FF
            or 0xFB50 <= cp <= 0xFDFF or 0xFE70 <= cp <= 0xFEFF)


def has_arabic(text: str) -> bool:
    return any(is_arabic_char(c) for c in text)


def base_direction(text: str, default: str = "L") -> str:
    """'R' if the first strong character is right-to-left, else 'L'."""
    for ch in text:
        bidi = unicodedata.bidirectional(ch)
        if bidi in ("R", "AL"):
            return "R"
        if bidi == "L":
            return "L"
    return default


def visual(text: str, base_dir: str) -> str:
    """Reshape + reorder one line (no wrapping)."""
    if not text:
        return ""
    shaped = arabic_reshaper.reshape(text) if has_arabic(text) else text
    return _mirror_brackets(get_display(shaped, base_dir=base_dir), base_dir)


_MIRROR = {"(": ")", ")": "(", "[": "]", "]": "[", "{": "}", "}": "{", "<": ">", ">": "<", "«": "»", "»": "«"}


def _strong(ch: str) -> str:
    b = unicodedata.bidirectional(ch)
    if b in ("R", "AL") or 0xFB50 <= ord(ch) <= 0xFEFF:  # incl. Arabic presentation forms
        return "R"
    return "L" if b == "L" else ""


def _mirror_brackets(vis: str, base_dir: str) -> str:
    """Rule L4 on a visual line: mirror brackets that resolved right-to-left.

    A bracket resolves to the paragraph direction unless both its nearest strong neighbours have
    the opposite direction (UAX #9 N0 for the common cases: an English word in brackets inside
    Arabic text, Arabic in brackets inside English text, numbers in brackets).
    """
    if not any(c in _MIRROR for c in vis):
        return vis
    out = list(vis)
    for i, ch in enumerate(vis):
        if ch not in _MIRROR:
            continue
        left = next((d for d in map(_strong, reversed(vis[:i])) if d), "")
        right = next((d for d in map(_strong, vis[i + 1:]) if d), "")
        if base_dir == "R":
            rtl = not (left == "L" and right == "L")
        else:
            rtl = left == "R" and right == "R"
        if rtl:
            out[i] = _MIRROR[ch]
    return "".join(out)


def runs(visual_text: str, bold: bool = False) -> list[tuple[str, str]]:
    """Split a visual string into (font, text) runs by script. Neutral chars join the previous run."""
    register_fonts()
    ar_font, lat_font = (AR_BOLD, LAT_BOLD) if bold else (AR, LAT)
    out: list[tuple[str, str]] = []
    for ch in visual_text:
        if is_arabic_char(ch):
            font = ar_font
        elif ch.isspace():
            font = out[-1][0] if out else lat_font
        elif out and out[-1][0] == ar_font and ord(ch) in _coverage[ar_font] and \
                unicodedata.category(ch).startswith("P"):
            font = ar_font  # Arabic punctuation-compatible glyph available: keep the run
        else:
            font = lat_font
        if out and out[-1][0] == font:
            out[-1] = (font, out[-1][1] + ch)
        else:
            out.append((font, ch))
    return out


def line_width(visual_text: str, size: float, bold: bool = False) -> float:
    return sum(pdfmetrics.stringWidth(t, f, size) for f, t in runs(visual_text, bold))


def wrap_logical(text: str, size: float, width: float, base_dir: str, bold: bool = False) -> list[str]:
    """Wrap logical text into lines that fit ``width`` once shaped (returns visual lines)."""
    lines_out: list[str] = []
    for para in text.split("\n"):
        words = para.split(" ")
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if current and line_width(visual(candidate, base_dir), size, bold) > width:
                lines_out.append(visual(current, base_dir))
                current = word
            else:
                current = candidate
        lines_out.append(visual(current, base_dir))
    return lines_out


@dataclass
class TextStyle:
    size: float = 9.5
    leading: float | None = None
    bold: bool = False
    align: str = "auto"  # auto | left | right | center
    color: tuple[float, float, float] = (0.1, 0.1, 0.1)
    space_after: float = 0.0
    base_dir: str = "auto"  # auto | L | R

    @property
    def line_height(self) -> float:
        return self.leading or self.size * 1.55


class BidiText(Flowable):
    """A paragraph that renders Arabic, English and mixed text correctly."""

    def __init__(self, text: str, style: TextStyle | None = None, rtl_default: bool = True) -> None:
        super().__init__()
        register_fonts()
        self.text = "" if text is None else str(text)
        self.style = style or TextStyle()
        default = "R" if rtl_default else "L"
        self.base_dir = self.style.base_dir if self.style.base_dir in ("L", "R") else base_direction(self.text, default)
        self.lines: list[str] = []
        self._width = 0.0

    def wrap(self, avail_width: float, avail_height: float) -> tuple[float, float]:
        self._width = avail_width
        self.lines = wrap_logical(self.text, self.style.size, avail_width, self.base_dir, self.style.bold)
        return avail_width, len(self.lines) * self.style.line_height + self.style.space_after

    def draw(self) -> None:
        c = self.canv
        st = self.style
        align = st.align
        if align == "auto":
            align = "right" if self.base_dir == "R" else "left"
        c.setFillColorRGB(*st.color)
        y = len(self.lines) * st.line_height + st.space_after - st.size * 1.15
        for line in self.lines:
            w = line_width(line, st.size, st.bold)
            x = {"left": 0.0, "right": self._width - w, "center": (self._width - w) / 2}[align]
            for font, chunk in runs(line, st.bold):
                c.setFont(font, st.size)
                c.drawString(x, y, chunk)
                x += pdfmetrics.stringWidth(chunk, font, st.size)
            y -= st.line_height


def draw_text(canvas, x: float, y: float, text: str, size: float, align: str = "left",  # type: ignore[no-untyped-def]
              bold: bool = False, rtl_default: bool = True) -> float:
    """Draw a single line at (x, y) on a raw canvas. Returns its width."""
    register_fonts()
    vis = visual(text, base_direction(text, "R" if rtl_default else "L"))
    w = line_width(vis, size, bold)
    if align == "right":
        x -= w
    elif align == "center":
        x -= w / 2
    for font, chunk in runs(vis, bold):
        canvas.setFont(font, size)
        canvas.drawString(x, y, chunk)
        x += pdfmetrics.stringWidth(chunk, font, size)
    return w
