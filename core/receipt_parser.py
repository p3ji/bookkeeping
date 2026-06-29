"""
Structured data extraction from receipt OCR text.
Handles: English retail receipts, Chinese wholesale invoices, telecom bills,
         credit-card statement summary pages.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ReceiptData:
    vendor: str = ""
    date: str = ""            # YYYY-MM-DD or empty
    total: Optional[float] = None
    subtotal: Optional[float] = None
    tax_gst: Optional[float] = None
    tax_hst: Optional[float] = None
    tax_pst: Optional[float] = None
    tax_total: Optional[float] = None
    line_items: list[dict] = field(default_factory=list)
    raw_text: str = ""
    doc_type: str = "unknown"  # "retail", "invoice", "telecom", "statement"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DATE_PATTERNS = [
    re.compile(r"\b(\d{4}[-/]\d{2}[-/]\d{2})\b"),                # 2025-06-19
    re.compile(r"\b(\d{2}[-/]\d{2}[-/]\d{4})\b"),                # 19-06-2025
    re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b"),          # 6/19/25 or 06/19/2025
    re.compile(r"\b([A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})\b"),   # June 19, 2025 / Jun 19 2025
    re.compile(r"\b(\d{1,2}\s+[A-Za-z]{3,9}\.?\s+\d{4})\b"),     # 19 Jun 2025
    re.compile(r"\b(\d{2}-[A-Za-z]{3}-\d{4})\b"),                 # 22-May-2026
]

_MONEY_RE = re.compile(r"\$?\s*([\d,]+\.\d{2})")


def _find_date(text: str) -> str:
    import pandas as pd
    for pat in _DATE_PATTERNS:
        # Try ALL matches per pattern — first match may be unparseable (e.g. OCR artifact)
        for m in pat.finditer(text):
            raw = m.group(1)
            try:
                return pd.to_datetime(raw, dayfirst=False).strftime("%Y-%m-%d")
            except Exception:
                try:
                    return pd.to_datetime(raw, dayfirst=True).strftime("%Y-%m-%d")
                except Exception:
                    continue
    return ""


def _find_amount_on_line(line: str) -> Optional[float]:
    """Return last dollar amount found on a line, or None."""
    amounts = _MONEY_RE.findall(line)
    if amounts:
        try:
            return round(float(amounts[-1].replace(",", "")), 2)
        except ValueError:
            pass
    return None


def _clean_ocr_garbage(text: str) -> str:
    """Strip lone question marks and leading single-char / symbol noise words."""
    text = re.sub(r"\?", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    # Remove leading words of ≤2 chars (OCR noise) until the first real word
    parts = text.split()
    while parts and len(re.sub(r"[^A-Za-z]", "", parts[0])) <= 2:
        parts.pop(0)
    return " ".join(parts).strip()


# ---------------------------------------------------------------------------
# English retail receipt parser
# ---------------------------------------------------------------------------

def _parse_retail(text: str) -> ReceiptData:
    r = ReceiptData(raw_text=text, doc_type="retail")

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # Vendor: first line with ≥4 actual alpha chars that isn't an address/date
    for line in lines[:12]:
        alpha = len(re.findall(r"[A-Za-z]", line))
        if (alpha >= 4
                and not re.match(r"^\d", line)
                and not re.search(r"\d{5,}", line)):
            r.vendor = _clean_ocr_garbage(line)
            break

    r.date = _find_date(text)

    # Scan every line for total / tax keywords
    for line in lines:
        ll = line.lower()
        m = _MONEY_RE.search(line)
        if not m:
            continue
        val = round(float(m.group(1).replace(",", "")), 2)

        if re.search(r"\bsub.?total\b", ll):
            r.subtotal = val
        elif re.search(r"\bhst\b", ll):
            r.tax_hst = val
        elif re.search(r"\bgst\b", ll) and "hst" not in ll:
            r.tax_gst = val
        elif re.search(r"\bpst\b|\bqst\b", ll):
            r.tax_pst = val
        elif re.search(r"\btax\b", ll) and r.tax_hst is None and r.tax_gst is None:
            r.tax_hst = val
        elif re.search(r"\btotal\b", ll) and not re.search(r"\bsub\b|\bpre\b", ll):
            if r.total is None or val > r.total:
                r.total = val

    # Fallback 1: credit-card terminal "Amount" line (often more reliable than receipt total)
    if r.total is None:
        for line in lines:
            if re.search(r"\bamount\b", line, re.I):
                val = _find_amount_on_line(line)
                if val:
                    r.total = val
                    break

    # Fallback 2: POS thermal slips where "Amount" and the value are on separate lines
    # and OCR breaks the decimal: e.g. "mount =" then "圖 4000000004 1019 78 2" → $78.20
    if r.total is None:
        for i, line in enumerate(lines):
            if re.search(r"mount", line, re.I) and i + 1 < len(lines):
                digits = re.findall(r"\d+", lines[i + 1])
                # Need the last two groups where the second looks like cents (≤2 digits)
                if len(digits) >= 2 and len(digits[-1]) <= 2:
                    try:
                        candidate = float(f"{digits[-2]}.{digits[-1].ljust(2, '0')}")
                        if 0.01 < candidate < 50000:
                            r.total = round(candidate, 2)
                    except ValueError:
                        pass
                break

    # Derive tax_total
    parts = [x for x in [r.tax_gst, r.tax_hst, r.tax_pst] if x]
    if parts:
        r.tax_total = round(sum(parts), 2)

    return r


# ---------------------------------------------------------------------------
# Chinese / bilingual wholesale invoice parser
# ---------------------------------------------------------------------------

# Product code within the first 4 chars of the stripped line (non-greedy anchor).
# Hyphenated alternative tested FIRST so A034-14 beats A034:
#   1. Hyphenated: R83-10B, A034-14  (e.g. [A]+034-14)
#   2. Simple ≥5 chars: LAN209, S090S, KOU010, RBOL
# Non-greedy .{0,4}? tries 0 leading chars first.
_INVOICE_CODE_RE = re.compile(
    r"^.{0,4}?(?P<code>[A-Z][A-Z0-9]{1,4}-[A-Z0-9]{1,4}|[A-Z][A-Z0-9]{3,8})\b"
)
_INVOICE_AMT_RE  = re.compile(r"\$\s*(?P<amount>[\d,]+\.\d{2})")


def _parse_invoice(text: str) -> ReceiptData:
    r = ReceiptData(raw_text=text, doc_type="invoice")

    r.date = _find_date(text)

    # Vendor: first line with a word of ≥5 consecutive alpha chars, before the table header.
    # Must NOT contain a dollar amount (product description lines do; vendor header does not).
    for line in text.splitlines()[:15]:
        line = line.strip()
        # "alesman" catches OCR of "Salesman:" ("S" sometimes dropped)
        if re.search(r"ali?esman|ship\s*via|p\.?o\.?\s*no|unit.?price|invoice", line, re.I):
            break
        if _MONEY_RE.search(line):            # skip lines with prices (product rows)
            continue
        if _INVOICE_CODE_RE.match(line):   # skip lines that start with a product code
            continue
        if re.search(r"[A-Za-z]{5,}", line) and not re.match(r"^[\d\s$|]+$", line):
            candidate = _clean_ocr_garbage(line).strip(" |-")
            if len(re.sub(r"[^A-Za-z]", "", candidate)) >= 5:
                if not r.vendor:
                    r.vendor = candidate

    # CJK brand fallback: if no ASCII vendor found, count the most common 2-character
    # CJK bigram across product lines — Tesseract spaces out hanzi ("唐 龍") so we
    # work with adjacent-character pairs extracted from runs of CJK chars and spaces.
    if not r.vendor:
        from collections import Counter
        # Units, quantities and Chinese numerals — not brand material
        _CJK_SKIP = set("合打磅粒到有和的盒罐袋個等份包件瓶支條塊一二三四五六七八九十百千萬")
        # Collapse spaces between CJK chars to find runs, then form bigrams
        cjk_runs = re.findall(r"(?:[一-鿿]\s*){2,}", text)
        bigrams: list[str] = []
        for run in cjk_runs:
            chars = re.findall(r"[一-鿿]", run)
            for i in range(len(chars) - 1):
                bg = chars[i] + chars[i + 1]
                # Skip if both chars are the same (noise) or either is a unit/numeral
                if bg[0] == bg[1] or any(c in _CJK_SKIP for c in bg):
                    continue
                bigrams.append(bg)
        if bigrams:
            freq = Counter(bigrams)
            brand, count = freq.most_common(1)[0]
            if count >= 3:  # must appear in ≥3 product lines to be reliable
                r.vendor = brand

    # Extract line items — line-by-line: need product code AND a $ amount
    items = []
    seen_amounts: set[tuple] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        code_m = _INVOICE_CODE_RE.match(line)
        amt_m  = _INVOICE_AMT_RE.search(line)
        if not (code_m and amt_m):
            continue
        code   = code_m.group("code")
        amount = round(float(amt_m.group("amount").replace(",", "")), 2)
        # Deduplicate same (code, amount) — OCR sometimes duplicates lines
        key = (code, amount)
        if key in seen_amounts:
            continue
        seen_amounts.add(key)
        # Description: strip the code, OCR noise, and trailing amount portion
        desc_raw = line
        desc_raw = re.sub(r"\$[\d,]+\.\d{2}.*$", "", desc_raw)   # remove trailing amount
        desc_raw = re.sub(r"\b" + re.escape(code) + r"\b", "", desc_raw, 1)  # remove code
        desc_raw = re.sub(r"[\d.]+\s*(?:打|磅|KG|kg|合|盒|罐|袋|NPN|&|\||\*)\s*", " ", desc_raw)
        desc = re.sub(r"\s{2,}", " ", desc_raw).strip(" |?*&")
        items.append({
            "code": code,
            "description": desc,
            "amount": amount,
        })
    r.line_items = items

    # Totals at bottom
    for line in text.splitlines():
        ll = line.lower()
        amounts = _MONEY_RE.findall(line)
        if not amounts:
            continue
        val = round(float(amounts[-1].replace(",", "")), 2)

        if "gst" in ll and "hst" not in ll:
            r.tax_gst = val
        if "hst" in ll:
            r.tax_hst = val
        if "pst" in ll:
            r.tax_pst = val
        if re.search(r"invoice\s+total|pre.?tax\s+total|合共\s*金\s*額", ll):
            r.subtotal = val

    # Derive total from line items if not found explicitly
    if items:
        items_sum = round(sum(i["amount"] for i in items), 2)
        parts = [x for x in [r.tax_gst, r.tax_hst, r.tax_pst] if x]
        r.tax_total = round(sum(parts), 2) if parts else None
        r.total = round(items_sum + (r.tax_total or 0), 2)

    return r


# ---------------------------------------------------------------------------
# Telecom bill parser (Rogers / Bell / Telus / Freedom Mobile)
# ---------------------------------------------------------------------------

_TELECOM_VENDORS = re.compile(
    r"\b(rogers|bell|telus|freedom\s*mobile|virgin\s*plus|koodo|fido|shaw|videotron)\b",
    re.I,
)


def _parse_telecom(text: str) -> ReceiptData:
    r = ReceiptData(raw_text=text, doc_type="telecom")
    r.date = _find_date(text)

    m = _TELECOM_VENDORS.search(text)
    if m:
        r.vendor = m.group(1).title()

    for line in text.splitlines():
        ll = line.lower()
        vals = _MONEY_RE.findall(line)
        if not vals:
            continue
        val = round(float(vals[-1].replace(",", "")), 2)

        if re.search(r"amount\s+due|total\s+due|balance\s+due", ll):
            r.total = val
        elif re.search(r"\bgst\b", ll) and "hst" not in ll:
            r.tax_gst = val
        elif re.search(r"\bhst\b", ll):
            r.tax_hst = val
        elif re.search(r"\bpst\b", ll):
            r.tax_pst = val

    parts = [x for x in [r.tax_gst, r.tax_hst, r.tax_pst] if x]
    if parts:
        r.tax_total = round(sum(parts), 2)

    return r


# ---------------------------------------------------------------------------
# Statement summary page (cover/overview page — no individual transactions)
# ---------------------------------------------------------------------------

def _parse_statement_summary(text: str) -> ReceiptData:
    """Extract summary info from a statement cover page (not transaction lines)."""
    r = ReceiptData(raw_text=text, doc_type="statement")
    r.date = _find_date(text)

    for line in text.splitlines():
        ll = line.lower()
        vals = _MONEY_RE.findall(line)
        if not vals:
            continue
        val = round(float(vals[-1].replace(",", "")), 2)

        if re.search(r"payment", ll) and not re.search(r"minimum|due", ll):
            r.subtotal = val   # payment made
        elif re.search(r"minimum.{0,10}payment|amount.{0,5}due", ll):
            r.total = val      # minimum payment due
        elif re.search(r"total.{0,10}balance|balance.{0,10}owing", ll):
            r.total = r.total or val

    return r


# ---------------------------------------------------------------------------
# Auto-detect and dispatch
# ---------------------------------------------------------------------------

def _detect_doc_type(text: str) -> str:
    tl = text.lower()
    if _TELECOM_VENDORS.search(text):
        return "telecom"

    # Invoice heuristics: product codes + price table keywords
    invoice_score = 0
    if re.search(r"\b(?:salesman|aliesman)\b", tl):
        invoice_score += 2
    if re.search(r"ship\s*via|p\.?o\.?\s*no", tl):
        invoice_score += 2
    if re.search(r"unit.?price|invoice.?total|pre.?tax\s+total|合共\s*金\s*額", tl):
        invoice_score += 2
    # LAN/KOU/R8 product codes common in Chinese wholesale invoices
    if len(re.findall(r"\b(?:LAN|KOU|R8|A0)\d{2,4}\b", text)) >= 2:
        invoice_score += 3
    if invoice_score >= 2:
        return "invoice"

    if re.search(r"statement|transactions\s+from|cibc|mastercard|visa|amount\s+due\s+this", tl):
        return "statement"

    return "retail"


def extract_receipt_data(text: str) -> ReceiptData:
    """
    Auto-detect document type and extract structured receipt data.
    Returns a ReceiptData with vendor, date, total, tax breakdown, and line items.
    """
    if not text or not text.strip():
        return ReceiptData(raw_text=text)

    doc_type = _detect_doc_type(text)

    if doc_type == "invoice":
        return _parse_invoice(text)
    if doc_type == "telecom":
        return _parse_telecom(text)
    if doc_type == "statement":
        return _parse_statement_summary(text)

    return _parse_retail(text)


def extract_receipt_data_from_file(file_path: str | Path) -> ReceiptData:
    """OCR the file and extract structured receipt data."""
    from core.ocr import extract_text
    path = Path(file_path)
    text = extract_text(path)
    data = extract_receipt_data(text)
    if not data.vendor:
        data.vendor = path.stem  # fall back to filename
    return data
