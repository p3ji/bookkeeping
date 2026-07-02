"""Settings page — province, business info, export controls."""
import streamlit as st
import pandas as pd

st.set_page_config(page_title="Settings", page_icon="⚙️", layout="wide")

from core.database import init_db, get_setting, save_setting, get_receipt_stats
from core.export import export_all_verified
from core.ocr import ocr_capabilities
from core.playwright_extractor import is_playwright_available
from config import TAX_RATES, EXPORTS_DIR, RECEIPTS_DIR, STATEMENTS_DIR

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

# OCR Quality Diagnostics
st.subheader("🔍 OCR Quality & Diagnostics")
stats = get_receipt_stats()
if stats["total"] == 0:
    st.caption("No receipts indexed in the database yet.")
else:
    c1, c2, c3 = st.columns(3)
    c1.metric("Total Indexed Receipts", stats["total"])
    c2.metric("Average OCR Confidence", f"{stats['avg_confidence'] * 100:.1f}%")
    c3.metric("Low Confidence (<50%)", stats["low_confidence_count"],
              delta="review needed" if stats["low_confidence_count"] > 0 else None,
              delta_color="inverse")
    
    if stats["low_confidence_count"] > 0:
        with st.expander("📋 View low-confidence receipts needing review"):
            st.warning("The following receipts had low-confidence OCR scores. They may be blurry, low-light, or have complex layouts. Consider using Gemini to re-extract them or manually editing them in Triage.")
            low_conf_df = pd.DataFrame(stats["low_confidence_list"])
            # Format columns
            low_conf_df["confidence"] = low_conf_df["confidence"].map(lambda x: f"{x * 100:.0f}%")
            low_conf_df["amount"] = low_conf_df["amount"].map(lambda x: f"${x:,.2f}")
            # Rename for display
            low_conf_df.columns = ["File Path", "Extracted Vendor", "Extracted Amount", "Confidence Score"]
            st.dataframe(low_conf_df, hide_index=True, use_container_width=True)

st.divider()

# ---------------------------------------------------------------------------
# Local Offline LLM (Ollama)
# ---------------------------------------------------------------------------
st.header("Local LLM (Ollama)")
st.caption(
    "A local Ollama vision model (like `llama3.2-vision` or `llava`) can be used for "
    "100% private, offline LLM-assisted transaction extraction."
)

# Ollama package status
try:
    import ollama
    _ollama_pkg = True
except ImportError:
    _ollama_pkg = False

_ollama_server = False
_ollama_model = None

if _ollama_pkg:
    try:
        from core.llm_extractor import is_ollama_server_running, ollama_vision_model
        _ollama_server = is_ollama_server_running()
        if _ollama_server:
            _ollama_model = ollama_vision_model()
    except Exception:
        _ollama_server = False

oc1, oc2, oc3 = st.columns([1, 2, 3])
oc1.markdown(f"{'✅' if _ollama_pkg else '❌'} **ollama python package**")
oc2.caption("Ollama library integration")
if not _ollama_pkg:
    oc3.caption("To install: `pip install ollama`")

oc1, oc2, oc3 = st.columns([1, 2, 3])
oc1.markdown(f"{'✅' if _ollama_server else '❌'} **Ollama Local Server**")
oc2.caption("Ollama service running on localhost")
if not _ollama_server:
    oc3.caption("Start the Ollama app on your machine (download from ollama.com)")

oc1, oc2, oc3 = st.columns([1, 2, 3])
oc1.markdown(f"{'✅' if _ollama_model else '❌'} **Vision Model**")
oc2.caption(f"Vision model found: `{_ollama_model or 'None'}`")
if not _ollama_model:
    oc3.caption("Run: `ollama pull llama3.2-vision` in your command prompt")

ollama_enabled = st.checkbox(
    "Enable Local LLM (Ollama) extraction",
    value=get_setting("ollama_enabled", "true") == "true",
    disabled=not (_ollama_server and _ollama_model is not None),
    help="Toggle whether to use the local Ollama LLM cascade for extraction.",
)
if ollama_enabled != (get_setting("ollama_enabled", "true") == "true"):
    save_setting("ollama_enabled", "true" if ollama_enabled else "false")
    st.rerun()

st.divider()

# ---------------------------------------------------------------------------
# Cloud LLM (optional, opt-in)
# ---------------------------------------------------------------------------
st.header("Cloud LLM (optional)")
st.caption(
    "Cloud LLM options are second, opt-in fallbacks for documents the local model "
    "or deterministic OCR still can't read."
)

from core.llm_extractor import is_cloud_llm_available, is_gemini_available
import os as _os

# Claude Status
_has_key = bool(_os.environ.get("ANTHROPIC_API_KEY"))
try:
    import anthropic  # noqa: F401
    _has_pkg = True
except ImportError:
    _has_pkg = False

st.subheader("Claude (Anthropic)")
cc1, cc2, cc3 = st.columns([1, 2, 3])
cc1.markdown(f"{'✅' if _has_pkg else '❌'} **anthropic package**")
cc2.caption("Claude vision extraction library")
if not _has_pkg:
    cc3.caption("To enable: `pip install anthropic`")

cc1, cc2, cc3 = st.columns([1, 2, 3])
cc1.markdown(f"{'✅' if _has_key else '❌'} **ANTHROPIC_API_KEY**")
cc2.caption("API key, read from the environment — never stored in this app's database")
if not _has_key:
    cc3.caption("Set the `ANTHROPIC_API_KEY` environment variable, then restart the app")

cloud_enabled = st.checkbox(
    "Enable Cloud LLM (Claude) as an extraction method",
    value=get_setting("cloud_llm_enabled", "false") == "true",
    disabled=not is_cloud_llm_available(),
    help="Off by default. Turning this on lets you select Cloud LLM when importing "
         "statements or scanning receipts. Enabling it means document images you choose "
         "to process this way are sent to Anthropic's API for that extraction.",
)
if cloud_enabled != (get_setting("cloud_llm_enabled", "false") == "true"):
    save_setting("cloud_llm_enabled", "true" if cloud_enabled else "false")
    st.rerun()

st.write("")

# Gemini Status
_has_gemini_key = bool(_os.environ.get("GEMINI_API_KEY"))
st.subheader("Gemini (Google AI Studio)")
gc1, gc2, gc3 = st.columns([1, 2, 3])
gc1.markdown(f"{'✅' if _has_gemini_key else '❌'} **GEMINI_API_KEY**")
gc2.caption("API key, read from the environment — never stored in this app's database")
if not _has_gemini_key:
    gc3.caption("Set the `GEMINI_API_KEY` environment variable, then restart the app")

gemini_enabled = st.checkbox(
    "Enable Cloud LLM (Gemini) as an extraction method",
    value=get_setting("gemini_llm_enabled", "false") == "true",
    disabled=not is_gemini_available(),
    help="Off by default. Turning this on lets the application use Google Gemini API "
         "as a fallback or comparison extraction method.",
)
if gemini_enabled != (get_setting("gemini_llm_enabled", "false") == "true"):
    save_setting("gemini_llm_enabled", "true" if gemini_enabled else "false")
    st.rerun()

st.divider()

# ---------------------------------------------------------------------------
# Folder paths
# ---------------------------------------------------------------------------
st.header("Data Folders")

info = {
    "Statements (`statements/`)": str(STATEMENTS_DIR),
    "Receipts (`receipts/`)": str(RECEIPTS_DIR),
    "Markdown Exports (`exports/`)": str(EXPORTS_DIR),
}
for label, path in info.items():
    st.text_input(label, value=path, disabled=True)

st.caption("These paths are relative to the app directory and are created automatically on first run.")
