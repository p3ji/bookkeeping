"""Triage Inbox — classify transactions as business/personal, assign CRA category."""
import json
from pathlib import Path

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Triage", page_icon="🔍", layout="wide")

from core.database import (
    init_db, get_all_transactions, get_transaction, update_transaction,
    get_setting, get_summary_stats,
)
from core.audit import check_audit_flags
from core.tax import calculate_net, calculate_deductible
from core.ocr import render_pdf_preview, render_image_preview, extract_text
from core.receipt_parser import extract_receipt_data
from core.export import export_transaction_md
from config import CRA_LINES, RECEIPTS_DIR

init_db()

st.title("🔍 Triage Inbox")

province = get_setting("province", "ON")

stats = get_summary_stats()
unverified_count = stats["total"] - stats["verified"]
if unverified_count > 0:
    st.info(
        f"📋 You have **{unverified_count}** unverified transactions totaling "
        f"**${stats['unverified_total']:,.2f}** waiting for triage."
    )

# ---------------------------------------------------------------------------
# Sidebar filters
# ---------------------------------------------------------------------------
with st.sidebar:
    st.subheader("Filters")
    show_filter = st.selectbox(
        "Show",
        ["All", "Unverified only", "Business only", "Personal only"],
        index=0,
    )
    search_vendor = st.text_input("Search vendor", placeholder="e.g. Rogers")

filters: dict = {}
if show_filter == "Unverified only":
    filters["unverified_only"] = True
elif show_filter == "Business only":
    filters["business_only"] = True

df_all = get_all_transactions(filters)

if search_vendor:
    df_all = df_all[df_all["vendor"].str.contains(search_vendor, case=False, na=False)]

if df_all.empty:
    st.info("No transactions match the current filters.", icon="ℹ️")
    st.stop()

# ---------------------------------------------------------------------------
# Build display table
# ---------------------------------------------------------------------------
display_df = df_all[
    [c for c in ["date", "vendor", "amount_gross", "cra_description",
                 "is_business", "verified_status", "receipt_path"]
     if c in df_all.columns]
].copy()

display_df["date"] = pd.to_datetime(display_df["date"]).dt.strftime("%Y-%m-%d")
display_df["amount_gross"] = display_df["amount_gross"].apply(
    lambda x: f"${float(x):,.2f}"
)
display_df["Receipt"] = display_df["receipt_path"].apply(
    lambda p: "✅" if (p and str(p) not in ("", "None")) else "❌"
)
display_df["Status"] = display_df.apply(
    lambda r: "✅ Verified" if r.get("verified_status") else (
        "🟢 Business" if r.get("is_business") else "⚫ Personal"
    ), axis=1,
)

pending = int((~df_all["verified_status"].astype(bool)).sum())
st.caption(
    f"{len(df_all)} transactions total — **{pending} unverified**  |  "
    f"Click a row to classify it below."
)

grid_df = display_df[
    ["date", "vendor", "amount_gross", "cra_description", "Status", "Receipt"]
].rename(columns={
    "date": "Date", "vendor": "Vendor", "amount_gross": "Amount",
    "cra_description": "Category",
})

# ---------------------------------------------------------------------------
# Full-width transaction table
# ---------------------------------------------------------------------------
event = st.dataframe(
    grid_df,
    use_container_width=True,
    on_select="rerun",
    selection_mode="single-row",
    hide_index=True,
    height=400,
)

selected_idx = None
if event.selection and event.selection.rows:
    selected_idx = event.selection.rows[0]

# ---------------------------------------------------------------------------
# Detail panel — shown below the table when a row is selected
# ---------------------------------------------------------------------------
if selected_idx is None:
    st.info("Select a row above to classify the transaction.", icon="👆")
    st.stop()

tx_row = df_all.iloc[selected_idx]
tx_id  = str(tx_row["transaction_id"])
tx     = get_transaction(tx_id)

if tx is None:
    st.error("Transaction not found.")
    st.stop()

st.divider()
st.subheader(f"{tx['vendor']}  —  ${float(tx['amount_gross']):,.2f}")
st.caption(f"Date: {tx['date']}  |  ID: {tx_id}")

# Two columns: receipt preview (left) | classification form (right)
col_receipt, col_form = st.columns([1, 1], gap="large")

# ── Receipt preview ──────────────────────────────────────────────────────────
with col_receipt:
    receipt_path = tx.get("receipt_path")
    if receipt_path and Path(receipt_path).exists():
        rp = Path(receipt_path)
        st.markdown(f"**Receipt:** `{rp.name}`")
        # Document rendering with highlighted extraction bounding boxes
        from core.ocr import draw_highlights_on_image
        
        vendor_val = tx.get("vendor", "")
        date_val = str(tx.get("date") or "")
        try:
            total_val = float(tx.get("amount_gross") or 0.0)
        except ValueError:
            total_val = None
        try:
            tax_val = float(tx.get("gst_hst_amount") or 0.0)
        except ValueError:
            tax_val = None
            
        img_bytes = draw_highlights_on_image(
            rp, 
            vendor=vendor_val, 
            date=date_val, 
            total=total_val, 
            tax=tax_val
        )
        
        # Fallback to standard preview if drawing highlights returned None
        if not img_bytes:
            if rp.suffix.lower() == ".pdf":
                img_bytes = render_pdf_preview(rp)
            else:
                img_bytes = render_image_preview(rp)
                
        if img_bytes:
            st.image(img_bytes, use_container_width=True)
        else:
            st.image(str(rp), use_container_width=True)

        # Show structured data extracted from receipt
        raw_text = tx.get("raw_receipt_text") or ""
        if raw_text:
            data = extract_receipt_data(raw_text)
            with st.expander("📋 Extracted Receipt Data"):
                if data.vendor:
                    st.write(f"**Vendor:** {data.vendor}")
                if data.date:
                    st.write(f"**Date:** {data.date}")
                if data.total is not None:
                    st.write(f"**Total:** ${data.total:.2f}")
                taxes = {}
                if data.tax_hst:
                    taxes["HST"] = data.tax_hst
                if data.tax_gst:
                    taxes["GST"] = data.tax_gst
                if data.tax_pst:
                    taxes["PST"] = data.tax_pst
                if taxes:
                    st.write("**Tax:** " + "  |  ".join(
                        f"{k}: ${v:.2f}" for k, v in taxes.items()
                    ))
                if data.line_items:
                    st.write(f"**Line items:** {len(data.line_items)}")
                    st.dataframe(data.line_items, hide_index=True,
                                 use_container_width=True)
            
            # Show side-by-side extraction methods comparison
            extraction_details = tx.get("extraction_details")
            if isinstance(extraction_details, str):
                try:
                    extraction_details = json.loads(extraction_details)
                except Exception:
                    extraction_details = {}
            if extraction_details and isinstance(extraction_details, dict) and len(extraction_details) > 0:
                with st.expander("🔍 Compare Extraction Methods"):
                    from core.extraction import METHOD_LABELS
                    from core.receipt_parser import ReceiptData
                    
                    comp_rows = []
                    for field_name in ["vendor", "date", "total", "tax"]:
                        row_dict = {"Field": field_name.capitalize()}
                        for m in ["deterministic", "ollama", "cloud", "gemini", "playwright"]:
                            m_data = extraction_details.get(m)
                            if not m_data:
                                row_dict[METHOD_LABELS.get(m, m)] = "—"
                                continue
                            
                            # Resolve value
                            if m == "playwright":
                                val = m_data.get(field_name) if isinstance(m_data, dict) else getattr(m_data, field_name, "—")
                            elif isinstance(m_data, dict):
                                val = m_data.get(field_name, "—")
                            else:
                                val = getattr(m_data, field_name, "—")
                                
                            if field_name == "total" or field_name == "tax":
                                if field_name == "tax":
                                    if isinstance(m_data, dict):
                                        val = (m_data.get("tax_hst") or 0.0) + (m_data.get("tax_gst") or 0.0) or (m_data.get("hst") or 0.0) + (m_data.get("gst") or 0.0)
                                    else:
                                        val = (getattr(m_data, "tax_hst", 0.0) or 0.0) + (getattr(m_data, "tax_gst", 0.0) or 0.0)
                                try:
                                    if val is not None and val != "—":
                                        row_dict[METHOD_LABELS.get(m, m)] = f"${float(val):,.2f}"
                                    else:
                                        row_dict[METHOD_LABELS.get(m, m)] = "—"
                                except ValueError:
                                    row_dict[METHOD_LABELS.get(m, m)] = str(val)
                            else:
                                row_dict[METHOD_LABELS.get(m, m)] = str(val) if val is not None else "—"
                        
                        # Reconciled value from current transaction
                        if field_name == "vendor":
                            row_dict["Reconciled (Final)"] = tx.get("vendor", "—")
                        elif field_name == "date":
                            row_dict["Reconciled (Final)"] = str(tx.get("date") or "—")
                        elif field_name == "total":
                            row_dict["Reconciled (Final)"] = f"${float(tx.get('amount_gross', 0.0)):,.2f}"
                        elif field_name == "tax":
                            row_dict["Reconciled (Final)"] = f"${float(tx.get('gst_hst_amount', 0.0)):,.2f}"
                            
                        comp_rows.append(row_dict)
                    st.table(pd.DataFrame(comp_rows))
    else:
        st.markdown("**No receipt matched.**")
        with st.expander("📎 Attach receipt manually"):
            manual_receipt = st.file_uploader(
                "Upload receipt", type=["pdf", "jpg", "jpeg", "png"],
                key=f"receipt_{tx_id}",
            )
            if manual_receipt:
                tx_date  = pd.to_datetime(tx["date"])
                month_dir = RECEIPTS_DIR / str(tx_date.year) / f"{tx_date.month:02d}"
                month_dir.mkdir(parents=True, exist_ok=True)
                save_path = month_dir / manual_receipt.name
                save_path.write_bytes(manual_receipt.read())
                raw_text  = extract_text(save_path)
                if st.button("Attach", key=f"attach_{tx_id}"):
                    update_transaction(tx_id, {
                        "receipt_path": str(save_path),
                        "raw_receipt_text": raw_text,
                        "is_business": tx.get("is_business", True),
                        "business_percentage": tx.get("business_percentage", 1.0),
                        "cra_line": tx.get("cra_line"),
                        "cra_description": tx.get("cra_description"),
                        "audit_flags": tx.get("audit_flags", []),
                        "verified_status": tx.get("verified_status", False),
                        "notes": tx.get("notes", ""),
                        "amount_net": tx.get("amount_net"),
                        "gst_hst_amount": tx.get("gst_hst_amount", 0),
                    })
                    st.success("Receipt attached.")
                    st.rerun()

# ── Classification form ───────────────────────────────────────────────────────
with col_form:
    with st.form(key=f"form_{tx_id}"):

        col_biz, col_pct = st.columns([1, 2])
        with col_biz:
            is_business = st.toggle(
                "Business Expense",
                value=bool(tx.get("is_business", True)),
            )
        with col_pct:
            business_pct = st.slider(
                "Business-Use %", 0, 100,
                value=int(float(tx.get("business_percentage") or 1.0) * 100),
                step=5,
                disabled=not is_business,
            )

        cra_options   = [("", "— Select category —")] + list(CRA_LINES.items())
        current_line  = tx.get("cra_line") or ""
        try:
            current_idx = [o[0] for o in cra_options].index(current_line)
        except ValueError:
            current_idx = 0

        cra_choice = st.selectbox(
            "CRA T2125 Category",
            options=cra_options,
            format_func=lambda o: f"Line {o[0]}: {o[1]}" if o[0] else o[1],
            index=current_idx,
            disabled=not is_business,
        )
        cra_line = cra_choice[0]
        cra_desc = cra_choice[1] if cra_choice[0] else None

        gst_hst = st.number_input(
            "GST/HST Amount ($)",
            min_value=0.0,
            value=float(tx.get("gst_hst_amount") or 0),
            step=0.01, format="%.2f",
            help="Auto-estimated. Override if your receipt shows a different amount.",
        )

        notes = st.text_area(
            "Notes",
            value=tx.get("notes") or "",
            placeholder="Business purpose, attendees, etc.",
            height=80,
        )

        verified = st.checkbox(
            "Mark as Verified ✅",
            value=bool(tx.get("verified_status", False)),
        )

        # Live deductible preview
        amount_gross = float(tx.get("amount_gross") or 0)
        biz_ratio    = business_pct / 100 if is_business else 0
        breakdown    = calculate_deductible(amount_gross, gst_hst, biz_ratio,
                                            is_business, cra_line)
        pc1, pc2, pc3 = st.columns(3)
        pc1.metric("Gross",       f"${amount_gross:,.2f}")
        pc2.metric("Deductible",  f"${breakdown['deductible_gross']:,.2f}")
        pc3.metric("ITC",         f"${breakdown['deductible_itc']:,.2f}")
        if breakdown["note"]:
            st.caption(f"ℹ️ {breakdown['note']}")

        col_save, col_export = st.columns(2)
        save_btn   = col_save.form_submit_button("💾 Save", type="primary",
                                                  use_container_width=True)
        export_btn = col_export.form_submit_button("📄 Save & Export MD",
                                                    use_container_width=True)

    if save_btn or export_btn:
        new_flags  = check_audit_flags(
            vendor=str(tx.get("vendor", "")),
            amount_gross=amount_gross,
            cra_line=cra_line or None,
            business_percentage=biz_ratio,
            raw_text=str(tx.get("raw_receipt_text") or ""),
        )
        amount_net = calculate_net(amount_gross, gst_hst)
        update_transaction(tx_id, {
            "is_business": is_business,
            "business_percentage": biz_ratio,
            "cra_line": cra_line or None,
            "cra_description": cra_desc,
            "audit_flags": new_flags,
            "verified_status": verified,
            "notes": notes,
            "amount_net": amount_net,
            "gst_hst_amount": gst_hst,
            "receipt_path": tx.get("receipt_path"),
            "raw_receipt_text": tx.get("raw_receipt_text"),
        })

        if new_flags:
            for flag in new_flags:
                st.warning(f"⚠️ {flag}")

        if export_btn and verified:
            updated_tx = get_transaction(tx_id)
            if updated_tx:
                out = export_transaction_md(updated_tx)
                st.success(f"Exported to `{out.name}`")

        st.success("Saved.", icon="✅")
        st.rerun()

    # Existing audit flags
    existing_flags = tx.get("audit_flags", [])
    if isinstance(existing_flags, str):
        existing_flags = json.loads(existing_flags or "[]")
    if existing_flags:
        st.divider()
        st.markdown("**Audit Flags**")
        for flag in existing_flags:
            st.warning(flag, icon="⚠️")
