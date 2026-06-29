"""Triage Inbox — classify transactions as business/personal, assign CRA category."""
import io
import json
from pathlib import Path

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Triage", page_icon="🔍", layout="wide")

from core.database import (
    init_db, get_all_transactions, get_transaction, update_transaction,
    get_setting,
)
from core.audit import check_audit_flags
from core.tax import estimate_tax, calculate_net, calculate_deductible
from core.ocr import render_pdf_preview, extract_text
from core.export import export_transaction_md
from config import CRA_LINES, RECEIPTS_DIR

init_db()

st.title("🔍 Triage Inbox")

province = get_setting("province", "ON")

# ---------------------------------------------------------------------------
# Filters sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.subheader("Filters")
    show_filter = st.selectbox(
        "Show",
        ["All", "Unverified only", "Business only", "Personal only"],
        index=1,
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
# Prepare display dataframe
# ---------------------------------------------------------------------------
DISPLAY_COLS = ["date", "vendor", "amount_gross", "cra_description",
                "is_business", "verified_status", "receipt_path"]

display_df = df_all[
    [c for c in DISPLAY_COLS if c in df_all.columns]
].copy()

display_df["date"] = pd.to_datetime(display_df["date"]).dt.strftime("%Y-%m-%d")
display_df["amount_gross"] = display_df["amount_gross"].apply(lambda x: f"${float(x):,.2f}")
display_df["receipt"] = display_df["receipt_path"].apply(
    lambda p: "✅" if (p and str(p) != "None" and str(p).strip()) else "❌"
)
display_df["status"] = display_df.apply(
    lambda r: "✅ Verified" if r.get("verified_status") else (
        "🟢 Business" if r.get("is_business") else "⚫ Personal"
    ), axis=1
)

grid_df = display_df[["date", "vendor", "amount_gross", "cra_description",
                       "status", "receipt"]].rename(columns={
    "date": "Date", "vendor": "Vendor", "amount_gross": "Amount",
    "cra_description": "Category", "status": "Status", "receipt": "Receipt",
})

# ---------------------------------------------------------------------------
# Split pane layout
# ---------------------------------------------------------------------------
left, right = st.columns([1, 1], gap="large")

with left:
    st.subheader(f"Transactions ({len(grid_df)})")
    event = st.dataframe(
        grid_df,
        use_container_width=True,
        on_select="rerun",
        selection_mode="single-row",
        hide_index=True,
        height=500,
    )

    selected_idx = None
    if event.selection and event.selection.rows:
        selected_idx = event.selection.rows[0]

with right:
    if selected_idx is None:
        st.info("Select a transaction on the left to review it.", icon="👈")
    else:
        tx_row = df_all.iloc[selected_idx]
        tx_id = str(tx_row["transaction_id"])
        tx = get_transaction(tx_id)
        if tx is None:
            st.error("Transaction not found.")
            st.stop()

        st.subheader(f"{tx['vendor']}")
        st.caption(f"ID: {tx_id}  |  Date: {tx['date']}")

        # ----------------------------------------------------------------
        # Receipt preview
        # ----------------------------------------------------------------
        receipt_path = tx.get("receipt_path")
        if receipt_path and Path(receipt_path).exists():
            rp = Path(receipt_path)
            st.markdown(f"**Receipt:** `{rp.name}`")
            if rp.suffix.lower() == ".pdf":
                img_bytes = render_pdf_preview(rp)
                if img_bytes:
                    st.image(img_bytes, use_container_width=True)
                else:
                    st.warning("PDF preview unavailable (PyMuPDF not installed).")
            else:
                st.image(str(rp), use_container_width=True)
        else:
            with st.expander("📎 Attach Receipt Manually"):
                manual_receipt = st.file_uploader(
                    "Upload receipt file", type=["pdf", "jpg", "jpeg", "png"],
                    key=f"receipt_{tx_id}"
                )
                if manual_receipt:
                    # Save to receipts/year/month/
                    tx_date = pd.to_datetime(tx["date"])
                    month_dir = RECEIPTS_DIR / str(tx_date.year) / f"{tx_date.month:02d}"
                    month_dir.mkdir(parents=True, exist_ok=True)
                    save_path = month_dir / manual_receipt.name
                    save_path.write_bytes(manual_receipt.read())
                    raw_text = extract_text(save_path)
                    if st.button("Attach Receipt", key=f"attach_{tx_id}"):
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

        st.divider()

        # ----------------------------------------------------------------
        # Classification form
        # ----------------------------------------------------------------
        with st.form(key=f"form_{tx_id}"):
            st.markdown("**Classify Transaction**")

            col_biz, col_pct = st.columns([1, 2])
            with col_biz:
                is_business = st.toggle(
                    "Business Expense",
                    value=bool(tx.get("is_business", True)),
                    key=f"biz_{tx_id}",
                )
            with col_pct:
                business_pct = st.slider(
                    "Business-Use %",
                    0, 100,
                    value=int(float(tx.get("business_percentage") or 1.0) * 100),
                    step=5,
                    disabled=not is_business,
                    key=f"pct_{tx_id}",
                )

            # CRA Category
            cra_options = [("", "— Select category —")] + list(CRA_LINES.items())
            current_line = tx.get("cra_line") or ""
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
                key=f"cra_{tx_id}",
            )
            cra_line = cra_choice[0]
            cra_desc = cra_choice[1] if cra_choice[0] else None

            # GST/HST override
            gst_default = float(tx.get("gst_hst_amount") or 0)
            gst_hst = st.number_input(
                "GST/HST Amount ($)",
                min_value=0.0, value=gst_default, step=0.01, format="%.2f",
                help="Extracted from receipt. Override if incorrect.",
                key=f"gst_{tx_id}",
            )

            notes = st.text_area(
                "Notes",
                value=tx.get("notes") or "",
                placeholder="Business purpose, attendees, etc.",
                height=80,
                key=f"notes_{tx_id}",
            )

            verified = st.checkbox(
                "Mark as Verified ✅",
                value=bool(tx.get("verified_status", False)),
                key=f"verified_{tx_id}",
            )

            # Deductible preview
            amount_gross = float(tx.get("amount_gross") or 0)
            biz_ratio = business_pct / 100 if is_business else 0
            breakdown = calculate_deductible(amount_gross, gst_hst, biz_ratio,
                                             is_business, cra_line)
            with st.container():
                pc1, pc2, pc3 = st.columns(3)
                pc1.metric("Gross", f"${amount_gross:,.2f}")
                pc2.metric("Deductible", f"${breakdown['deductible_gross']:,.2f}")
                pc3.metric("ITC", f"${breakdown['deductible_itc']:,.2f}")
                if breakdown["note"]:
                    st.caption(f"ℹ️ {breakdown['note']}")

            col_save, col_export = st.columns(2)
            save_btn = col_save.form_submit_button("💾 Save", type="primary", use_container_width=True)
            export_btn = col_export.form_submit_button("📄 Save & Export MD", use_container_width=True)

        if save_btn or export_btn:
            new_flags = check_audit_flags(
                vendor=str(tx.get("vendor", "")),
                amount_gross=amount_gross,
                cra_line=cra_line if cra_line else None,
                business_percentage=biz_ratio,
                raw_text=str(tx.get("raw_receipt_text") or ""),
            )
            amount_net = calculate_net(amount_gross, gst_hst)
            update_transaction(tx_id, {
                "is_business": is_business,
                "business_percentage": biz_ratio,
                "cra_line": cra_line if cra_line else None,
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

        # Show existing audit flags
        existing_flags = tx.get("audit_flags", [])
        if isinstance(existing_flags, str):
            existing_flags = json.loads(existing_flags or "[]")
        if existing_flags:
            st.divider()
            st.markdown("**Audit Flags**")
            for flag in existing_flags:
                st.warning(flag, icon="⚠️")
