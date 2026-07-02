"""Import page — CSV upload, statement OCR, web-based bill extraction."""
import io
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="Import", page_icon="📥", layout="wide")

from core.database import init_db, get_setting
from core.ingestion import import_csv, import_statement, import_statement_multi
from core.matching import index_receipts, match_all_transactions
from core.ocr import ocr_capabilities
from core.llm_extractor import INSTALL_INSTRUCTIONS
from core.extraction import available_methods, METHOD_LABELS
from core.playwright_extractor import is_playwright_available, extract_from_url, extract_from_html_file
from config import IMPORTS_DIR, RECEIPTS_DIR

init_db()

st.title("📥 Import")

province = get_setting("province", "ON")
ocr = ocr_capabilities()
_pw_ok = is_playwright_available()

# ---------------------------------------------------------------------------
# Section 1: Credit Card Statement
# ---------------------------------------------------------------------------
st.header("1. Credit Card Statement")
st.caption("Upload a CSV export, drop a PDF/image, or extract from a web-based bill page.")

tab_upload, tab_folder, tab_statement, tab_web = st.tabs(
    ["Upload CSV", "From imports/ Folder", "Statement PDF / Image", "From URL / HTML (Playwright)"]
)

with tab_upload:
    uploaded = st.file_uploader(
        "Choose a CSV file", type=["csv"], key="csv_upload",
        help="Export from your bank website (Capital One / Costco Mastercard portal)",
    )
    if uploaded and st.button("Import Statement", key="btn_import_upload", type="primary"):
        with st.spinner("Parsing and saving transactions…"):
            try:
                result = import_csv(uploaded.read(), filename=uploaded.name, province=province)
                st.success(
                    f"Imported **{result['new_records']}** transactions "
                    f"from `{result['filename']}` ({result['total_rows']} rows parsed).",
                    icon="✅",
                )
            except Exception as e:
                st.error(f"Import failed: {e}", icon="🚨")

with tab_folder:
    csv_files = list(IMPORTS_DIR.glob("*.csv"))
    if not csv_files:
        st.info(f"No CSV files found in `{IMPORTS_DIR}`. Drop files there and refresh.")
    else:
        chosen = st.selectbox("Select file", [f.name for f in csv_files])
        if st.button("Import Selected", key="btn_import_folder", type="primary"):
            fp = IMPORTS_DIR / chosen
            with st.spinner("Importing…"):
                try:
                    result = import_csv(fp, filename=chosen, province=province)
                    st.success(
                        f"Imported **{result['new_records']}** transactions from `{chosen}`.",
                        icon="✅",
                    )
                except Exception as e:
                    st.error(f"Import failed: {e}", icon="🚨")

with tab_statement:
    _stmt_methods_avail = available_methods()

    if len(_stmt_methods_avail) == 1:
        with st.expander("💡 Better extraction available — click to learn more"):
            st.markdown(INSTALL_INSTRUCTIONS)

    stmt_methods = st.multiselect(
        "Extraction method(s) — select more than one to cross-check results",
        options=_stmt_methods_avail,
        default=["deterministic"],
        format_func=lambda m: METHOD_LABELS.get(m, m),
        key="stmt_methods",
    )

    st.markdown(
        "Upload a credit card statement as a **PDF, JPEG, or PNG**. "
        "For **digital PDFs** (downloaded from your bank portal), Deterministic (OCR/regex) "
        "works great. For **phone photos** of paper statements, an LLM method gives much "
        "better results. Selecting more than one method cross-checks them: agreeing "
        "transactions import normally, disagreements are flagged for review on Triage."
    )

    if (not ocr["pdfplumber"] and not ocr["tesseract"]
            and "ollama" not in _stmt_methods_avail and "cloud" not in _stmt_methods_avail):
        st.error("No extraction libraries available. Install pdfplumber, Tesseract, or Ollama.", icon="🚨")
    elif not stmt_methods:
        st.info("Select at least one extraction method above.")
    else:
        stmt_file = st.file_uploader(
            "Upload statement",
            type=["pdf", "jpg", "jpeg", "png", "tif", "tiff"],
            key="stmt_upload",
        )
        from datetime import date as _today
        stmt_year = st.number_input(
            "Statement year (if not printed on statement)",
            min_value=2015, max_value=_today.today().year + 1,
            value=_today.today().year,
            step=1,
        )
        if stmt_file and st.button("Parse & Import Statement", key="btn_stmt", type="primary"):
            if stmt_methods == ["deterministic"]:
                with st.spinner("Extracting transactions via OCR — this may take a moment…"):
                    try:
                        result = import_statement(
                            stmt_file.read(),
                            filename=stmt_file.name,
                            province=province,
                            default_year=int(stmt_year),
                        )
                        st.success(
                            f"Imported **{result['new_records']}** transactions "
                            f"from `{stmt_file.name}` ({result['total_rows']} found).",
                            icon="✅",
                        )
                        if result.get("preview"):
                            st.markdown("**Preview (first 10):**")
                            st.dataframe(result["preview"], hide_index=True)
                    except Exception as e:
                        msg = str(e)
                        if "\n\nRaw extracted text" in msg:
                            headline, _, raw = msg.partition("\n\nRaw extracted text")
                            st.error(headline, icon="🚨")
                            with st.expander("🔍 Raw extracted text (debug)"):
                                st.text(raw.replace("(first 3000 chars):\n", ""))
                        else:
                            st.error(f"Statement parsing failed: {msg}", icon="🚨")
                        st.caption(
                            "Tip: For best results with phone photos, select an LLM extraction "
                            "method above. For digital statements, export as CSV or PDF from "
                            "your bank portal."
                        )
            else:
                label = " + ".join(METHOD_LABELS.get(m, m) for m in stmt_methods)
                with st.spinner(f"Extracting via {label} — may take up to 30 s…"):
                    try:
                        result = import_statement_multi(
                            stmt_file.read(),
                            filename=stmt_file.name,
                            province=province,
                            default_year=int(stmt_year),
                            methods=stmt_methods,
                        )
                        st.success(
                            f"Imported **{result['new_records']}** transactions "
                            f"from `{stmt_file.name}` via {label}.",
                            icon="✅",
                        )
                        if result.get("flagged_count"):
                            st.warning(
                                f"{result['flagged_count']} of {result['new_records']} "
                                f"transactions need review — extraction methods disagreed. "
                                f"See Triage.",
                                icon="⚠️",
                            )
                        if result.get("preview"):
                            st.markdown("**Preview (first 10):**")
                            st.dataframe(result["preview"], hide_index=True)
                    except Exception as e:
                        st.error(f"Statement parsing failed: {e}", icon="🚨")

with tab_web:
    st.markdown(
        "Extract transactions or bill data from **web-rendered pages** — online banking portals, "
        "telecom My Account bills (Rogers, Bell, Telus, Freedom Mobile), or saved HTML files. "
        "Playwright handles JavaScript-rendered content that plain URL fetching misses."
    )

    if not _pw_ok:
        st.error(
            "Playwright is not available. Run `pip install playwright` then "
            "`playwright install chromium`.",
            icon="🚨",
        )
    else:
        web_mode = st.radio(
            "Source",
            ["🌐 URL (online portal)", "📄 Local HTML file"],
            horizontal=True,
            key="web_mode",
        )

        if "URL" in web_mode:
            web_url = st.text_input(
                "Bill or statement URL",
                placeholder="https://myaccount.freedommobile.ca/billing/current",
                key="web_url",
            )
            source_label = web_url
            use_local = False
        else:
            html_files = list(IMPORTS_DIR.glob("*.html")) + list(IMPORTS_DIR.glob("*.htm"))
            if html_files:
                chosen_html = st.selectbox(
                    "Select HTML file from imports/",
                    [f.name for f in html_files],
                    key="html_sel",
                )
                source_label = chosen_html
                use_local = True
            else:
                st.info(f"No HTML files found in `{IMPORTS_DIR}`. Drop a saved bill page there.")
                source_label = None
                use_local = False

        from datetime import date as _today2
        web_year = st.number_input(
            "Year (for statement transaction dates)",
            min_value=2015, max_value=_today2.today().year + 1,
            value=_today2.today().year, step=1, key="web_year",
        )

        if source_label and st.button("Extract & Import", key="btn_web", type="primary"):
            with st.spinner("Launching Playwright browser — this may take a few seconds…"):
                try:
                    if use_local:
                        result_pw = extract_from_html_file(
                            IMPORTS_DIR / chosen_html,
                            default_year=int(web_year),
                        )
                    else:
                        result_pw = extract_from_url(web_url, default_year=int(web_year))
                except Exception as exc:
                    st.error(f"Playwright extraction failed: {exc}", icon="🚨")
                    result_pw = None

            if result_pw:
                st.success(
                    f"Extracted `{result_pw['doc_type']}` document via Playwright.",
                    icon="✅",
                )

                rd = result_pw.get("receipt_data")
                txs = result_pw.get("transactions", [])

                if rd and rd.doc_type != "unknown":
                    import dataclasses
                    d = dataclasses.asdict(rd)
                    d.pop("raw_text", None)
                    items = d.pop("line_items", [])
                    st.markdown("**Extracted bill data:**")
                    col_a, col_b = st.columns(2)
                    col_a.metric("Vendor", rd.vendor or "—")
                    col_b.metric("Total Due", f"${rd.total:.2f}" if rd.total else "—")
                    col_a.metric("Date", rd.date or "—")
                    col_b.metric("HST", f"${rd.tax_hst:.2f}" if rd.tax_hst else "—")
                    if items:
                        st.dataframe(items, hide_index=True, use_container_width=True)

                    # Import the bill as a single transaction
                    if rd.total and rd.vendor:
                        if st.button("Import as transaction", key="btn_web_import"):
                            from core.ingestion import import_statement_rows
                            rows = [{"date": rd.date or str(_today2.today()),
                                     "vendor": rd.vendor,
                                     "amount_gross": rd.total}]
                            res = import_statement_rows(rows, filename=source_label, province=province)
                            st.success(
                                f"Imported **{res['new_records']}** transaction(s) from `{source_label}`.",
                                icon="✅",
                            )

                elif txs:
                    from core.ingestion import import_statement_rows
                    res = import_statement_rows(txs, filename=source_label, province=province)
                    st.success(
                        f"Imported **{res['new_records']}** transactions from `{source_label}`.",
                        icon="✅",
                    )
                    if res.get("preview"):
                        st.dataframe(res["preview"], hide_index=True)
                else:
                    st.warning("No structured data extracted. Check raw text below.", icon="⚠️")

                with st.expander("Raw extracted text"):
                    st.text(result_pw.get("raw_text", "")[:3000])

st.divider()

# ---------------------------------------------------------------------------
# Section 2: Receipt Scanning
# ---------------------------------------------------------------------------
st.header("2. Scan & Match Receipts")

cap_notes = []
if not ocr["pdfplumber"]:
    cap_notes.append("`pdfplumber` missing — digital PDF text extraction disabled")
if not ocr["pymupdf"]:
    cap_notes.append("`PyMuPDF` missing — PDF page rendering disabled")
if not ocr["tesseract"]:
    cap_notes.append(
        "`pytesseract` / Tesseract not found — scanned image OCR disabled. "
        "Install [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) and set "
        "`TESSERACT_PATH` environment variable."
    )

if cap_notes:
    with st.expander("⚠️ OCR Capabilities (click to expand)"):
        for note in cap_notes:
            st.warning(note)

st.markdown(
    "Place receipt files (PDF, JPEG, PNG) under `receipts/YYYY/MM/` then click **Scan**. "
    "The engine will OCR each file and automatically link it to the closest matching transaction."
)

receipt_files_total = len(list(RECEIPTS_DIR.rglob("*")))
st.caption(f"Receipts folder: `{RECEIPTS_DIR}` — {receipt_files_total} files found")

receipt_methods = st.multiselect(
    "Extraction method(s) for receipt scanning — select more than one to cross-check",
    options=available_methods(),
    default=["deterministic"],
    format_func=lambda m: METHOD_LABELS.get(m, m),
    key="receipt_methods",
)

col_scan, col_match = st.columns(2)

with col_scan:
    if st.button("🔍 Scan Receipts (OCR)", type="primary", disabled=not receipt_methods):
        progress_bar = st.progress(0, text="Starting…")
        status_text = st.empty()
        count_box = [0]

        def on_progress(current, total, name):
            count_box[0] = current
            pct = int(current / total * 100) if total else 0
            progress_bar.progress(pct / 100, text=f"{current}/{total}: {name}")
            status_text.text(f"Processing: {name}")

        scan_result = index_receipts(RECEIPTS_DIR, progress_cb=on_progress, methods=receipt_methods)
        progress_bar.empty()
        status_text.empty()
        st.success(f"Indexed **{scan_result['new_count']}** new receipt files.", icon="✅")
        if scan_result.get("flagged_count"):
            st.warning(
                f"{scan_result['flagged_count']} receipt(s) had low-confidence or "
                f"disagreeing extraction — worth a manual look before relying on "
                f"auto-matching.",
                icon="⚠️",
            )

with col_match:
    if st.button("🔗 Match Receipts to Transactions"):
        with st.spinner("Running matching engine…"):
            result = match_all_transactions(province=province)
        st.success(
            f"Matched **{result['matched']}** transactions. "
            f"**{result['unmatched']}** still need manual receipt assignment.",
            icon="✅",
        )

st.divider()

# ---------------------------------------------------------------------------
# Section 3: Drop zone instructions
# ---------------------------------------------------------------------------
with st.expander("📂 How to organise receipts"):
    st.markdown(f"""
**Recommended folder structure:**
```
receipts/
├── 2024/
│   ├── 01/    ← January 2024 receipts
│   │   ├── rogers_jan2024.pdf
│   │   └── costco_0115.jpg
│   └── 02/
│       └── ...
└── 2025/
    └── ...
```

The matching engine searches ±1 month from the transaction date, so exact folder placement is not required but helps performance.

**Supported formats:** PDF (text or scanned), JPEG, PNG, TIFF, BMP
""")
