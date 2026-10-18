"""Optional offline OCR through a locally installed Tesseract executable.

OCR output is stored as an *unverified* note attached to the source file. It is never
used to fill license fields automatically.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from ..core.errors import SlsError

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}


class OcrUnavailableError(SlsError):
    code = "ocr_unavailable"


def find_tesseract(configured: str = "") -> Path | None:
    if configured:
        p = Path(configured)
        return p if p.is_file() else None
    found = shutil.which("tesseract")
    if found:
        return Path(found)
    win = Path("C:/Program Files/Tesseract-OCR/tesseract.exe")
    return win if win.is_file() else None


def available_languages(exe: Path) -> list[str]:
    out = subprocess.run([str(exe), "--list-langs"], capture_output=True, text=True, timeout=30, check=False)
    return [ln.strip() for ln in out.stdout.splitlines()[1:] if ln.strip()]


def ocr_file(path: Path, configured: str = "", languages: str = "ara+eng", max_pages: int = 20) -> tuple[str, str]:
    """Return (text, languages used). PDFs are rasterised locally with pypdfium2."""
    exe = find_tesseract(configured)
    if exe is None:
        raise OcrUnavailableError("Tesseract OCR is not installed or not configured (Settings → Tesseract path).")
    langs = available_languages(exe)
    wanted = [lang for lang in languages.split("+") if lang in langs]
    if not wanted:
        raise OcrUnavailableError(f"None of the OCR languages {languages} are installed (available: {langs}).")
    lang_arg = "+".join(wanted)
    suffix = path.suffix.lower()
    with tempfile.TemporaryDirectory() as tmp:
        images: list[Path] = []
        if suffix == ".pdf":
            import pypdfium2 as pdfium

            pdf = pdfium.PdfDocument(str(path))
            for i in range(min(len(pdf), max_pages)):
                img = Path(tmp) / f"p{i + 1}.png"
                pdf[i].render(scale=300 / 72).to_pil().save(img)
                images.append(img)
        elif suffix in IMAGE_SUFFIXES:
            images.append(path)
        else:
            raise OcrUnavailableError(f"OCR is not supported for {suffix} files.")
        texts = []
        for i, img in enumerate(images):
            res = subprocess.run([str(exe), str(img), "stdout", "-l", lang_arg], capture_output=True, text=True,
                                 timeout=300, check=False)
            if res.returncode != 0:
                raise OcrUnavailableError(f"Tesseract failed on page {i + 1}: {res.stderr.strip()[:300]}")
            texts.append(f"--- page {i + 1} ---\n{res.stdout.strip()}")
    return "\n\n".join(texts), lang_arg
