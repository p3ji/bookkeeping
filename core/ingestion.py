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


def _parse_amount(val) -> float | None:
    if pd.isna(val):
        return None
    cleaned = re.sub(r"[,$\s]", "", str(val))
    try:
        return abs(float(cleaned))
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

        # Determine amount: use debit column if present, else amount column
        amount = None
        if mapping["debit"]:
            amount = _parse_amount(r.get(mapping["debit"]))
        if (amount is None or amount == 0) and mapping["amount"]:
            amount = _parse_amount(r.get(mapping["amount"]))

        # Skip credits (payments back to card) — they are zero or the debit is empty
        # but credit column has a value
        if amount is None or amount <= 0:
            if mapping["credit"]:
                credit_val = _parse_amount(r.get(mapping["credit"]))
                if credit_val and credit_val > 0:
                    continue  # this row is a payment, skip
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
