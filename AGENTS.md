# AGENTS.md — Bookkeeping App

Context file for AI coding agents (Claude Code, Copilot, Cursor, etc.).
Read this before making changes to the codebase.

---

## What this app does

Local-first, fully offline bookkeeping tool for a Canadian family business.
Ingests Costco Mastercard statements (CSV **or** PDF/image via OCR), matches
transactions to receipt files, classifies them to CRA Form T2125 lines, tracks
GST/HST Input Tax Credits, flags audit risks, and exports verified records to
Obsidian-compatible Markdown.

**No cloud services. No external APIs. Everything runs on localhost.**

---

## Tech stack

| Layer | Choice | Notes |
|---|---|---|
| UI | Streamlit 1.35+ | `python -m streamlit run app.py` |
| Database | DuckDB 1.x | Single file at `data/bookkeeping.duckdb` |
| PDF text | pdfplumber | Digital (text-based) PDFs |
| PDF render | PyMuPDF (fitz) | Renders pages to images for preview + OCR |
| OCR | Tesseract 5 + pytesseract | Scanned images/PDFs |
| Fuzzy match | thefuzz | Vendor name similarity scoring |
| Charts | Plotly | Dashboard bar/pie charts |

---

## Repository layout

```
bookkeeping/
├── app.py                  ← Streamlit home page; calls init_db() on startup
├── config.py               ← ALL paths, tax rates, CRA line codes — edit here first
├── requirements.txt
├── AGENTS.md               ← this file
│
├── core/
│   ├── database.py         ← DuckDB schema, CRUD, analytics queries
│   ├── ingestion.py        ← CSV parser + statement OCR parser + import pipeline
│   ├── ocr.py              ← text extraction (pdfplumber → PyMuPDF → Tesseract cascade)
│   ├── matching.py         ← receipt indexing + weighted transaction↔receipt matching
│   ├── tax.py              ← GST/HST regex extraction + embedded-tax estimation
│   ├── categorization.py   ← keyword → CRA T2125 line auto-categorization
│   ├── audit.py            ← CRA audit risk flag generation
│   └── export.py           ← Obsidian Markdown export with YAML frontmatter
│
├── pages/
│   ├── 1_Import.py         ← CSV upload | imports/ folder | Statement PDF/image
│   ├── 2_Triage.py         ← Full-width table + per-transaction classification form
│   ├── 3_Dashboard.py      ← Monthly bar chart, category pie, summary table
│   └── 4_Settings.py       ← Province/tax rate, Markdown export, OCR status
│
├── imports/                ← drop CSV or PDF statements here (git-ignored)
├── receipts/YYYY/MM/       ← drop receipt PDFs/images here (git-ignored)
├── exports/                ← verified Markdown records written here (*.md git-ignored)
└── data/                   ← DuckDB database (git-ignored)
```

---

## Key conventions

### Never
- **Never upload or provided any personal information (e.g. name, identifiers) to GitHub or anywhere over the internet**

### Database
- **Never call `current_timestamp` inside `UPDATE SET` or `ON CONFLICT DO UPDATE SET`** — DuckDB
  parses it as a column name. Use `now()` instead. `DEFAULT current_timestamp` in `CREATE TABLE`
  is fine.
- `audit_flags` column is stored as a JSON string (`TEXT`), not an array. Always
  `json.dumps(list)` before writing and `json.loads(str)` after reading.
- `upsert_transaction()` uses `ON CONFLICT DO NOTHING` — re-importing the same CSV is safe
  (idempotent). The unique key is an MD5 hash of `date|vendor|amount`.

### Ingestion
- `parse_csv()` handles Capital One / Costco MC, TD, Scotiabank, and generic formats.
  It auto-detects column names (case-insensitive). Extend `_detect_columns()` to add
  new bank formats — do **not** hard-code column indices.
- `_parse_amount()` always returns `abs(value)` by default. Pass `keep_sign=True` when
  using a single Amount column to distinguish charges (positive) from payments (negative).
- `parse_statement_file()` handles non-CSV statement formats (PDF, JPEG, PNG).
  It first tries pdfplumber table extraction, then falls back to line-by-line OCR text parsing.
- `_line_to_tx()` is the regex engine for statement text lines. When adding new date
  formats, add them to the `date_pats` list inside that function (ordered most-specific first).

### OCR pipeline (`core/ocr.py`)
Cascade: pdfplumber (native text) → PyMuPDF render → Tesseract. Each step is guarded
with `try/except` so missing libraries degrade gracefully rather than crashing.
`ocr_capabilities()` returns the live status dict — use it in UI to show warnings.

### Categorization
Rules in `core/categorization.py → _RULES` are checked in order; **first match wins**.
Add more-specific rules before less-specific ones. The categorization is a starting
point — users always override in Triage.

### CRA rules enforced in code
- **Meals & Entertainment (Line 8523):** `calculate_deductible()` automatically applies the
  50% CRA limit by halving `effective_pct`.
- **Audit flags** are regenerated every time a transaction is saved in Triage, so they
  always reflect the current category and proration.

### Tax rates
All rates are in `config.py → TAX_RATES`. Province is stored in `app_settings` table
and defaults to `ON` (Ontario, 13% HST). GST/HST amounts are *estimated* from gross
using the embedded-tax formula `gross × rate / (1 + rate)` and overridden when the
actual amount is extracted from receipt OCR text.

### Streamlit conventions
- Every page calls `init_db()` at the top — it's idempotent (`CREATE TABLE IF NOT EXISTS`).
- `st.set_page_config()` is called at the top of every page file.
- Use `st.rerun()` after any write operation so the UI reflects the saved state.
- Session state is not persisted across browser refreshes — all state lives in DuckDB.

---

## Data privacy — what is and is not committed

| Path | Committed? | Why |
|---|---|---|
| `data/` | **No** | Contains the DuckDB database with all financial records |
| `imports/` | **No** | Contains raw bank statement CSVs/PDFs |
| `receipts/` | **No** | Contains receipt images with personal purchase data |
| `exports/*.md` | **No** | Generated Markdown contains transaction amounts and vendors |
| `exports/.gitkeep` | Yes | Preserves the directory in git |
| `core/`, `pages/`, `app.py`, etc. | Yes | Source code only, no personal data |

**Never add personal data, API keys, or financial records to committed files.**

---

## Running the app

```bash
# Install dependencies
pip install -r requirements.txt

# Install Tesseract OCR (Windows)
# Download from https://github.com/UB-Mannheim/tesseract/wiki
# Default path: C:\Program Files\Tesseract-OCR\tesseract.exe

# Start
python -m streamlit run app.py
```

Or double-click `run.bat` on Windows.

---

## Common tasks for agents

### Add a new CRA T2125 line
1. Add to `config.py → CRA_LINES` dict
2. Optionally add keyword rules to `core/categorization.py → _RULES`

### Add a new bank CSV format
1. Add column name variants to `_detect_columns()` in `core/ingestion.py`
2. Test with a sample CSV via `parse_csv()`

### Add a new audit flag
1. Add a condition block to `core/audit.py → check_audit_flags()`
2. The function receives: vendor, amount_gross, cra_line, business_percentage, raw_text

### Extend statement parsing for a new layout
1. Add date format patterns to `_line_to_tx()` in `core/ingestion.py`
2. Or add a new `_extract_via_*()` function and call it in `parse_statement_file()`
   before the line-by-line fallback

### Add a new dashboard chart
- All data comes from `core/database.py` analytics functions
- Add a new query function there, then use Plotly in `pages/3_Dashboard.py`
