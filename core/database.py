"""DuckDB connection, schema management, and CRUD helpers."""
import json

class DataclassEncoder(json.JSONEncoder):
    def default(self, o):
        # Gracefully handle custom ReceiptData dataclass serialization
        from core.receipt_parser import ReceiptData
        if isinstance(o, ReceiptData):
            return {
                "vendor": o.vendor,
                "date": o.date,
                "total": o.total,
                "subtotal": o.subtotal,
                "tax_gst": o.tax_gst,
                "tax_hst": o.tax_hst,
                "tax_pst": o.tax_pst,
                "line_items": o.line_items,
                "doc_type": o.doc_type,
                "raw_text": o.raw_text
            }
        if hasattr(o, "__dict__"):
            return o.__dict__
        return super().default(o)
from contextlib import contextmanager

import duckdb
import pandas as pd

from config import DB_PATH


@contextmanager
def db_conn(read_only=False):
    conn = duckdb.connect(str(DB_PATH), read_only=read_only)
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with db_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id VARCHAR PRIMARY KEY,
                date DATE NOT NULL,
                vendor VARCHAR NOT NULL,
                amount_gross DECIMAL(10,2) NOT NULL,
                amount_net DECIMAL(10,2),
                gst_hst_amount DECIMAL(10,2) DEFAULT 0,
                is_business BOOLEAN DEFAULT TRUE,
                business_percentage DECIMAL(7,4) DEFAULT 1.0000,
                cra_line VARCHAR,
                cra_description VARCHAR,
                receipt_path VARCHAR,
                raw_receipt_text TEXT,
                audit_flags TEXT DEFAULT '[]',
                verified_status BOOLEAN DEFAULT FALSE,
                notes TEXT DEFAULT '',
                import_source VARCHAR,
                created_at TIMESTAMP DEFAULT current_timestamp,
                updated_at TIMESTAMP DEFAULT current_timestamp
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS receipt_index (
                receipt_id VARCHAR PRIMARY KEY,
                file_path VARCHAR UNIQUE,
                file_modified TIMESTAMP,
                date_extracted DATE,
                amount_extracted DECIMAL(10,2),
                vendor_extracted VARCHAR,
                raw_text TEXT,
                indexed_at TIMESTAMP DEFAULT current_timestamp
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS import_log (
                import_id VARCHAR PRIMARY KEY,
                filename VARCHAR,
                imported_at TIMESTAMP DEFAULT current_timestamp,
                row_count INTEGER,
                new_records INTEGER,
                status VARCHAR
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key VARCHAR PRIMARY KEY,
                value TEXT
            )
        """)

        # Extraction provenance — safe no-op migration on existing DB files.
        conn.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS "
                      "extraction_method VARCHAR DEFAULT 'deterministic'")
        conn.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS "
                      "extraction_confidence DECIMAL(5,4)")
        conn.execute("ALTER TABLE transactions ADD COLUMN IF NOT EXISTS "
                      "extraction_details TEXT")
        conn.execute("ALTER TABLE receipt_index ADD COLUMN IF NOT EXISTS "
                      "extraction_method VARCHAR DEFAULT 'deterministic'")
        conn.execute("ALTER TABLE receipt_index ADD COLUMN IF NOT EXISTS "
                      "extraction_confidence DECIMAL(5,4)")
        conn.execute("ALTER TABLE receipt_index ADD COLUMN IF NOT EXISTS "
                      "extraction_details TEXT")

        _defaults = [
            ("province", "ON"),
            ("business_name", "My Business"),
            ("fiscal_year_start", "01"),
            ("cloud_llm_enabled", "false"),
        ]
        for k, v in _defaults:
            try:
                conn.execute(
                    "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT DO NOTHING",
                    [k, v],
                )
            except Exception:
                pass
        conn.commit()


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

def get_all_transactions(filters: dict | None = None) -> pd.DataFrame:
    with db_conn(read_only=True) as conn:
        conditions, params = [], []
        if filters:
            if filters.get("year"):
                conditions.append("YEAR(date) = ?")
                params.append(int(filters["year"]))
            if filters.get("month"):
                conditions.append("MONTH(date) = ?")
                params.append(int(filters["month"]))
            if filters.get("cra_line"):
                conditions.append("cra_line = ?")
                params.append(filters["cra_line"])
            if filters.get("verified_only"):
                conditions.append("verified_status = TRUE")
            if filters.get("business_only"):
                conditions.append("is_business = TRUE")
            if filters.get("unverified_only"):
                conditions.append("verified_status = FALSE")

        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        try:
            df = conn.execute(
                f"SELECT * FROM transactions{where} ORDER BY date DESC",
                params,
            ).df()
        except Exception:
            df = pd.DataFrame()
        return df


def get_transaction(transaction_id: str) -> dict | None:
    with db_conn(read_only=True) as conn:
        result = conn.execute(
            "SELECT * FROM transactions WHERE transaction_id = ?",
            [transaction_id],
        ).df()
    if result.empty:
        return None
    row = result.iloc[0].to_dict()
    row["audit_flags"] = json.loads(row.get("audit_flags") or "[]")
    row["extraction_details"] = json.loads(row.get("extraction_details") or "{}")
    return row


def transaction_exists(transaction_id: str) -> bool:
    """Check if a transaction exists in the database by its ID."""
    with db_conn(read_only=True) as conn:
        r = conn.execute(
            "SELECT 1 FROM transactions WHERE transaction_id = ?",
            [transaction_id],
        ).fetchone()
        return r is not None


def upsert_transaction(tx: dict):
    flags = tx.get("audit_flags", [])
    flags_json = json.dumps(flags) if isinstance(flags, list) else (flags or "[]")
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO transactions (
                transaction_id, date, vendor, amount_gross, amount_net,
                gst_hst_amount, is_business, business_percentage, cra_line,
                cra_description, receipt_path, raw_receipt_text, audit_flags,
                verified_status, notes, import_source,
                extraction_method, extraction_confidence, extraction_details
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (transaction_id) DO NOTHING
            """,
            [
                tx["transaction_id"], tx["date"], tx["vendor"],
                tx["amount_gross"], tx.get("amount_net"), tx.get("gst_hst_amount", 0),
                tx.get("is_business", True), tx.get("business_percentage", 1.0),
                tx.get("cra_line"), tx.get("cra_description"), tx.get("receipt_path"),
                tx.get("raw_receipt_text"), flags_json,
                tx.get("verified_status", False), tx.get("notes", ""),
                tx.get("import_source"),
                tx.get("extraction_method", "deterministic"), tx.get("extraction_confidence"),
                json.dumps(tx.get("extraction_details", {}), cls=DataclassEncoder) if isinstance(tx.get("extraction_details"), dict) else tx.get("extraction_details"),
            ],
        )
        conn.commit()


def update_transaction(transaction_id: str, updates: dict):
    flags = updates.get("audit_flags", [])
    flags_json = json.dumps(flags) if isinstance(flags, list) else (flags or "[]")
    with db_conn() as conn:
        conn.execute(
            """
            UPDATE transactions SET
                is_business         = ?,
                business_percentage = ?,
                cra_line            = ?,
                cra_description     = ?,
                receipt_path        = ?,
                raw_receipt_text    = ?,
                audit_flags         = ?,
                verified_status     = ?,
                notes               = ?,
                amount_net          = ?,
                gst_hst_amount      = ?,
                updated_at          = now()
            WHERE transaction_id = ?
            """,
            [
                updates.get("is_business"),
                updates.get("business_percentage"),
                updates.get("cra_line"),
                updates.get("cra_description"),
                updates.get("receipt_path"),
                updates.get("raw_receipt_text"),
                flags_json,
                updates.get("verified_status"),
                updates.get("notes"),
                updates.get("amount_net"),
                updates.get("gst_hst_amount"),
                transaction_id,
            ],
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Receipt index
# ---------------------------------------------------------------------------

def upsert_receipt_index(receipt: dict):
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO receipt_index (
                receipt_id, file_path, file_modified, date_extracted,
                amount_extracted, vendor_extracted, raw_text,
                extraction_method, extraction_confidence, extraction_details
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (file_path) DO UPDATE SET
                date_extracted        = excluded.date_extracted,
                amount_extracted      = excluded.amount_extracted,
                vendor_extracted      = excluded.vendor_extracted,
                raw_text              = excluded.raw_text,
                extraction_method     = excluded.extraction_method,
                extraction_confidence = excluded.extraction_confidence,
                extraction_details    = excluded.extraction_details,
                indexed_at            = now()
            """,
            [
                receipt["receipt_id"], receipt["file_path"],
                receipt.get("file_modified"), receipt.get("date_extracted"),
                receipt.get("amount_extracted"), receipt.get("vendor_extracted"),
                receipt.get("raw_text"),
                receipt.get("extraction_method", "deterministic"), receipt.get("extraction_confidence"),
                json.dumps(receipt.get("extraction_details", {}), cls=DataclassEncoder) if isinstance(receipt.get("extraction_details"), dict) else receipt.get("extraction_details"),
            ],
        )
        conn.commit()


def get_all_receipts() -> pd.DataFrame:
    with db_conn(read_only=True) as conn:
        try:
            return conn.execute("SELECT * FROM receipt_index").df()
        except Exception:
            return pd.DataFrame()


def get_indexed_paths() -> set:
    with db_conn(read_only=True) as conn:
        try:
            rows = conn.execute("SELECT file_path FROM receipt_index").fetchall()
            return {r[0] for r in rows}
        except Exception:
            return set()


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

def get_summary_stats() -> dict:
    with db_conn(read_only=True) as conn:
        try:
            r = conn.execute("""
                SELECT
                    COUNT(*)                                                          AS total,
                    SUM(CASE WHEN verified_status  THEN 1 ELSE 0 END)                AS verified,
                    SUM(CASE WHEN is_business AND verified_status
                             THEN amount_gross * business_percentage ELSE 0 END)     AS business_total,
                    SUM(CASE WHEN is_business AND verified_status
                             THEN COALESCE(gst_hst_amount,0) * business_percentage
                             ELSE 0 END)                                              AS itc_total,
                    SUM(CASE WHEN receipt_path IS NULL THEN 1 ELSE 0 END)            AS missing_receipts,
                    SUM(CASE WHEN NOT verified_status THEN amount_gross ELSE 0 END)  AS unverified_total
                FROM transactions
            """).fetchone()
            return {
                "total": int(r[0] or 0),
                "verified": int(r[1] or 0),
                "business_total": float(r[2] or 0),
                "itc_total": float(r[3] or 0),
                "missing_receipts": int(r[4] or 0),
                "unverified_total": float(r[5] or 0.0),
            }
        except Exception:
            return {"total": 0, "verified": 0, "business_total": 0.0,
                    "itc_total": 0.0, "missing_receipts": 0, "unverified_total": 0.0}


def get_monthly_summary(year: int) -> pd.DataFrame:
    with db_conn(read_only=True) as conn:
        try:
            return conn.execute("""
                SELECT
                    MONTH(date)                                                           AS month,
                    SUM(amount_gross)                                                     AS gross_expenses,
                    SUM(CASE WHEN is_business
                             THEN amount_gross * business_percentage ELSE 0 END)          AS deductible,
                    SUM(CASE WHEN is_business
                             THEN COALESCE(gst_hst_amount,0) * business_percentage
                             ELSE 0 END)                                                  AS itc,
                    COUNT(*)                                                              AS tx_count
                FROM transactions
                WHERE YEAR(date) = ? AND verified_status = TRUE
                GROUP BY MONTH(date)
                ORDER BY MONTH(date)
            """, [year]).df()
        except Exception:
            return pd.DataFrame()


def get_category_summary(year: int | None = None, month: int | None = None) -> pd.DataFrame:
    with db_conn(read_only=True) as conn:
        conditions = [
            "is_business = TRUE",
            "verified_status = TRUE",
            "cra_line IS NOT NULL",
        ]
        params = []
        if year:
            conditions.append("YEAR(date) = ?")
            params.append(year)
        if month:
            conditions.append("MONTH(date) = ?")
            params.append(month)
        where = " AND ".join(conditions)
        try:
            return conn.execute(f"""
                SELECT
                    cra_line,
                    cra_description,
                    COUNT(*)                                                AS tx_count,
                    SUM(amount_gross * business_percentage)                AS total_gross,
                    SUM(COALESCE(gst_hst_amount,0) * business_percentage)  AS total_itc
                FROM transactions
                WHERE {where}
                GROUP BY cra_line, cra_description
                ORDER BY total_gross DESC
            """, params).df()
        except Exception:
            return pd.DataFrame()


def get_available_years() -> list[int]:
    with db_conn(read_only=True) as conn:
        try:
            rows = conn.execute(
                "SELECT DISTINCT YEAR(date) FROM transactions ORDER BY 1 DESC"
            ).fetchall()
            return [int(r[0]) for r in rows if r[0]]
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_setting(key: str, default: str = "") -> str:
    with db_conn(read_only=True) as conn:
        try:
            r = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", [key]
            ).fetchone()
            return r[0] if r else default
        except Exception:
            return default


def save_setting(key: str, value: str):
    with db_conn() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            [key, value],
        )


def get_receipt_stats() -> dict:
    """Get metrics about indexed receipts, including low-confidence items."""
    with db_conn(read_only=True) as conn:
        try:
            # Check if table exists
            table_check = conn.execute("SELECT * FROM information_schema.tables WHERE table_name = 'receipt_index'").fetchone()
            if not table_check:
                return {"total": 0, "avg_confidence": 0.0, "low_confidence_count": 0, "low_confidence_list": []}

            r = conn.execute("""
                SELECT
                    COUNT(*),
                    AVG(extraction_confidence),
                    SUM(CASE WHEN extraction_confidence < 0.5 THEN 1 ELSE 0 END)
                FROM receipt_index
            """).fetchone()
            
            # Fetch low confidence files
            low_conf = conn.execute("""
                SELECT file_path, vendor_extracted, amount_extracted, extraction_confidence
                FROM receipt_index
                WHERE extraction_confidence < 0.5
                ORDER BY extraction_confidence ASC
            """).fetchall()
            
            return {
                "total": int(r[0] or 0),
                "avg_confidence": float(r[1] or 0.0),
                "low_confidence_count": int(r[2] or 0),
                "low_confidence_list": [
                    {"path": row[0], "vendor": row[1] or "Unknown", "amount": row[2] or 0.0, "confidence": row[3] or 0.0}
                    for row in low_conf
                ]
            }
        except Exception:
            return {"total": 0, "avg_confidence": 0.0, "low_confidence_count": 0, "low_confidence_list": []}
