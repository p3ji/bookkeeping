# User-Friendly Universal Ingestion, Provenance, & Preview Plan (Built on b4b89c2)

This plan simplifies the user interface, tracks extraction provenance, and displays **original document previews and local folder links** side-by-side with computer-extracted values to make manual validation effortless.

## User Review Required

> [!IMPORTANT]
> **Local File System Links**: Since this is a localhost-only Streamlit app, we can provide clickable `file:///` links to the local file system. This allows the user to click a link and immediately open/reveal the receipt file in their system's file explorer.

## Proposed Changes

---

### 1. Universal Drop Zone & Match Card UI (`pages/1_Import.py`)

- **Universal Drop Zone**: A single `st.file_uploader` for all document types (`["csv", "pdf", "jpg", "jpeg", "png", "html", "htm"]`).
- **Match Card Layout**: When a single receipt/bill is uploaded, it is automatically processed and a card is displayed with:
  - **Left Column: Source Preview**: A visual thumbnail of the uploaded image/PDF page (using the existing `render_image_preview` / `render_pdf_preview`) and a clickable folder link (e.g. `file:///C:/Users/.../receipts/2026/06/receipt.jpg`) to open the source file on the computer.
  - **Right Column: Match & Provenance**:
    - The suggested transaction match from the database.
    - Single-click actions (`Link & Approve`, `Create New Transaction`, `Skip`).
    - Expandable **"🔍 Compare Extraction Methods"** section showing the side-by-side table of Date, Vendor, Total, and Tax extracted by **Deterministic vs. Ollama vs. Cloud**.

---

### 2. Auto-Router & Cascade Fallback (`core/extraction.py`)

#### [MODIFY] [extraction.py](file:///C:/Users/pushp/Documents/Projects/bookkeeping/core/extraction.py)
- Refactor the extraction cascade to run methods automatically:
  1. Deterministic first.
  2. Fall back to Ollama and/or Cloud LLM if deterministic returns low confidence.
  3. Store the output of all attempted methods in the `by_method` dictionary.
- Return the full `by_method` structure in `ExtractionResult`.

---

### 3. Store Provenance Data (`core/database.py`)

#### [MODIFY] [database.py](file:///C:/Users/pushp/Documents/Projects/bookkeeping/core/database.py)
- Update `init_db()` to run:
  - `ALTER TABLE transactions ADD COLUMN IF NOT EXISTS extraction_details TEXT` (for storing `by_method` JSON).
  - `ALTER TABLE receipt_index ADD COLUMN IF NOT EXISTS extraction_details TEXT`.
- Update `upsert_transaction` and `upsert_receipt_index` to serialize and save the `by_method` dictionary as JSON.

---

### 4. Provenance & Preview UI in Triage (`pages/2_Triage.py`)

#### [MODIFY] [2_Triage.py](file:///C:/Users/pushp/Documents/Projects/bookkeeping/pages/2_Triage.py)
- Expand the current receipt preview section to show:
  - The clickable `file:///` link to open the folder on disk.
  - The side-by-side **Extraction Provenance** table below the receipt preview, making it easy to eyeball the image and compare it directly to each method's output.

---

## Verification Plan

### Manual Verification
1. **Upload & Auto-Save**: Drag and drop a receipt on the Import page. Verify it gets auto-saved to the local directory (e.g., `receipts/2026/06/...`).
2. **Match Card Preview**: Verify that the Match Card displays a rendering of the uploaded document, a local folder link, and the comparison table.
3. **Local Link Test**: Click the local folder link and verify that the operating system opens/highlights the file.
4. **Triage Review**: Select the transaction in the Triage Inbox and verify the receipt image, link, and extraction details table are all visible and aligned.
