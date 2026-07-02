"""Reconciliation — prove every business expense has a supporting receipt."""
from pathlib import Path

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Reconcile", page_icon="🧾", layout="wide")

from core.database import init_db, get_all_transactions, get_all_receipts, get_setting
from core.matching import _score, match_all_transactions
from config import MATCH_SCORE_THRESHOLD

init_db()

st.title("🧾 Reconciliation")
st.caption(
    "**Goal: every business expense you claim has a supporting receipt.** "
    "This page replaces the manual eyeball process — it shows how much of the ledger "
    "is covered by receipts, exactly which expenses are missing one, and how "
    "trustworthy each automatic receipt-to-transaction match is."
)

province = get_setting("province", "ON")
tx_df = get_all_transactions()
rx_df = get_all_receipts()

if tx_df.empty:
    st.info("No transactions yet — import a statement on the **Import** page first.", icon="ℹ️")
    st.stop()

# ---------------------------------------------------------------------------
# Prepare data
# ---------------------------------------------------------------------------
tx_df = tx_df.copy()
tx_df["receipt_path"] = tx_df["receipt_path"].fillna("").astype(str)
tx_df["has_receipt"] = tx_df["receipt_path"].str.len().gt(0) & (tx_df["receipt_path"] != "None")
tx_df["amount_gross"] = tx_df["amount_gross"].astype(float)
tx_df["date"] = pd.to_datetime(tx_df["date"])

years = sorted(tx_df["date"].dt.year.unique().tolist(), reverse=True)
col_year, col_run = st.columns([1, 3])
with col_year:
    year_choice = st.selectbox("Year", ["All years"] + [str(y) for y in years])
with col_run:
    st.markdown("<div style='height:1.7em'></div>", unsafe_allow_html=True)
    if st.button("🔗 Run Auto-Match Now", help="Score every unlinked receipt against every "
                 "unlinked transaction and link pairs scoring ≥ "
                 f"{MATCH_SCORE_THRESHOLD}/100."):
        with st.spinner("Matching receipts to transactions…"):
            res = match_all_transactions(province=province)
        st.success(f"Auto-matched **{res['matched']}** transactions; "
                   f"**{res['unmatched']}** still need a receipt.", icon="✅")
        st.rerun()

if year_choice != "All years":
    tx_df = tx_df[tx_df["date"].dt.year == int(year_choice)]

biz = tx_df[tx_df["is_business"].astype(bool)]
biz_with = biz[biz["has_receipt"]]
biz_without = biz[~biz["has_receipt"]]

# ---------------------------------------------------------------------------
# Section 1 — Coverage headline
# ---------------------------------------------------------------------------
st.divider()
total_biz = len(biz)
covered_n = len(biz_with)
covered_amt = float(biz_with["amount_gross"].sum())
missing_amt = float(biz_without["amount_gross"].sum())
total_amt = covered_amt + missing_amt
pct_n = (covered_n / total_biz * 100) if total_biz else 0.0
pct_amt = (covered_amt / total_amt * 100) if total_amt else 0.0

m1, m2, m3, m4 = st.columns(4)
m1.metric("Business Expenses", f"{total_biz:,}", help="Transactions currently marked as business.")
m2.metric("With Receipt", f"{covered_n:,} ({pct_n:.0f}%)",
          delta=f"{total_biz - covered_n} missing" if total_biz > covered_n else "complete",
          delta_color="inverse" if total_biz > covered_n else "normal")
m3.metric("$ Covered by Receipts", f"${covered_amt:,.2f}")
m4.metric("$ Missing Receipts", f"${missing_amt:,.2f}",
          delta_color="inverse" if missing_amt else "off")

st.progress(pct_amt / 100 if total_amt else 0.0,
            text=f"**{pct_amt:.1f}%** of business expense dollars are backed by a receipt")

# ---------------------------------------------------------------------------
# Section 2 — Business expenses missing receipts (the to-do list)
# ---------------------------------------------------------------------------
st.divider()
st.subheader(f"❌ Missing Receipts ({len(biz_without)})")
st.caption(
    "These business expenses have **no receipt on file**. The CRA can disallow a claimed "
    "expense without support — find the receipt (email, vendor portal, paper pile) and drop "
    "it on the **Import** page, where it will be matched back automatically."
)
if biz_without.empty:
    st.success("Every business expense has a receipt. Nothing to chase. 🎉", icon="✅")
else:
    missing_view = biz_without[["date", "vendor", "amount_gross", "cra_description", "verified_status"]].copy()
    missing_view = missing_view.sort_values("amount_gross", ascending=False)
    missing_view["date"] = missing_view["date"].dt.strftime("%Y-%m-%d")
    missing_view["amount_gross"] = missing_view["amount_gross"].map(lambda x: f"${x:,.2f}")
    missing_view["verified_status"] = missing_view["verified_status"].map(lambda v: "✅" if v else "—")
    missing_view.columns = ["Date", "Vendor", "Amount", "CRA Category", "Verified"]
    st.dataframe(missing_view, hide_index=True, use_container_width=True)
    st.download_button(
        "⬇️ Download missing-receipt list (CSV)",
        missing_view.to_csv(index=False).encode("utf-8"),
        file_name="missing_receipts.csv",
        mime="text/csv",
    )

# ---------------------------------------------------------------------------
# Section 3 — Linked matches audit (how good are the auto-matches?)
# ---------------------------------------------------------------------------
st.divider()
st.subheader(f"🔍 Linked Matches Audit ({len(biz_with)})")
st.caption(
    "Every transaction↔receipt link, re-scored so you can spot-check the weakest ones "
    "instead of eyeballing all of them. **🟢 Strong (≥80)** amounts/dates agree. "
    "**🟡 Fair (60–79)** partial agreement — glance at it. **🔴 Weak (<60)** linked manually "
    "or on thin evidence — verify it. Sorted weakest-first."
)

if biz_with.empty:
    st.info("No transactions have receipts linked yet. Import receipts or run auto-match above.")
else:
    rx_lookup = {}
    if not rx_df.empty:
        rx_lookup = {str(r["file_path"]): r for _, r in rx_df.iterrows()}

    audit_rows = []
    for _, tx in biz_with.iterrows():
        rx = rx_lookup.get(tx["receipt_path"])
        if rx is not None:
            score = _score(
                tx["date"], float(tx["amount_gross"]), str(tx["vendor"]),
                rx.get("date_extracted"), rx.get("amount_extracted"),
                str(rx.get("vendor_extracted") or ""),
            )
            rx_vendor = str(rx.get("vendor_extracted") or "—")
            rx_amount = rx.get("amount_extracted")
            rx_amount = f"${float(rx_amount):,.2f}" if pd.notna(rx_amount) and rx_amount is not None else "—"
            rx_date = str(rx.get("date_extracted") or "—")
        else:
            score, rx_vendor, rx_amount, rx_date = None, "(not indexed)", "—", "—"

        if score is None:
            badge = "⚪ Unscored"
        elif score >= 80:
            badge = f"🟢 Strong ({score})"
        elif score >= MATCH_SCORE_THRESHOLD:
            badge = f"🟡 Fair ({score})"
        else:
            badge = f"🔴 Weak ({score})"

        audit_rows.append({
            "_sort": -1 if score is None else score,
            "Match Quality": badge,
            "Tx Date": tx["date"].strftime("%Y-%m-%d"),
            "Tx Vendor": str(tx["vendor"]),
            "Tx Amount": f"${float(tx['amount_gross']):,.2f}",
            "Receipt Vendor": rx_vendor,
            "Receipt Amount": rx_amount,
            "Receipt Date": rx_date,
            "Receipt File": Path(tx["receipt_path"]).name,
            "Verified": "✅" if tx.get("verified_status") else "—",
        })

    audit_df = pd.DataFrame(audit_rows).sort_values("_sort").drop(columns=["_sort"])
    weak_n = sum(1 for r in audit_rows if r["Match Quality"].startswith(("🔴", "⚪")))
    if weak_n:
        st.warning(f"**{weak_n}** link(s) are weak or unscored — worth a manual look. "
                   "Open the transaction on the **Triage** page to see the receipt "
                   "image next to the extracted values.", icon="⚠️")
    st.dataframe(audit_df, hide_index=True, use_container_width=True)

# ---------------------------------------------------------------------------
# Section 4 — Receipts not linked to any transaction
# ---------------------------------------------------------------------------
st.divider()
linked_paths = set(tx_df[tx_df["has_receipt"]]["receipt_path"].tolist())
orphans = pd.DataFrame()
if not rx_df.empty:
    rx_df = rx_df.copy()
    rx_df["file_path"] = rx_df["file_path"].astype(str)
    orphans = rx_df[~rx_df["file_path"].isin(linked_paths)]

st.subheader(f"🧾 Unlinked Receipts ({len(orphans)})")
st.caption(
    "Receipt files that are indexed but not attached to any transaction. Either the matching "
    "transaction hasn't been imported yet (import the statement covering that period), the "
    "receipt is personal, or extraction misread the amount/date so no transaction scored high "
    "enough. Click a path to open the original file and check."
)
if orphans.empty:
    st.success("Every indexed receipt is linked to a transaction.", icon="✅")
else:
    orphan_rows = []
    for _, rx in orphans.iterrows():
        p = Path(rx["file_path"])
        amt = rx.get("amount_extracted")
        conf = rx.get("extraction_confidence")
        orphan_rows.append({
            "File": p.name,
            "Extracted Vendor": str(rx.get("vendor_extracted") or "—"),
            "Extracted Date": str(rx.get("date_extracted") or "—"),
            "Extracted Amount": f"${float(amt):,.2f}" if pd.notna(amt) and amt is not None else "—",
            "OCR Confidence": f"{float(conf) * 100:.0f}%" if pd.notna(conf) and conf is not None else "—",
            "Open": p.resolve().as_uri() if p.exists() else "",
        })
    st.dataframe(
        pd.DataFrame(orphan_rows),
        hide_index=True,
        use_container_width=True,
        column_config={"Open": st.column_config.LinkColumn("Open", display_text="📂 open file")},
    )

# ---------------------------------------------------------------------------
# Section 5 — Monthly coverage
# ---------------------------------------------------------------------------
st.divider()
st.subheader("📅 Monthly Coverage")
st.caption("Receipt coverage of business expenses month by month — spot the months to chase down.")

if biz.empty:
    st.info("No business transactions in the selected period.")
else:
    grp = biz.copy()
    grp["Month"] = grp["date"].dt.strftime("%Y-%m")
    monthly = grp.groupby("Month").apply(
        lambda g: pd.Series({
            "Business Tx": len(g),
            "With Receipt": int(g["has_receipt"].sum()),
            "Coverage %": (g["has_receipt"].mean() * 100) if len(g) else 0.0,
            "$ Total": g["amount_gross"].sum(),
            "$ Missing": g.loc[~g["has_receipt"], "amount_gross"].sum(),
        }),
        include_groups=False,
    ).reset_index().sort_values("Month", ascending=False)

    monthly["Coverage %"] = monthly["Coverage %"].map(lambda x: f"{x:.0f}%")
    monthly["$ Total"] = monthly["$ Total"].map(lambda x: f"${x:,.2f}")
    monthly["$ Missing"] = monthly["$ Missing"].map(lambda x: f"${x:,.2f}")
    monthly["Business Tx"] = monthly["Business Tx"].astype(int)
    monthly["With Receipt"] = monthly["With Receipt"].astype(int)
    st.dataframe(monthly, hide_index=True, use_container_width=True)

# ---------------------------------------------------------------------------
# How it works
# ---------------------------------------------------------------------------
with st.expander("ℹ️ How automatic matching works"):
    st.markdown(f"""
Each receipt is compared against every transaction that doesn't have a receipt yet, and scored out of **100**:

| Signal | Points | Rule |
|---|---|---|
| **Amount** | up to 50 | exact = 50 · within 2% = 35 · within 5% = 20 · within 10% = 10 |
| **Date** | up to 30 | same day = 30 · ±1 day = 22 · within a few days = 12 |
| **Vendor name** | up to 20 | fuzzy text similarity between the statement vendor and the OCR'd receipt vendor |

A link is only made automatically when the score is **≥ {MATCH_SCORE_THRESHOLD}**, which requires at
least two signals to agree (e.g. exact amount + close date). Everything the system does is
reviewable: the *Linked Matches Audit* above re-scores every link, and the **Triage** page shows
the receipt image side-by-side with what was extracted, with the matched values highlighted on
the image itself.
""")
