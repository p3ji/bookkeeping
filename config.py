"""Application-wide configuration and constants."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
IMPORTS_DIR = BASE_DIR / "imports"
RECEIPTS_DIR = BASE_DIR / "receipts"
EXPORTS_DIR = BASE_DIR / "exports"

for _d in [DATA_DIR, IMPORTS_DIR, RECEIPTS_DIR, EXPORTS_DIR]:
    _d.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "bookkeeping.duckdb"

# Canadian provincial tax rates
TAX_RATES = {
    "ON":  {"rate": 0.13,  "name": "Ontario HST"},
    "NS":  {"rate": 0.15,  "name": "Nova Scotia HST"},
    "NB":  {"rate": 0.15,  "name": "New Brunswick HST"},
    "NL":  {"rate": 0.15,  "name": "Newfoundland HST"},
    "PEI": {"rate": 0.15,  "name": "PEI HST"},
    "BC":  {"rate": 0.05,  "name": "BC GST"},
    "AB":  {"rate": 0.05,  "name": "Alberta GST"},
    "SK":  {"rate": 0.05,  "name": "Saskatchewan GST"},
    "MB":  {"rate": 0.05,  "name": "Manitoba GST"},
    "QC":  {"rate": 0.05,  "name": "Quebec GST"},
    "NT":  {"rate": 0.05,  "name": "NWT GST"},
    "YT":  {"rate": 0.05,  "name": "Yukon GST"},
    "NU":  {"rate": 0.05,  "name": "Nunavut GST"},
}

DEFAULT_PROVINCE = os.environ.get("PROVINCE", "ON")

# Tesseract OCR executable path (Windows default)
TESSERACT_PATH = os.environ.get(
    "TESSERACT_PATH",
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
)

# Matching engine thresholds
MATCH_SCORE_THRESHOLD = 60
DATE_WINDOW_DAYS = 3

# CRA Form T2125 line codes
CRA_LINES = {
    "8521": "Advertising",
    "8523": "Meals & Entertainment (50% deductible limit applies)",
    "8590": "Bad Debts",
    "8600": "Business Taxes, Licences & Memberships",
    "8690": "Insurance",
    "8710": "Interest & Bank Charges",
    "8760": "Business-Use-of-Home Expenses",
    "8810": "Office Expenses",
    "8811": "Supplies",
    "9060": "Salaries, Wages & Benefits",
    "9200": "Travel Expenses",
    "9220": "Telephone & Utilities",
    "9270": "Other Expenses",
    "9281": "Motor Vehicle Expenses",
}

RECEIPT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".bmp"}
