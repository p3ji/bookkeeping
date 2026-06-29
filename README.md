# Bookkeeping — Local CRA-Compliant Bookkeeping App

A fully offline desktop bookkeeping app for Canadian sole proprietors.  
Ingests Costco Mastercard CSV statements, matches receipts via local OCR,  
auto-classifies to CRA Form T2125 lines, calculates GST/HST ITCs, and flags audit risks.

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. (Optional) Install Tesseract OCR for scanned receipts

Download from: https://github.com/UB-Mannheim/tesseract/wiki  
Default install path: `C:\Program Files\Tesseract-OCR\tesseract.exe`  
Override with env var: `set TESSERACT_PATH=C:\path\to\tesseract.exe`

### 3. Run the app

```bash
streamlit run app.py
```

Or double-click `run.bat`.

---

## How to Use

### Import your statement
1. Export your Costco Mastercard CSV from the Capital One portal
2. Go to **Import** page → upload the CSV or drop it in `imports/`
3. Click **Import Statement**

### Add receipts (optional but recommended)
Organise receipts under `receipts/YYYY/MM/`:
```
receipts/
└── 2024/
    ├── 01/
    │   ├── rogers_jan2024.pdf
    │   └── costco_0115.jpg
    └── 02/
```
Then go to **Import** → click **Scan Receipts** then **Match Receipts**.

### Triage transactions
Go to the **Triage** page:
- Select a transaction row
- Toggle **Business / Personal**
- Adjust **Business-Use %** slider (for phone/internet proration)
- Pick **CRA T2125 Category**
- Review any **Audit Flags**
- Click **Save** (or **Save & Export MD** to write an Obsidian-compatible Markdown file)

### View totals
Go to the **Dashboard** page for:
- Monthly totals (gross, deductible, GST/HST ITCs)
- Spending breakdown by CRA category (pie chart)
- Year-to-date summary table
- Downloadable CSV export

---

## CRA T2125 Categories Supported

| Line   | Description                              |
|--------|------------------------------------------|
| 8521   | Advertising                              |
| 8523   | Meals & Entertainment *(50% limit auto-applied)* |
| 8600   | Business Taxes, Licences & Memberships   |
| 8690   | Insurance                                |
| 8710   | Interest & Bank Charges                  |
| 8810   | Office Expenses                          |
| 8811   | Supplies                                 |
| 9200   | Travel Expenses                          |
| 9220   | Telephone & Utilities                    |
| 9270   | Other Expenses                           |
| 9281   | Motor Vehicle Expenses                   |

---

## Audit Risk Flags (auto-generated)

- **High-value meal** — Amount > $500 on Meals & Entertainment
- **Potential capital asset** — Receipt text contains laptop/computer keywords under Supplies
- **100% telecom allocation** — Full deduction of personal phone/internet is a CRA trigger
- **100% vehicle deduction** — Requires mileage logbook
- **Large single expense** — Transaction > $2,000 flagged for documentation

---

## Folder Structure

```
bookkeeping/
├── app.py               ← Streamlit entry point
├── config.py            ← Paths, tax rates, CRA lines
├── requirements.txt
├── run.bat              ← Windows launcher
├── core/
│   ├── database.py      ← DuckDB schema & CRUD
│   ├── ingestion.py     ← CSV parsing
│   ├── ocr.py           ← PDF/image text extraction
│   ├── matching.py      ← Receipt-to-transaction matching
│   ├── tax.py           ← GST/HST ITC logic
│   ├── categorization.py← CRA T2125 auto-categorization
│   ├── audit.py         ← Audit risk flagging
│   └── export.py        ← Markdown sync export
├── pages/
│   ├── 1_Import.py      ← CSV + receipt ingestion UI
│   ├── 2_Triage.py      ← Transaction review UI
│   ├── 3_Dashboard.py   ← Analytics & charts
│   └── 4_Settings.py    ← Province, export, OCR status
├── imports/             ← Drop CSV files here
├── receipts/YYYY/MM/    ← Drop receipt PDFs/images here
├── exports/             ← Markdown exports written here
└── data/                ← DuckDB database (auto-created)
```

---

## Privacy

All data stays on your hard drive. No external APIs. No cloud sync. No telemetry.
