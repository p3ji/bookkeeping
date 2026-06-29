"""
Vision-LLM based document extraction via Ollama (local, offline).

Falls back gracefully to Tesseract OCR when Ollama is not available.
Install Ollama from https://ollama.com/download then:
  ollama pull llama3.2-vision   # ~7 GB, best accuracy
  ollama pull llava             # ~4 GB, lighter alternative
"""
from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Ollama availability check
# ---------------------------------------------------------------------------

_OLLAMA_OK: Optional[bool] = None      # None = not yet checked
_PREFERRED_MODELS = ["llama3.2-vision", "llava", "llava:13b", "llava:7b"]


def _encode_image(path: Path) -> str:
    """Return base64-encoded image bytes for Ollama API."""
    from PIL import Image, ImageOps
    img = Image.open(str(path))
    img = ImageOps.exif_transpose(img)  # correct phone EXIF rotation first
    # Resize to max 1600px wide for speed
    w, h = img.size
    if w > 1600:
        img = img.resize((1600, int(h * 1600 / w)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


def is_ollama_available() -> bool:
    """Return True if the Ollama server is running and has a vision model."""
    global _OLLAMA_OK
    if _OLLAMA_OK is not None:
        return _OLLAMA_OK
    try:
        import ollama
        models = [m.model for m in ollama.list().models]
        _OLLAMA_OK = any(
            any(pref in m for pref in _PREFERRED_MODELS) for m in models
        )
    except Exception:
        _OLLAMA_OK = False
    return _OLLAMA_OK


def ollama_vision_model() -> Optional[str]:
    """Return the name of the first available vision model, or None."""
    try:
        import ollama
        models = [m.model for m in ollama.list().models]
        for pref in _PREFERRED_MODELS:
            for m in models:
                if pref in m:
                    return m
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Statement extraction
# ---------------------------------------------------------------------------

_STATEMENT_PROMPT = """You are a bookkeeping assistant. Extract ALL credit card transactions from this statement image.
Ignore header, summary, and footer rows. Only return actual purchase/charge transactions (not payments or credits back to the account).

Return ONLY a JSON array, no other text. Each element:
{
  "date": "YYYY-MM-DD",
  "vendor": "merchant name only (no city/province/category)",
  "amount": 12.34
}

Rules:
- date: use the transaction date (first date column), same year as statement if only month+day shown
- vendor: clean name only (e.g. "Costco Wholesale", "Shell", "MSFT")
- amount: positive number, no $ sign
- Skip lines that are payments TO the card, credits, or totals"""


def extract_statement_transactions(
    image_path: str | Path,
    default_year: int,
) -> list[dict]:
    """
    Use a local Ollama vision LLM to extract transactions from a statement image.
    Returns list of {date, vendor, amount_gross} dicts on success, [] on failure.
    """
    path = Path(image_path)
    if not path.exists():
        return []

    model = ollama_vision_model()
    if not model:
        return []

    try:
        import ollama
        img_b64 = _encode_image(path)
        response = ollama.chat(
            model=model,
            messages=[{
                "role": "user",
                "content": _STATEMENT_PROMPT,
                "images": [img_b64],
            }],
            options={"temperature": 0},
        )
        raw = response.message.content.strip()
        # Extract JSON array from response (model sometimes adds prose)
        json_match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not json_match:
            return []
        rows = json.loads(json_match.group(0))
        results = []
        for row in rows:
            try:
                import pandas as pd
                date_str = pd.to_datetime(str(row.get("date", "")),
                                          dayfirst=False).strftime("%Y-%m-%d")
                amount = float(row.get("amount", 0))
                vendor = str(row.get("vendor", "")).strip()
                if date_str and amount > 0 and vendor:
                    results.append({
                        "date": date_str,
                        "vendor": vendor,
                        "amount_gross": round(amount, 2),
                    })
            except Exception:
                continue
        return results
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Receipt extraction
# ---------------------------------------------------------------------------

_RECEIPT_PROMPT = """You are a bookkeeping assistant. Extract key information from this receipt image.
Return ONLY a JSON object, no other text:
{
  "vendor": "store/company name",
  "date": "YYYY-MM-DD or empty string if not found",
  "total": 12.34,
  "subtotal": 10.00,
  "gst": 0.50,
  "hst": 1.30,
  "pst": 0.50,
  "line_items": [
    {"description": "item name", "qty": 1, "unit_price": 10.00, "amount": 10.00}
  ]
}
Rules:
- Use null for amounts not present
- line_items can be empty []
- For Chinese receipts extract both Chinese and English text where present
- date format: YYYY-MM-DD"""


def extract_receipt_data_llm(image_path: str | Path) -> dict:
    """
    Use a local Ollama vision LLM to extract structured data from a receipt image.
    Returns dict matching ReceiptData fields on success, {} on failure.
    """
    path = Path(image_path)
    if not path.exists():
        return {}

    model = ollama_vision_model()
    if not model:
        return {}

    try:
        import ollama
        img_b64 = _encode_image(path)
        response = ollama.chat(
            model=model,
            messages=[{
                "role": "user",
                "content": _RECEIPT_PROMPT,
                "images": [img_b64],
            }],
            options={"temperature": 0},
        )
        raw = response.message.content.strip()
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            return {}
        return json.loads(json_match.group(0))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Install guidance
# ---------------------------------------------------------------------------

INSTALL_INSTRUCTIONS = """**To enable LLM-based extraction (much better for photos):**

1. Install Ollama: https://ollama.com/download  (Windows installer)
2. After install, open a terminal and run:
   ```
   ollama pull llama3.2-vision
   ```
   (~7 GB download; runs entirely on your machine, no internet needed after download)

3. Restart this Streamlit app — the LLM option will appear automatically.

**Lighter alternative** (less accurate, ~4 GB):
```
ollama pull llava
```"""
