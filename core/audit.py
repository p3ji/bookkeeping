"""CRA audit risk flagging for business transactions."""
from __future__ import annotations

from core.categorization import is_capital_asset


def check_audit_flags(
    vendor: str,
    amount_gross: float,
    cra_line: str | None,
    business_percentage: float,
    raw_text: str = "",
) -> list[str]:
    """Return a list of audit warning strings for a transaction."""
    flags: list[str] = []

    # 1. High-value meal/entertainment
    if cra_line == "8523" and amount_gross > 500:
        flags.append(
            "HIGH-VALUE MEAL: Amount exceeds $500. Ensure attendee names, "
            "business purpose, and venue are documented."
        )

    # 2. Potential capital asset under Supplies
    if cra_line == "8811" and raw_text and is_capital_asset(raw_text):
        flags.append(
            "POTENTIAL CAPITAL ASSET: Receipt text suggests an electronic device or "
            "computer. Evaluate under Capital Cost Allowance (CCA Class 50) rather "
            "than direct expensing as Supplies."
        )

    # 3. Aggressive 100% telecom/utility allocation
    if cra_line == "9220" and business_percentage >= 1.0:
        flags.append(
            "100% TELECOM/UTILITIES: Claiming full personal phone or internet as "
            "business-only is a CRA high-risk trigger. Confirm you have a separate "
            "dedicated business line, or prorate to reflect personal use."
        )

    # 4. High-value single transaction (general)
    if amount_gross > 2000 and cra_line not in ("9281", "9200"):
        flags.append(
            f"LARGE EXPENSE (${amount_gross:,.2f}): High-value transaction. "
            "Ensure receipt is retained and business purpose is documented."
        )

    # 5. Vehicle at 100%
    if cra_line == "9281" and business_percentage >= 1.0:
        flags.append(
            "100% VEHICLE: CRA requires a mileage logbook to support 100% business "
            "vehicle deduction. Personal use portion must be tracked."
        )

    return flags
