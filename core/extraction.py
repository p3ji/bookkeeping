"""
Shared multi-method extraction + reconciliation layer.

Callers select any combination of extraction methods ("deterministic", "ollama",
"cloud") for a statement or receipt import. Selecting one method runs it alone
(matches today's behavior). Selecting several runs them all and reconciles the
results: the deterministic value always wins when present, LLM methods only ever
corroborate or contest it via a review flag — they never get unilateral write
access to the ledger.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


METHOD_LABELS = {
    "deterministic": "Deterministic (OCR/regex)",
    "ollama": "Local LLM (Ollama)",
    "cloud": "Cloud LLM (Claude)",
}

_AMOUNT_TOL = 0.02
_VENDOR_MATCH_THRESHOLD = 85


@dataclass
class ExtractionResult:
    method_used: str
    confidence: float
    rows: list[dict] = field(default_factory=list)     # statements: parsed transactions
    receipt: Any = None                                 # receipts: a ReceiptData
    flags: list[str] = field(default_factory=list)      # receipt-level disagreement notes
    by_method: dict = field(default_factory=dict)       # raw per-method output, for debugging


def available_methods(check_enabled: bool = True) -> list[str]:
    """Return the subset of ['deterministic', 'ollama', 'cloud'] usable right now.

    check_enabled=True also requires the user's explicit cloud_llm_enabled opt-in
    (Settings page) before 'cloud' is offered, even if an API key is present.
    """
    from core.llm_extractor import is_ollama_available, is_cloud_llm_available

    methods = ["deterministic"]
    if is_ollama_available():
        methods.append("ollama")
    if is_cloud_llm_available():
        if not check_enabled:
            methods.append("cloud")
        else:
            from core.database import get_setting
            if get_setting("cloud_llm_enabled", "false") == "true":
                methods.append("cloud")
    return methods


def _vendor_agree(a: str, b: str, threshold: int = _VENDOR_MATCH_THRESHOLD) -> bool:
    if not a or not b:
        return False
    try:
        from thefuzz import fuzz
        return fuzz.partial_ratio(a.lower(), b.lower()) >= threshold
    except ImportError:
        return a.strip().lower() == b.strip().lower()


def _to_float(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Statements
# ---------------------------------------------------------------------------

def _deterministic_statement_rows(data: bytes, filename: str, default_year: int) -> list[dict]:
    from core.ingestion import parse_statement_file
    try:
        df = parse_statement_file(data, filename=filename, default_year=default_year)
        return df.to_dict("records")
    except Exception:
        return []


def _statement_confidence(rows: list[dict]) -> float:
    if not rows:
        return 0.0
    return min(1.0, 0.3 + 0.07 * len(rows))


def _reconcile_statement_rows(by_method: dict[str, list[dict]]) -> tuple[list[dict], str]:
    """
    Merge per-method transaction lists by matching (date, amount within $0.02).
    Deterministic rows always win when present. Rows only found by a subset of
    methods, or with a vendor mismatch, get a "_review_note" key that callers
    should turn into an audit flag before persisting.
    """
    method_names = [m for m in by_method if by_method.get(m) is not None]
    if len(method_names) <= 1:
        only = method_names[0] if method_names else "deterministic"
        return list(by_method.get(only, [])), only

    base_method = "deterministic" if "deterministic" in by_method else method_names[0]
    base_rows = [dict(r) for r in by_method.get(base_method, [])]
    other_methods = [m for m in method_names if m != base_method]

    used: dict[str, set[int]] = {m: set() for m in other_methods}
    result: list[dict] = []

    for row in base_rows:
        notes = []
        for m in other_methods:
            match_idx = None
            for i, cand in enumerate(by_method[m]):
                if i in used[m]:
                    continue
                if (cand.get("date") == row.get("date")
                        and abs(cand.get("amount_gross", 0) - row.get("amount_gross", 0)) <= _AMOUNT_TOL):
                    match_idx = i
                    break
            if match_idx is None:
                notes.append(f"Not confirmed by {METHOD_LABELS.get(m, m)}")
                continue
            used[m].add(match_idx)
            cand = by_method[m][match_idx]
            if not _vendor_agree(row.get("vendor", ""), cand.get("vendor", "")):
                notes.append(f'{METHOD_LABELS.get(m, m)} read vendor as "{cand.get("vendor", "")}"')
        if notes:
            row["_review_note"] = "; ".join(notes)
        result.append(row)

    # Rows an LLM method found but the base method missed entirely.
    for m in other_methods:
        for i, cand in enumerate(by_method[m]):
            if i in used[m]:
                continue
            extra = dict(cand)
            extra["_review_note"] = (
                f"Found by {METHOD_LABELS.get(m, m)} only — not confirmed, please verify"
            )
            result.append(extra)

    return result, "+".join([base_method] + other_methods)


def extract_statement(source, filename: str, methods: list[str], default_year: int) -> ExtractionResult:
    """
    source: bytes, a file-like object, or a path.
    Runs each selected method and reconciles the results into one ExtractionResult.
    """
    import os as _os
    import tempfile

    if hasattr(source, "read"):
        data = source.read()
    elif isinstance(source, (bytes, bytearray)):
        data = bytes(source)
    else:
        data = Path(source).read_bytes()

    suffix = Path(filename).suffix or ".pdf"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)

    by_method: dict[str, list[dict]] = {}
    try:
        if "deterministic" in methods:
            by_method["deterministic"] = _deterministic_statement_rows(data, filename, default_year)
        if "ollama" in methods:
            from core.llm_extractor import extract_statement_transactions
            by_method["ollama"] = extract_statement_transactions(tmp_path, default_year)
        if "cloud" in methods:
            from core.llm_extractor import extract_statement_transactions_cloud
            by_method["cloud"] = extract_statement_transactions_cloud(tmp_path, default_year)
    finally:
        _os.unlink(tmp_path)

    rows, method_used = _reconcile_statement_rows(by_method)
    confidence = _statement_confidence(rows)
    return ExtractionResult(method_used=method_used, confidence=confidence, rows=rows, by_method=by_method)


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------

def _as_receipt_data(val):
    from core.receipt_parser import ReceiptData

    if val is None:
        return None
    if isinstance(val, ReceiptData):
        return val if (val.vendor or val.total is not None) else None
    if isinstance(val, dict) and val:
        return ReceiptData(
            vendor=str(val.get("vendor") or ""),
            date=str(val.get("date") or ""),
            total=_to_float(val.get("total")),
            subtotal=_to_float(val.get("subtotal")),
            tax_gst=_to_float(val.get("gst")),
            tax_hst=_to_float(val.get("hst")),
            tax_pst=_to_float(val.get("pst")),
            line_items=val.get("line_items") or [],
            doc_type="retail",
        )
    return None


def _receipt_confidence(r) -> float:
    fields = [bool(r.vendor), bool(r.date), r.total is not None]
    return sum(fields) / len(fields)


def _reconcile_receipt(by_method: dict) -> tuple[Any, str, list[str]]:
    from core.receipt_parser import ReceiptData

    parsed = {m: _as_receipt_data(v) for m, v in by_method.items()}
    parsed = {m: v for m, v in parsed.items() if v is not None}

    if not parsed:
        return ReceiptData(), "deterministic", []

    base_method = "deterministic" if "deterministic" in parsed else next(iter(parsed))
    base = parsed[base_method]
    flags: list[str] = []

    for m, other in parsed.items():
        if m == base_method:
            continue
        if base.vendor and other.vendor and not _vendor_agree(base.vendor, other.vendor):
            flags.append(f'{METHOD_LABELS.get(m, m)} read vendor as "{other.vendor}"')
        elif not base.vendor and other.vendor:
            base.vendor = other.vendor  # deterministic found nothing — fill the gap

        if base.date and other.date and base.date != other.date:
            flags.append(f'{METHOD_LABELS.get(m, m)} read date as "{other.date}"')
        elif not base.date and other.date:
            base.date = other.date

        if base.total is not None and other.total is not None and abs(base.total - other.total) > 0.01:
            flags.append(f"{METHOD_LABELS.get(m, m)} read total as ${other.total:.2f}")
        elif base.total is None and other.total is not None:
            base.total = other.total

    method_used = "+".join(parsed.keys()) if len(parsed) > 1 else base_method
    return base, method_used, flags


def extract_receipt(source, methods: list[str]) -> ExtractionResult:
    """source: path to a receipt file on disk."""
    path = Path(source)
    by_method: dict[str, Any] = {}

    if "deterministic" in methods:
        from core.receipt_parser import extract_receipt_data_from_file
        by_method["deterministic"] = extract_receipt_data_from_file(path)
    if "ollama" in methods:
        from core.llm_extractor import extract_receipt_data_llm
        by_method["ollama"] = extract_receipt_data_llm(path)
    if "cloud" in methods:
        from core.llm_extractor import extract_receipt_data_llm_cloud
        by_method["cloud"] = extract_receipt_data_llm_cloud(path)

    receipt, method_used, flags = _reconcile_receipt(by_method)
    confidence = _receipt_confidence(receipt)
    return ExtractionResult(
        method_used=method_used, confidence=confidence,
        receipt=receipt, flags=flags, by_method=by_method,
    )
