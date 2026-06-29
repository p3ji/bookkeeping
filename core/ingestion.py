"""CSV ingestion: parse Costco Mastercard statements and load to DB."""
from __future__ import annotations

import hashlib
import io
import re
import uuid
from pathlib import Path

import pandas as pd

from config import DEFAULT_PROVINCE
from core.categorization import auto_categorize
from core.audit import check_audit_flags
from core.tax import estimate_tax, calculate_net
from core.database import upsert_transaction, get_indexed_paths


# ---------------------------------------------------------------------------
# Vendor normalisation
# ---------------------------------------------------------------------------

_NOISE = re.compile(
    r"\b(#\d+|store\s*\d+|branch\s*\d+|canada|inc\.?|ltd\.?|corp\.?|co\.?)\b",
    re.I,
)

def normalize_vendor(raw: str) -> str:
    name = raw.strip().title()
    name = _NOISE.sub("", name)
    name = re.sub(r"\s{2,}", " ", name).strip()
    return name or raw.strip().title()


# ---------------------------------------------------------------------------
# Transaction ID (deterministic, idempotent)
# ---------------------------------------------------------------------------

def make_tx_id(date: str, vendor: str, amount: float) -> str:
    key = f"{date}|{vendor.lower()}|{amount:.2f}"
    return hashlib.md5(key.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# CSV parsing — multi-format detector
# ---------------------------------------------------------------------------

_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%b %d, %Y", "%B %d, %Y",
                 "%d-%m-%Y", "%Y/%m/%d"]

def _parse_date(val: str) -> str | None:
    val = str(val).strip()
    for fmt in _DATE_FORMATS:
        try:
            return pd.to_datetime(val, format=fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    try:
        return pd.to_datetime(val).strftime("%Y-%m-%d")
    except Exception:
        return None


def _parse_amount(val, keep_sign: bool = False) -> float | None:
    if pd.isna(val):
        return None
    s = str(val).strip()
    if not s:
        return None
    s = re.sub(r"[$€£¥\s]", "", s)
    if s.startswith("(") and s.endswith(")"):   # (45.67) → negative
        s = "-" + s[1:-1]
    s = s.replace(",", "")
    if s.endswith("-"):                          # 45.67- → negative
        s = "-" + s[:-1]
    try:
        v = float(s)
        return v if keep_sign else abs(v)
    except ValueError:
        return None


def _detect_columns(df: pd.DataFrame) -> dict[str, str] | None:
    """
    Map raw CSV columns to standard fields: date, vendor, amount.
    Handles Capital One / Costco, TD, Scotiabank, generic formats.
    """
    cols_lower = {c.lower().strip(): c for c in df.columns}

    def find(candidates):
        for c in candidates:
            if c in cols_lower:
                return cols_lower[c]
        return None

    date_col = find([
        "transaction date", "date", "trans date", "posting date",
        "transaction_date", "trans_date",
    ])
    vendor_col = find([
        "description", "merchant", "vendor", "payee", "details",
        "transaction description", "memo",
    ])

    # Amount: prefer "debit" (charges) over combined "amount"
    debit_col  = find(["debit", "withdrawals", "charges", "debit amount"])
    credit_col = find(["credit", "deposits", "payments", "credit amount"])
    amount_col = find(["amount", "billing amount", "transaction amount"])

    if not (date_col and vendor_col):
        return None

    return {
        "date": date_col,
        "vendor": vendor_col,
        "debit": debit_col,
        "credit": credit_col,
        "amount": amount_col,
    }


def parse_csv(source: str | Path | bytes | io.BytesIO) -> pd.DataFrame:
    """
    Parse a credit card CSV statement.
    Returns a normalised DataFrame with columns:
        date, vendor, amount_gross, raw_date, raw_vendor
    Raises ValueError on unrecognised format.
    """
    if isinstance(source, (str, Path)):
        raw = pd.read_csv(source, dtype=str, skip_blank_lines=True)
    else:
        raw = pd.read_csv(source if hasattr(source, "read") else io.BytesIO(source),
                          dtype=str, skip_blank_lines=True)

    raw.columns = [c.strip().strip('"') for c in raw.columns]
    mapping = _detect_columns(raw)
    if mapping is None:
        raise ValueError(
            "Unrecognised CSV format. Expected columns like 'Date', 'Description', "
            "'Debit'/'Credit' or 'Amount'."
        )

    rows = []
    for _, r in raw.iterrows():
        date_str = _parse_date(r.get(mapping["date"], ""))
        if not date_str:
            continue

        vendor_raw = str(r.get(mapping["vendor"], "")).strip()
        if not vendor_raw:
            continue

        # Determine amount
        amount = None
        if mapping["debit"]:
            # Separate Debit column: always positive charges
            amount = _parse_amount(r.get(mapping["debit"]))
            # If debit is empty, check if credit has a value → payment row, skip
            if (amount is None or amount == 0) and mapping["credit"]:
                credit_val = _parse_amount(r.get(mapping["credit"]))
                if credit_val and credit_val > 0:
                    continue  # payment row
        elif mapping["amount"]:
            # Single Amount column: preserve sign to detect payments
            signed = _parse_amount(r.get(mapping["amount"]), keep_sign=True)
            if signed is None:
                continue
            if signed <= 0:
                continue  # negative or zero = payment/refund, skip
            amount = signed

        if amount is None or amount <= 0:
            continue

        rows.append({
            "date": date_str,
            "vendor": normalize_vendor(vendor_raw),
            "amount_gross": round(amount, 2),
            "raw_vendor": vendor_raw,
        })

    if not rows:
        raise ValueError("No debit transactions found in CSV.")

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Import pipeline
# ---------------------------------------------------------------------------

def import_csv(
    source: str | Path | bytes | io.BytesIO,
    filename: str = "upload",
    province: str = DEFAULT_PROVINCE,
) -> dict:
    """
    Parse, categorise, flag, and persist CSV transactions.
    Returns a summary dict.
    """
    df = parse_csv(source)

    new_count = 0
    skipped = 0

    for _, row in df.iterrows():
        tx_id = make_tx_id(row["date"], row["vendor"], row["amount_gross"])

        cra_line, cra_desc = auto_categorize(row["vendor"])
        gst_hst = estimate_tax(row["amount_gross"], province)
        amount_net = calculate_net(row["amount_gross"], gst_hst)
        flags = check_audit_flags(
            vendor=row["vendor"],
            amount_gross=row["amount_gross"],
            cra_line=cra_line,
            business_percentage=1.0,
            raw_text="",
        )

        tx = {
            "transaction_id": tx_id,
            "date": row["date"],
            "vendor": row["vendor"],
            "amount_gross": row["amount_gross"],
            "amount_net": amount_net,
            "gst_hst_amount": gst_hst,
            "is_business": True,  # default; user triage decides
            "business_percentage": 1.0,
            "cra_line": cra_line,
            "cra_description": cra_desc,
            "receipt_path": None,
            "raw_receipt_text": None,
            "audit_flags": flags,
            "verified_status": False,
            "notes": "",
            "import_source": filename,
        }
        upsert_transaction(tx)
        new_count += 1

    return {
        "total_rows": len(df),
        "new_records": new_count,
        "skipped": skipped,
        "filename": filename,
    }


# ---------------------------------------------------------------------------
# Statement parsing (PDF / image — non-CSV formats)
# ---------------------------------------------------------------------------

_SKIP_KEYWORDS: set[str] = {
    "total", "balance", "minimum", "payment due", "interest charge",
    "credit limit", "available credit", "statement", "account number",
    "previous balance", "new balance", "amount due", "opening balance",
    "closing balance", "annual fee", "thank you", "rewards", "page",
    "billing period", "customer service", "tel:", "www.", ".com",
    "void", "subtotal", "fee", "apr", "annual percentage",
}

# Date at line start, amount at line end
_LINE_TX_RE = re.compile(
    r"""
    ^
    (?:                                              # date group (various formats)
        (\d{4}-\d{2}-\d{2})                         #  YYYY-MM-DD
        |(\d{1,2}/\d{1,2}(?:/\d{2,4})?)             #  MM/DD or MM/DD/YYYY
        |([A-Za-z]{3}\.?\s+\d{1,2}(?:[,\s]+\d{4})?) #  Jan 15 or Jan 15, 2025
    )
    \s+
    (.+?)                                            # vendor (non-greedy)
    \s+
    ([\d,]+\.\d{2})                                 # amount
    \s*(?:CR)?\s*
    $
    """,
    re.VERBOSE,
)


def _line_to_tx(line: str, default_year: int) -> dict | None:
    """Try to parse one statement text line into a transaction dict."""
    line = line.strip()
    if len(line) < 8:
        return None

    lower = line.lower()
    if any(kw in lower for kw in _SKIP_KEYWORDS):
        return None

    # Amount must be at end
    amt_m = re.search(r"([\d,]+\.\d{2})\s*(?:CR)?\s*$", line)
    if not amt_m:
        return None

    amount = _parse_amount(amt_m.group(1))
    if not amount or amount <= 0:
        return None

    # Skip credits/refunds (CR suffix)
    if line.rstrip().upper().endswith("CR"):
        return None

    prefix = line[: amt_m.start()].strip()
    if not prefix:
        return None

    # Date at start of prefix
    date_pats = [
        re.compile(r"^\d{4}-\d{2}-\d{2}"),
        re.compile(r"^\d{1,2}/\d{1,2}/\d{4}"),
        re.compile(r"^\d{1,2}/\d{1,2}/\d{2}"),
        re.compile(r"^\d{1,2}/\d{1,2}"),
        re.compile(r"^[A-Za-z]{3}\.?\s+\d{1,2}[,\s]+\d{4}"),
        re.compile(r"^[A-Za-z]{3}\.?\s+\d{1,2}"),
    ]
    date_str = None
    vendor_str = prefix

    for pat in date_pats:
        m = pat.match(prefix)
        if m:
            raw_date = m.group(0)
            # Append default year if only MM/DD or Mon DD found
            if re.match(r"^\d{1,2}/\d{1,2}$", raw_date):
                raw_date = f"{raw_date}/{default_year}"
            elif re.match(r"^[A-Za-z]{3}\.?\s+\d{1,2}$", raw_date):
                raw_date = f"{raw_date} {default_year}"
            date_str = _parse_date(raw_date)
            # Vendor is whatever follows the date; might start with a second date
            remainder = prefix[m.end():].strip()
            # Drop a trailing second date (Posted vs Transaction date columns)
            sec = re.match(r"^\d{1,2}/\d{1,2}(?:/\d{2,4})?\s*", remainder)
            if not sec:
                sec = re.match(r"^[A-Za-z]{3}\.?\s+\d{1,2}(?:[,\s]+\d{4})?\s*", remainder)
            vendor_str = remainder[sec.end():].strip() if sec else remainder
            break

    if not date_str or not vendor_str or len(vendor_str) < 2:
        return None

    # Drop reference/confirmation numbers (long digit strings at start of vendor)
    vendor_str = re.sub(r"^\d{6,}\s*", "", vendor_str).strip()
    if not vendor_str:
        return None

    return {
        "date": date_str,
        "vendor": normalize_vendor(vendor_str),
        "amount_gross": round(amount, 2),
    }


def _extract_via_tables(pdf_path: Path, default_year: int) -> list[dict]:
    """Use pdfplumber table extractor — works well on digital PDFs with explicit table borders."""
    try:
        import pdfplumber
    except ImportError:
        return []
    results = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables():
                    for row in table:
                        if not row or len(row) < 2:
                            continue
                        cells = [str(c or "").strip() for c in row]
                        date_str = None
                        amount = None
                        vendor_parts = []

                        for i, cell in enumerate(cells):
                            if not date_str:
                                d = _parse_date(cell)
                                if d:
                                    date_str = d
                                    continue
                            if re.match(r"^[\d,]+\.\d{2}$", cell):
                                amount = _parse_amount(cell)
                            else:
                                vendor_parts.append(cell)

                        vendor = normalize_vendor(" ".join(v for v in vendor_parts if v))
                        lower_v = vendor.lower()
                        if (date_str and amount and amount > 0 and vendor
                                and not any(kw in lower_v for kw in _SKIP_KEYWORDS)):
                            results.append({
                                "date": date_str,
                                "vendor": vendor,
                                "amount_gross": round(amount, 2),
                            })
    except Exception:
        pass
    return results


def _extract_via_words(pdf_path: Path, default_year: int) -> tuple[list[dict], str]:
    """
    Reconstruct lines from word bounding-boxes — handles multi-column PDFs like
    Capital One / Costco Mastercard where text is positioned but not in HTML tables.
    Returns (transactions, reconstructed_text) so callers can show debug info.
    """
    try:
        import pdfplumber
    except ImportError:
        return [], ""

    results: list[dict] = []
    all_lines: list[str] = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                words = page.extract_words(x_tolerance=3, y_tolerance=3,
                                           keep_blank_chars=False)
                if not words:
                    continue

                # Group words by y-coordinate bucket (3 pt tolerance → same line)
                lines_by_y: dict[int, list] = {}
                for w in words:
                    y_key = round(float(w["top"]) / 3) * 3
                    lines_by_y.setdefault(y_key, []).append(w)

                for y_key in sorted(lines_by_y):
                    sorted_words = sorted(lines_by_y[y_key], key=lambda w: float(w["x0"]))
                    line_text = " ".join(w["text"] for w in sorted_words)
                    all_lines.append(line_text)
                    tx = _line_to_tx(line_text, default_year)
                    if tx:
                        results.append(tx)
    except Exception:
        pass

    return results, "\n".join(all_lines)


def parse_statement_file(
    source,
    filename: str = "statement",
    default_year: int | None = None,
) -> pd.DataFrame:
    """
    Extract transactions from a credit card statement PDF or image.
    Works on: digital PDFs (text-based), scanned PDFs, JPEG/PNG photos.
    Returns the same normalised DataFrame as parse_csv().
    """
    from datetime import date as _date
    from core.ocr import extract_text, render_pdf_preview

    if default_year is None:
        default_year = _date.today().year

    # Save bytes to a temp file so OCR tools can open it
    import tempfile, os
    if hasattr(source, "read"):
        data = source.read()
        suffix = Path(filename).suffix or ".pdf"
    else:
        data = source
        suffix = Path(filename).suffix or ".pdf"

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)

    rows: list[dict] = []
    debug_text = ""
    try:
        # 1. Try pdfplumber table extraction (PDFs with explicit table borders)
        if suffix.lower() == ".pdf":
            rows = _extract_via_tables(tmp_path, default_year)

        # 2. Try word-position extraction (handles Capital One / Costco MC multi-column layout)
        if not rows and suffix.lower() == ".pdf":
            rows, debug_text = _extract_via_words(tmp_path, default_year)

        # 3. Fall back to full OCR text parsing (scanned PDFs and images)
        if not rows:
            text = extract_text(tmp_path)
            debug_text = text
            if not text.strip():
                raise ValueError(
                    "No text could be extracted. Ensure Tesseract is installed "
                    "for scanned statements."
                )
            for line in text.splitlines():
                tx = _line_to_tx(line, default_year)
                if tx:
                    rows.append(tx)
    finally:
        os.unlink(tmp_path)

    if not rows:
        raise ValueError(
            "No transactions found in statement.\n\n"
            "Raw extracted text (first 3000 chars):\n"
            + (debug_text[:3000] if debug_text else "(nothing extracted)")
        )

    df = pd.DataFrame(rows).drop_duplicates(subset=["date", "vendor", "amount_gross"])
    return df


def import_statement(
    source,
    filename: str = "statement",
    province: str = DEFAULT_PROVINCE,
    default_year: int | None = None,
) -> dict:
    """Parse a statement PDF/image and persist transactions. Same return shape as import_csv."""
    df = parse_statement_file(source, filename, default_year)

    new_count = 0
    for _, row in df.iterrows():
        tx_id = make_tx_id(row["date"], row["vendor"], row["amount_gross"])
        cra_line, cra_desc = auto_categorize(row["vendor"])
        gst_hst    = estimate_tax(row["amount_gross"], province)
        amount_net = calculate_net(row["amount_gross"], gst_hst)
        flags      = check_audit_flags(
            vendor=row["vendor"], amount_gross=row["amount_gross"],
            cra_line=cra_line, business_percentage=1.0, raw_text="",
        )
        upsert_transaction({
            "transaction_id": tx_id,
            "date": row["date"],
            "vendor": row["vendor"],
            "amount_gross": row["amount_gross"],
            "amount_net": amount_net,
            "gst_hst_amount": gst_hst,
            "is_business": True,
            "business_percentage": 1.0,
            "cra_line": cra_line,
            "cra_description": cra_desc,
            "receipt_path": None,
            "raw_receipt_text": None,
            "audit_flags": flags,
            "verified_status": False,
            "notes": "",
            "import_source": filename,
        })
        new_count += 1

    return {
        "total_rows": len(df),
        "new_records": new_count,
        "skipped": 0,
        "filename": filename,
        "preview": df.head(10).to_dict("records"),
    }
