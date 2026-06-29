"""Dashboard — aggregate views by month, year, and CRA category."""
from datetime import date

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title="Dashboard", page_icon="📊", layout="wide")

from core.database import (
    init_db, get_summary_stats, get_available_years,
    get_monthly_summary, get_category_summary, get_all_transactions,
    get_setting,
)

init_db()

st.title("📊 Dashboard")

province = get_setting("province", "ON")
business_name = get_setting("business_name", "My Business")
st.caption(f"{business_name} · Province: {province}")

# ---------------------------------------------------------------------------
# Year / Month filters
# ---------------------------------------------------------------------------
years = get_available_years()
if not years:
    st.info("No verified transactions yet. Complete triage first.", icon="ℹ️")
    st.stop()

MONTHS = {
    0: "All Months", 1: "January", 2: "February", 3: "March",
    4: "April", 5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}

col_yr, col_mo, col_cat = st.columns(3)
with col_yr:
    sel_year = st.selectbox("Year", years, index=0)
with col_mo:
    sel_month_name = st.selectbox("Month", list(MONTHS.values()), index=0)
    sel_month = [k for k, v in MONTHS.items() if v == sel_month_name][0]
with col_cat:
    cat_df_all = get_category_summary(year=sel_year, month=sel_month if sel_month else None)
    cat_options = ["All Categories"] + list(cat_df_all["cra_line"].dropna().unique()) if not cat_df_all.empty else ["All Categories"]
    sel_cat = st.selectbox("CRA Category", cat_options, index=0)

st.divider()

# ---------------------------------------------------------------------------
# Top metric cards
# ---------------------------------------------------------------------------
stats = get_summary_stats()

# Filtered stats from the detailed tables
tx_filters = {"year": sel_year, "business_only": True, "verified_only": True}
if sel_month:
    tx_filters["month"] = sel_month
df_filtered = get_all_transactions(tx_filters)

if sel_cat != "All Categories":
    df_filtered = df_filtered[df_filtered["cra_line"] == sel_cat]

def safe_sum(col):
    if col in df_filtered.columns and not df_filtered.empty:
        return float(df_filtered[col].sum())
    return 0.0

gross_total = safe_sum("amount_gross")
itc_total   = safe_sum("gst_hst_amount")
net_total   = gross_total - itc_total

# Meals: apply 50% limit
if not df_filtered.empty and "cra_line" in df_filtered.columns:
    meals_mask = df_filtered["cra_line"] == "8523"
    deductible = (
        df_filtered.loc[~meals_mask, "amount_gross"].fillna(0).sum() +
        df_filtered.loc[meals_mask,  "amount_gross"].fillna(0).sum() * 0.50
    )
else:
    deductible = gross_total

missing_rcpts = int((df_filtered["receipt_path"].isna() | (df_filtered["receipt_path"] == "")).sum()) \
    if not df_filtered.empty else 0

mc1, mc2, mc3, mc4, mc5 = st.columns(5)
mc1.metric("Gross Expenses",      f"${gross_total:,.2f}")
mc2.metric("Deductible Amount",   f"${deductible:,.2f}")
mc3.metric("GST/HST ITCs",        f"${itc_total:,.2f}")
mc4.metric("Transactions",        f"{len(df_filtered):,}")
mc5.metric("Missing Receipts",    f"{missing_rcpts:,}",
           delta_color="inverse" if missing_rcpts else "off")

st.divider()

# ---------------------------------------------------------------------------
# Monthly bar chart
# ---------------------------------------------------------------------------
monthly_df = get_monthly_summary(sel_year)

if not monthly_df.empty:
    monthly_df["month_name"] = monthly_df["month"].apply(lambda m: MONTHS.get(int(m), str(m)))
    monthly_df["gross_expenses"] = monthly_df["gross_expenses"].astype(float)
    monthly_df["deductible"]     = monthly_df["deductible"].astype(float)
    monthly_df["itc"]            = monthly_df["itc"].astype(float)

    fig_monthly = go.Figure()
    fig_monthly.add_bar(
        x=monthly_df["month_name"], y=monthly_df["gross_expenses"],
        name="Gross Expenses", marker_color="#636EFA",
    )
    fig_monthly.add_bar(
        x=monthly_df["month_name"], y=monthly_df["deductible"],
        name="Deductible", marker_color="#00CC96",
    )
    fig_monthly.add_bar(
        x=monthly_df["month_name"], y=monthly_df["itc"],
        name="GST/HST ITC", marker_color="#EF553B",
    )
    fig_monthly.update_layout(
        title=f"Monthly Summary — {sel_year}",
        barmode="group",
        xaxis_title="Month",
        yaxis_title="Amount ($)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        height=380,
    )
    st.plotly_chart(fig_monthly, use_container_width=True)
else:
    st.info("No verified transactions for the selected year.")

st.divider()

# ---------------------------------------------------------------------------
# Category breakdown
# ---------------------------------------------------------------------------
col_pie, col_table = st.columns([1, 1], gap="large")

with col_pie:
    cat_df = get_category_summary(
        year=sel_year,
        month=sel_month if sel_month else None,
    )
    if not cat_df.empty:
        cat_df["total_gross"] = cat_df["total_gross"].astype(float)
        cat_df["label"] = cat_df.apply(
            lambda r: f"Line {r['cra_line']}: {(r['cra_description'] or '')[:28]}", axis=1
        )
        fig_pie = px.pie(
            cat_df,
            values="total_gross",
            names="label",
            title="Spending by CRA Category",
            hole=0.35,
        )
        fig_pie.update_traces(textposition="inside", textinfo="percent+label")
        fig_pie.update_layout(showlegend=False, height=380)
        st.plotly_chart(fig_pie, use_container_width=True)
    else:
        st.info("No categorised business transactions.")

with col_table:
    if not cat_df.empty:
        st.subheader("CRA T2125 Summary")
        summary = cat_df.copy()
        summary["Gross"] = summary["total_gross"].apply(lambda x: f"${float(x):,.2f}")
        summary["ITC"]   = summary["total_itc"].apply(lambda x: f"${float(x):,.2f}")
        summary["Count"] = summary["tx_count"].astype(int)
        st.dataframe(
            summary[["cra_line", "cra_description", "Count", "Gross", "ITC"]].rename(
                columns={"cra_line": "Line", "cra_description": "Description"}
            ),
            use_container_width=True,
            hide_index=True,
        )

st.divider()

# ---------------------------------------------------------------------------
# Transaction ledger
# ---------------------------------------------------------------------------
with st.expander("📋 Transaction Detail", expanded=False):
    if df_filtered.empty:
        st.info("No transactions match current filters.")
    else:
        show_cols = ["date", "vendor", "amount_gross", "gst_hst_amount",
                     "cra_description", "business_percentage", "verified_status"]
        show_cols = [c for c in show_cols if c in df_filtered.columns]
        ledger = df_filtered[show_cols].copy()
        ledger["date"]             = pd.to_datetime(ledger["date"]).dt.strftime("%Y-%m-%d")
        ledger["amount_gross"]     = ledger["amount_gross"].apply(lambda x: f"${float(x):,.2f}")
        ledger["gst_hst_amount"]   = ledger["gst_hst_amount"].apply(lambda x: f"${float(x or 0):,.2f}")
        ledger["business_percentage"] = ledger["business_percentage"].apply(
            lambda x: f"{int(float(x or 1) * 100)}%"
        )
        ledger["verified_status"] = ledger["verified_status"].apply(
            lambda x: "✅" if x else "⏳"
        )
        ledger.rename(columns={
            "date": "Date", "vendor": "Vendor", "amount_gross": "Amount",
            "gst_hst_amount": "GST/HST", "cra_description": "Category",
            "business_percentage": "Biz %", "verified_status": "Verified",
        }, inplace=True)
        st.dataframe(ledger, use_container_width=True, hide_index=True)

        csv_bytes = df_filtered.to_csv(index=False).encode()
        st.download_button(
            "⬇️ Download as CSV",
            data=csv_bytes,
            file_name=f"transactions_{sel_year}.csv",
            mime="text/csv",
        )
