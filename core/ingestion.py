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
    "information about", "your payment", "amount due",
}

# CIBC Costco MC spend category labels that sit between vendor and amount
_CIBC_CATEGORIES: tuple[str, ...] = (
    "Restaurants",
    "Retail and Grocery",
    "Personal and Household Expenses",
    "Professional and Financial Services",
    "Foreign Currency Transactions",
    "Transportation",
    "Health and Education",
    "Home and Office Improvement",
    "Entertainment",
    "Education",
    "Gas and Automobile",
    "Travel",
    "Insurance",
    "Utilities",
)
# Build a regex to strip category labels (case-insensitive) from end of vendor string
_CATEGORY_STRIP_RE = re.compile(
    r"\s+(" + "|".join(re.escape(c) for c in _CIBC_CATEGORIES) + r")\s*$",
    re.IGNORECASE,
)


def _line_to_tx(line: str, default_year: int) -> dict | None:
    """Try to parse one statement text line into a transaction dict."""
    line = line.strip()
    if len(line) < 8:
        return None

    lower = line.lower()
    if any(kw in lower for kw in _SKIP_KEYWORDS):
        return None

    # Strip trailing OCR punctuation that often appears after amounts (e.g. "59.3.")
    line = re.sub(r"([0-9])[.\s]+$", r"\1", line).strip()

    # Amount must be at end — allow 1 or 2 decimal digits (OCR sometimes drops trailing zero)
    amt_m = re.search(r"([\d,]+\.\d{1,2})\s*(?:CR)?\s*$", line)
    if not amt_m:
        return None

    # Skip credits/refunds (CR suffix)
    if line.rstrip().upper().endswith("CR"):
        return None

    amount = _parse_amount(amt_m.group(1))
    if not amount or amount <= 0:
        return None

    prefix = line[: amt_m.start()].strip()
    # Strip leading OCR garbage (|, @, ., 一, etc.) before the date
    prefix = re.sub(r"^[^A-Za-z0-9]+", "", prefix).strip()
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
        re.compile(r"^[A-Za-z]{3}\d{1,2}"),    # compact: "Jun19" (no space — OCR artifact)
    ]
    date_str = None
    vendor_str = prefix

    for pat in date_pats:
        m = pat.match(prefix)
        if m:
            raw_date = m.group(0)
            if re.match(r"^\d{1,2}/\d{1,2}$", raw_date):
                raw_date = f"{raw_date}/{default_year}"
            elif re.match(r"^[A-Za-z]{3}\.?\s+\d{1,2}$", raw_date):
                raw_date = f"{raw_date} {default_year}"
            raw_date_clean = raw_date
            # Normalise compact format "Jun19" → "Jun 19"
            compact_m = re.match(r"^([A-Za-z]{3})(\d{1,2})$", raw_date.strip())
            if compact_m:
                raw_date_clean = f"{compact_m.group(1)} {compact_m.group(2)}"
            if re.match(r"^\d{1,2}/\d{1,2}$", raw_date_clean):
                raw_date_clean = f"{raw_date_clean}/{default_year}"
            elif re.match(r"^[A-Za-z]{3}\.?\s+\d{1,2}$", raw_date_clean):
                raw_date_clean = f"{raw_date_clean} {default_year}"
            date_str = _parse_date(raw_date_clean)
            remainder = prefix[m.end():].strip()
            # Drop a trailing second date (Posted vs Transaction date columns)
            sec = re.match(r"^\d{1,2}/\d{1,2}(?:/\d{2,4})?\s*", remainder)
            if not sec:
                sec = re.match(r"^[A-Za-z]{3}\.?\s*\d{1,2}(?:[,\s]+\d{4})?\s*", remainder)
            if not sec:
                sec = re.match(r"^[A-Za-z]{3}\d{1,2}\s*", remainder)  # compact second date
            vendor_str = remainder[sec.end():].strip() if sec else remainder
            break

    if not date_str or not vendor_str or len(vendor_str) < 2:
        return None

    # Drop long reference/confirmation numbers at start of vendor
    vendor_str = re.sub(r"^\d{6,}\s*", "", vendor_str).strip()

    # Strip CIBC spend category labels from end of vendor string
    vendor_str = _CATEGORY_STRIP_RE.sub("", vendor_str).strip()

    # Strip trailing province codes (ON, BC, QC, AB, etc.) and city left-overs
    vendor_str = re.sub(
        r"\s+\b(ON|BC|AB|QC|MB|SK|NS|NB|PE|NL|NT|NU|YT)\b\s*$", "", vendor_str
    ).strip()

    if not vendor_str or len(vendor_str) < 2:
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


def _ocr_image_to_lines(img_path: Path, lang: str = "eng") -> list[str]:
    """
    Open, preprocess and OCR an image; return Tesseract-native text lines
    grouped by (block, paragraph, line) for multi-column statement fidelity.
    Uses English-only by default for statements (avoids Chinese model interference).
    """
    import pytesseract as _tess
    import pandas as pd
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    from config import TESSERACT_PATH
    import os

    if os.path.exists(TESSERACT_PATH):
        _tess.pytesseract.tesseract_cmd = TESSERACT_PATH

    img = Image.open(str(img_path))
    img = ImageOps.exif_transpose(img)
    img = img.convert("L")
    img = ImageEnhance.Contrast(img).enhance(1.8)
    img = img.filter(ImageFilter.SHARPEN)

    raw = _tess.image_to_data(
        img, lang=lang, config="--psm 6 --oem 1",
        output_type=_tess.Output.DATAFRAME,
    )
    raw["conf"] = pd.to_numeric(raw["conf"], errors="coerce")
    raw = raw[(raw["conf"] > 20) & (raw["text"].fillna("").str.strip() != "")]

    lines: list[str] = []
    for _key, grp in raw.groupby(["block_num", "par_num", "line_num"]):
        text = " ".join(grp.sort_values("left")["text"].fillna("").astype(str).values).strip()
        if text:
            lines.append(text)
    return lines


def _extract_via_image_words(img_path: Path, default_year: int) -> tuple[list[dict], str]:
    """
    Extract transactions from an IMAGE statement (JPG/PNG).
    Uses Tesseract's native block/paragraph/line grouping (more robust than y-bucketing
    for multi-column layouts like CIBC Costco MC).
    Returns (transactions, reconstructed_text).
    """
    try:
        lines = _ocr_image_to_lines(img_path)
    except Exception:
        return [], ""

    debug_text = "\n".join(lines)

    # First pass: direct parse
    results = []
    for line in lines:
        tx = _line_to_tx(line, default_year)
        if tx:
            results.append(tx)

    # Second pass: merge fragmented lines (date on one line, amount on the next)
    if len(results) < 3:
        results = _merge_fragmented_lines(lines, default_year)

    # Third pass: look-back date assignment for vendor+amount lines that have no date.
    # These appear when the date column falls in a different Tesseract block (tilted photos).
    DATELESS_AMT_RE = re.compile(r"([\d,]+\.\d{1,2})\s*$")
    NO_DATE_RE = re.compile(r"^[A-Za-z]{3,}")  # starts with a vendor name (≥3 alpha), not a date
    seen_amounts = {round(r["amount_gross"], 2) for r in results}
    last_date = None
    for line in lines:
        if not line.strip():
            continue
        # Track the last date seen in ANY line
        for pat in [
            re.compile(r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s*\d{1,2}", re.I),
        ]:
            dm = pat.search(line)
            if dm:
                rd = dm.group(0).strip()
                # Add year if missing
                if not re.search(r"\d{4}", rd):
                    rd = f"{rd} {default_year}"
                candidate_date = _parse_date(rd)
                if candidate_date:
                    last_date = candidate_date
                    break

        if not last_date:
            continue

        # Strip trailing punctuation before amount check (same as _line_to_tx does)
        stripped_line = re.sub(r"([0-9])[.\s]+$", r"\1", line).strip()
        # Check if it's a dateless vendor+amount line
        amt_m = DATELESS_AMT_RE.search(stripped_line)
        if not amt_m or not NO_DATE_RE.match(re.sub(r"^[^A-Za-z0-9]+", "", stripped_line)):
            continue
        amount = _parse_amount(amt_m.group(1))
        # Skip amounts that are clearly credit-limit / balance figures (>= $10,000),
        # not individual transactions.  Legitimate large single charges are rare in
        # OCR-from-photo paths and would typically come from a digital PDF instead.
        if not amount or amount <= 0 or amount >= 10000 or round(amount, 2) in seen_amounts:
            continue

        # Try parsing with injected date (use stripped_line so trailing "." doesn't block)
        synthetic = f"{last_date} {stripped_line}"
        tx = _line_to_tx(synthetic, default_year)
        if tx:
            results.append(tx)
            seen_amounts.add(round(tx["amount_gross"], 2))

    return results, debug_text


def _merge_fragmented_lines(lines: list[str], default_year: int) -> list[dict]:
    """
    Handle OCR output where a transaction's date/vendor and amount are split across
    adjacent lines (common for tilted photos of multi-column statements).
    For each line with a recognisable date but no amount, look ahead up to 4 lines.
    """
    MONTH_RE = re.compile(
        r"^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+\d{1,2}", re.I
    )
    AMT_RE = re.compile(r"[\d,]+\.\d{1,2}\s*(?:CR)?\s*$")

    results: list[dict] = []
    used: set[int] = set()
    i = 0
    while i < len(lines):
        if i in used:
            i += 1
            continue

        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # Try direct parse first
        tx = _line_to_tx(line, default_year)
        if tx:
            results.append(tx)
            used.add(i)
            i += 1
            continue

        # If line has a date but no amount, try merging with subsequent lines
        if MONTH_RE.match(line) and not AMT_RE.search(line):
            for lookahead in range(1, 5):
                j = i + lookahead
                if j >= len(lines) or j in used:
                    continue
                candidate = (line + " " + lines[j].strip()).strip()
                tx = _line_to_tx(candidate, default_year)
                if tx:
                    results.append(tx)
                    used.add(i)
                    used.add(j)
                    i = j + 1
                    break
            else:
                i += 1
        else:
            i += 1

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
        if suffix.lower() == ".pdf":
            # 1a. pdfplumber table extraction (PDFs with explicit table borders)
            rows = _extract_via_tables(tmp_path, default_year)

            # 1b. Word-position extraction (Capital One / Costco MC multi-column PDF)
            if not rows:
                rows, debug_text = _extract_via_words(tmp_path, default_year)

        elif suffix.lower() in {".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp"}:
            # 2. Word-position extraction for image-based statements (phone photos)
            rows, debug_text = _extract_via_image_words(tmp_path, default_year)

        # 3. Fallback: full OCR text → line parser (works for simpler layouts)
        if not rows:
            text = extract_text(tmp_path, is_statement=True)
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


def import_statement_rows(
    rows: list[dict],
    filename: str = "statement",
    province: str = DEFAULT_PROVINCE,
) -> dict:
    """
    Persist pre-parsed transaction rows (from Vision LLM) to the database.
    Each row must have: date (YYYY-MM-DD), vendor (str), amount_gross (float).
    Returns the same shape as import_statement().
    """
    if not rows:
        return {"total_rows": 0, "new_records": 0, "skipped": 0,
                "filename": filename, "preview": []}

    df = pd.DataFrame(rows).drop_duplicates(subset=["date", "vendor", "amount_gross"])

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
