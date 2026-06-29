"""Export verified transactions to Markdown (Obsidian-compatible)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from config import EXPORTS_DIR, CRA_LINES


def export_transaction_md(tx: dict, export_dir: Path = EXPORTS_DIR) -> Path:
    """
    Write a single transaction to a Markdown file with YAML frontmatter.
    Returns the path of the written file.
    """
    tx_date = tx.get("date")
    if hasattr(tx_date, "strftime"):
        date_str = tx_date.strftime("%Y-%m-%d")
    else:
        date_str = str(tx_date)[:10]

    vendor_slug = (
        str(tx.get("vendor", "unknown"))
        .lower()
        .replace(" ", "-")
        .replace("/", "-")[:40]
    )
    tx_id = tx.get("transaction_id", "")[:8]
    filename = f"{date_str}_{vendor_slug}_{tx_id}.md"

    flags = tx.get("audit_flags", [])
    if isinstance(flags, str):
        flags = json.loads(flags or "[]")

    cra_line = tx.get("cra_line") or ""
    cra_desc = tx.get("cra_description") or CRA_LINES.get(cra_line, "")

    business_pct = float(tx.get("business_percentage") or 1.0)
    amount_gross = float(tx.get("amount_gross") or 0)
    gst_hst = float(tx.get("gst_hst_amount") or 0)
    deductible = round(amount_gross * business_pct, 2)
    itc = round(gst_hst * business_pct, 2)

    receipt_rel = tx.get("receipt_path") or ""

    flags_yaml = "\n".join(f"  - \"{f}\"" for f in flags) if flags else "  []"
    flags_section = f"audit_flags:\n{flags_yaml}" if flags else "audit_flags: []"

    frontmatter = f"""---
transaction_id: "{tx_id}"
date: {date_str}
vendor: "{tx.get('vendor', '')}"
amount_gross: {amount_gross:.2f}
gst_hst_amount: {gst_hst:.2f}
cra_line: "{cra_line}"
cra_description: "{cra_desc}"
is_business: {str(bool(tx.get('is_business', True))).lower()}
business_percentage: {business_pct:.4f}
deductible_amount: {deductible:.2f}
gst_hst_itc: {itc:.2f}
receipt_path: "{receipt_rel}"
verified: {str(bool(tx.get('verified_status', False))).lower()}
notes: "{tx.get('notes', '')}"
{flags_section}
---
"""

    body_lines = [
        f"# {tx.get('vendor', 'Transaction')} — ${amount_gross:,.2f}",
        "",
        f"**Date:** {date_str}  ",
        f"**Category:** {cra_desc} (CRA Line {cra_line})  " if cra_line else "",
        f"**Business Use:** {int(business_pct * 100)}%  ",
        f"**Net Deductible:** ${deductible:,.2f}  ",
        f"**GST/HST ITC:** ${itc:,.2f}  ",
    ]

    if tx.get("notes"):
        body_lines += ["", f"**Notes:** {tx['notes']}"]

    if flags:
        body_lines += ["", "## Audit Flags", ""]
        for f in flags:
            body_lines.append(f"- ⚠️ {f}")

    if receipt_rel:
        body_lines += ["", f"**Receipt:** [{Path(receipt_rel).name}]({receipt_rel})"]

    content = frontmatter + "\n".join(body_lines) + "\n"

    out_path = export_dir / filename
    out_path.write_text(content, encoding="utf-8")
    return out_path


def export_all_verified(export_dir: Path = EXPORTS_DIR) -> list[Path]:
    from core.database import get_all_transactions
    df = get_all_transactions({"verified_only": True, "business_only": True})
    paths = []
    for _, row in df.iterrows():
        try:
            paths.append(export_transaction_md(row.to_dict(), export_dir))
        except Exception:
            pass
    return paths
