"""GST/HST extraction from receipt text and ITC calculation."""
from __future__ import annotations

import re

from config import TAX_RATES, DEFAULT_PROVINCE


# Patterns to locate GST/HST line amounts in receipt text
_TAX_PATTERNS = [
    # "HST  $14.23" or "GST $1.55"
    re.compile(r"\b(?:HST|GST|QST|TVQ|TPS|TVH)\b[:\s]*\$?\s*([\d,]+\.?\d{0,2})", re.I),
    # "Tax: 14.23"
    re.compile(r"\bTax[:\s]+\$?\s*([\d,]+\.?\d{0,2})", re.I),
    # "13% HST 14.23"
    re.compile(r"\d{1,2}%\s*(?:HST|GST)\s*\$?\s*([\d,]+\.?\d{0,2})", re.I),
    # CRA business number pattern "RT0001" nearby — not a dollar amount but flags a receipt as tax-registered
]

_AMOUNT_PATTERNS = [
    re.compile(r"\bTotal[:\s]+\$?\s*([\d,]+\.?\d{0,2})", re.I),
    re.compile(r"\bSubtotal[:\s]+\$?\s*([\d,]+\.?\d{0,2})", re.I),
    re.compile(r"\bGrand Total[:\s]+\$?\s*([\d,]+\.?\d{0,2})", re.I),
    re.compile(r"\bAmount Due[:\s]+\$?\s*([\d,]+\.?\d{0,2})", re.I),
    re.compile(r"\bBalance Due[:\s]+\$?\s*([\d,]+\.?\d{0,2})", re.I),
]

_VENDOR_PATTERNS = [
    re.compile(r"^(.+?)\n", re.M),  # First non-blank line
]


def extract_tax_from_text(text: str) -> float:
    """Return the first GST/HST dollar amount found in receipt text, or 0.0."""
    for pat in _TAX_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                continue
    return 0.0


def extract_amount_from_text(text: str) -> float | None:
    """Try to extract total amount from receipt text."""
    for pat in _AMOUNT_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                continue
    # Fallback: find the largest dollar amount on a "Total" line
    amounts = re.findall(r"\$\s*([\d,]+\.\d{2})", text)
    if amounts:
        values = [float(a.replace(",", "")) for a in amounts]
        return max(values)
    return None


def estimate_tax(gross_amount: float, province: str = DEFAULT_PROVINCE) -> float:
    """Estimate GST/HST from a gross amount using the provincial rate."""
    rate = TAX_RATES.get(province, TAX_RATES["ON"])["rate"]
    # Tax is embedded: tax = gross * rate / (1 + rate)
    return round(gross_amount * rate / (1 + rate), 2)


def calculate_net(gross: float, tax: float) -> float:
    return round(gross - tax, 2)


def calculate_deductible(amount_gross: float, gst_hst: float,
                         business_pct: float, is_business: bool,
                         cra_line: str | None = None) -> dict:
    """
    Return a breakdown dict used for the ledger and export.
    Meals & Entertainment (8523) are subject to the 50% CRA limit.
    """
    if not is_business:
        return {"deductible_gross": 0.0, "deductible_itc": 0.0, "note": "Personal expense"}

    effective_pct = business_pct
    note = ""
    if cra_line == "8523":
        effective_pct = business_pct * 0.50
        note = "50% meal/entertainment limit applied"

    deductible_gross = round(amount_gross * effective_pct, 2)
    deductible_itc = round(gst_hst * effective_pct, 2)
    return {
        "deductible_gross": deductible_gross,
        "deductible_itc": deductible_itc,
        "note": note,
    }
