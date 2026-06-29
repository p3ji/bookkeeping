"""Local text extraction from PDF and image receipts."""
from __future__ import annotations

import io
import os
import re
from pathlib import Path

from config import TESSERACT_PATH, RECEIPT_EXTENSIONS

# --- optional imports handled gracefully ---
try:
    import pdfplumber
    _PDFPLUMBER = True
except ImportError:
    _PDFPLUMBER = False

try:
    import fitz  # PyMuPDF
    _FITZ = True
except ImportError:
    _FITZ = False

try:
    import pytesseract
    from PIL import Image
    if os.path.exists(TESSERACT_PATH):
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
    _TESSERACT = True
except ImportError:
    _TESSERACT = False


# ---------------------------------------------------------------------------
# Build Tesseract language string from what's actually installed
# ---------------------------------------------------------------------------

def _build_lang_string() -> str:
    """
    Return the best available Tesseract language string.
    Always includes English; adds Simplified and Traditional Chinese when
    their traineddata files are present in the Tesseract tessdata directory.
    """
    if not _TESSERACT:
        return "eng"

    # Locate tessdata directory (sibling of the tesseract executable)
    tess_exe = Path(TESSERACT_PATH)
    tessdata = tess_exe.parent / "tessdata"

    langs = ["eng"]
    for lang_code in ("chi_sim", "chi_tra"):
        if (tessdata / f"{lang_code}.traineddata").exists():
            langs.append(lang_code)

    return "+".join(langs)


_OCR_LANG: str = _build_lang_string()
_HAS_CHINESE: bool = "chi_sim" in _OCR_LANG or "chi_tra" in _OCR_LANG


# ---------------------------------------------------------------------------
# Public extraction API
# ---------------------------------------------------------------------------

def extract_text(file_path: str | Path) -> str:
    """Extract text from a PDF or image file. Returns empty string on failure."""
    path = Path(file_path)
    if not path.exists():
        return ""

    suffix = path.suffix.lower()

    if suffix == ".pdf":
        text = _extract_pdf_text(path)
        if text.strip():
            return text
        # Fallback: render first page as image and OCR
        return _ocr_pdf_page(path)

    if suffix in {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp"}:
        return _ocr_image(path)

    return ""


def _extract_pdf_text(path: Path) -> str:
    """Use pdfplumber to extract native text from a digital PDF."""
    if not _PDFPLUMBER:
        return ""
    try:
        with pdfplumber.open(str(path)) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages[:5]]
        return "\n".join(pages)
    except Exception:
        return ""


def _ocr_pdf_page(path: Path) -> str:
    """Render first PDF page via PyMuPDF then run multi-language Tesseract OCR."""
    if not (_FITZ and _TESSERACT):
        return ""
    try:
        doc = fitz.open(str(path))
        page = doc[0]
        mat = fitz.Matrix(2.0, 2.0)  # 2x scale improves OCR quality
        pix = page.get_pixmap(matrix=mat)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        return pytesseract.image_to_string(img, lang=_OCR_LANG)
    except Exception:
        return ""


def _ocr_image(path: Path) -> str:
    if not _TESSERACT:
        return ""
    try:
        img = Image.open(str(path))
        return pytesseract.image_to_string(img, lang=_OCR_LANG)
    except Exception:
        return ""


def render_pdf_preview(file_path: str | Path, page: int = 0) -> bytes | None:
    """Render a PDF page to PNG bytes for display in Streamlit."""
    if not _FITZ:
        return None
    try:
        doc = fitz.open(str(file_path))
        if page >= len(doc):
            page = 0
        pix = doc[page].get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
        return pix.tobytes("png")
    except Exception:
        return None


def find_receipt_files(receipts_dir: str | Path) -> list[Path]:
    """Walk the receipts directory and return all supported receipt files."""
    root = Path(receipts_dir)
    if not root.exists():
        return []
    return [
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in RECEIPT_EXTENSIONS
    ]


def ocr_capabilities() -> dict:
    return {
        "pdfplumber": _PDFPLUMBER,
        "pymupdf": _FITZ,
        "tesseract": _TESSERACT,
        "chinese": _HAS_CHINESE,
        "ocr_lang": _OCR_LANG,
    }
