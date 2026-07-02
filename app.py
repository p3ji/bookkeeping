"""Main Streamlit entry point — home/overview page."""
import streamlit as st

st.set_page_config(
    page_title="Bookkeeping",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Initialise DB on first load
from core.database import init_db, get_summary_stats, get_available_years
init_db()

st.title("📚 CRA-Compliant Local Bookkeeping")
st.caption("Offline · Private · T2125-ready")

st.divider()

stats = get_summary_stats()
years = get_available_years()

col1, col2, col3, col4, col5, col6 = st.columns(6)
col1.metric("Transactions", f"{stats['total']:,}")
col2.metric("Verified", f"{stats['verified']:,}",
            delta=f"{stats['total'] - stats['verified']} pending" if stats['total'] > stats['verified'] else None,
            delta_color="inverse")
col3.metric("Pending Triage", f"${stats['unverified_total']:,.2f}")
col4.metric("Business Expenses", f"${stats['business_total']:,.2f}")
col5.metric("GST/HST ITCs", f"${stats['itc_total']:,.2f}")
col6.metric("Missing Receipts", f"{stats['missing_receipts']:,}",
            delta_color="inverse" if stats["missing_receipts"] else "off")

st.divider()

col_a, col_b = st.columns(2)

with col_a:
    st.subheader("Quick Start")
    st.markdown("""
1. **Import** — Drop statements (CSV/PDF/photo) and receipts; they're extracted and auto-matched
2. **Triage** — Classify each transaction as Business or Personal, set proration & CRA category
3. **Reconcile** — See receipt coverage, chase missing receipts, audit every auto-match
4. **Dashboard** — View monthly totals, year-to-date, and charts by CRA category
5. **Settings** — Set your province, business name, and export verified records to Markdown
""")

with col_b:
    st.subheader("Folder Structure")
    st.code("""
bookkeeping/
├── statements/       ← Drop CSV/PDF statements here
├── receipts/
│   └── YYYY/MM/      ← Organize receipts by year/month
├── exports/          ← Verified Markdown records written here
└── data/             ← DuckDB database (auto-created)
""", language="text")

if not years:
    st.info(
        "No transactions yet. Go to **Import** to load your first statement.",
        icon="ℹ️",
    )
else:
    st.divider()
    st.subheader(f"Active Tax Years: {', '.join(str(y) for y in years)}")
