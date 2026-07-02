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
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageDraw
    if os.path.exists(TESSERACT_PATH):
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH
    _TESSERACT = True
except ImportError:
    _TESSERACT = False

try:
    import cv2
    import numpy as np
    _CV2 = True
except ImportError:
    _CV2 = False

# PSM 6 = assume uniform block, OEM 1 = LSTM only (best accuracy)
_TESS_CONFIG = "--psm 6 --oem 1"
_MIN_WIDTH_PX = 1500  # upscale images narrower than this


# ---------------------------------------------------------------------------
# Build language string from installed tessdata
# ---------------------------------------------------------------------------

def _build_lang_string() -> str:
    if not _TESSERACT:
        return "eng"
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
# Image preprocessing
# ---------------------------------------------------------------------------

def deskew_image(img: "Image.Image", max_angle: float = 20.0) -> "Image.Image":
    """
    Detect and correct text skew using OpenCV.
    max_angle: skip correction if detected angle exceeds this (degrees).
    Returns the original image unchanged if OpenCV is unavailable or angle is too large.
    """
    return _deskew_cv2(img, max_angle=max_angle)


def _deskew_cv2(img: "Image.Image", max_angle: float = 45.0) -> "Image.Image":
    """Use OpenCV to detect and correct text skew angle."""
    if not _CV2:
        return img
    try:
        arr = np.array(img.convert("L"))
        _, thresh = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        coords = np.column_stack(np.where(thresh > 0))
        if len(coords) < 100:
            return img
        angle = cv2.minAreaRect(coords)[-1]
        # minAreaRect returns angle in [-90, 0); convert to actual skew
        if angle < -45:
            angle = 90 + angle
        if abs(angle) < 0.5 or abs(angle) > max_angle:
            return img
        (h, w) = arr.shape
        center = (w // 2, h // 2)
        M = cv2.getRotationMatrix2D(center, angle, 1.0)
        rotated = cv2.warpAffine(arr, M, (w, h),
                                 flags=cv2.INTER_CUBIC,
                                 borderMode=cv2.BORDER_REPLICATE)
        return Image.fromarray(rotated)
    except Exception:
        return img


def _preprocess(img: "Image.Image", for_statement: bool = False) -> "Image.Image":
    """
    Full preprocessing pipeline for OCR:
      1. Apply EXIF orientation (phone photos need this — prevents upside-down reads)
      2. Greyscale
      3. Upscale if image is too small
      4. Contrast enhancement
      5. Sharpen
      6. Deskew (OpenCV) — corrects slight tilt in scanned documents
    """
    # Step 1: EXIF orientation — must happen before anything else
    img = ImageOps.exif_transpose(img)

    # Step 2: Greyscale
    img = img.convert("L")

    # Step 3: Upscale narrow images
    w, h = img.size
    if w < _MIN_WIDTH_PX:
        scale = _MIN_WIDTH_PX / w
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    # Step 4: CLAHE — better than flat contrast for uneven-lit phone photos
    if _CV2:
        try:
            arr = np.array(img)
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            img = Image.fromarray(clahe.apply(arr))
        except Exception:
            img = ImageEnhance.Contrast(img).enhance(1.8)
    else:
        img = ImageEnhance.Contrast(img).enhance(1.8)

    # Step 5: Sharpen
    img = img.filter(ImageFilter.SHARPEN)

    # Step 6: Deskew (skip for statements — whole-page skew confuses column layout)
    if not for_statement:
        img = _deskew_cv2(img)

    return img


# ---------------------------------------------------------------------------
# Public extraction API
# ---------------------------------------------------------------------------

def extract_text(file_path: str | Path, is_statement: bool = False) -> str:
    """
    Extract text from a PDF or image file.
    Set is_statement=True for credit card statement images (skips deskew, handles multi-page).
    Returns empty string on failure.
    """
    path = Path(file_path)
    if not path.exists():
        return ""

    suffix = path.suffix.lower()

    if suffix == ".pdf":
        text = _extract_pdf_text(path)
        if text.strip():
            return text
        return _ocr_pdf_pages(path, is_statement=is_statement)

    if suffix in {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp"}:
        return _ocr_image(path, is_statement=is_statement)

    return ""


def _extract_pdf_text(path: Path) -> str:
    """Use pdfplumber to extract native text from a digital PDF (all pages)."""
    if not _PDFPLUMBER:
        return ""
    try:
        with pdfplumber.open(str(path)) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
        return "\n".join(pages)
    except Exception:
        return ""


def _ocr_pdf_pages(path: Path, is_statement: bool = False) -> str:
    """Render ALL PDF pages via PyMuPDF then OCR each with Tesseract."""
    if not (_FITZ and _TESSERACT):
        return ""
    results = []
    try:
        doc = fitz.open(str(path))
        for page in doc:
            mat = fitz.Matrix(3.0, 3.0)  # 216 DPI for small Chinese characters
            pix = page.get_pixmap(matrix=mat)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            img = _preprocess(img, for_statement=is_statement)
            text = pytesseract.image_to_string(img, lang=_OCR_LANG, config=_TESS_CONFIG)
            results.append(text)
    except Exception:
        pass
    return "\n".join(results)


def _ocr_image(path: Path, is_statement: bool = False) -> str:
    """OCR a single image file."""
    if not _TESSERACT:
        return ""
    try:
        img = Image.open(str(path))
        img = _preprocess(img, for_statement=is_statement)
        return pytesseract.image_to_string(img, lang=_OCR_LANG, config=_TESS_CONFIG)
    except Exception:
        return ""


def extract_words_with_positions(
    img: "Image.Image",
) -> list[dict]:
    """
    Return a list of {text, left, top, width, height, conf} dicts from Tesseract.
    Only includes words with confidence > 25 and non-empty text.
    """
    if not _TESSERACT:
        return []
    try:
        import pandas as pd
        data = pytesseract.image_to_data(
            img, lang=_OCR_LANG, config=_TESS_CONFIG,
            output_type=pytesseract.Output.DATAFRAME,
        )
        data = data[(data["conf"] > 25) & (data["text"].str.strip() != "")]
        return data[["text", "left", "top", "width", "height", "conf"]].to_dict("records")
    except Exception:
        return []


def reconstruct_lines_from_words(
    words: list[dict],
    y_tolerance: int = 15,
    x_gap_threshold: int = 80,
) -> list[str]:
    """
    Group words into lines by y-position proximity, then sort by x within each line.
    y_tolerance: words within this many pixels vertically are on the same line.
    x_gap_threshold: gaps wider than this in pixels get an extra space inserted.
    """
    if not words:
        return []

    # Sort by top (y), then left (x)
    words = sorted(words, key=lambda w: (w["top"], w["left"]))

    lines: list[list[dict]] = []
    current: list[dict] = [words[0]]

    for word in words[1:]:
        # Compare to the median top of the current line
        line_y = sum(w["top"] for w in current) / len(current)
        if abs(word["top"] - line_y) <= y_tolerance:
            current.append(word)
        else:
            lines.append(sorted(current, key=lambda w: w["left"]))
            current = [word]
    if current:
        lines.append(sorted(current, key=lambda w: w["left"]))

    result = []
    for line_words in lines:
        parts = []
        prev_right = None
        for w in line_words:
            word_text = str(w["text"]) if w["text"] == w["text"] else ""  # NaN guard
            if not word_text.strip():
                continue
            left = int(w["left"])
            if prev_right is not None and (left - prev_right) > x_gap_threshold:
                parts.append("  ")  # extra space for column gaps
            parts.append(word_text)
            prev_right = left + int(w["width"])
        if parts:
            result.append(" ".join(parts))

    return result


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


def render_image_preview(file_path: str | Path, max_width: int = 800) -> bytes | None:
    """Return EXIF-corrected image bytes (PNG) for Streamlit preview."""
    try:
        img = Image.open(str(file_path))
        img = ImageOps.exif_transpose(img)
        # Downscale for preview
        w, h = img.size
        if w > max_width:
            img = img.resize((max_width, int(h * max_width / w)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
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
        "opencv": _CV2,
        "ocr_lang": _OCR_LANG,
    }


def draw_highlights_on_image(
    file_path: str | Path,
    vendor: str = "",
    date: str = "",
    total: float | None = None,
    tax: float | None = None,
) -> bytes | None:
    """
    Open the receipt file, perform word OCR to get coordinates, search for matching 
    vendor, date, total, and tax strings, and draw colored highlight boxes over them.
    Returns PNG bytes of the highlighted image.
    """
    if not _TESSERACT:
        return None
        
    try:
        import pandas as pd
        path = Path(file_path)
        img = None
        if path.suffix.lower() == ".pdf":
            if not _FITZ:
                return None
            doc = fitz.open(str(path))
            page = doc[0]
            pix = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0))
            img = Image.open(io.BytesIO(pix.tobytes("png")))
        else:
            img = Image.open(str(path))
            img = ImageOps.exif_transpose(img)
            
        w, h = img.size
        # Downscale for preview (max width 800) to keep memory usage low and matches fast
        max_width = 800
        if w > max_width:
            img = img.resize((max_width, int(h * max_width / w)), Image.LANCZOS)
            
        # Get words and their positions on the preprocessed/scaled image
        words = extract_words_with_positions(img)
        if not words:
            # Try preprocessing if raw image gave nothing
            p_img = _preprocess(img)
            words = extract_words_with_positions(p_img)
            
        if not words:
            # Return original preview if no OCR matches
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
            
        # Create transparent overlay layer
        overlay = Image.new("RGBA", img.size, (200, 200, 200, 0))
        draw = ImageDraw.Draw(overlay)
        
        # Helper to draw box
        def draw_box(box, color):
            # box: {left, top, width, height}
            l, t, w_b, h_b = box["left"], box["top"], box["width"], box["height"]
            # draw a semi-transparent rectangle
            draw.rectangle([l, t, l + w_b, t + h_b], fill=color)

        # Prepare queries
        # Vendor words
        vendor_words = [vw.strip().lower() for vw in vendor.split() if len(vw.strip()) > 2] if vendor else []
        # Date components (e.g. "2026-07-02" -> ["2026", "07", "02"] or month name)
        date_words = []
        if date:
            date_words.extend([d.strip() for d in date.split("-") if d.strip()])
            try:
                dt_obj = pd.to_datetime(date)
                date_words.append(dt_obj.strftime("%b")) # e.g. "Jul"
                date_words.append(dt_obj.strftime("%B")) # e.g. "July"
            except Exception:
                pass
        date_words = [dw.lower() for dw in date_words if len(dw) > 1]
        
        # Total strings
        total_strs = []
        if total is not None and total > 0:
            total_strs.append(f"{total:.2f}")
            total_strs.append(f"{total:.0f}")
            total_strs.append(str(total))
            
        # Tax strings
        tax_strs = []
        if tax is not None and tax > 0:
            tax_strs.append(f"{tax:.2f}")
            tax_strs.append(str(tax))

        for word in words:
            text = word["text"].strip().lower()
            if not text:
                continue
                
            # Match Vendor -> Blue
            if any(vw in text for vw in vendor_words):
                draw_box(word, (0, 100, 255, 90))
                
            # Match Date -> Green
            elif any(dw in text for dw in date_words):
                draw_box(word, (0, 200, 0, 90))
                
            # Match Total -> Yellow
            elif any(ts in text for ts in total_strs):
                draw_box(word, (255, 200, 0, 95))
                
            # Match Tax -> Red
            elif any(txs in text for txs in tax_strs):
                draw_box(word, (255, 0, 0, 90))

        # Composite overlay onto original image
        img = img.convert("RGBA")
        highlighted = Image.alpha_composite(img, overlay)
        
        buf = io.BytesIO()
        highlighted.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None
