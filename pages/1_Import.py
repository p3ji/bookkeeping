"""Universal Import Inbox — Upload any document type and link in real-time."""
import io
import os
import hashlib
import json
import tempfile
import shutil
from pathlib import Path
from datetime import datetime
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Import & Link", page_icon="📥", layout="wide")

from core.database import (
    init_db, get_setting, upsert_transaction, upsert_receipt_index,
    update_transaction, get_all_transactions
)
from core.ingestion import import_csv, import_statement_rows, make_tx_id, normalize_vendor
from core.matching import index_receipts, match_all_transactions, find_best_matches
from core.ocr import ocr_capabilities, render_pdf_preview, render_image_preview, extract_text
from core.receipt_parser import _detect_doc_type, ReceiptData
from core.extraction import extract_statement, extract_receipt, available_methods, METHOD_LABELS
from core.playwright_extractor import is_playwright_available, extract_from_url
from config import STATEMENTS_DIR, RECEIPTS_DIR, DEFAULT_PROVINCE
from core.tax import estimate_tax, calculate_net
from core.audit import check_audit_flags

init_db()

# Initialize session state for pending uploads queue
if "pending_imports" not in st.session_state:
    st.session_state.pending_imports = {}
if "processed_files" not in st.session_state:
    st.session_state.processed_files = set()  # set of (name, size)
if "last_processed_batch" not in st.session_state:
    st.session_state.last_processed_batch = []

# Display persistent success message from rerun
if "import_success_msg" in st.session_state and st.session_state.import_success_msg:
    st.success(st.session_state.import_success_msg, icon="✅")
    del st.session_state.import_success_msg

# Import settings sidebar
with st.sidebar:
    st.header("Import Settings")
    default_year = st.number_input(
        "Default Year for Statements",
        min_value=2015,
        max_value=datetime.now().year + 1,
        value=datetime.now().year,
        step=1,
        help="If a statement line doesn't specify a year, this year is assumed."
    )
    
    st.divider()
    st.subheader("⚙️ Extraction Pipeline")
    from core.extraction import available_methods, METHOD_LABELS
    active = available_methods(check_enabled=True)
    all_methods = ["deterministic", "ollama", "cloud", "gemini"]
    
    for m in all_methods:
        is_active = m in active
        label = METHOD_LABELS.get(m, m)
        if is_active:
            st.markdown(f"🟢 **{label}**: Active")
        else:
            st.markdown(f"⚪ **{label}**: Inactive (enable in Settings)")

st.title("📥 Import & Link")
st.caption("Drag & drop statements, receipts, invoices, or telecom bills. The system will automatically process and match them.")

province = get_setting("province", "ON")
ocr = ocr_capabilities()
pw_ok = is_playwright_available()

# Initialize dynamic keys for file uploaders to allow clearing them on success (Bug 5)
if "statements_uploader_key" not in st.session_state:
    st.session_state.statements_uploader_key = 0
if "receipts_uploader_key" not in st.session_state:
    st.session_state.receipts_uploader_key = 0

# ---------------------------------------------------------------------------
# Section 1: Ingestion Zone
# ---------------------------------------------------------------------------
col_upload, col_url = st.columns([2, 1], gap="medium")

with col_upload:
    st.markdown("**📁 Manual Document Ingestion**")
    c_stmt, c_rcpt = st.columns(2)
    with c_stmt:
        uploaded_statements = st.file_uploader(
            "📄 Credit Card Statements",
            type=["csv", "pdf", "jpg", "jpeg", "png"],
            accept_multiple_files=True,
            key=f"statements_uploader_{st.session_state.statements_uploader_key}"
        )
    with c_rcpt:
        uploaded_receipts = st.file_uploader(
            "📎 Receipts & Invoices",
            type=["pdf", "jpg", "jpeg", "png", "html", "htm"],
            accept_multiple_files=True,
            key=f"receipts_uploader_{st.session_state.receipts_uploader_key}"
        )

with col_url:
    with st.container(border=True):
        st.markdown("**🌐 Web Bill Fetcher**")
        web_url = st.text_input("Bill or statement URL", placeholder="https://myaccount.freedommobile.ca/...", key="web_url_input")
        fetch_btn = st.button("Fetch & Process URL", type="secondary", use_container_width=True, disabled=not pw_ok or not web_url)
        if not pw_ok:
            st.caption("⚠️ Playwright not installed. Web bill fetching disabled.")

# ---------------------------------------------------------------------------
# Process uploaded files and URLs
# ---------------------------------------------------------------------------
new_files_to_process = []
if uploaded_statements:
    for f in uploaded_statements:
        file_key = (f.name, f.size, "statement")
        if file_key not in st.session_state.processed_files:
            new_files_to_process.append((f, file_key, "statement"))

if uploaded_receipts:
    for f in uploaded_receipts:
        file_key = (f.name, f.size, "receipt")
        if file_key not in st.session_state.processed_files:
            new_files_to_process.append((f, file_key, "receipt"))

# Run processing
if new_files_to_process or (fetch_btn and web_url):
    st.session_state.last_processed_batch = [] # Reset batch summary
    with st.spinner("Processing documents..."):
        # Handle files
        for f, file_key, target_type in new_files_to_process:
            file_bytes = f.read()
            filename = f.name
            
            # 1. Handle CSV (Statement)
            if filename.lower().endswith(".csv"):
                try:
                    res = import_csv(io.BytesIO(file_bytes), filename=filename, province=province)
                    st.session_state.processed_files.add(file_key)
                    st.session_state.last_processed_batch.append({
                        "filename": filename,
                        "type": "Statement (CSV)",
                        "status": f"✅ Imported ({res['new_records']} new)",
                        "confidence": 1.0
                    })
                    st.session_state.import_success_msg = (
                        f"**CSV Statement `{filename}` Imported Successfully!**\n\n"
                        f"* 🔍 **Total Detected:** {res['total_rows']} transactions (${res['detected_sum']:,.2f} gross)\n"
                        f"* ✨ **New Transactions:** {res['new_records']} added (${res['new_sum']:,.2f} gross)\n"
                        f"* 📋 **Needing Triage:** {res['new_records']} (added as unverified)\n"
                        f"* ⏭️ **Skipped (Duplicates):** {res['skipped']} already in database"
                    )
                except Exception as e:
                    st.error(f"Failed to import CSV `{filename}`: {e}")
                continue

            # 2. Handle PDF/Image/HTML
            suffix = Path(filename).suffix or ".pdf"
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(file_bytes)
                tmp_path = Path(tmp.name)
            
            try:
                doc_type = target_type
                text = ""
                # Fallback to auto-detection if target_type is not provided
                if not doc_type:
                    text = extract_text(tmp_path)
                    doc_type = _detect_doc_type(text)
                
                import uuid
                import_id = uuid.uuid4().hex[:16]
                
                if doc_type == "statement":
                    # Parse as statement
                    res = extract_statement(tmp_path, filename=filename, default_year=default_year)
                    st.session_state.pending_imports[import_id] = {
                        "type": "statement",
                        "filename": filename,
                        "rows": res.rows,
                        "by_method": res.by_method,
                        "method_status": res.method_status,
                        "confidence": res.confidence,
                        "method_used": res.method_used,
                    }
                    st.session_state.last_processed_batch.append({
                        "filename": filename,
                        "type": f"Statement ({res.method_used})",
                        "status": "✅ Extracted (Review in Inbox)",
                        "confidence": res.confidence
                    })
                else:
                    # Parse as receipt
                    res = extract_receipt(tmp_path)
                    rd = res.receipt
                    
                    # Determine save path based on date
                    year_str = datetime.now().strftime("%Y")
                    month_str = datetime.now().strftime("%m")
                    if rd and rd.date:
                        try:
                            dt = pd.to_datetime(rd.date)
                            year_str = f"{dt.year}"
                            month_str = f"{dt.month:02d}"
                        except Exception:
                            pass
                    
                    target_dir = RECEIPTS_DIR / year_str / month_str
                    target_dir.mkdir(parents=True, exist_ok=True)
                    
                    stem = Path(filename).stem
                    dest_path = target_dir / filename
                    if dest_path.exists():
                        dest_path = target_dir / f"{stem}_{int(datetime.now().timestamp())}{suffix}"
                        
                    shutil.copy(tmp_path, dest_path)
                    
                    # Index in Database
                    mtime = datetime.fromtimestamp(os.path.getmtime(dest_path))
                    upsert_receipt_index({
                        "receipt_id": hashlib.md5(str(dest_path).encode()).hexdigest()[:16],
                        "file_path": str(dest_path),
                        "file_modified": mtime.strftime("%Y-%m-%d %H:%M:%S"),
                        "date_extracted": rd.date if (rd and rd.date) else None,
                        "amount_extracted": rd.total if (rd and rd.total is not None) else None,
                        "vendor_extracted": rd.vendor if (rd and rd.vendor) else filename,
                        "raw_text": ((rd.raw_text if rd else "") or text or extract_text(dest_path))[:10_000],
                        "extraction_method": res.method_used,
                        "extraction_confidence": res.confidence,
                        "extraction_details": res.by_method
                    })
                    
                    # Find matching candidates
                    candidates = find_best_matches(
                        rd.date if (rd and rd.date) else None,
                        rd.total if (rd and rd.total is not None) else None,
                        rd.vendor if (rd and rd.vendor) else filename
                    )
                    
                    st.session_state.pending_imports[import_id] = {
                        "type": "receipt",
                        "filename": filename,
                        "receipt": rd,
                        "flags": res.flags,
                        "by_method": res.by_method,
                        "path": dest_path,
                        "candidates": candidates,
                        "confidence": res.confidence,
                        "method_used": res.method_used
                    }
                    status_str = f"✅ Extracted ({res.method_used})"
                    if res.confidence < 0.50:
                        status_str = "⚠️ Struggled (Low Confidence OCR)"
                    elif not rd or not rd.vendor or not rd.date or rd.total is None or rd.total == 0.0:
                        status_str = "⚠️ Struggled (Incomplete Fields)"
                    st.session_state.last_processed_batch.append({
                        "filename": filename,
                        "type": "Receipt/Invoice",
                        "status": status_str,
                        "confidence": res.confidence
                    })
                
                st.session_state.processed_files.add(file_key)
            except Exception as e:
                st.error(f"Failed to process `{filename}`: {e}")
            finally:
                if tmp_path.exists():
                    os.unlink(tmp_path)
                    
        # Handle URL Fetch
        if fetch_btn and web_url:
            try:
                res_pw = extract_from_url(web_url, default_year=default_year)
                doc_type = res_pw["doc_type"]
                from urllib.parse import urlparse
                filename = f"web_{urlparse(web_url).netloc or 'bill'}_{int(datetime.now().timestamp())}"
                import uuid
                import_id = uuid.uuid4().hex[:16]
                
                if doc_type == "statement":
                    st.session_state.pending_imports[import_id] = {
                        "type": "statement",
                        "filename": filename,
                        "rows": res_pw.get("transactions", []),
                        "by_method": {"playwright": res_pw.get("transactions", [])},
                        "confidence": 1.0,
                        "method_used": "playwright",
                    }
                    st.session_state.last_processed_batch.append({
                        "filename": filename,
                        "type": "Statement (Web)",
                        "status": "✅ Fetched via Playwright",
                        "confidence": 1.0
                    })
                else:
                    # Save HTML content as receipt file
                    year_str = datetime.now().strftime("%Y")
                    month_str = datetime.now().strftime("%m")
                    rd = res_pw.get("receipt_data")
                    if rd and rd.date:
                        try:
                            dt = pd.to_datetime(rd.date)
                            year_str = f"{dt.year}"
                            month_str = f"{dt.month:02d}"
                        except Exception:
                            pass
                    
                    target_dir = RECEIPTS_DIR / year_str / month_str
                    target_dir.mkdir(parents=True, exist_ok=True)
                    dest_path = target_dir / f"{filename}.html"
                    dest_path.write_text(res_pw.get("raw_text", ""), encoding="utf-8")
                    
                    # Index
                    upsert_receipt_index({
                        "receipt_id": hashlib.md5(str(dest_path).encode()).hexdigest()[:16],
                        "file_path": str(dest_path),
                        "file_modified": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "date_extracted": rd.date if (rd and rd.date) else None,
                        "amount_extracted": rd.total if (rd and rd.total is not None) else None,
                        "vendor_extracted": rd.vendor if (rd and rd.vendor) else filename,
                        "raw_text": res_pw.get("raw_text", "")[:10_000],
                        "extraction_method": "playwright",
                        "extraction_confidence": 1.0,
                        "extraction_details": {"playwright": rd.__dict__ if rd else {}}
                    })
                    
                    candidates = find_best_matches(
                        rd.date if (rd and rd.date) else None,
                        rd.total if (rd and rd.total is not None) else None,
                        rd.vendor if (rd and rd.vendor) else filename
                    )
                    
                    st.session_state.pending_imports[import_id] = {
                        "type": "receipt",
                        "filename": filename + ".html",
                        "receipt": rd,
                        "flags": [],
                        "by_method": {"playwright": rd.__dict__ if rd else {}},
                        "path": dest_path,
                        "candidates": candidates,
                        "confidence": 1.0,
                        "method_used": "playwright"
                    }
                    st.session_state.last_processed_batch.append({
                        "filename": filename + ".html",
                        "type": "Receipt (Web)",
                        "status": "✅ Fetched via Playwright",
                        "confidence": 1.0
                    })
                st.success(f"Scraped and parsed web page from `{web_url}`", icon="✅")
            except Exception as e:
                st.error(f"Failed to scrape URL: {e}")
    st.rerun()

# ---------------------------------------------------------------------------
# Section 2: Uploads Review / Match Cards Inbox
# ---------------------------------------------------------------------------
# Last Batch Summary UI
if st.session_state.last_processed_batch:
    st.divider()
    st.subheader("📊 Last Upload Batch Summary")
    summary_df = pd.DataFrame(st.session_state.last_processed_batch)
    summary_df["confidence"] = summary_df["confidence"].map(lambda x: f"{x * 100:.0f}%")
    summary_df.columns = ["File Name", "Type", "Extraction Status", "OCR Confidence"]
    st.dataframe(summary_df, hide_index=True, use_container_width=True)
    
    col_c1, col_c2 = st.columns([1, 6])
    if col_c1.button("Clear Summary Table", key="btn_clear_batch_summary"):
        st.session_state.last_processed_batch = []
        st.rerun()

st.divider()
st.subheader("📥 Review Inbox")

if not st.session_state.pending_imports:
    st.info("No pending documents to review. Drag & drop files above to start!")
else:
    statements = {k: v for k, v in st.session_state.pending_imports.items() if v["type"] == "statement"}
    receipts = {k: v for k, v in st.session_state.pending_imports.items() if v["type"] == "receipt"}
    
    st.caption(f"You have **{len(st.session_state.pending_imports)}** pending documents to review.")
    
    tab_stmt, tab_rcpt = st.tabs([
        f"📄 Statements ({len(statements)})",
        f"📎 Receipts ({len(receipts)})"
    ])
    
    with tab_stmt:
        if not statements:
            st.info("No pending credit card statements to review.")
        else:
            for import_id, item in list(statements.items()):
                with st.container(border=True):
                    st.markdown(f"### 📄 Statement: `{item['filename']}`")
                    st.caption(f"Method: `{item['method_used']}` | Confidence: `{item['confidence'] * 100:.0f}%`")
                    
                    # Display Method Performance Metrics
                    st.markdown("##### ⚙️ Extraction Method Performance")
                    by_method = item.get("by_method", {})
                    method_status = item.get("method_status", {})
                    from core.extraction import METHOD_LABELS
                    _STATE_ICON = {"ok": "🟢", "empty": "🟡", "unavailable": "🔴"}
                    cols_m = st.columns(len(by_method) if by_method else 1)
                    for idx, (m, m_rows) in enumerate(by_method.items()):
                        count = len(m_rows) if isinstance(m_rows, list) else (0 if m_rows is None else 1)
                        stt = method_status.get(m, {})
                        state = stt.get("state", "ok" if count else "empty")
                        icon = _STATE_ICON.get(state, "")
                        cols_m[idx].metric(
                            label=f"{icon} {METHOD_LABELS.get(m, m)}",
                            value=(f"{count} transactions" if state != "unavailable" else "unavailable"),
                        )
                    # Explain any method that could not run, so a silent skip is never mistaken for "found nothing"
                    for m, stt in method_status.items():
                        if stt.get("state") == "unavailable":
                            st.warning(f"**{stt.get('label', m)} did not run.** {stt.get('detail', '')}", icon="🔴")
                        
                    st.markdown("##### 📋 Extracted Transactions Preview")
                    rows_df = pd.DataFrame(item["rows"])
                    if not rows_df.empty:
                        st.dataframe(rows_df[["date", "vendor", "amount_gross"]], hide_index=True, use_container_width=True)
                    else:
                        st.warning("No transactions extracted from this statement.")
                    
                    col_btn1, col_btn2 = st.columns([1, 4])
                    if col_btn1.button("📥 Import All Transactions", key=f"btn_import_stmt_{import_id}", type="primary"):
                        res = import_statement_rows(item["rows"], filename=item["filename"], province=province)
                        
                        # Build method performance breakdown text
                        from core.extraction import METHOD_LABELS
                        breakdown_lines = []
                        for m, m_rows in item.get("by_method", {}).items():
                            m_label = METHOD_LABELS.get(m, m)
                            m_count = len(m_rows) if isinstance(m_rows, list) else 0
                            breakdown_lines.append(f"  * **{m_label}**: Extracted {m_count} transactions")
                        breakdown_str = "\n".join(breakdown_lines)
                        
                        st.session_state.import_success_msg = (
                            f"**Statement `{item['filename']}` Imported Successfully!**\n\n"
                            f"* 🔍 **Total Detected:** {res['total_rows']} transactions (${res['detected_sum']:,.2f} gross)\n"
                            f"* ✨ **New Transactions:** {res['new_records']} added (${res['new_sum']:,.2f} gross)\n"
                            f"* 📋 **Needing Triage:** {res['new_records']} (added as unverified)\n"
                            f"* ⏭️ **Skipped (Duplicates):** {res['skipped']} already in database\n\n"
                            f"**Method Performance Breakdown:**\n"
                            f"{breakdown_str}"
                        )
                        del st.session_state.pending_imports[import_id]
                        st.rerun()
                    if col_btn2.button("Dismiss", key=f"btn_dismiss_stmt_{import_id}"):
                        del st.session_state.pending_imports[import_id]
                        st.rerun()

    with tab_rcpt:
        if not receipts:
            st.info("No pending receipts to review.")
        else:
            for import_id, item in list(receipts.items()):
                with st.container(border=True):
                    # Receipt Match Card
                    rd = item["receipt"]
                    path = item["path"]
                
                    st.markdown(f"### 📎 Receipt: `{item['filename']}`")
                    
                    card_confidence = item.get("confidence", 1.0)
                    if card_confidence < 0.50:
                        st.error("⚠️ **OCR struggled with this receipt** (Low Confidence scan). The extracted vendor, date, or total may be incorrect. Please review the highlights, parsed breakdown table, or use Gemini to correct it.", icon="⚠️")
                
                    col_preview, col_match = st.columns([1, 2], gap="large")
                
                    with col_preview:
                        # Document rendering with highlighted extraction bounding boxes
                        preview_bytes = None
                        from core.ocr import draw_highlights_on_image
                    
                        # Highlight extracted info
                        vendor_val = rd.vendor if rd else ""
                        date_val = rd.date if rd else ""
                        total_val = rd.total if (rd and rd.total is not None) else None
                        gst_hst_val = (rd.tax_hst or 0.0) + (rd.tax_gst or 0.0) if rd else None
                    
                        preview_bytes = draw_highlights_on_image(
                            path, 
                            vendor=vendor_val, 
                            date=date_val, 
                            total=total_val, 
                            tax=gst_hst_val
                        )
                    
                        # Fallback to standard preview if drawing highlights failed or returned None
                        if not preview_bytes:
                            if path.suffix.lower() == ".pdf":
                                preview_bytes = render_pdf_preview(path)
                            else:
                                preview_bytes = render_image_preview(path)
                    
                        if preview_bytes:
                            st.image(preview_bytes, width="stretch")
                        else:
                            st.caption("📄 No preview available")
                    
                        # Local path / Folder Link
                        abs_uri = Path(path).resolve().as_uri()
                        st.markdown(f"📂 [Reveal file in Explorer]({abs_uri})")
                
                    with col_match:
                        # Display warning if OCR struggled (Bug 1 & 6)
                        card_confidence = item.get("confidence", 1.0)
                        total_missing = (rd.total is None or rd.total == 0.0) if rd else True
                        date_missing = (not rd.date or rd.date == "Unknown") if rd else True
                        vendor_missing = (not rd.vendor or rd.vendor == "Unknown") if rd else True
                        
                        if card_confidence < 0.50 or total_missing or date_missing or vendor_missing:
                            st.error("⚠️ **OCR struggled with this receipt** (Low Confidence or missing fields). The extracted vendor, date, or total may be incorrect or missing. Please review and edit the details below.", icon="⚠️")
                        
                        # Checkbox to toggle editing/override of details
                        edit_override = st.checkbox("✏️ Override Extracted Info", key=f"override_{import_id}", value=total_missing)
                        
                        if edit_override:
                            st.markdown("**Edit Extracted Details:**")
                            col_ed1, col_ed2 = st.columns(2)
                            with col_ed1:
                                vendor_val = st.text_input("Vendor", value=rd.vendor if rd and rd.vendor else "", key=f"ed_vendor_{import_id}")
                                date_val = st.text_input("Date (YYYY-MM-DD)", value=rd.date if rd and rd.date else "", key=f"ed_date_{import_id}")
                            with col_ed2:
                                total_val = st.number_input("Total Amount ($)", min_value=0.0, value=float(rd.total) if rd and rd.total is not None else 0.0, step=0.01, format="%.2f", key=f"ed_total_{import_id}")
                                gst_hst_val = st.number_input("GST/HST Tax ($)", min_value=0.0, value=float((rd.tax_hst or 0.0) + (rd.tax_gst or 0.0)) if rd else 0.0, step=0.01, format="%.2f", key=f"ed_tax_{import_id}")
                        else:
                            # Display extracted details as metrics
                            vendor_val = rd.vendor if rd else "Unknown"
                            date_val = rd.date if rd else "Unknown"
                            total_val = rd.total if (rd and rd.total is not None) else 0.0
                            gst_hst_val = (rd.tax_hst or 0.0) + (rd.tax_gst or 0.0) if rd else 0.0
                        
                            st.markdown(f"**Extracted Info:**")
                            c1, c2, c3, c4 = st.columns(4)
                            c1.metric("Vendor", vendor_val)
                            c2.metric("Date", date_val)
                            c3.metric("Total", f"${total_val:,.2f}")
                            c4.metric("GST/HST", f"${gst_hst_val:,.2f}")
                    
                        # Line items breakdown
                        if rd and rd.line_items:
                            with st.expander("🔍 Show parsed line items breakdown"):
                                items_df = pd.DataFrame(rd.line_items)
                                if not items_df.empty:
                                    cols_to_show = [col for col in ["code", "description", "amount"] if col in items_df.columns]
                                    items_df_clean = items_df[cols_to_show].copy()
                                    items_df_clean.columns = [col.title() for col in cols_to_show]
                                    if "Amount" in items_df_clean.columns:
                                        items_df_clean["Amount"] = items_df_clean["Amount"].map(lambda x: f"${x:,.2f}" if isinstance(x, (int, float)) else str(x))
                                    st.dataframe(items_df_clean, hide_index=True, use_container_width=True)
                                    
                                    sum_eq = " + ".join([f"${i['amount']:,.2f}" for i in rd.line_items])
                                    st.caption(f"**Sum Equation:** {sum_eq} = **${total_val:,.2f}**")
                                    
                        # Extraction warning flags
                        if item.get("flags"):
                            for f in item["flags"]:
                                st.warning(f"⚠️ {f}")
                    
                        # Match recommendation options
                        candidates = item["candidates"]
                        st.markdown("**Suggested Matches from Transactions Ledger:**")
                    
                        if not candidates:
                            st.info("No close matching transaction found in ledger. You can create a new transaction below.")
                            selected_tx_id = None
                        else:
                            options = {}
                            for cand in candidates:
                                label = f"📅 {cand['date']} | 🏢 {cand['vendor']} | 💵 ${cand['amount_gross']:,.2f} (Match Score: {cand['score']}/100)"
                                options[cand["transaction_id"]] = label
                        
                            selected_tx_id = st.radio(
                                "Link to this transaction:",
                                options=list(options.keys()),
                                format_func=lambda tid: options[tid],
                                key=f"match_radio_{import_id}"
                            )
                    
                        # Actions
                        a1, a2, a3 = st.columns(3)
                    
                        # Link & Approve
                        link_disabled = not selected_tx_id
                        if a1.button("🔗 Link & Approve", key=f"btn_link_{import_id}", type="primary", disabled=link_disabled):
                            # Link receipt to transaction in DB
                            tx_id = selected_tx_id
                            tx_df = get_all_transactions()
                            tx = tx_df[tx_df["transaction_id"] == tx_id].iloc[0].to_dict()
                        
                            update_transaction(tx_id, {
                                "receipt_path": str(path),
                                "raw_receipt_text": rd.raw_text if rd else "",
                                "gst_hst_amount": gst_hst_val if gst_hst_val > 0 else float(tx.get("gst_hst_amount") or 0.0),
                                "is_business": bool(tx.get("is_business", True)),
                                "business_percentage": float(tx.get("business_percentage") or 1.0),
                                "cra_line": tx.get("cra_line"),
                                "cra_description": tx.get("cra_description"),
                                "audit_flags": [],
                                "verified_status": True,  # User confirmed match
                                "notes": tx.get("notes") or "",
                                "amount_net": float(tx["amount_gross"]) - (gst_hst_val if gst_hst_val > 0 else float(tx.get("gst_hst_amount") or 0.0)),
                            })
                            st.success("Receipt linked & verified!", icon="✅")
                            del st.session_state.pending_imports[import_id]
                            st.rerun()
                        
                        # Create New Transaction
                        create_disabled = (total_val == 0.0)
                        if a2.button("➕ Create New Transaction", key=f"btn_create_{import_id}", disabled=create_disabled, help="Disabled if total amount is $0.00. Override total to enable."):
                            tx_id = make_tx_id(date_val, vendor_val, total_val)
                            gst_hst = gst_hst_val if gst_hst_val > 0 else estimate_tax(total_val, province)
                            amount_net = calculate_net(total_val, gst_hst)
                        
                            from core.categorization import auto_categorize as ac
                            cra_line, cra_desc = ac(vendor_val)
                        
                            upsert_transaction({
                                "transaction_id": tx_id,
                                "date": date_val if date_val and date_val != "Unknown" else datetime.now().strftime("%Y-%m-%d"),
                                "vendor": normalize_vendor(vendor_val),
                                "amount_gross": total_val,
                                "amount_net": amount_net,
                                "gst_hst_amount": gst_hst,
                                "is_business": True,
                                "business_percentage": 1.0,
                                "cra_line": cra_line,
                                "cra_description": cra_desc,
                                "receipt_path": str(path),
                                "raw_receipt_text": rd.raw_text if rd else "",
                                "audit_flags": [],
                                "verified_status": True,  # immediately verified
                                "notes": "Created from receipt upload",
                                "import_source": item["filename"],
                                "extraction_method": item["method_used"],
                                "extraction_confidence": item["confidence"],
                                "extraction_details": item["by_method"]
                            })
                            st.success("New transaction created and linked!", icon="✅")
                            del st.session_state.pending_imports[import_id]
                            st.rerun()
                        
                        # Skip
                        if a3.button("Dismiss / Match Later", key=f"btn_skip_{import_id}"):
                            del st.session_state.pending_imports[import_id]
                            st.rerun()
                    
                        # Expandable Provenance Compare Table
                        with st.expander("🔍 Compare Extraction Methods"):
                            by_method = item["by_method"]
                            comp_rows = []
                        
                            # Populate comparison fields
                            for field_name in ["vendor", "date", "total", "tax"]:
                                row_dict = {"Field": field_name.capitalize()}
                                for m in ["deterministic", "ollama", "cloud", "gemini", "playwright"]:
                                    if m not in by_method:
                                        row_dict[METHOD_LABELS.get(m, m)] = "—"
                                        continue
                                    m_data = by_method.get(m)
                                    if m_data is None:
                                        row_dict[METHOD_LABELS.get(m, m)] = "⚠️ Failed"
                                        continue
                                
                                    # Resolve value
                                    if m == "playwright":
                                        val = m_data.get(field_name) if isinstance(m_data, dict) else getattr(m_data, field_name, "—")
                                    elif isinstance(m_data, ReceiptData):
                                        val = getattr(m_data, field_name, "—")
                                    elif isinstance(m_data, dict):
                                        val = m_data.get(field_name, "—")
                                    else:
                                        val = "—"
                                    
                                    if field_name == "total" or field_name == "tax":
                                        if field_name == "tax":  # tax special check
                                            if isinstance(m_data, ReceiptData):
                                                val = (m_data.tax_hst or 0.0) + (m_data.tax_gst or 0.0)
                                            elif isinstance(m_data, dict):
                                                val = (m_data.get("tax_hst") or 0.0) + (m_data.get("tax_gst") or 0.0) or (m_data.get("hst") or 0.0) + (m_data.get("gst") or 0.0)
                                        try:
                                            if val is not None and val != "—":
                                                row_dict[METHOD_LABELS.get(m, m)] = f"${float(val):,.2f}"
                                            else:
                                                row_dict[METHOD_LABELS.get(m, m)] = "—"
                                        except ValueError:
                                            row_dict[METHOD_LABELS.get(m, m)] = str(val)
                                    else:
                                        row_dict[METHOD_LABELS.get(m, m)] = str(val) if val is not None else "—"
                            
                                # Final / Reconciled column
                                if field_name == "vendor":
                                    row_dict["Reconciled (Final)"] = vendor_val
                                elif field_name == "date":
                                    row_dict["Reconciled (Final)"] = date_val
                                elif field_name == "total":
                                    row_dict["Reconciled (Final)"] = f"${total_val:,.2f}"
                                elif field_name == "tax":
                                    row_dict["Reconciled (Final)"] = f"${gst_hst_val:,.2f}"
                                
                                comp_rows.append(row_dict)
                            
                            st.table(pd.DataFrame(comp_rows))

# ---------------------------------------------------------------------------
# Section 3: Batch utilities & Instructions
# ---------------------------------------------------------------------------
st.divider()
st.subheader("📂 Batch Indexing & Matching")
with st.expander("Show Batch Options"):
    st.markdown(
        "If you drop statement files directly into `statements/` or receipt files into `receipts/YYYY/MM/`, "
        "you can trigger batch operations here."
    )
    col1, col2, col3 = st.columns(3)
    with col1:
        import time
        if st.button("📄 Scan Folder Statements", use_container_width=True):
            progress_bar = st.progress(0, text="Scanning statements folder…")
            status_text = st.empty()
            
            stmt_files = []
            valid_exts = {".csv", ".pdf", ".jpg", ".jpeg", ".png"}
            for p in STATEMENTS_DIR.glob("**/*"):
                if p.is_file() and p.suffix.lower() in valid_exts:
                    stmt_files.append(p)
            
            imported_count = 0
            pending_count = 0
            skipped_count = 0
            
            total_files = len(stmt_files)
            for idx, p in enumerate(stmt_files):
                filename = p.name
                file_key = f"{filename}_{p.stat().st_size}"
                
                # Check if already processed or already in pending imports
                already_pending = any(item.get("filename") == filename for item in st.session_state.pending_imports.values())
                if file_key in st.session_state.processed_files or already_pending:
                    skipped_count += 1
                    continue
                
                pct = int((idx + 1) / total_files * 100)
                progress_bar.progress(pct / 100, text=f"Processing {idx+1}/{total_files}: {filename}")
                status_text.text(f"Processing: {filename}")
                
                try:
                    if p.suffix.lower() == ".csv":
                        # Direct import CSV
                        import io
                        with open(p, "rb") as f:
                            res = import_csv(io.BytesIO(f.read()), filename=filename, province=province)
                        st.session_state.processed_files.add(file_key)
                        imported_count += 1
                    else:
                        # PDF/Image statement OCR
                        from core.extraction import available_methods
                        methods = available_methods(check_enabled=True)
                        from core.ingestion import extract_statement
                        res = extract_statement(p, filename=filename, default_year=default_year)
                        
                        import_id = f"file_{int(time.time())}_{idx}"
                        st.session_state.pending_imports[import_id] = {
                            "type": "statement",
                            "filename": filename,
                            "path": str(p),
                            "rows": res.rows,
                            "by_method": res.by_method,
                            "method_used": res.method_used,
                            "confidence": res.confidence
                        }
                        st.session_state.processed_files.add(file_key)
                        pending_count += 1
                except Exception as e:
                    st.error(f"Failed to process statement `{filename}`: {e}")
            
            progress_bar.empty()
            status_text.empty()
            st.success(
                f"Scan complete! Imported **{imported_count}** CSVs, "
                f"added **{pending_count}** statements to review queue, "
                f"skipped **{skipped_count}** duplicates.",
                icon="✅"
            )
            st.rerun()

    with col2:
        if st.button("🔍 Scan Folder Receipts (Batch OCR)", use_container_width=True):
            progress_bar = st.progress(0, text="Starting folder scan…")
            status_text = st.empty()
            
            def on_progress(current, total, name):
                pct = int(current / total * 100) if total else 0
                progress_bar.progress(pct / 100, text=f"{current}/{total}: {name}")
                status_text.text(f"Processing: {name}")

            scan_result = index_receipts(RECEIPTS_DIR, progress_cb=on_progress)
            progress_bar.empty()
            status_text.empty()
            st.success(f"Scanned receipts: Indexed **{scan_result['new_count']}** new files. Flagged **{scan_result['flagged_count']}**.", icon="✅")
            
    with col2:
        if st.button("🔗 Run Batch Matching Engine", use_container_width=True):
            with st.spinner("Matching receipts to transactions…"):
                result = match_all_transactions(province=province)
            st.success(
                f"Matched **{result['matched']}** transactions. "
                f"**{result['unmatched']}** still need manual receipt assignment.",
                icon="✅"
            )
