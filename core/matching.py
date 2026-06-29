"""Receipt-to-transaction matching engine."""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from config import MATCH_SCORE_THRESHOLD, DATE_WINDOW_DAYS, RECEIPTS_DIR
from core.ocr import extract_text, find_receipt_files
from core.tax import extract_amount_from_text, extract_tax_from_text
from core.database import (
    upsert_receipt_index, get_all_receipts, get_indexed_paths, update_transaction,
    get_all_transactions,
)

try:
    from thefuzz import fuzz
    _FUZZ = True
except ImportError:
    _FUZZ = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _receipt_id(file_path: str) -> str:
    return hashlib.md5(file_path.encode()).hexdigest()[:16]


def _parse_date_from_text(text: str):
    """Try to parse the first recognisable date in OCR text."""
    import re
    from dateutil import parser as dp

    # Common date patterns
    patterns = [
        r"\b(\d{4}-\d{2}-\d{2})\b",
        r"\b(\d{2}/\d{2}/\d{4})\b",
        r"\b(\w+ \d{1,2},?\s+\d{4})\b",
        r"\b(\d{1,2} \w+ \d{4})\b",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            try:
                return dp.parse(m.group(1), dayfirst=False).date()
            except Exception:
                continue
    return None


def _vendor_similarity(a: str, b: str) -> int:
    if not _FUZZ or not a or not b:
        return 0
    return fuzz.partial_ratio(a.lower(), b.lower())


# ---------------------------------------------------------------------------
# Receipt indexing
# ---------------------------------------------------------------------------

def index_receipts(receipts_dir: Path = RECEIPTS_DIR, progress_cb=None) -> int:
    """
    Scan receipts directory, OCR unindexed files, and save to receipt_index.
    Returns the number of files newly indexed.
    """
    all_files = find_receipt_files(receipts_dir)
    indexed_paths = get_indexed_paths()
    new_count = 0

    for i, fp in enumerate(all_files):
        file_path_str = str(fp)
        if file_path_str in indexed_paths:
            continue  # already indexed

        if progress_cb:
            progress_cb(i + 1, len(all_files), fp.name)

        text = extract_text(fp)
        amount = extract_amount_from_text(text)
        tax = extract_tax_from_text(text)
        date_found = _parse_date_from_text(text)

        # Rough vendor: use directory name or first text line
        vendor_guess = fp.parent.name  # month directory is a weak signal
        if text:
            first_line = text.strip().split("\n")[0][:50]
            if len(first_line) > 3:
                vendor_guess = first_line

        mtime = datetime.fromtimestamp(os.path.getmtime(fp))

        upsert_receipt_index({
            "receipt_id": _receipt_id(file_path_str),
            "file_path": file_path_str,
            "file_modified": mtime.strftime("%Y-%m-%d %H:%M:%S"),
            "date_extracted": str(date_found) if date_found else None,
            "amount_extracted": amount,
            "vendor_extracted": vendor_guess,
            "raw_text": text[:10_000],  # cap stored text
        })
        new_count += 1

    return new_count


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score(tx_date, tx_amount: float, tx_vendor: str,
           rx_date, rx_amount, rx_vendor: str) -> int:
    score = 0

    # Amount (0–50 pts)
    if rx_amount is not None:
        diff_pct = abs(tx_amount - rx_amount) / max(tx_amount, 0.01)
        if diff_pct == 0:
            score += 50
        elif diff_pct < 0.02:
            score += 35
        elif diff_pct < 0.05:
            score += 20
        elif diff_pct < 0.10:
            score += 10

    # Date (0–30 pts)
    if rx_date is not None:
        try:
            t = pd.to_datetime(tx_date).date()
            r = pd.to_datetime(rx_date).date()
            days = abs((t - r).days)
            if days == 0:
                score += 30
            elif days <= 1:
                score += 22
            elif days <= DATE_WINDOW_DAYS:
                score += 12
        except Exception:
            pass

    # Vendor (0–20 pts)
    sim = _vendor_similarity(tx_vendor, rx_vendor)
    if sim >= 90:
        score += 20
    elif sim >= 75:
        score += 12
    elif sim >= 55:
        score += 5

    return score


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def match_all_transactions(province: str = "ON") -> dict:
    """
    For every unverified, unmatched transaction, find the best receipt candidate
    and update the transaction's receipt_path.
    Returns a summary dict.
    """
    receipts_df = get_all_receipts()
    if receipts_df.empty:
        return {"matched": 0, "unmatched": 0}

    transactions_df = get_all_transactions()
    if transactions_df.empty:
        return {"matched": 0, "unmatched": 0}

    unmatched = transactions_df[
        transactions_df["receipt_path"].isna() |
        (transactions_df["receipt_path"] == "")
    ]

    matched_count = 0
    unmatched_count = 0

    for _, tx in unmatched.iterrows():
        best_score = 0
        best_receipt_path = None
        best_text = None
        best_tax = 0.0

        for _, rx in receipts_df.iterrows():
            score = _score(
                tx["date"], float(tx["amount_gross"]), str(tx["vendor"]),
                rx.get("date_extracted"), rx.get("amount_extracted"),
                str(rx.get("vendor_extracted") or ""),
            )
            if score > best_score:
                best_score = score
                best_receipt_path = rx["file_path"]
                best_text = rx.get("raw_text") or ""
                best_tax = extract_tax_from_text(best_text or "")

        if best_score >= MATCH_SCORE_THRESHOLD and best_receipt_path:
            update_transaction(tx["transaction_id"], {
                "receipt_path": best_receipt_path,
                "raw_receipt_text": best_text,
                "gst_hst_amount": best_tax if best_tax > 0 else float(tx.get("gst_hst_amount") or 0),
                "is_business": bool(tx.get("is_business", True)),
                "business_percentage": float(tx.get("business_percentage") or 1.0),
                "cra_line": tx.get("cra_line"),
                "cra_description": tx.get("cra_description"),
                "audit_flags": [],
                "verified_status": bool(tx.get("verified_status", False)),
                "notes": tx.get("notes") or "",
                "amount_net": float(tx["amount_gross"]) - (best_tax if best_tax > 0 else float(tx.get("gst_hst_amount") or 0)),
            })
            matched_count += 1
        else:
            unmatched_count += 1

    return {"matched": matched_count, "unmatched": unmatched_count}
