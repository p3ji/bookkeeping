"""
Playwright-based extraction for web-rendered documents.

Use cases:
- Online bank statements (CIBC, RBC, TD online banking — JavaScript-rendered)
- Telecom bill pages (Freedom Mobile, Rogers, Bell, Telus My Account portals)
- Any URL where the statement/bill is rendered in-browser rather than as a static file

For local files (PDF, JPEG, PNG) use core/ocr.py instead.
For Ollama Vision LLM use core/llm_extractor.py instead.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def is_playwright_available() -> bool:
    """Return True if Playwright and at least one browser are installed."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            # Try to get the chromium executable path without launching
            browser = p.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def extract_text_from_url(url: str, timeout_ms: int = 15000) -> str:
    """
    Load a URL (or local file:// path) with Playwright and return visible text.
    Waits for network idle so JavaScript-rendered content is included.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            # inner_text() gives human-readable text without HTML tags,
            # preserving whitespace/newline structure from the rendered layout.
            text = page.inner_text("body")
        finally:
            browser.close()
    return text


def extract_text_from_html_file(file_path: str | Path) -> str:
    """Load a local HTML file via Playwright (file:// URL)."""
    path = Path(file_path).resolve()
    url = path.as_uri()          # e.g. file:///C:/Users/.../bill.html
    return extract_text_from_url(url)


# ---------------------------------------------------------------------------
# High-level: extract + parse in one call
# ---------------------------------------------------------------------------

def extract_from_url(
    url: str,
    default_year: Optional[int] = None,
) -> dict:
    """
    Load a URL with Playwright, then route extracted text through the
    appropriate parser (telecom / statement / receipt / invoice).

    Returns a dict with:
      - doc_type: str
      - raw_text: str
      - receipt_data: ReceiptData (for receipts/telecom/invoices)
      - transactions: list[dict] (for bank statements — same shape as parse_statement_file)
    """
    from datetime import date as _date
    from core.receipt_parser import extract_receipt_data, _detect_doc_type

    if default_year is None:
        default_year = _date.today().year

    text = extract_text_from_url(url)
    doc_type = _detect_doc_type(text)

    result: dict = {"doc_type": doc_type, "raw_text": text, "url": url}

    if doc_type == "statement":
        # Try line-by-line transaction extraction
        from core.ingestion import _line_to_tx
        transactions = []
        for line in text.splitlines():
            tx = _line_to_tx(line, default_year)
            if tx:
                transactions.append(tx)
        result["transactions"] = transactions
        result["receipt_data"] = None
    else:
        # receipt / telecom / invoice
        receipt_data = extract_receipt_data(text)
        result["receipt_data"] = receipt_data
        result["transactions"] = []

    return result


def extract_from_html_file(
    file_path: str | Path,
    default_year: Optional[int] = None,
) -> dict:
    """Same as extract_from_url but takes a local HTML file path."""
    path = Path(file_path).resolve()
    url = path.as_uri()
    result = extract_from_url(url, default_year=default_year)
    result["url"] = str(file_path)   # show the original path, not the file:// URI
    return result
