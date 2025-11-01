import os
import time
from datetime import datetime, date
from typing import Tuple, Optional, Dict

import pandas as pd
import requests
import streamlit as st
from dateutil import parser as dtparser
from supabase import create_client, Client
import matplotlib.pyplot as plt

# ---------- Streamlit config ----------
st.set_page_config(page_title="Portfolio Tracker", page_icon="📈", layout="wide")

# ---------- Secrets / Env ----------
def _get_secret(name: str, default: str = "") -> str:
    # prefer st.secrets, then environment
    val = None
    try:
        val = st.secrets.get(name)  # type: ignore[attr-defined]
    except Exception:
        pass
    if val is None:
        val = os.getenv(name, default)
    return (val or "").strip()

SUPABASE_URL = _get_secret("SUPABASE_URL")
SUPABASE_KEY = _get_secret("SUPABASE_KEY")
ALPHAVANTAGE_KEY = _get_secret("ALPHAVANTAGE_KEY")
BASE_CURRENCY = _get_secret("BASE_CURRENCY", "CAD")

if not SUPABASE_URL or not SUPABASE_KEY:
    st.error("Missing Supabase credentials. Set SUPABASE_URL and SUPABASE_KEY in Secrets.")
    st.stop()

# Supabase client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------- DB helpers ----------
def get_holdings() -> pd.DataFrame:
    res = supabase.table("holdings").select("*").order("symbol").execute()
    return pd.DataFrame(res.data or [])

def upsert_holding(symbol: str, quantity: float, cost_basis: float, currency: str) -> None:
    payload = {
        "symbol": symbol.upper().strip(),
        "quantity": float(quantity or 0.0),
        "cost_basis": float(cost_basis or 0.0),
        "currency": (currency or BASE_CURRENCY).upper().strip(),
        "updated_at": datetime.utcnow().isoformat()
    }
    supabase.table("holdings").upsert(payload, on_conflict="symbol").execute()

def delete_holding(symbol: str) -> None:
    supabase.table("holdings").delete().eq("symbol", symbol).execute()

def get_cached_price(symbol: str) -> Optional[Dict]:
    res = supabase.table("prices_daily").select("*").eq("symbol", symbol)\
        .order("asof", desc=True).limit(1).execute()
    rows = res.data or []
    return rows[0] if rows else None

def cache_price(symbol: str, price: float, asof: str) -> None:
    supabase.table("prices_daily").insert({
        "symbol": symbol,
        "price": float(price),
        "asof": asof
    }).execute()

# ---------- Alpha Vantage fetcher (free-plan friendly) ----------
def fetch_price_alpha_vantage(symbol: str) -> Tuple[float, str]:
    """
    Robust price fetch:
      1) GLOBAL_QUOTE (free, latest price)
      2) TIME_SERIES_DAILY (often free)
      3) TIME_SERIES_DAILY_ADJUSTED (may be premium)
    Raises ValueError with explicit messages on rate-limit/premium endpoints.
    Returns: (price: float, asof: str)
    """
    if not ALPHAVANTAGE_KEY:
        raise ValueError("Alpha Vantage key missing")

    def _call(func: str, extra: Optional[dict] = None) -> dict:
        params = {"function": func, "apikey": ALPHAVANTAGE_KEY}
        if extra:
            params.update(extra)
        r = requests.get("https://www.alphavantage.co/query", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        # Clear diagnostics for user-friendly errors
        if "Information" in data:
            # premium-only or plan limitation message
            raise ValueError(f"API info: {data['Information']}")
        if "Note" in data:
            # rate limit exceeded (5/min, 500/day on free plan)
            raise ValueError(f"Rate limit hit: {data['Note']}")
        if "Error Message" in data:
            raise ValueError(f"API error: {data['Error Message']}")
        return data

    # 1) GLOBAL_QUOTE
    try:
        gq = _call("GLOBAL_QUOTE", {"symbol": symbol})
        g = gq.get("Global Quote", {}) or {}
        px = g.get("05. price")
        latest_day = g.get("07. latest trading day") or g.get("10. latest trading day") or ""
        if px:
            return float(px), latest_day
    except Exception:
        # try next fallback
        pass

    # 2) TIME_SERIES_DAILY
    try:
        d = _call("TIME_SERIES_DAILY", {"symbol": symbol})
        series = d.get("Time Series (Daily)")
        if series:
            last_date = max(series.keys())
            price = float(series[last_date]["4. close"])
            return price, last_date
    except Exception:
        pass

    # 3) TIME_SERIES_DAILY_ADJUSTED (may be premium)
    d2 = _call("TIME_SERIES_DAILY_ADJUSTED", {"symbol": symbol})
    series2 = d2.get("Time Series (Daily)")
    if not series2:
        raise ValueError("No time series returned by Alpha Vantage")
    last_date = max(series2.keys())
    last_row = series2[last_date]
    price = float(last_row.get("5. adjusted close") or last_row.get("4. close"))
    return price, last_date

def ensure_price(symbol: str, force_refresh: bool = False) -> Tuple[Optional[float], Optional[str], bool]:
    """
    Return (price, asof, fetched_now: bool).
    Uses cache from today unless force_refresh=True; otherwise calls AV with fallbacks.
    """
    # Try cache first, unless forcing refresh
    if not force_refresh:
        cached = get_cached_price(symbol)
        if cached:
            try:
                asof_dt = dtparser.parse(str(cached["asof"])).date()
            except Exception:
                asof_dt = None
            if asof_dt == date.today():
                return float(cached["price"]), str(cached["asof"]), False

    # Fetch from AV (may raise ValueError with clear reason)
    price, asof = fetch_price_alpha_vantage(symbol)
    # Cache new value
    cache_price(symbol, price, asof)
    return price, asof, True

# ---------- UI ----------
st.title("📈 Portfolio Tracker (MVP)")
st.caption("Streamlit + Supabase + Alpha Vantage | Free-plan friendly price fetch")

# Quick connectivity indicator (non-sensitive)
with st.sidebar.expander("Status"):
    st.write("Supabase URL:", SUPABASE_URL or "(missing)")
    st.write("Alpha Vantage key present:", bool(ALPHAVANTAGE_KEY))

page = st.sidebar.radio("Navigation", ["Overview", "Holdings", "Price Cache"])

# --- Holdings page ---
if page == "Holdings":
    st.header("🧾 Manage Holdings")
    with st.form("add_form", clear_on_submit=True):
        c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
        symbol = c1.text_input("Symbol (e.g., FTEC, AAPL, SHOP.TRT if needed)")
        quantity = c2.number_input("Quantity", value=0.0, step=1.0, min_value=0.0, format="%.4f")
        cost_basis = c3.number_input("Cost Basis (per share)", value=0.0, step=0.01, min_value=0.0, format="%.4f")
        currency = c4.text_input("Currency", value=BASE_CURRENCY)
        submitted = st.form_submit_button("Add / Update")
        if submitted and symbol:
            upsert_holding(symbol, quantity, cost_basis, currency)
            st.success(f"Upserted {symbol.upper().strip()}")

    st.divider()
    df = get_holdings()
    if df.empty:
        st.info("No holdings yet. Add some above.")
    else:
        st.dataframe(df, use_container_width=True)
        to_delete = st.multiselect("Select symbols to delete", df["symbol"].tolist())
        if st.button("Delete selected"):
            for s in to_delete:
                delete_holding(s)
            st.success("Deleted.")

# --- Price cache page ---
elif page == "Price Cache":
    st.header("🗃️ Cached Prices")
    res = supabase.table("prices_daily").select("*").order("asof", desc=True).limit(500).execute()
    dfp = pd.DataFrame(res.data or [])
    st.dataframe(dfp, use_container_width=True)

# --- Overview (main) ---
else:
    st.header("📊 Overview")
    df = get_holdings()
    if df.empty:
        st.info("Add holdings in the 'Holdings' tab to see your portfolio.")
        st.stop()

    st.subheader("Latest Prices")
    col1, col2 = st.columns([1, 4])
    force = col1.checkbox("Force refresh (bypass today's cache)", value=False)
    do_update = col1.button("Update prices")

    prices: Dict[str, float] = {}
    last_asof: Dict[str, str] = {}
    calls_made = 0

    if do_update:
        for sym in df["symbol"].tolist():
            try:
                price, asof, fetched_now = ensure_price(sym, force_refresh=force)
                if price is not None:
                    prices[sym] = float(price)
                    last_asof[sym] = str(asof)
                    if fetched_now:
                        calls_made += 1
                        # Be gentle with free tier (<= 5 req/min): sleep only when we actually hit the API
                        time.sleep(12)
            except Exception as e:
                st.error(f"Failed to fetch {sym}: {e}")
                cached = get_cached_price(sym)
                if cached:
                    prices[sym] = float(cached["price"])
                    last_asof[sym] = str(cached["asof"])

        if calls_made == 0 and not prices:
            if not ALPHAVANTAGE_KEY:
                st.warning("No Alpha Vantage key configured; cannot fetch live prices.")
            else:
                st.warning("No prices fetched (possibly rate limit or premium endpoint). Try again in ~60 seconds.")

    # Merge any newly-fetched prices with cached values for display
    if not prices:
        # If user didn't click update, try to use today's cache for display
        for sym in df["symbol"].tolist():
            cached = get_cached_price(sym)
            if cached:
                prices[sym] = float(cached["price"])
                last_asof[sym] = str(cached["asof"])

    # ---- Safer numeric conversions & calculations
    df["last_price"] = pd.to_numeric(df["symbol"].map(prices), errors="coerce")
    df["asof"] = df["symbol"].map(last_asof).astype("string")
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce").fillna(0.0)
    df["cost_basis"] = pd.to_numeric(df["cost_basis"], errors="coerce").fillna(0.0)

    df["market_value"] = (df["quantity"] * df["last_price"].fillna(0.0)).astype(float)
    df["pnl_per_share"] = (df["last_price"].fillna(0.0) - df["cost_basis"]).round(4)
    df["unrealized_pnl"] = (df["pnl_per_share"] * df["quantity"]).round(2)

    with st.expander("Holdings with pricing"):
        st.dataframe(df, use_container_width=True)

    # Totals
    total_value = float(df["market_value"].sum())
    total_cost = float((df["quantity"] * df["cost_basis"]).sum())
    total_pnl = float(df["unrealized_pnl"].sum())

    m1, m2, m3 = st.columns(3)
    m1.metric("Total Market Value", f"{total_value:,.2f} {BASE_CURRENCY}")
    m2.metric("Total Cost", f"{total_cost:,.2f} {BASE_CURRENCY}")
    m3.metric("Unrealized P&L", f"{total_pnl:,.2f} {BASE_CURRENCY}")

    # Allocation chart (by symbol) — only if there is something to plot
    alloc = (
        df.loc[df["market_value"] > 0, ["symbol", "market_value"]]
          .groupby("symbol", as_index=True)["market_value"]
          .sum()
          .sort_values(ascending=False)
    )

    if not alloc.empty and float(alloc.sum()) > 0:
        st.subheader("Allocation by Symbol")
        fig = plt.figure()
        plt.pie(alloc.values, labels=alloc.index, autopct="%1.1f%%", startangle=140)
        st.pyplot(fig)
    else:
        st.info("No market value to chart yet — add holdings and fetch prices first.")

    st.caption("Notes: GLOBAL_QUOTE used for free Alpha Vantage plan; daily endpoints are used as fallbacks. Respect rate limits (≤5 req/min).")

