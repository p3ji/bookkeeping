"""Settings page — province, business info, export controls."""
import streamlit as st

st.set_page_config(page_title="Settings", page_icon="⚙️", layout="wide")

from core.database import init_db, get_setting, save_setting
from core.export import export_all_verified
from core.ocr import ocr_capabilities
from core.playwright_extractor import is_playwright_available
from config import TAX_RATES, EXPORTS_DIR, RECEIPTS_DIR, IMPORTS_DIR

init_db()

st.title("⚙️ Settings")

# ---------------------------------------------------------------------------
# Business Info
# ---------------------------------------------------------------------------
st.header("Business Information")

with st.form("settings_form"):
    col1, col2 = st.columns(2)
    with col1:
        province = st.selectbox(
            "Province / Territory",
            options=list(TAX_RATES.keys()),
            index=list(TAX_RATES.keys()).index(get_setting("province", "ON")),
            format_func=lambda p: f"{p} — {TAX_RATES[p]['name']} ({TAX_RATES[p]['rate']*100:.0f}%)",
        )
    with col2:
        fiscal_month = st.selectbox(
            "Fiscal Year Start Month",
            options=list(range(1, 13)),
            index=int(get_setting("fiscal_year_start", "1")) - 1,
            format_func=lambda m: [
                "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"
            ][m - 1],
        )

    if st.form_submit_button("💾 Save Settings", type="primary"):
        save_setting("province", province)
        save_setting("fiscal_year_start", str(fiscal_month))
        st.success("Settings saved.", icon="✅")
        st.rerun()

st.divider()

# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
st.header("Export Verified Records")
st.markdown(
    f"Export all verified business transactions as individual Markdown files "
    f"(Obsidian-compatible YAML frontmatter). Files are written to `{EXPORTS_DIR}`."
)

col_exp, col_stats = st.columns(2)
with col_exp:
    if st.button("📄 Export All Verified to Markdown", type="primary"):
        with st.spinner("Exporting…"):
            paths = export_all_verified(EXPORTS_DIR)
        st.success(f"Exported **{len(paths)}** files to `{EXPORTS_DIR}`.", icon="✅")
        for p in paths[:10]:
            st.caption(f"`{p.name}`")
        if len(paths) > 10:
            st.caption(f"…and {len(paths) - 10} more")

with col_stats:
    md_files = list(EXPORTS_DIR.glob("*.md"))
    st.metric("Previously Exported Records", len(md_files))

st.divider()

# ---------------------------------------------------------------------------
# OCR / Dependencies status
# ---------------------------------------------------------------------------
st.header("OCR & Dependency Status")

caps = ocr_capabilities()
rows = [
    ("pdfplumber", caps["pdfplumber"], "Digital PDF text extraction",
     "`pip install pdfplumber`"),
    ("PyMuPDF (fitz)", caps["pymupdf"], "PDF rendering & scanned PDF OCR",
     "`pip install PyMuPDF`"),
    ("Tesseract OCR", caps["tesseract"], "Image & scanned receipt OCR",
     "Install [Tesseract](https://github.com/UB-Mannheim/tesseract/wiki) then set `TESSERACT_PATH`"),
    ("Chinese OCR (chi_sim / chi_tra)", caps["chinese"],
     "Simplified & Traditional Chinese receipt text",
     "In Tesseract installer → Additional language data, tick **Chinese (Simplified)** and **Chinese (Traditional)**"),
    ("OpenCV", caps.get("opencv", False), "Image deskew & fine rotation correction",
     "`pip install opencv-python-headless`"),
    ("Playwright + Chromium", is_playwright_available(),
     "Web-based bill/statement extraction (telecom portals, online banking)",
     "`pip install playwright` then `playwright install chromium`"),
]

for name, ok, purpose, fix in rows:
    c1, c2, c3 = st.columns([1, 2, 3])
    c1.markdown(f"{'✅' if ok else '❌'} **{name}**")
    c2.caption(purpose)
    if not ok:
        c3.caption(f"To enable: {fix}")

if caps["tesseract"]:
    st.caption(f"Active OCR language string: `{caps['ocr_lang']}`")

st.divider()

# ---------------------------------------------------------------------------
# Folder paths
# ---------------------------------------------------------------------------
st.header("Data Folders")

info = {
    "CSV Imports (`imports/`)": str(IMPORTS_DIR),
    "Receipts (`receipts/`)": str(RECEIPTS_DIR),
    "Markdown Exports (`exports/`)": str(EXPORTS_DIR),
}
for label, path in info.items():
    st.text_input(label, value=path, disabled=True)

st.caption("These paths are relative to the app directory and are created automatically on first run.")
