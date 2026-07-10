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
import os
import re
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ollama availability check
# ---------------------------------------------------------------------------

# Preference order for auto-selection. llama3.2-vision is listed last: Ollama
# >=0.30.0 dropped support for its 'mllama' architecture (upstream llama.cpp never
# implemented it — https://github.com/ollama/ollama/issues/16490), so on current
# Ollama builds it reliably fails to load. Kept in the list (rather than removed)
# in case a future Ollama/llama.cpp release restores support.
_PREFERRED_MODELS = ["qwen2.5vl", "minicpm-v", "granite3.2-vision", "moondream",
                     "gemma3", "llava", "llava:13b", "llava:7b", "llama3.2-vision"]

# Last runtime error from an actual Ollama inference call (not just availability).
# A model can be *listed* yet fail to *load* (e.g. an mllama-architecture model on
# an Ollama build that doesn't support it) — we capture that here so the UI can
# explain why the LLM method produced nothing instead of silently skipping.
_LAST_OLLAMA_ERROR: Optional[str] = None

# Models that failed to *load* this session (e.g. unsupported architecture on the
# installed Ollama build). Kept separate from "not installed" so we can skip a
# known-broken model and fall through to the next installed one, instead of
# giving up on Ollama entirely just because the first-preference model is broken.
_OLLAMA_BROKEN_MODELS: set[str] = set()

# Ollama's chat API defaults num_ctx to 4096 regardless of a model's max context
# length. A single statement/receipt photo alone can burn ~4200+ tokens just
# encoding the image, so the default silently truncates/rejects real requests.
# 8192 gives headroom for image tokens + prompt + a multi-transaction JSON reply.
_OLLAMA_NUM_CTX = 8192


def ollama_last_error() -> Optional[str]:
    """Return a human-readable reason the last Ollama call failed, or None."""
    return _LAST_OLLAMA_ERROR


def _note_ollama_failure(exc: Exception, model: Optional[str] = None) -> None:
    """Record an inference failure. If the model can't be loaded at all, mark
    just that model as broken (so callers fall through to the next installed
    model) rather than disabling Ollama support for the whole session."""
    global _LAST_OLLAMA_ERROR
    msg = str(exc)
    lowered = msg.lower()
    if "unknown model architecture" in lowered or "error loading model" in lowered:
        _LAST_OLLAMA_ERROR = (
            f"Ollama model `{model}` can't be loaded by the installed Ollama runtime "
            "(architecture unsupported). Update Ollama, or use a different installed "
            "vision model. Details: " + msg.splitlines()[0]
        )
        if model:
            _OLLAMA_BROKEN_MODELS.add(model)
    else:
        _LAST_OLLAMA_ERROR = msg.splitlines()[0] if msg else "Unknown Ollama error"


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


def is_ollama_server_running() -> bool:
    """Quickly check if the Ollama local server port 11434 is listening.

    Uses a 0.6s timeout: long enough that a live-but-busy server (e.g. mid-way
    through loading a multi-GB vision model) still answers, short enough that a
    genuinely absent server doesn't stall the UI.
    """
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=0.6):
            return True
    except Exception:
        return False


def _ranked_vision_models(skip_broken: bool = True) -> list[str]:
    """All installed vision models, in preference order. Excludes models already
    known to fail to load this session unless skip_broken=False (used by the
    Settings/diagnostics page, which wants to show broken models too)."""
    try:
        import ollama
        models = [m.model for m in ollama.list().models]
    except Exception:
        return []
    ranked = []
    for pref in _PREFERRED_MODELS:
        for m in models:
            if pref in m and m not in ranked:
                if skip_broken and m in _OLLAMA_BROKEN_MODELS:
                    continue
                ranked.append(m)
    return ranked


def is_ollama_available() -> bool:
    """Return True if the Ollama server is running and has a usable vision model
    (installed AND not already known to fail to load this session)."""
    from core.database import get_setting
    if get_setting("ollama_enabled", "true") == "false":
        return False

    if not is_ollama_server_running():
        return False

    return bool(_ranked_vision_models())


def ollama_vision_model() -> Optional[str]:
    """Return the name of the first usable (installed, not known-broken) vision
    model, or None."""
    if not is_ollama_server_running():
        return None
    ranked = _ranked_vision_models()
    return ranked[0] if ranked else None


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
) -> list[dict] | None:
    """
    Use a local Ollama vision LLM to extract transactions from a statement image.
    Tries installed vision models in preference order, skipping any that fail to
    *load* (e.g. an unsupported architecture) so one broken model doesn't take
    down the whole method. Returns list of {date, vendor, amount_gross} dicts on
    success, None if every candidate model failed or none is installed.
    """
    path = Path(image_path)
    if not path.exists():
        logger.error(f"Ollama Statement extraction failed: file does not exist {image_path}")
        return None

    candidates = _ranked_vision_models()
    if not candidates:
        logger.warning("Ollama Statement extraction skipped: no vision model found or server offline")
        return None

    for model in candidates:
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
                options={"temperature": 0, "num_ctx": _OLLAMA_NUM_CTX},
            )
            raw = response.message.content.strip()
            # Extract JSON array from response (model sometimes adds prose)
            json_match = re.search(r"\[.*\]", raw, re.DOTALL)
            if not json_match:
                logger.error(f"Ollama Statement extraction failed: no JSON array found in response: {raw}")
                return None
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
                except Exception as e:
                    logger.warning(f"Ollama Statement row extraction skipped row {row}: {e}")
                    continue
            return results
        except Exception as e:
            _note_ollama_failure(e, model)
            logger.exception(f"Ollama Statement extraction failed for {image_path} with model {model}")
            if model in _OLLAMA_BROKEN_MODELS:
                continue  # try the next candidate model
            return None
    return None


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


def extract_receipt_data_llm(image_path: str | Path) -> dict | None:
    """
    Use a local Ollama vision LLM to extract structured data from a receipt image.
    Tries installed vision models in preference order, skipping any that fail to
    *load* so one broken model doesn't take down the whole method. Returns dict
    matching ReceiptData fields on success, None if every candidate failed.
    """
    path = Path(image_path)
    if not path.exists():
        logger.error(f"Ollama Receipt extraction failed: file does not exist {image_path}")
        return None

    candidates = _ranked_vision_models()
    if not candidates:
        logger.warning("Ollama Receipt extraction skipped: no vision model found or server offline")
        return None

    for model in candidates:
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
                options={"temperature": 0, "num_ctx": _OLLAMA_NUM_CTX},
            )
            raw = response.message.content.strip()
            json_match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not json_match:
                logger.error(f"Ollama Receipt extraction failed: no JSON object found in response: {raw}")
                return None
            return json.loads(json_match.group(0))
        except Exception as e:
            _note_ollama_failure(e, model)
            logger.exception(f"Ollama Receipt extraction failed for {image_path} with model {model}")
            if model in _OLLAMA_BROKEN_MODELS:
                continue  # try the next candidate model
            return None
    return None


# ---------------------------------------------------------------------------
# Cloud LLM (Anthropic) — opt-in, gated by Settings > cloud_llm_enabled
# ---------------------------------------------------------------------------

_ANTHROPIC_MODEL = "claude-sonnet-5"


def is_cloud_llm_available() -> bool:
    """Return True if the anthropic package is installed and an API key is set.

    Capability check only — does NOT check the cloud_llm_enabled app setting.
    Callers that need the user's explicit opt-in should also check that
    (see core.extraction.available_methods).
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def _cloud_vision_call(prompt: str, img_b64: str, max_tokens: int = 2048) -> str:
    """Send an image + prompt to the cloud vision LLM and return its text response."""
    import anthropic
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=_ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        temperature=0,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg", "data": img_b64,
                }},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    return "".join(block.text for block in response.content if block.type == "text").strip()


def extract_statement_transactions_cloud(
    image_path: str | Path,
    default_year: int,
) -> list[dict] | None:
    """Cloud-LLM equivalent of extract_statement_transactions(). None on failure."""
    path = Path(image_path)
    if not path.exists():
        logger.error(f"Claude Statement extraction failed: file does not exist {image_path}")
        return None
    if not is_cloud_llm_available():
        logger.warning("Claude Statement extraction skipped: ANTHROPIC_API_KEY missing or package not installed")
        return None

    try:
        img_b64 = _encode_image(path)
        raw = _cloud_vision_call(_STATEMENT_PROMPT, img_b64)
        json_match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not json_match:
            logger.error(f"Claude Statement extraction failed: no JSON array found in response: {raw}")
            return None
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
            except Exception as e:
                logger.warning(f"Claude Statement row extraction skipped row {row}: {e}")
                continue
        return results
    except Exception as e:
        logger.exception(f"Claude Statement extraction failed for {image_path}")
        return None


def extract_receipt_data_llm_cloud(image_path: str | Path) -> dict | None:
    """Cloud-LLM equivalent of extract_receipt_data_llm(). None on failure."""
    path = Path(image_path)
    if not path.exists():
        logger.error(f"Claude Receipt extraction failed: file does not exist {image_path}")
        return None
    if not is_cloud_llm_available():
        logger.warning("Claude Receipt extraction skipped: ANTHROPIC_API_KEY missing or package not installed")
        return None

    try:
        img_b64 = _encode_image(path)
        raw = _cloud_vision_call(_RECEIPT_PROMPT, img_b64)
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            logger.error(f"Claude Receipt extraction failed: no JSON object found in response: {raw}")
            return None
        return json.loads(json_match.group(0))
    except Exception as e:
        logger.exception(f"Claude Receipt extraction failed for {image_path}")
        return None


# ---------------------------------------------------------------------------
# Cloud LLM (Google Gemini) — opt-in
# ---------------------------------------------------------------------------

def is_gemini_available() -> bool:
    """Return True if the GEMINI_API_KEY environment variable is set."""
    return bool(os.environ.get("GEMINI_API_KEY"))


def _gemini_vision_call(prompt: str, img_b64: str) -> str:
    """Send an image + prompt to the Gemini API and return its JSON text response."""
    import requests
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return ""
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inlineData": {
                    "mimeType": "image/jpeg",
                    "data": img_b64
                }}
            ]
        }],
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        res_json = response.json()
        return res_json["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return ""


def extract_statement_transactions_gemini(
    image_path: str | Path,
    default_year: int,
) -> list[dict] | None:
    """Gemini equivalent of extract_statement_transactions(). None on failure."""
    path = Path(image_path)
    if not path.exists():
        logger.error(f"Gemini Statement extraction failed: file does not exist {image_path}")
        return None
    if not is_gemini_available():
        logger.warning("Gemini Statement extraction skipped: GEMINI_API_KEY environment variable missing")
        return None

    try:
        img_b64 = _encode_image(path)
        raw = _gemini_vision_call(_STATEMENT_PROMPT, img_b64)
        if not raw:
            logger.error("Gemini Statement extraction failed: empty response from API")
            return None
        
        rows = json.loads(raw)
        results = []
        for row in rows:
            try:
                import pandas as pd
                date_str = pd.to_datetime(str(row.get("date", "")), dayfirst=False).strftime("%Y-%m-%d")
                amount = float(row.get("amount", 0))
                vendor = str(row.get("vendor", "")).strip()
                if date_str and amount > 0 and vendor:
                    results.append({
                        "date": date_str,
                        "vendor": vendor,
                        "amount_gross": round(amount, 2),
                    })
            except Exception as e:
                logger.warning(f"Gemini Statement row extraction skipped row {row}: {e}")
                continue
        return results
    except Exception as e:
        logger.exception(f"Gemini Statement extraction failed for {image_path}")
        return None


def extract_receipt_data_llm_gemini(image_path: str | Path) -> dict | None:
    """Gemini equivalent of extract_receipt_data_llm(). None on failure."""
    path = Path(image_path)
    if not path.exists():
        logger.error(f"Gemini Receipt extraction failed: file does not exist {image_path}")
        return None
    if not is_gemini_available():
        logger.warning("Gemini Receipt extraction skipped: GEMINI_API_KEY environment variable missing")
        return None

    try:
        img_b64 = _encode_image(path)
        raw = _gemini_vision_call(_RECEIPT_PROMPT, img_b64)
        if not raw:
            logger.error("Gemini Receipt extraction failed: empty response from API")
            return None
        return json.loads(raw)
    except Exception as e:
        logger.exception(f"Gemini Receipt extraction failed for {image_path}")
        return None


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
