"""Import page — CSV upload and receipt scanning."""
import io
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="Import", page_icon="📥", layout="wide")

from core.database import init_db, get_setting
from core.ingestion import import_csv, import_statement
from core.matching import index_receipts, match_all_transactions
from core.ocr import ocr_capabilities
from config import IMPORTS_DIR, RECEIPTS_DIR

init_db()

st.title("📥 Import")

province = get_setting("province", "ON")
ocr = ocr_capabilities()

# ---------------------------------------------------------------------------
# Section 1: CSV Import
# ---------------------------------------------------------------------------
st.header("1. Credit Card Statement")
st.caption("Upload a Costco Mastercard (Capital One) CSV export, or drop the file into the `imports/` folder.")

tab_upload, tab_folder, tab_statement = st.tabs(
    ["Upload CSV", "From imports/ Folder", "Statement PDF / Image"]
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
    st.markdown(
        "Upload a credit card statement as a **PDF, JPEG, or PNG**. "
        "The app will OCR it and extract individual transactions automatically. "
        "Works on downloaded PDF statements and phone photos of paper statements."
    )
    if not ocr["pdfplumber"] and not ocr["tesseract"]:
        st.error("No OCR libraries available. Install pdfplumber and/or Tesseract.", icon="🚨")
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
                    st.error(f"Statement parsing failed: {e}", icon="🚨")
                    st.caption(
                        "Tip: If this is a scanned/photographed statement, ensure Tesseract is installed. "
                        "For best results, export your statement as CSV from the bank portal."
                    )

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

col_scan, col_match = st.columns(2)

with col_scan:
    if st.button("🔍 Scan Receipts (OCR)", type="primary"):
        progress_bar = st.progress(0, text="Starting…")
        status_text = st.empty()
        count_box = [0]

        def on_progress(current, total, name):
            count_box[0] = current
            pct = int(current / total * 100) if total else 0
            progress_bar.progress(pct / 100, text=f"{current}/{total}: {name}")
            status_text.text(f"Processing: {name}")

        new_indexed = index_receipts(RECEIPTS_DIR, progress_cb=on_progress)
        progress_bar.empty()
        status_text.empty()
        st.success(f"Indexed **{new_indexed}** new receipt files.", icon="✅")

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
