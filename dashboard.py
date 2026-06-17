"""
concisepost — Live Telemetry & Revenue Dashboard (Streamlit)
============================================================

A premium, dark-mode control panel for the concisepost FastAPI control plane.
It fetches live analytics from ``GET /api/v1/dashboard/summary`` using the
``X-ConcisePost-API-Key`` header and renders, across two tabs:

  📊 Optimization — financial savings, tokens saved, loops prevented, efficiency,
                    plus simulated savings-over-time and per-agent charts.
  💰 Revenue      — MRR and ARR derived from the tenant's subscription tier,
                    a plan ladder, and a 12-month revenue projection.

If the API is unreachable (e.g. a free-tier cold start), it falls back to
representative demo data so the dashboard always looks great.

Deploy on Streamlit Community Cloud:
    1. Put dashboard.py + requirements.txt in a GitHub repo.
    2. share.streamlit.io -> New app -> pick the repo -> main file dashboard.py.

Requirements (requirements.txt):
    streamlit>=1.36
    requests>=2.31
    plotly>=5.22
    pandas>=2.0
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

# --------------------------------------------------------------------------- #
# Brand palette
# --------------------------------------------------------------------------- #
BG = "#0B0F19"          # deep charcoal
CARD = "#121826"        # raised charcoal
EMERALD = "#00E676"     # primary
CYAN = "#00E5FF"        # secondary
RED = "#FF5252"         # alert / loops
TEXT = "#E6EDF3"        # primary text
MUTED = "#8B98A9"       # muted text

DEFAULT_BASE_URL = "https://concisepost-api.onrender.com"
DEMO_API_KEY = "cp_live_demo_5f3b9a1c7e2d48f6"
SUMMARY_PATH = "/api/v1/dashboard/summary"

# Subscription price per tier (USD / month). Drives MRR & ARR.
TIER_PRICING = {"free": 0, "pro": 49, "team": 129, "enterprise": 499}

# Representative demo payload (matches the API response shape) used when the
# live endpoint is unavailable or when the user explicitly selects demo mode.
DEMO_DATA = {
    "company_id": "demo-acme-robotics",
    "tier": "pro",
    "total_optimized_messages": 24178,
    "total_raw_tokens_saved": 9847221,
    "cumulative_usd_saved": 49.236105,
    "loops_prevented_count": 17,
    "percentage_efficiency": 41.7,
    "monthly_message_limit": 25000,
    "optimized_messages_this_month": 24178,
    "quota_remaining": 822,
}


# --------------------------------------------------------------------------- #
# Page config + global CSS (dark theme)
# --------------------------------------------------------------------------- #
st.set_page_config(
    page_title="ConcisePost · Live Dashboard",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

GLOBAL_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
[data-testid="stAppViewContainer"] { background-color: #0B0F19; }
[data-testid="stHeader"] { background: rgba(0,0,0,0); }
[data-testid="stSidebar"] { background-color: #0E1320; border-right: 1px solid rgba(255,255,255,0.06); }
[data-testid="stSidebar"] * { color: #E6EDF3; }
.block-container { padding-top: 2rem; padding-bottom: 3rem; max-width: 1320px; }
h1, h2, h3, h4, p, span, div, label { color: #E6EDF3; }
a { color: #00E5FF; text-decoration: none; }
.stButton > button {
    background: linear-gradient(90deg, #00E676, #00E5FF);
    color: #07120B; font-weight: 700; border: none; border-radius: 10px;
    padding: 0.5rem 1rem;
}
.stButton > button:hover { filter: brightness(1.08); color: #07120B; }
.stTabs [data-baseweb="tab-list"] { gap: 6px; }
.stTabs [data-baseweb="tab"] {
    background: #0E1320; border: 1px solid rgba(255,255,255,0.07);
    border-radius: 10px; padding: 8px 18px; color: #8B98A9;
}
.stTabs [aria-selected="true"] {
    background: #10261A; color: #00E676; border: 1px solid #00E67655;
}
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-thumb { background: #1C2433; border-radius: 8px; }
</style>
"""
st.markdown(GLOBAL_CSS, unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def human_int(n) -> str:
    """Compact human-readable integer (1.2K, 9.8M, 3.4B)."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "0"
    if abs(n) >= 1_000_000_000:
        return f"{n / 1e9:.2f}B"
    if abs(n) >= 1_000_000:
        return f"{n / 1e6:.2f}M"
    if abs(n) >= 1_000:
        return f"{n / 1e3:.1f}K"
    return f"{n:,.0f}"


def usd(n, cents: bool = True) -> str:
    """Format a USD amount; keeps precision for tiny alpha-stage values."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "$0.00"
    if 0 < abs(n) < 0.01:
        return f"${n:.6f}"
    return f"${n:,.2f}" if cents else f"${n:,.0f}"


def metric_card(label: str, value_html: str, accent: str, sub_html: str = "") -> str:
    """Return inline-styled HTML for a premium metric card (no <style> braces)."""
    return (
        f'<div style="background:linear-gradient(145deg,#121826,#0E1320);'
        f'border:1px solid {accent}2E;border-radius:18px;padding:22px 24px;'
        f'box-shadow:0 8px 30px rgba(0,0,0,0.45);min-height:184px;'
        f'display:flex;flex-direction:column;justify-content:space-between;">'
        f'<div style="font-size:12px;letter-spacing:1.6px;text-transform:uppercase;'
        f'color:#8B98A9;font-weight:600;">{label}</div>'
        f'<div style="font-size:38px;font-weight:800;color:{accent};line-height:1.05;'
        f'margin-top:6px;">{value_html}</div>'
        f'<div>{sub_html}</div>'
        f'</div>'
    )


def progress_bar(pct: float, caption: str) -> str:
    """Emerald->cyan progress bar with a caption (pct clamped 0..100)."""
    pct = max(0.0, min(100.0, float(pct)))
    return (
        f'<div style="height:10px;border-radius:999px;background:#1C2433;'
        f'overflow:hidden;margin-top:14px;">'
        f'<div style="height:100%;width:{pct:.1f}%;'
        f'background:linear-gradient(90deg,#00E676,#00E5FF);"></div></div>'
        f'<div style="font-size:12px;color:#8B98A9;margin-top:8px;">{caption}</div>'
    )


def plan_ladder(current_tier: str) -> str:
    """Horizontal plan ladder highlighting the tenant's current tier."""
    tiers = [("Free", 0), ("Pro", 49), ("Team", 129), ("Enterprise", 499)]
    cells = ""
    for name, price in tiers:
        active = name.lower() == (current_tier or "").lower()
        border = EMERALD if active else "rgba(255,255,255,0.08)"
        bg = "#10261A" if active else "#0E1320"
        name_color = EMERALD if active else TEXT
        badge = (
            f'<div style="display:inline-block;background:{EMERALD}1F;color:{EMERALD};'
            f'border:1px solid {EMERALD}55;border-radius:999px;font-size:10px;'
            f'font-weight:700;padding:2px 8px;margin-bottom:8px;">CURRENT</div>'
            if active else
            '<div style="height:21px;margin-bottom:8px;"></div>'
        )
        cells += (
            f'<div style="flex:1;background:{bg};border:1px solid {border};'
            f'border-radius:14px;padding:16px 12px;text-align:center;">'
            f'{badge}'
            f'<div style="color:{name_color};font-weight:700;font-size:15px;">{name}</div>'
            f'<div style="color:#8B98A9;font-size:13px;margin-top:2px;">'
            f'${price}/mo</div></div>'
        )
    return f'<div style="display:flex;gap:10px;">{cells}</div>'


def style_fig(fig: go.Figure, title: str) -> go.Figure:
    """Apply the dark brand theme to a Plotly figure."""
    fig.update_layout(
        title=dict(text=title, font=dict(color=TEXT, size=16, family="Inter")),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color=MUTED, family="Inter"),
        margin=dict(l=8, r=8, t=52, b=8),
        height=350,
        showlegend=False,
        xaxis=dict(gridcolor="rgba(255,255,255,0.05)", zeroline=False,
                   linecolor="rgba(255,255,255,0.10)"),
        yaxis=dict(gridcolor="rgba(255,255,255,0.06)", zeroline=False),
        hoverlabel=dict(bgcolor="#121826", font_color=TEXT,
                        bordercolor="rgba(255,255,255,0.1)"),
    )
    return fig


def savings_over_time(total_usd: float, days: int = 30, seed: int = 0) -> pd.DataFrame:
    """Simulated daily + cumulative savings that sum to the real total."""
    rng = random.Random(seed)
    weights = [rng.uniform(0.35, 1.7) for _ in range(days)]
    tot = sum(weights) or 1.0
    daily = [total_usd * w / tot for w in weights]
    cumulative, running = [], 0.0
    for d in daily:
        running += d
        cumulative.append(running)
    today = date.today()
    dates = [today - timedelta(days=days - 1 - i) for i in range(days)]
    return pd.DataFrame(
        {"date": dates, "daily_usd": daily, "cumulative_usd": cumulative}
    )


def tokens_by_agent(total_tokens: float, seed: int = 0) -> pd.DataFrame:
    """Simulated per-agent token-savings breakdown that sums to the real total."""
    agents = ["researcher", "writer", "planner", "coder", "reviewer", "orchestrator"]
    rng = random.Random(seed + 7)
    weights = [rng.uniform(0.5, 1.6) for _ in agents]
    tot = sum(weights) or 1.0
    values = [int(total_tokens * w / tot) for w in weights]
    df = pd.DataFrame({"agent_id": agents, "tokens_saved": values})
    return df.sort_values("tokens_saved", ascending=False).reset_index(drop=True)


@st.cache_data(ttl=30, show_spinner=False)
def fetch_summary(base_url: str, api_key: str) -> dict:
    """Fetch live dashboard analytics. Cached for 30s; raises on failure."""
    url = base_url.rstrip("/") + SUMMARY_PATH
    resp = requests.get(
        url,
        headers={"X-ConcisePost-API-Key": api_key},
        timeout=60,  # generous: free-tier cold start can take ~60s
    )
    resp.raise_for_status()
    return resp.json()


# --------------------------------------------------------------------------- #
# Sidebar — connection settings
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.markdown(
        '<div style="font-size:22px;font-weight:800;">⚡ ConcisePost</div>'
        '<div style="color:#8B98A9;font-size:12px;margin-bottom:18px;">'
        'Live Telemetry Console</div>',
        unsafe_allow_html=True,
    )

    source = st.radio("Data source", ["Live API", "Demo data"], index=0)
    base_url = st.text_input("API base URL", value=DEFAULT_BASE_URL,
                             help="Your live Render service URL.")
    api_key = st.text_input("X-ConcisePost-API-Key", value=DEMO_API_KEY,
                            type="password")
    fallback = st.checkbox("Use demo data if the API is unreachable", value=True)

    if st.button("🔄 Refresh now", use_container_width=True):
        fetch_summary.clear()
        st.rerun()

    st.markdown(
        '<div style="color:#8B98A9;font-size:11px;margin-top:18px;line-height:1.5;">'
        'Free-tier servers sleep when idle — the first request after a nap can '
        'take ~60 seconds to wake up.</div>',
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------- #
# Data acquisition
# --------------------------------------------------------------------------- #
status = "demo"
error_msg = ""

if source == "Live API":
    try:
        with st.spinner("Connecting to the live API… (first call can take ~60s "
                        "if the server was asleep)"):
            data = fetch_summary(base_url, api_key)
        status = "live"
    except Exception as exc:  # network error, auth error, cold-start timeout
        error_msg = str(exc)
        if fallback:
            data = DEMO_DATA
            status = "demo-fallback"
        else:
            st.error(f"Could not reach the live API: {error_msg}")
            st.stop()
else:
    data = DEMO_DATA
    status = "demo"

# Safely coerce values. The API field is `tier`; accept `billing_tier` too.
company_id = str(data.get("company_id", "—"))
tier_raw = str(data.get("billing_tier") or data.get("tier") or "free").lower()
tier = tier_raw.upper()
total_messages = int(data.get("total_optimized_messages", 0) or 0)
tokens_saved = int(data.get("total_raw_tokens_saved", 0) or 0)
usd_saved = float(data.get("cumulative_usd_saved", 0) or 0.0)
loops = int(data.get("loops_prevented_count", 0) or 0)
efficiency = float(data.get("percentage_efficiency", 0) or 0.0)
limit = data.get("monthly_message_limit", None)
used_month = int(data.get("optimized_messages_this_month", 0) or 0)
limit = int(limit) if limit not in (None, "") else None

# Revenue from subscription tier.
mrr = TIER_PRICING.get(tier_raw, 0)
arr = mrr * 12

seed = int(abs(usd_saved * 100) + tokens_saved) % (2**31)


# --------------------------------------------------------------------------- #
# Header + connection status pill
# --------------------------------------------------------------------------- #
if status == "live":
    pill = (f'<span style="background:{EMERALD}1F;color:{EMERALD};border:1px solid '
            f'{EMERALD}55;padding:5px 12px;border-radius:999px;font-size:12px;'
            f'font-weight:700;">● LIVE</span>')
elif status == "demo-fallback":
    pill = (f'<span style="background:{RED}1F;color:{RED};border:1px solid {RED}55;'
            f'padding:5px 12px;border-radius:999px;font-size:12px;font-weight:700;">'
            f'● API ASLEEP · SHOWING DEMO</span>')
else:
    pill = (f'<span style="background:{CYAN}1F;color:{CYAN};border:1px solid {CYAN}55;'
            f'padding:5px 12px;border-radius:999px;font-size:12px;font-weight:700;">'
            f'● DEMO DATA</span>')

st.markdown(
    f'<div style="display:flex;align-items:center;justify-content:space-between;'
    f'flex-wrap:wrap;gap:10px;margin-bottom:4px;">'
    f'<div style="font-size:30px;font-weight:800;">'
    f'<span style="background:linear-gradient(90deg,#00E676,#00E5FF);'
    f'-webkit-background-clip:text;-webkit-text-fill-color:transparent;">'
    f'ConcisePost</span> <span style="color:#E6EDF3;">Dashboard</span></div>'
    f'{pill}</div>',
    unsafe_allow_html=True,
)
st.markdown(
    f'<div style="color:#8B98A9;font-size:13px;margin-bottom:18px;">'
    f'Tenant <b style="color:#E6EDF3;">{company_id}</b> · Plan '
    f'<b style="color:#00E5FF;">{tier}</b> · Updated '
    f'{datetime.now().strftime("%b %d, %Y · %H:%M:%S")}</div>',
    unsafe_allow_html=True,
)

if status == "demo-fallback":
    st.warning(f"Live API unavailable ({error_msg}). Showing demo data — press "
               f"**Refresh now** once the server has woken up.", icon="⚠️")


# --------------------------------------------------------------------------- #
# Tabs
# --------------------------------------------------------------------------- #
tab_opt, tab_rev = st.tabs(["📊  Optimization", "💰  Revenue"])

# ----------------------------- OPTIMIZATION ------------------------------- #
with tab_opt:
    c1, c2, c3, c4 = st.columns(4, gap="medium")

    with c1:
        st.markdown(
            metric_card(
                "Total Financial Savings",
                usd(usd_saved),
                EMERALD,
                '<div style="font-size:12px;color:#8B98A9;margin-top:14px;">'
                'Estimated from live per-model token pricing</div>',
            ),
            unsafe_allow_html=True,
        )

    with c2:
        if limit and limit > 0:
            pct = min(100.0, used_month / limit * 100.0)
            bar = progress_bar(
                pct, f"{used_month:,} / {limit:,} messages this month · {pct:.0f}%"
            )
        else:
            bar = ('<div style="font-size:12px;color:#8B98A9;margin-top:14px;">'
                   'Unlimited plan — no monthly ceiling</div>')
        st.markdown(
            metric_card("Total Tokens Saved", human_int(tokens_saved), EMERALD, bar),
            unsafe_allow_html=True,
        )

    with c3:
        shield = (
            f'<div style="display:flex;align-items:center;gap:10px;margin-top:14px;">'
            f'<div style="width:34px;height:34px;border-radius:9px;background:{RED}1F;'
            f'border:1px solid {RED}55;display:flex;align-items:center;'
            f'justify-content:center;font-size:18px;box-shadow:0 0 16px {RED}33;">🛡️'
            f'</div><div style="font-size:12px;color:#8B98A9;">agent loops cut before'
            f'<br>they burned the budget</div></div>'
        )
        st.markdown(
            metric_card("Runaway Loops Prevented", f"{loops:,}", RED, shield),
            unsafe_allow_html=True,
        )

    with c4:
        eff_bar = progress_bar(efficiency, f"{efficiency:.1f}% of raw tokens removed")
        st.markdown(
            metric_card("Overall Optimization Efficiency", f"{efficiency:.1f}%",
                        CYAN, eff_bar),
            unsafe_allow_html=True,
        )

    st.markdown('<div style="height:26px;"></div>', unsafe_allow_html=True)
    g1, g2 = st.columns(2, gap="medium")

    with g1:
        sdf = savings_over_time(usd_saved, days=30, seed=seed)
        line = go.Figure()
        line.add_trace(go.Scatter(
            x=sdf["date"], y=sdf["cumulative_usd"], mode="lines",
            line=dict(color=EMERALD, width=3, shape="spline"),
            fill="tozeroy", fillcolor="rgba(0,230,118,0.12)",
            hovertemplate="%{x|%b %d}<br>$%{y:.2f} cumulative<extra></extra>",
        ))
        st.plotly_chart(style_fig(line, "Savings Over Time (30-day, simulated)"),
                        use_container_width=True, theme=None,
                        config={"displayModeBar": False})

    with g2:
        adf = tokens_by_agent(tokens_saved, seed=seed)
        colors = [EMERALD if i % 2 == 0 else CYAN for i in range(len(adf))]
        bar_fig = go.Figure(go.Bar(
            x=adf["agent_id"], y=adf["tokens_saved"],
            marker=dict(color=colors, line=dict(width=0)),
            hovertemplate="%{x}<br>%{y:,} tokens saved<extra></extra>",
        ))
        st.plotly_chart(
            style_fig(bar_fig, "Token Usage Breakdown by Agent ID (simulated)"),
            use_container_width=True, theme=None,
            config={"displayModeBar": False})

    st.markdown(
        '<div style="color:#8B98A9;font-size:12px;margin-top:6px;">'
        'The two charts above are simulated for alpha testing and are seeded from '
        'your real totals, so they stay consistent between refreshes. Per-day and '
        'per-agent breakdowns become live once the matching API endpoints ship.</div>',
        unsafe_allow_html=True,
    )

# ------------------------------- REVENUE ---------------------------------- #
with tab_rev:
    r1, r2 = st.columns(2, gap="medium")

    with r1:
        st.markdown(
            metric_card(
                "Monthly Recurring Revenue (MRR)",
                usd(mrr, cents=False),
                EMERALD,
                f'<div style="font-size:12px;color:#8B98A9;margin-top:14px;">'
                f'From the <b style="color:#00E676;">{tier}</b> subscription plan'
                f'</div>',
            ),
            unsafe_allow_html=True,
        )

    with r2:
        st.markdown(
            metric_card(
                "Annual Recurring Revenue (ARR)",
                usd(arr, cents=False),
                CYAN,
                '<div style="font-size:12px;color:#8B98A9;margin-top:14px;">'
                'MRR × 12 months</div>',
            ),
            unsafe_allow_html=True,
        )

    st.markdown('<div style="height:22px;"></div>', unsafe_allow_html=True)
    st.markdown(
        '<div style="font-size:13px;letter-spacing:1.4px;text-transform:uppercase;'
        'color:#8B98A9;font-weight:600;margin-bottom:10px;">Subscription Plan</div>',
        unsafe_allow_html=True,
    )
    st.markdown(plan_ladder(tier_raw), unsafe_allow_html=True)

    st.markdown('<div style="height:26px;"></div>', unsafe_allow_html=True)
    months = list(range(1, 13))
    today = date.today()
    month_labels = [(today.replace(day=1) + timedelta(days=31 * (m - 1))).strftime("%b")
                    for m in months]
    cumulative_rev = [mrr * m for m in months]
    rev = go.Figure()
    rev.add_trace(go.Scatter(
        x=month_labels, y=cumulative_rev, mode="lines+markers",
        line=dict(color=CYAN, width=3, shape="spline"),
        marker=dict(color=CYAN, size=6),
        fill="tozeroy", fillcolor="rgba(0,229,255,0.10)",
        hovertemplate="Month %{x}<br>$%{y:,.0f} cumulative<extra></extra>",
    ))
    st.plotly_chart(
        style_fig(rev, "Projected Cumulative Revenue (12 months at current plan)"),
        use_container_width=True, theme=None, config={"displayModeBar": False})

    st.markdown(
        '<div style="color:#8B98A9;font-size:12px;margin-top:6px;">'
        'MRR and ARR reflect the subscription revenue of the authenticated account '
        f'(the <b style="color:#00E5FF;">{tier}</b> plan). For total company-wide '
        'MRR across all tenants, aggregate this value over every active subscription.'
        '</div>',
        unsafe_allow_html=True,
    )

st.markdown(
    '<div style="text-align:center;color:#3F4A5A;font-size:12px;margin-top:34px;">'
    'ConcisePost · inter-agent message optimization · '
    'because your agents should pay for what they mean, not what they say.</div>',
    unsafe_allow_html=True,
)
