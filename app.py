"""
JM Financial | Risk Intelligence Dashboard
==========================================
Real-time news aggregator for risk officers.

Run with:  streamlit run risk_dashboard.py
Install:   pip install streamlit requests beautifulsoup4 lxml feedparser
"""

import streamlit as st
import feedparser
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import json
import os
import re
import urllib.parse
from collections import defaultdict

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
DB_FILE             = "manual_headlines.json"
ADMIN_PASSWORD      = "JM_RISK_2026"
AUTO_REFRESH_SECONDS = 90

# Optional proxy — fill if needed inside JMF network
PROXY_USER = ""
PROXY_PASS = ""
PROXY_ADDR = ""   # e.g. "10.60.52.39:8080"

PROXIES = None
if PROXY_USER and PROXY_ADDR:
    eu = urllib.parse.quote(PROXY_USER)
    ep = urllib.parse.quote(PROXY_PASS)
    PROXIES = {
        "http":  f"http://{eu}:{ep}@{PROXY_ADDR}",
        "https": f"http://{eu}:{ep}@{PROXY_ADDR}",
    }

# ─────────────────────────────────────────────
# PORTFOLIO STOCKS — edit this list as needed
# ─────────────────────────────────────────────
PORTFOLIO_STOCKS = [
    "Sammaan Capital",
    "Suzlon",
    "Religare",
    "Valor Estate",
    # Add more stocks here — up to 10 recommended
    # "Stock Name",
]

# ─────────────────────────────────────────────
# PRIORITY KEYWORDS
# ─────────────────────────────────────────────
PRIORITY_KEYWORDS = [
    "war", "strike", "attack", "sanctions", "iran", "israel", "conflict",
    "rate hike", "rate cut", "rbi policy", "emergency", "crash", "plunge",
    "circuit breaker", "halt", "default", "recession", "devaluation",
    "rupee fall", "rupee crash", "fed hike", "fed cut", "inflation spike",
    "oil price", "crude surge", "gold rally", "market fall", "nifty down",
    "sensex crash", "sebi", "imf warning", "world bank", "selloff",
    "crisis", "collapse", "black swan", "geopolitical", "trump tariff",
    "china taiwan", "north korea", "nuclear", "penalty", "suspension",
    "fraud", "scam", "ban", "action against",
]

# ─────────────────────────────────────────────
# SENTIMENT KEYWORDS
# ─────────────────────────────────────────────
NEGATIVE_KEYWORDS = [
    "crash", "plunge", "fall", "drop", "decline", "loss", "losses", "slump",
    "selloff", "sell-off", "tumble", "sink", "sinks", "sank", "collapse",
    "collapses", "crisis", "recession", "default", "fraud", "scam", "ban",
    "penalty", "suspension", "warning", "risk", "threat", "attack", "war",
    "conflict", "sanctions", "halt", "circuit breaker", "devaluation",
    "inflation spike", "rate hike", "downgrade", "cut", "cuts", "probe",
    "investigation", "sebi action", "npa", "writeoff", "write-off",
    "layoff", "layoffs", "fired", "bankrupt", "insolvency", "debt",
    "miss", "misses", "disappoints", "disappointing", "weak", "slowdown",
    "contraction", "negative", "bearish", "bear market", "correction",
    "volatility", "uncertainty", "concern", "worried", "fear", "panic",
    "outflow", "outflows", "exodus", "dumped", "dumping",
]

POSITIVE_KEYWORDS = [
    "rally", "surge", "gain", "gains", "rise", "rises", "rose", "jump",
    "jumps", "jumped", "soar", "soars", "soared", "high", "record",
    "all-time high", "ath", "profit", "profits", "revenue", "growth",
    "upgrade", "buy", "bullish", "bull", "outperform", "beat", "beats",
    "strong", "robust", "positive", "boost", "boosts", "boosted",
    "inflow", "inflows", "investment", "order", "orders", "win", "wins",
    "contract", "expansion", "rate cut", "rate cuts", "easing",
    "recovery", "recovers", "rebound", "rebounds", "optimism",
    "opportunity", "partnership", "deal", "acquisition", "merger",
    "dividend", "buyback", "approval", "approved", "launches", "launch",
    "breakthrough", "innovation", "milestone", "commissioning",
    "capacity addition", "capex", "turnaround", "outperform",
]

def get_sentiment(title: str) -> str:
    """Returns 'positive', 'negative', or 'neutral' based on title keywords."""
    t = title.lower()
    neg_score = sum(1 for kw in NEGATIVE_KEYWORDS if kw in t)
    pos_score = sum(1 for kw in POSITIVE_KEYWORDS if kw in t)
    if neg_score > pos_score:
        return "negative"
    elif pos_score > neg_score:
        return "positive"
    return "neutral"

# Keywords that indicate filings / court orders — filter these OUT from RBI/NSE tabs
EXCLUDE_CIRCULAR_KEYWORDS = [
    "court", "tribunal", "writ", "petition", "judgment", "judgement",
    "filing", "filed", "stock exchange filing", "bse filing", "nse filing",
    "annual report", "quarterly result", "q1 result", "q2 result",
    "q3 result", "q4 result", "earnings", "balance sheet",
    "ipo filing", "drhp", "prospectus",
]

# ─────────────────────────────────────────────
# RSS SOURCES — Main news tabs
# ─────────────────────────────────────────────

def gn(q):
    return f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=en-IN&gl=IN&ceid=IN:en"

FEED_SOURCES = {
    "🇮🇳 India Markets": [
        gn("NIFTY OR SENSEX OR NSE OR BSE India stock market"),
        gn("RBI policy rate India"),
        gn("India stock market today"),
    ],
    "💵 Currency & Forex": [
        gn("Indian Rupee USD exchange rate"),
        gn("Dollar index DXY Rupee"),
        gn("RBI forex intervention currency"),
        gn("INR USD EUR GBP exchange"),
    ],
    "🛢️ Commodities & Oil": [
        gn("crude oil price Brent WTI"),
        gn("gold price silver commodity"),
        gn("OPEC oil production"),
    ],
    "🌍 Geopolitical Risk": [
        gn("Iran war sanctions Middle East"),
        gn("Russia Ukraine war economy"),
        gn("China Taiwan geopolitical risk"),
        gn("US sanctions tariff trade war"),
    ],
    "📊 Global Macro": [
        gn("Federal Reserve interest rate inflation"),
        gn("US economy recession GDP"),
        gn("IMF World Bank global economy"),
        gn("global inflation CPI"),
    ],
    "📰 Reuters Finance": [
        "https://feeds.reuters.com/reuters/businessNews",
        "https://feeds.reuters.com/reuters/INbusinessNews",
    ],
}

# ─────────────────────────────────────────────
# RBI CIRCULAR SOURCES
# ─────────────────────────────────────────────
RBI_CIRCULAR_FEEDS = [
    "https://www.rbi.org.in/rss/RBINotificationsRSS.xml",
    "https://www.rbi.org.in/rss/RBICircularsRSS.xml",
    gn("RBI circular master direction notification site:rbi.org.in"),
]

# ─────────────────────────────────────────────
# NSE CIRCULAR SOURCES
# ─────────────────────────────────────────────
NSE_CIRCULAR_FEEDS = [
    gn("NSE circular notice India site:nseindia.com OR site:nsearchives.nseindia.com"),
    gn("NSE India circular notice member broking 2025 OR 2026"),
    gn("National Stock Exchange circular notification"),
]

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def is_priority(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in PRIORITY_KEYWORDS)

def is_excluded_circular(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in EXCLUDE_CIRCULAR_KEYWORDS)

def parse_date(entry) -> datetime:
    for field in ("published_parsed", "updated_parsed"):
        val = getattr(entry, field, None)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)

def clean_title(title: str) -> str:
    title = re.sub(r'\s*[-–|]\s*[A-Z][^-–|]{2,40}$', '', title)
    return title.strip()

def fetch_feed(url: str, timeout: int = 6) -> list:
    articles = []
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        }
        kwargs = dict(headers=headers, timeout=timeout, verify=False)
        if PROXIES:
            kwargs["proxies"] = PROXIES
        resp = requests.get(url, **kwargs)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        for entry in feed.entries[:15]:
            title = clean_title(getattr(entry, "title", "") or "")
            link  = getattr(entry, "link", "") or ""
            if not title:
                continue
            dt = parse_date(entry)
            articles.append({
                "title":     title,
                "link":      link,
                "dt":        dt,
                "priority":  is_priority(title),
                "sentiment": get_sentiment(title),
            })
    except Exception:
        pass
    return articles

def fetch_urls_parallel(urls: list, filter_fn=None) -> list:
    articles = []
    seen = set()
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fetch_feed, url): url for url in urls}
        for future in as_completed(futures, timeout=12):
            try:
                for art in future.result():
                    key = art["title"][:60].lower()
                    if key not in seen:
                        if filter_fn is None or filter_fn(art):
                            seen.add(key)
                            articles.append(art)
            except Exception:
                pass
    articles.sort(key=lambda x: x["dt"], reverse=True)
    return articles

@st.cache_data(ttl=AUTO_REFRESH_SECONDS)
def fetch_all_feeds() -> dict:
    tasks = []
    for category, urls in FEED_SOURCES.items():
        for url in urls:
            tasks.append((category, url))

    cat_articles = defaultdict(list)
    cat_seen     = defaultdict(set)

    with ThreadPoolExecutor(max_workers=14) as ex:
        future_map = {ex.submit(fetch_feed, url): (cat, url) for cat, url in tasks}
        for future in as_completed(future_map, timeout=15):
            cat, _ = future_map[future]
            try:
                for art in future.result():
                    key = art["title"][:60].lower()
                    if key not in cat_seen[cat]:
                        cat_seen[cat].add(key)
                        cat_articles[cat].append(art)
            except Exception:
                pass

    result = {}
    for cat in FEED_SOURCES:
        arts = cat_articles.get(cat, [])
        arts.sort(key=lambda x: x["dt"], reverse=True)
        result[cat] = arts[:20]
    return result

@st.cache_data(ttl=180)
def fetch_rbi_circulars() -> list:
    arts = fetch_urls_parallel(
        RBI_CIRCULAR_FEEDS,
        filter_fn=lambda a: not is_excluded_circular(a["title"])
    )
    return arts[:25]

@st.cache_data(ttl=180)
def fetch_nse_circulars() -> list:
    arts = fetch_urls_parallel(
        NSE_CIRCULAR_FEEDS,
        filter_fn=lambda a: not is_excluded_circular(a["title"])
    )
    return arts[:25]

@st.cache_data(ttl=AUTO_REFRESH_SECONDS)
def fetch_portfolio_news() -> dict:
    """Fetch latest news for each stock in PORTFOLIO_STOCKS."""
    result = {}
    for stock in PORTFOLIO_STOCKS:
        feeds = [
            gn(f"{stock} stock NSE BSE India"),
            gn(f"{stock} share price news India"),
            gn(f"{stock} India company results earnings"),
        ]
        arts = fetch_urls_parallel(feeds)
        result[stock] = arts[:15]
    return result

def load_manual() -> list:
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_manual(data: list):
    with open(DB_FILE, "w") as f:
        json.dump(data[:20], f)

def time_ago(dt) -> str:
    if isinstance(dt, str):
        try: dt = datetime.fromisoformat(dt)
        except: return ""
    delta = datetime.now(timezone.utc) - dt
    secs  = int(delta.total_seconds())
    if secs < 60:    return f"{secs}s ago"
    if secs < 3600:  return f"{secs//60}m ago"
    if secs < 86400: return f"{secs//3600}h ago"
    return dt.strftime("%d %b")

# ─────────────────────────────────────────────
# PAGE CONFIG & CSS
# ─────────────────────────────────────────────

st.set_page_config(
    page_title="JM Financial | Risk Intelligence",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap');

html, body, [class*="css"] {
    font-family: 'IBM Plex Sans', sans-serif;
    background: #0a0c10 !important;
    color: #c9d1d9;
}
.stApp { background: #0a0c10; }

.dash-header {
    display: flex; align-items: center; gap: 16px;
    padding: 18px 0 12px;
    border-bottom: 1px solid #21262d;
    margin-bottom: 20px;
}
.dash-title {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 1.3rem; font-weight: 600;
    color: #e6edf3; letter-spacing: 0.08em;
}
.dash-sub {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.72rem; color: #8b949e;
    letter-spacing: 0.12em; text-transform: uppercase;
}
.live-badge {
    display: inline-flex; align-items: center; gap: 6px;
    background: #161b22; border: 1px solid #30363d;
    border-radius: 4px; padding: 4px 10px;
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.68rem; color: #3fb950;
}
.live-dot {
    width: 7px; height: 7px; border-radius: 50%;
    background: #3fb950; animation: pulse 1.5s infinite;
}
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }

.cat-header {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.78rem; font-weight: 600;
    color: #8b949e; letter-spacing: 0.15em; text-transform: uppercase;
    padding: 8px 0 6px; border-bottom: 1px solid #21262d; margin-bottom: 10px;
}

/* ── BASE NEWS CARD ── */
.news-card {
    background: #161b22; border: 1px solid #21262d;
    border-left: 3px solid #21262d; border-radius: 6px;
    padding: 10px 14px; margin-bottom: 8px;
    transition: border-color 0.2s, background 0.2s;
}
.news-card:hover { border-color: #388bfd; background: #1c2128; }

/* ── SENTIMENT: POSITIVE — green tint ── */
.news-card.sentiment-positive {
    border-left: 3px solid #2ea043;
    background: #0d1f15;
}
.news-card.sentiment-positive:hover {
    border-left-color: #3fb950;
    background: #112218;
}

/* ── SENTIMENT: NEGATIVE — red tint ── */
.news-card.sentiment-negative {
    border-left: 3px solid #8b1a1a;
    background: #160d0d;
}
.news-card.sentiment-negative:hover {
    border-left-color: #f85149;
    background: #1a1010;
}

/* ── PRIORITY overrides sentiment (always red + bright) ── */
.news-card.priority {
    border-left: 3px solid #f85149 !important;
    background: #1a1318 !important;
}
.news-card.priority:hover {
    border-left-color: #ff6b6b !important;
    background: #1e1520 !important;
}

/* ── MANUAL ALERT ── */
.news-card.manual { border-left: 3px solid #d29922; background: #16130a; }

/* ── CIRCULAR CARDS ── */
.news-card.rbi { border-left: 3px solid #d29922; }
.news-card.rbi:hover { border-left-color: #f0b429; background: #1c1810; }
.news-card.nse { border-left: 3px solid #388bfd; }
.news-card.nse:hover { border-left-color: #58a6ff; background: #101825; }

.card-title {
    font-size: 0.88rem; font-weight: 500;
    color: #e6edf3; line-height: 1.4; text-decoration: none;
}
.card-title:hover { color: #58a6ff; }
.card-meta {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.68rem; color: #6e7681;
    margin-top: 5px; display: flex; gap: 12px; align-items: center;
    flex-wrap: wrap;
}
.badge-priority {
    background: #3d1a1a; color: #f85149;
    border-radius: 3px; padding: 1px 7px;
    font-size: 0.62rem; font-weight: 600;
    letter-spacing: 0.08em; text-transform: uppercase;
}
.badge-positive {
    background: #0d2e14; color: #3fb950;
    border-radius: 3px; padding: 1px 7px;
    font-size: 0.62rem; font-weight: 600;
    letter-spacing: 0.06em;
}
.badge-negative {
    background: #2d1010; color: #f85149;
    border-radius: 3px; padding: 1px 7px;
    font-size: 0.62rem; font-weight: 600;
    letter-spacing: 0.06em;
}
.badge-manual {
    background: #2d2200; color: #d29922;
    border-radius: 3px; padding: 1px 7px;
    font-size: 0.62rem; font-weight: 600;
}
.badge-rbi {
    background: #2d2200; color: #d29922;
    border-radius: 3px; padding: 1px 7px;
    font-size: 0.62rem; font-weight: 600; letter-spacing: 0.06em;
}
.badge-nse {
    background: #0d1f3c; color: #58a6ff;
    border-radius: 3px; padding: 1px 7px;
    font-size: 0.62rem; font-weight: 600; letter-spacing: 0.06em;
}
.badge-stock {
    background: #1a1040; color: #a78bfa;
    border-radius: 3px; padding: 1px 7px;
    font-size: 0.62rem; font-weight: 600; letter-spacing: 0.06em;
}

/* Portfolio stock section header */
.stock-section-header {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.82rem; font-weight: 600;
    color: #a78bfa; letter-spacing: 0.12em; text-transform: uppercase;
    padding: 12px 0 6px;
    border-bottom: 1px solid #2d2060;
    margin-bottom: 10px;
    margin-top: 14px;
}
.stock-sentiment-bar {
    display: flex; gap: 14px; margin-bottom: 10px; flex-wrap: wrap;
}
.stock-sent-item {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 0.68rem; color: #6e7681;
}
.stock-sent-item .pos { color: #3fb950; font-weight: 600; }
.stock-sent-item .neg { color: #f85149; font-weight: 600; }
.stock-sent-item .neu { color: #8b949e; font-weight: 600; }

.summary-bar {
    display: flex; gap: 20px; flex-wrap: wrap;
    background: #161b22; border: 1px solid #21262d;
    border-radius: 6px; padding: 10px 18px; margin-bottom: 20px;
}
.sum-item { text-align: center; }
.sum-val {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 1.4rem; font-weight: 600; color: #e6edf3;
}
.sum-val.red { color: #f85149; }
.sum-val.green { color: #3fb950; }
.sum-label { font-size: 0.68rem; color: #6e7681; text-transform: uppercase; letter-spacing: 0.1em; }

section[data-testid="stSidebar"] {
    background: #0d1117 !important;
    border-right: 1px solid #21262d;
}
.stButton > button {
    background: #21262d; color: #c9d1d9;
    border: 1px solid #30363d; border-radius: 5px;
    font-family: 'IBM Plex Mono', monospace; font-size: 0.78rem;
}
.stButton > button:hover { background: #388bfd; color: #fff; border-color: #388bfd; }
[data-testid="stTextInput"] input {
    background: #161b22 !important; border: 1px solid #30363d !important;
    color: #c9d1d9 !important; font-family: 'IBM Plex Mono', monospace; font-size: 0.82rem;
}
div[role="tablist"] button {
    font-family: 'IBM Plex Mono', monospace !important;
    font-size: 0.78rem !important; color: #8b949e !important;
}
div[role="tablist"] button[aria-selected="true"] {
    color: #e6edf3 !important; border-bottom: 2px solid #388bfd !important;
}
.stMarkdown hr { border-color: #21262d; }
.stSpinner > div { border-top-color: #388bfd !important; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# SIDEBAR — Stock lookup + Admin only (circular lookup removed)
# ─────────────────────────────────────────────

with st.sidebar:
    st.markdown("""
    <div style='font-family:IBM Plex Mono,monospace;font-size:0.9rem;
    color:#e6edf3;font-weight:600;padding:8px 0 16px;
    border-bottom:1px solid #21262d;margin-bottom:16px;'>
    🛡️ RISK DESK
    </div>""", unsafe_allow_html=True)

    auto_refresh = st.toggle("⟳ Auto-refresh (90s)", value=True)

    if st.button("🔄 Refresh Now", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")

    # ── Stock News Lookup ──
    st.markdown("""<div style='font-family:IBM Plex Mono,monospace;
    font-size:0.72rem;color:#3fb950;text-transform:uppercase;
    letter-spacing:0.1em;margin-bottom:8px;'>📈 Stock News Lookup</div>""",
    unsafe_allow_html=True)

    stock_query = st.text_input(
        "Stock", label_visibility="collapsed",
        placeholder="e.g. Reliance, HDFC Bank, INFY",
        key="stock_input"
    )

    if stock_query:
        sc = stock_query.strip()
        stock_feeds = [
            gn(sc + " stock NSE BSE India"),
            gn(sc + " share price earnings results"),
            gn(sc + " India company news"),
        ]
        with st.spinner(f"Fetching {sc}…"):
            stock_arts = fetch_urls_parallel(stock_feeds)

        if stock_arts:
            st.markdown(f"""<div style='font-family:IBM Plex Mono,monospace;
            font-size:0.68rem;color:#8b949e;margin-bottom:8px;'>
            {len(stock_arts)} results · <span style='color:#e6edf3;'>
            {sc.upper()}</span></div>""", unsafe_allow_html=True)

            for art in stock_arts[:12]:
                is_p      = art.get("priority", False)
                sentiment = art.get("sentiment", "neutral")
                if is_p:
                    border, bg = "#f85149", "#1a1318"
                elif sentiment == "positive":
                    border, bg = "#2ea043", "#0d1f15"
                elif sentiment == "negative":
                    border, bg = "#8b1a1a", "#160d0d"
                else:
                    border, bg = "#388bfd", "#161b22"
                ago  = time_ago(art["dt"])
                prio = '<span style="color:#f85149;font-size:0.6rem;font-weight:700;">⚡ </span>' if is_p else ""
                sent_icon = "🟢 " if sentiment == "positive" else ("🔴 " if sentiment == "negative" else "")
                st.markdown(f"""
                <div style="background:{bg};border:1px solid #21262d;
                border-left:3px solid {border};border-radius:5px;
                padding:8px 12px;margin-bottom:6px;">
                  <a href="{art['link']}" target="_blank"
                     style="font-size:0.82rem;color:#e6edf3;
                     text-decoration:none;line-height:1.4;display:block;">
                     {prio}{sent_icon}{art['title']}
                  </a>
                  <div style="font-family:IBM Plex Mono,monospace;
                  font-size:0.65rem;color:#6e7681;margin-top:4px;">{ago}</div>
                </div>""", unsafe_allow_html=True)
        else:
            st.markdown("""<div style='font-family:IBM Plex Mono,monospace;
            font-size:0.75rem;color:#6e7681;padding:8px 0;'>
            No results. Try the full company name.</div>""", unsafe_allow_html=True)

    st.markdown("---")

    # ── Admin: push internal alert ──
    st.markdown("""<div style='font-family:IBM Plex Mono,monospace;
    font-size:0.72rem;color:#8b949e;text-transform:uppercase;
    letter-spacing:0.1em;margin-bottom:8px;'>Internal Alert</div>""",
    unsafe_allow_html=True)

    pwd = st.text_input("Password", type="password",
                        label_visibility="collapsed",
                        placeholder="Admin password")
    if pwd == ADMIN_PASSWORD:
        headline_text = st.text_input("Headline", placeholder="e.g. NIFTY circuit breaker triggered")
        headline_link = st.text_input("Link (optional)", placeholder="https://...")
        if st.button("🚨 Push Alert", use_container_width=True):
            if headline_text:
                current = load_manual()
                current.insert(0, {
                    "title":     headline_text,
                    "link":      headline_link or "#",
                    "dt":        datetime.now(timezone.utc).isoformat(),
                    "priority":  True,
                    "sentiment": "negative",
                    "manual":    True,
                })
                save_manual(current)
                st.success("Alert pushed!")
                time.sleep(0.5)
                st.rerun()

    st.markdown("---")
    st.markdown("""<div style='font-family:IBM Plex Mono,monospace;
    font-size:0.65rem;color:#484f58;'>
    Data: Google News · Reuters · RBI · NSE<br>
    News refresh: 90s · Circulars: 3min
    </div>""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# FETCH ALL DATA
# ─────────────────────────────────────────────

with st.spinner("Fetching live feeds…"):
    all_data        = fetch_all_feeds()
    rbi_circulars   = fetch_rbi_circulars()
    nse_circulars   = fetch_nse_circulars()
    portfolio_data  = fetch_portfolio_news()

manual_items = load_manual()

# Build combined "all news" list
all_articles = []
for cat, arts in all_data.items():
    for art in arts:
        all_articles.append({**art, "category": cat, "manual": False})

# Prepend manual alerts
for m in manual_items:
    all_articles.insert(0, {
        "title":     m["title"],
        "link":      m["link"],
        "dt":        m["dt"],
        "priority":  True,
        "sentiment": "negative",
        "category":  "🚨 Internal Alert",
        "manual":    True,
    })

all_articles.sort(key=lambda x: (
    datetime.fromisoformat(x["dt"]) if isinstance(x["dt"], str) else x["dt"]
), reverse=True)

priority_count  = sum(1 for a in all_articles if a.get("priority"))
positive_count  = sum(1 for a in all_articles if a.get("sentiment") == "positive")
negative_count  = sum(1 for a in all_articles if a.get("sentiment") == "negative")

# ─────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────

now_str = datetime.now().strftime("%d %b %Y · %H:%M:%S IST")

st.markdown(f"""
<div class="dash-header">
  <div>
    <div class="dash-title">JM FINANCIAL · RISK INTELLIGENCE</div>
    <div class="dash-sub">{now_str}</div>
  </div>
  <div style="margin-left:auto">
    <div class="live-badge"><div class="live-dot"></div>LIVE</div>
  </div>
</div>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# SUMMARY BAR — now includes sentiment counts
# ─────────────────────────────────────────────

col1, col2, col3, col4, col5, col6 = st.columns(6)
with col1:
    c = "red" if priority_count > 0 else "green"
    st.markdown(f"""<div class="summary-bar" style="justify-content:center">
    <div class="sum-item"><div class="sum-val {c}">{priority_count}</div>
    <div class="sum-label">Priority Alerts</div></div></div>""", unsafe_allow_html=True)
with col2:
    st.markdown(f"""<div class="summary-bar" style="justify-content:center">
    <div class="sum-item"><div class="sum-val">{len(all_articles)}</div>
    <div class="sum-label">Total Headlines</div></div></div>""", unsafe_allow_html=True)
with col3:
    st.markdown(f"""<div class="summary-bar" style="justify-content:center">
    <div class="sum-item"><div class="sum-val green">{positive_count}</div>
    <div class="sum-label">🟢 Positive</div></div></div>""", unsafe_allow_html=True)
with col4:
    st.markdown(f"""<div class="summary-bar" style="justify-content:center">
    <div class="sum-item"><div class="sum-val red">{negative_count}</div>
    <div class="sum-label">🔴 Negative</div></div></div>""", unsafe_allow_html=True)
with col5:
    st.markdown(f"""<div class="summary-bar" style="justify-content:center">
    <div class="sum-item"><div class="sum-val">{len(rbi_circulars)}</div>
    <div class="sum-label">RBI Circulars</div></div></div>""", unsafe_allow_html=True)
with col6:
    st.markdown(f"""<div class="summary-bar" style="justify-content:center">
    <div class="sum-item"><div class="sum-val">{len(nse_circulars)}</div>
    <div class="sum-label">NSE Circulars</div></div></div>""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# RENDER HELPERS
# ─────────────────────────────────────────────

def render_news_cards(articles: list):
    if not articles:
        st.markdown("""<div style='font-family:IBM Plex Mono,monospace;
        font-size:0.8rem;color:#484f58;padding:20px 0;'>
        No articles found.</div>""", unsafe_allow_html=True)
        return
    for art in articles:
        is_prio   = art.get("priority", False)
        is_manual = art.get("manual", False)
        sentiment = art.get("sentiment", "neutral")
        cat_label = art.get("category", "")

        # Determine card CSS class — priority always wins
        if is_manual:
            card_class = "news-card manual"
        elif is_prio:
            card_class = "news-card priority"
        elif sentiment == "positive":
            card_class = "news-card sentiment-positive"
        elif sentiment == "negative":
            card_class = "news-card sentiment-negative"
        else:
            card_class = "news-card"

        dt_obj = art["dt"]
        if isinstance(dt_obj, str):
            try: dt_obj = datetime.fromisoformat(dt_obj)
            except: dt_obj = datetime.now(timezone.utc)
        ago = time_ago(dt_obj)

        prio_badge   = '<span class="badge-priority">⚡ PRIORITY</span>' if is_prio else ""
        manual_badge = '<span class="badge-manual">📢 INTERNAL</span>' if is_manual else ""
        if not is_prio and not is_manual:
            if sentiment == "positive":
                sent_badge = '<span class="badge-positive">▲ POSITIVE</span>'
            elif sentiment == "negative":
                sent_badge = '<span class="badge-negative">▼ NEGATIVE</span>'
            else:
                sent_badge = ""
        else:
            sent_badge = ""

        st.markdown(f"""
        <div class="{card_class}">
          <a class="card-title" href="{art['link']}" target="_blank">{art['title']}</a>
          <div class="card-meta">
            <span>{ago}</span>
            <span style="color:#30363d">·</span>
            <span>{cat_label}</span>
            {prio_badge}{manual_badge}{sent_badge}
          </div>
        </div>""", unsafe_allow_html=True)

def render_circular_cards(articles: list, badge_class: str, card_extra_class: str):
    if not articles:
        st.markdown("""<div style='font-family:IBM Plex Mono,monospace;
        font-size:0.8rem;color:#484f58;padding:20px 0;'>
        No circulars fetched — check network or try Refresh Now.</div>""",
        unsafe_allow_html=True)
        return
    for art in articles:
        is_prio  = art.get("priority", False)
        card_cls = f"news-card {card_extra_class}" + (" priority" if is_prio else "")
        ago      = time_ago(art["dt"])
        prio_badge  = '<span class="badge-priority">⚡ PRIORITY</span>' if is_prio else ""
        type_badge  = f'<span class="{badge_class}">{"RBI" if "rbi" in card_extra_class else "NSE"} CIRCULAR</span>'
        st.markdown(f"""
        <div class="{card_cls}">
          <a class="card-title" href="{art['link']}" target="_blank">{art['title']}</a>
          <div class="card-meta">
            <span>{ago}</span>
            {type_badge}
            {prio_badge}
          </div>
        </div>""", unsafe_allow_html=True)

def render_portfolio_tab(portfolio_data: dict):
    """Render news for each portfolio stock, grouped by stock name."""
    if not portfolio_data:
        st.markdown("""<div style='font-family:IBM Plex Mono,monospace;
        font-size:0.8rem;color:#484f58;padding:20px 0;'>
        No portfolio stocks configured.</div>""", unsafe_allow_html=True)
        return

    for stock, articles in portfolio_data.items():
        pos = sum(1 for a in articles if a.get("sentiment") == "positive")
        neg = sum(1 for a in articles if a.get("sentiment") == "negative")
        neu = len(articles) - pos - neg

        st.markdown(f"""
        <div class="stock-section-header">📌 {stock.upper()}</div>
        <div class="stock-sentiment-bar">
          <div class="stock-sent-item">Headlines: <span style="color:#c9d1d9;font-weight:600;">{len(articles)}</span></div>
          <div class="stock-sent-item">🟢 Positive: <span class="pos">{pos}</span></div>
          <div class="stock-sent-item">🔴 Negative: <span class="neg">{neg}</span></div>
          <div class="stock-sent-item">⚪ Neutral: <span class="neu">{neu}</span></div>
        </div>
        """, unsafe_allow_html=True)

        if not articles:
            st.markdown("""<div style='font-family:IBM Plex Mono,monospace;
            font-size:0.78rem;color:#484f58;padding:6px 0 12px;'>
            No recent news found for this stock.</div>""", unsafe_allow_html=True)
            continue

        for art in articles:
            is_prio   = art.get("priority", False)
            sentiment = art.get("sentiment", "neutral")

            if is_prio:
                card_class = "news-card priority"
            elif sentiment == "positive":
                card_class = "news-card sentiment-positive"
            elif sentiment == "negative":
                card_class = "news-card sentiment-negative"
            else:
                card_class = "news-card"

            dt_obj = art["dt"]
            if isinstance(dt_obj, str):
                try: dt_obj = datetime.fromisoformat(dt_obj)
                except: dt_obj = datetime.now(timezone.utc)
            ago = time_ago(dt_obj)

            prio_badge = '<span class="badge-priority">⚡ PRIORITY</span>' if is_prio else ""
            stock_badge = f'<span class="badge-stock">{stock.upper()}</span>'
            if sentiment == "positive" and not is_prio:
                sent_badge = '<span class="badge-positive">▲ POSITIVE</span>'
            elif sentiment == "negative" and not is_prio:
                sent_badge = '<span class="badge-negative">▼ NEGATIVE</span>'
            else:
                sent_badge = ""

            st.markdown(f"""
            <div class="{card_class}">
              <a class="card-title" href="{art['link']}" target="_blank">{art['title']}</a>
              <div class="card-meta">
                <span>{ago}</span>
                {stock_badge}
                {sent_badge}
                {prio_badge}
              </div>
            </div>""", unsafe_allow_html=True)

        st.markdown("<div style='margin-bottom:8px;'></div>", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# MAIN TABS
# ─────────────────────────────────────────────

news_cats = list(FEED_SOURCES.keys())

tab_all, tab_priority, tab_portfolio, tab_rbi, tab_nse, *cat_tabs = st.tabs(
    ["All News", "🔴 Priority", "📂 Portfolio Stocks", "🏛️ RBI Circulars", "🔵 NSE Circulars"] + news_cats
)

with tab_all:
    render_news_cards(all_articles)

with tab_priority:
    prio_arts = [a for a in all_articles if a.get("priority")]
    if prio_arts:
        st.markdown(f"""<div style='font-family:IBM Plex Mono,monospace;
        font-size:0.72rem;color:#f85149;margin-bottom:12px;'>
        ⚡ {len(prio_arts)} priority items across all feeds</div>""",
        unsafe_allow_html=True)
    render_news_cards(prio_arts)

with tab_portfolio:
    total_stocks = len(PORTFOLIO_STOCKS)
    total_port_articles = sum(len(v) for v in portfolio_data.values())
    st.markdown(f"""<div style='font-family:IBM Plex Mono,monospace;
    font-size:0.72rem;color:#a78bfa;margin-bottom:4px;'>
    Tracking <span style='color:#e6edf3;font-weight:600;'>{total_stocks} stocks</span>
    · {total_port_articles} total headlines
    <br><span style='color:#484f58;'>
    Edit PORTFOLIO_STOCKS list in the code to add/remove stocks. Max 10 recommended.
    </span></div>""", unsafe_allow_html=True)
    st.markdown("---")
    render_portfolio_tab(portfolio_data)

with tab_rbi:
    st.markdown("""<div style='font-family:IBM Plex Mono,monospace;
    font-size:0.72rem;color:#d29922;margin-bottom:4px;'>
    Sources: RBI Notifications RSS · RBI Circulars RSS · RBI Press Releases
    <br><span style='color:#484f58;'>Filings, court orders and results are filtered out. Refreshes every 3 minutes.</span>
    </div>""", unsafe_allow_html=True)
    st.markdown("---")
    render_circular_cards(rbi_circulars, "badge-rbi", "rbi")

with tab_nse:
    st.markdown("""<div style='font-family:IBM Plex Mono,monospace;
    font-size:0.72rem;color:#388bfd;margin-bottom:4px;'>
    Sources: NSE India circulars & member notices via Google News
    <br><span style='color:#484f58;'>Filings, court orders and results are filtered out. Refreshes every 3 minutes.</span>
    </div>""", unsafe_allow_html=True)
    st.markdown("---")
    render_circular_cards(nse_circulars, "badge-nse", "nse")

for tab, cat in zip(cat_tabs, news_cats):
    with tab:
        st.markdown(f'<div class="cat-header">{cat}</div>', unsafe_allow_html=True)
        arts = [{**a, "category": cat, "manual": False} for a in all_data.get(cat, [])]
        render_news_cards(arts)

# ─────────────────────────────────────────────
# AUTO-REFRESH
# ─────────────────────────────────────────────

if auto_refresh:
    placeholder = st.empty()
    for remaining in range(AUTO_REFRESH_SECONDS, 0, -1):
        placeholder.markdown(f"""<div style='font-family:IBM Plex Mono,monospace;
        font-size:0.68rem;color:#484f58;text-align:right;padding-top:10px;'>
        Next refresh in {remaining}s</div>""", unsafe_allow_html=True)
        time.sleep(1)
    st.cache_data.clear()
    st.rerun()