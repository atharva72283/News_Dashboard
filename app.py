"""
JM Financial | Risk Intelligence Dashboard  v4.0
=================================================
✅ Live Market Monitor — yfinance (price + % change via fast_info)
✅ Stock Search by Name / NSE Code / BSE Code / ISIN in sidebar
✅ RBI Circulars — official rbi.org.in RSS (current FY only)
✅ NSE Circulars — official nsearchives RSS (current FY only)
✅ Reuters replaced by Moneycontrol + Economic Times + Mint RSS
✅ IST timestamps via pytz
✅ Admin news push panel with segment dropdown
✅ Keyword-based sentiment (no AI)
✅ Auto-refresh 90s

Install:  pip install streamlit requests beautifulsoup4 lxml feedparser yfinance pytz
Run:      streamlit run app.py
"""

import streamlit as st
import feedparser
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from datetime import datetime, timezone, timedelta
import pytz
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import json
import os
import urllib.parse
from collections import defaultdict
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
DB_FILE              = "manual_headlines.json"
ADMIN_PASSWORD       = "JM_RISK_2026"
AUTO_REFRESH_SECONDS = 90

PROXY_USER = ""
PROXY_PASS = ""
PROXY_ADDR = ""

PROXIES = None
if PROXY_USER and PROXY_ADDR:
    eu = urllib.parse.quote(PROXY_USER)
    ep = urllib.parse.quote(PROXY_PASS)
    PROXIES = {"http": f"http://{eu}:{ep}@{PROXY_ADDR}", "https": f"http://{eu}:{ep}@{PROXY_ADDR}"}

# ─────────────────────────────────────────────
# PORTFOLIO STOCKS (default)
# ─────────────────────────────────────────────
PORTFOLIO_STOCKS = [
    "Sammaan Capital",
    "Suzlon",
    "Religare",
    "Valor Estate",
]

# ─────────────────────────────────────────────
# NSE/BSE CODE → Yahoo Finance ticker mapping
# (user can search by any of these identifiers)
# ─────────────────────────────────────────────
NSE_BSE_YAHOO_MAP = {
    # NSE code    : yahoo ticker
    "RELIANCE":    "RELIANCE.NS",
    "TCS":         "TCS.NS",
    "INFY":        "INFY.NS",
    "HDFCBANK":    "HDFCBANK.NS",
    "ICICIBANK":   "ICICIBANK.NS",
    "SBIN":        "SBIN.NS",
    "AXISBANK":    "AXISBANK.NS",
    "KOTAKBANK":   "KOTAKBANK.NS",
    "WIPRO":       "WIPRO.NS",
    "LT":          "LT.NS",
    "SUZLON":      "SUZLON.NS",
    "RELIGARE":    "RELIGARE.NS",
    # BSE codes (numeric) — yfinance supports BSE too
    "500325":      "RELIANCE.BO",
    "532540":      "TCS.BO",
    "500209":      "INFY.BO",
    "500180":      "HDFCBANK.BO",
    "532174":      "ICICIBANK.BO",
    "500112":      "SBIN.BO",
    "532215":      "AXISBANK.BO",
}

# ─────────────────────────────────────────────
# LIVE MARKET TICKERS
# ─────────────────────────────────────────────
MARKET_TICKERS = {
    "NIFTY 50":    ("^NSEI",    "₹", False),   # (symbol, unit, is_index)
    "SENSEX":      ("^BSESN",   "₹", False),
    "BANK NIFTY":  ("^NSEBANK", "₹", False),
    "CRUDE (WTI)": ("CL=F",     "$", True),
    "BRENT":       ("BZ=F",     "$", True),
    "GOLD":        ("GC=F",     "$", True),
    "SILVER":      ("SI=F",     "$", True),
}

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
# SENTIMENT KEYWORDS (improved — context-aware)
# ─────────────────────────────────────────────
NEGATIVE_KEYWORDS = [
    "crash", "plunge", "fall", "drop", "decline", "loss", "losses", "slump",
    "selloff", "sell-off", "tumble", "sink", "sinks", "sank", "collapse",
    "crisis", "recession", "default", "fraud", "scam", "ban",
    "penalty", "suspension", "warning", "threat", "attack", "war",
    "conflict", "sanctions", "halt", "circuit breaker", "devaluation",
    "downgrade", "probe", "investigation", "sebi action", "npa",
    "writeoff", "write-off", "layoff", "layoffs", "bankrupt", "insolvency",
    "miss", "misses", "disappoints", "disappointing", "weak", "slowdown",
    "bearish", "bear market", "correction", "fear", "panic",
    "fii selling", "fpi selling", "outflow", "outflows", "dumped", "dumping",
]

POSITIVE_KEYWORDS = [
    "rally", "surge", "gain", "gains", "rise", "rises", "rose",
    "jump", "jumps", "jumped", "soar", "soars", "soared",
    "record high", "all-time high", "ath", "profit", "profits",
    "revenue", "growth", "upgrade", "bullish", "bull",
    "outperform", "beat", "beats", "strong", "robust",
    "boost", "boosts", "boosted",
    "fii buying", "fpi buying", "dii buying", "institutional buying",
    "net buyer", "net inflow", "inflow", "inflows",
    "investment", "win", "wins", "expansion",
    "rate cut", "rate cuts", "easing",
    "recovery", "recovers", "rebound", "rebounds", "optimism",
    "partnership", "deal", "acquisition", "merger",
    "dividend", "buyback", "approval", "approved", "launches",
    "breakthrough", "innovation", "milestone", "commissioning",
    "capacity addition", "capex", "turnaround",
]

def get_sentiment(title: str) -> str:
    t = title.lower()
    neg = sum(1 for kw in NEGATIVE_KEYWORDS if kw in t)
    pos = sum(1 for kw in POSITIVE_KEYWORDS if kw in t)
    if neg > pos:  return "negative"
    if pos > neg:  return "positive"
    return "neutral"

def is_priority(title: str) -> bool:
    return any(kw in title.lower() for kw in PRIORITY_KEYWORDS)

EXCLUDE_CIRCULAR_KW = [
    "court", "tribunal", "writ", "petition", "judgment", "judgement",
    "annual report", "quarterly result", "q1", "q2", "q3", "q4",
    "balance sheet", "ipo filing", "drhp",
]

# Only show circulars from current financial year (April onwards)
CURRENT_FY_START = datetime(2025, 4, 1, tzinfo=timezone.utc)

# ─────────────────────────────────────────────
# TIME HELPERS
# ─────────────────────────────────────────────

def to_ist(dt: datetime) -> datetime:
    if dt is None:
        return datetime.now(IST)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)

def fmt_ist(dt: datetime) -> str:
    """Format datetime as IST string."""
    ist_dt = to_ist(dt)
    return ist_dt.strftime("%d %b %Y %I:%M %p IST")

def time_ago(dt: datetime) -> str:
    if not isinstance(dt, datetime):
        return "unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = datetime.now(timezone.utc) - dt
    s = int(diff.total_seconds())
    if s < 0:      return "just now"
    if s < 60:     return f"{s}s ago"
    if s < 3600:   return f"{s//60}m ago"
    if s < 86400:  return f"{s//3600}h ago"
    return fmt_ist(dt)   # show full date for old news

def parse_dt(entry) -> datetime:
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)

def is_recent(dt: datetime, days: int = 7) -> bool:
    """Return True if dt is within last N days."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days <= days

def is_current_fy(dt: datetime) -> bool:
    """Return True if dt is in current financial year."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= CURRENT_FY_START

# ─────────────────────────────────────────────
# LIVE MARKET DATA — yfinance fast_info
# ─────────────────────────────────────────────

@st.cache_data(ttl=60, show_spinner=False)
def fetch_market_data() -> dict:
    """
    Uses yfinance Ticker.fast_info for reliable last_price + previous_close.
    Much more stable than history()-based approach.
    """
    results = {}
    for name, (sym, unit, _) in MARKET_TICKERS.items():
        try:
            t = yf.Ticker(sym)
            fi = t.fast_info
            price = float(fi.last_price)
            prev  = float(fi.previous_close)
            change = price - prev
            pct    = (change / prev * 100) if prev else 0.0
            results[name] = {"price": price, "change": change, "pct": pct, "unit": unit}
        except Exception:
            results[name] = {"price": None, "change": None, "pct": None, "unit": unit}
    return results

@st.cache_data(ttl=300, show_spinner=False)
def fetch_stock_quote(symbol: str) -> dict:
    """
    Fetch quote for a user-searched stock by NSE/BSE code or name.
    Returns price, change, pct, name.
    """
    # Normalise input
    sym_upper = symbol.strip().upper()

    # Check direct mapping first
    yahoo_sym = NSE_BSE_YAHOO_MAP.get(sym_upper)

    # If not in map, try appending .NS or .BO
    if not yahoo_sym:
        if sym_upper.isdigit():              # BSE code
            yahoo_sym = sym_upper + ".BO"
        elif sym_upper.isalpha():            # NSE code
            yahoo_sym = sym_upper + ".NS"
        else:                               # Try raw
            yahoo_sym = sym_upper

    try:
        t = yf.Ticker(yahoo_sym)
        fi = t.fast_info
        info = t.info
        price  = float(fi.last_price)
        prev   = float(fi.previous_close)
        change = price - prev
        pct    = (change / prev * 100) if prev else 0.0
        comp_name = info.get("longName") or info.get("shortName") or sym_upper
        return {
            "found":   True,
            "name":    comp_name,
            "symbol":  yahoo_sym,
            "price":   price,
            "change":  change,
            "pct":     pct,
            "error":   None,
        }
    except Exception as e:
        return {"found": False, "error": str(e), "symbol": yahoo_sym}

# ─────────────────────────────────────────────
# RSS SOURCES  (Reuters replaced by 3 India sources)
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
    ],
    "🛢️ Commodities & Oil": [
        gn("crude oil price Brent WTI"),
        gn("gold price silver commodity India"),
        gn("OPEC oil production today"),
    ],
    "🌍 Geopolitical Risk": [
        gn("Iran war sanctions Middle East"),
        gn("Russia Ukraine war economy"),
        gn("US sanctions tariff trade war India"),
    ],
    "📊 Global Macro": [
        gn("Federal Reserve interest rate inflation"),
        gn("US economy recession GDP"),
        gn("IMF World Bank global economy"),
    ],
    "📈 Moneycontrol": [
        "https://www.moneycontrol.com/rss/latestnews.xml",
        "https://www.moneycontrol.com/rss/marketreports.xml",
        "https://www.moneycontrol.com/rss/business.xml",
    ],
    "📰 Economic Times": [
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
        "https://economictimes.indiatimes.com/news/economy/rssfeeds/4719148.cms",
    ],
    "🗞️ Mint Markets": [
        "https://www.livemint.com/rss/markets",
        "https://www.livemint.com/rss/economy",
        "https://www.livemint.com/rss/companies",
    ],
}

# ─────────────────────────────────────────────
# RBI CIRCULARS — official rbi.org.in
# ─────────────────────────────────────────────
RBI_RSS_FEEDS = [
    "https://rbi.org.in/notifications_rss.xml",
    "https://rbi.org.in/pressreleases_rss.xml",
]
RBI_ASPX_URLS = [
    "https://www.rbi.org.in/Scripts/BS_CircularIndexDisplay.aspx",
    "https://www.rbi.org.in/Scripts/NotificationUser.aspx",
]

def fetch_rbi_aspx(url: str) -> list:
    try:
        from bs4 import BeautifulSoup
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        resp = requests.get(url, headers=headers, timeout=15, verify=False,
                            proxies=PROXIES or {})
        soup = BeautifulSoup(resp.content, "lxml")
        items = []
        for a in soup.find_all("a", href=True):
            href  = a["href"].strip()
            title = a.get_text(strip=True)
            if not title or len(title) < 20:
                continue
            if any(kw in href.lower() for kw in ["notification", "circular", "rdocs", "masters"]):
                if not href.startswith("http"):
                    href = "https://www.rbi.org.in" + href
                dt_now = datetime.now(timezone.utc)
                items.append({
                    "title": title, "link": href, "dt": dt_now,
                    "priority": is_priority(title),
                    "sentiment": get_sentiment(title),
                    "source": "RBI Direct",
                })
            if len(items) >= 15:
                break
        return items
    except Exception:
        return []

def fetch_rbi_circulars() -> list:
    all_items = []
    seen = set()

    for url in RBI_RSS_FEEDS:
        try:
            resp = requests.get(url, timeout=12, verify=False, proxies=PROXIES or {})
            feed = feedparser.parse(resp.content)
            for entry in feed.entries[:30]:
                title = entry.get("title", "").strip()
                link  = entry.get("link", "#")
                dt    = parse_dt(entry)
                if not title or title in seen:
                    continue
                if not is_current_fy(dt):           # Skip old FY circulars
                    continue
                if any(kw in title.lower() for kw in EXCLUDE_CIRCULAR_KW):
                    continue
                seen.add(title)
                all_items.append({
                    "title": title, "link": link, "dt": dt,
                    "priority": is_priority(title),
                    "sentiment": get_sentiment(title),
                    "source": "RBI RSS",
                })
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(fetch_rbi_aspx, u) for u in RBI_ASPX_URLS]
        for fut in as_completed(futs):
            for item in fut.result():
                if item["title"] not in seen and is_current_fy(item["dt"]):
                    seen.add(item["title"])
                    all_items.append(item)

    all_items.sort(key=lambda x: x["dt"], reverse=True)
    return all_items[:50]

# ─────────────────────────────────────────────
# NSE CIRCULARS — official nsearchives RSS
# ─────────────────────────────────────────────
NSE_RSS_FEEDS = [
    "https://nsearchives.nseindia.com/content/RSS/Circulars.xml",
    "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml",
]

def fetch_nse_circulars() -> list:
    all_items = []
    seen = set()

    for url in NSE_RSS_FEEDS:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept": "application/rss+xml,application/xml,text/xml,*/*",
                "Referer": "https://www.nseindia.com/",
            }
            resp = requests.get(url, headers=headers, timeout=15, verify=False,
                                proxies=PROXIES or {})
            feed = feedparser.parse(resp.content)
            for entry in feed.entries[:30]:
                title = entry.get("title", "").strip()
                link  = entry.get("link", "#")
                dt    = parse_dt(entry)
                if not title or title in seen:
                    continue
                if not is_current_fy(dt):           # Skip old FY circulars
                    continue
                if any(kw in title.lower() for kw in EXCLUDE_CIRCULAR_KW):
                    continue
                seen.add(title)
                all_items.append({
                    "title": title, "link": link, "dt": dt,
                    "priority": is_priority(title),
                    "sentiment": get_sentiment(title),
                    "source": "NSE RSS",
                })
        except Exception:
            pass

    all_items.sort(key=lambda x: x["dt"], reverse=True)
    return all_items[:50]

# ─────────────────────────────────────────────
# GENERAL FEED FETCHING
# ─────────────────────────────────────────────

def fetch_feed(url: str) -> list:
    try:
        resp = requests.get(url, timeout=10, verify=False, proxies=PROXIES or {})
        feed = feedparser.parse(resp.content)
        items = []
        for entry in feed.entries[:20]:
            title = entry.get("title", "").strip()
            link  = entry.get("link", "#")
            dt    = parse_dt(entry)
            if not title or not is_recent(dt, days=3):   # only last 3 days
                continue
            items.append({
                "title": title, "link": link, "dt": dt,
                "priority": is_priority(title),
                "sentiment": get_sentiment(title),
            })
        return items
    except Exception:
        return []

def fetch_all_feeds(feed_dict: dict) -> dict:
    results = defaultdict(list)
    all_urls = [(cat, url) for cat, urls in feed_dict.items() for url in urls]
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(fetch_feed, url): cat for cat, url in all_urls}
        for fut in as_completed(futures):
            results[futures[fut]].extend(fut.result())
    for cat in results:
        # Deduplicate by title within category
        seen = set()
        deduped = []
        for a in results[cat]:
            if a["title"] not in seen:
                seen.add(a["title"])
                deduped.append(a)
        results[cat] = sorted(deduped, key=lambda x: x["dt"], reverse=True)
    return dict(results)

# ─────────────────────────────────────────────
# MANUAL HEADLINES (Admin)
# ─────────────────────────────────────────────

def load_manual_headlines() -> list:
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE) as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_manual_headlines(items: list):
    with open(DB_FILE, "w") as f:
        json.dump(items, f, default=str)

# ─────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────

st.set_page_config(
    page_title="JM Financial | Risk Intelligence",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&display=swap');
html, body, [class*="css"] {
    font-family: 'IBM Plex Mono', 'Courier New', monospace !important;
    background-color: #0d1117 !important;
    color: #c9d1d9 !important;
}
.block-container { padding-top: 0.4rem !important; max-width: 1440px !important; }
section[data-testid="stSidebar"] { background: #0d1117 !important; border-right: 1px solid #21262d; }

/* ── MARKET MONITOR ── */
.market-monitor {
    background: #0d1117;
    border: 1px solid #21262d;
    border-radius: 10px;
    padding: 10px 16px 12px;
    margin-bottom: 12px;
}
.mm-title {
    font-size: 0.6rem; font-weight:700; color:#484f58;
    letter-spacing:0.14em; text-transform:uppercase; margin-bottom:8px;
}
.ticker-grid { display:flex; flex-wrap:wrap; gap:8px; }
.ticker-card {
    background:#161b22; border:1px solid #21262d;
    border-radius:8px; padding:7px 12px;
    min-width:110px; flex:1; text-align:center;
}
.ticker-card.up   { border-bottom:2px solid #3fb950; }
.ticker-card.down { border-bottom:2px solid #f85149; }
.ticker-card.flat { border-bottom:2px solid #484f58; }
.t-name  { font-size:0.56rem; color:#484f58; font-weight:700; text-transform:uppercase; letter-spacing:0.05em; }
.t-price { font-size:0.95rem; color:#e6edf3; font-weight:700; margin:2px 0; }
.t-up    { font-size:0.63rem; color:#3fb950; font-weight:600; }
.t-dn    { font-size:0.63rem; color:#f85149; font-weight:600; }
.t-flat  { font-size:0.63rem; color:#484f58; font-weight:600; }

/* ── HEADER ── */
.dash-header {
    background: linear-gradient(135deg,#161b22,#0d1117);
    border:1px solid #21262d; border-radius:8px;
    padding:10px 18px; margin-bottom:10px;
}
.dash-title { font-size:1.0rem; font-weight:700; color:#e6edf3; letter-spacing:0.04em; }
.dash-sub   { font-size:0.65rem; color:#484f58; margin-top:2px; }
.live-dot {
    width:7px; height:7px; background:#3fb950; border-radius:50%;
    display:inline-block; margin-right:5px;
    animation: pulse 2s infinite;
}
@keyframes pulse { 0%,100%{opacity:1}50%{opacity:0.25} }

/* ── STOCK SEARCH RESULT CARD ── */
.sq-card {
    background:#161b22; border:1px solid #21262d; border-radius:8px;
    padding:10px 14px; margin-bottom:8px;
}
.sq-name  { font-size:0.75rem; font-weight:700; color:#e6edf3; }
.sq-sym   { font-size:0.62rem; color:#484f58; margin-bottom:4px; }
.sq-price { font-size:1.1rem; font-weight:700; color:#e6edf3; }
.sq-up    { font-size:0.68rem; color:#3fb950; font-weight:600; }
.sq-dn    { font-size:0.68rem; color:#f85149; font-weight:600; }

/* ── NEWS CARDS ── */
.news-card {
    background:#161b22; border:1px solid #21262d;
    border-left:3px solid #21262d; border-radius:6px;
    padding:9px 13px; margin-bottom:5px;
    transition: border-left-color 0.15s;
}
.news-card:hover { border-left-color:#388bfd; }
.news-card.priority           { border-left-color:#f85149 !important; background:#1a0f0f; }
.news-card.sentiment-positive { border-left-color:#3fb950; background:#0d1a10; }
.news-card.sentiment-negative { border-left-color:#f85149; background:#1a0d0d; }
.news-card.rbi  { border-left-color:#d29922; }
.news-card.nse  { border-left-color:#388bfd; }
.card-title {
    font-size:0.8rem; font-weight:500; color:#c9d1d9 !important;
    text-decoration:none !important; line-height:1.45;
    display:block; margin-bottom:5px;
}
.card-title:hover { color:#58a6ff !important; }
.card-meta {
    font-size:0.65rem; color:#484f58;
    display:flex; gap:7px; align-items:center; flex-wrap:wrap;
}

/* ── BADGES ── */
.bp  { background:#f85149; color:#fff; font-size:0.58rem; font-weight:700; padding:1px 5px; border-radius:3px; }
.bpo { background:#1a4a1f; color:#3fb950; font-size:0.58rem; font-weight:700; padding:1px 5px; border-radius:3px; border:1px solid #3fb950; }
.bne { background:#4a1a1a; color:#f85149; font-size:0.58rem; font-weight:700; padding:1px 5px; border-radius:3px; border:1px solid #f85149; }
.bm  { background:#1a2a4a; color:#388bfd; font-size:0.58rem; font-weight:700; padding:1px 5px; border-radius:3px; }
.br  { background:#2a1f00; color:#d29922; font-size:0.58rem; font-weight:700; padding:1px 5px; border-radius:3px; border:1px solid #d29922; }
.bn  { background:#001a3a; color:#388bfd; font-size:0.58rem; font-weight:700; padding:1px 5px; border-radius:3px; border:1px solid #388bfd; }
.bst { background:#2a1a4a; color:#a78bfa; font-size:0.58rem; font-weight:700; padding:1px 5px; border-radius:3px; }
.bsrc{ background:#21262d; color:#8b949e; font-size:0.56rem; padding:1px 5px; border-radius:3px; }

.cat-header {
    font-size:0.72rem; font-weight:700; color:#388bfd;
    letter-spacing:0.08em; padding:5px 0 8px;
    border-bottom:1px solid #21262d; margin-bottom:10px;
}
.stock-hdr {
    font-size:0.75rem; font-weight:700; color:#a78bfa;
    letter-spacing:0.05em; padding:6px 0 3px;
    border-bottom:1px solid #21262d; margin:10px 0 7px;
}
.sent-bar {
    display:flex; gap:14px; font-size:0.65rem;
    color:#484f58; margin-bottom:8px; flex-wrap:wrap;
}
.pos { color:#3fb950; font-weight:600; }
.neg { color:#f85149; font-weight:600; }
.neu { color:#484f58; font-weight:600; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# LIVE MARKET MONITOR
# ─────────────────────────────────────────────

market_data = fetch_market_data()
now_ist_str = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")

mhtml = '<div class="market-monitor">'
mhtml += f'<div class="mm-title">⚡ LIVE MARKET MONITOR &nbsp;·&nbsp; Yahoo Finance &nbsp;·&nbsp; {now_ist_str} &nbsp;·&nbsp; Refreshes every 60s</div>'
mhtml += '<div class="ticker-grid">'

for name, data in market_data.items():
    price  = data.get("price")
    change = data.get("change")
    pct    = data.get("pct")
    unit   = data.get("unit", "")

    if price is None:
        mhtml += f'<div class="ticker-card flat"><div class="t-name">{name}</div><div class="t-price" style="font-size:0.78rem;color:#484f58">—</div><div class="t-flat">N/A</div></div>'
        continue

    direction = "up" if change >= 0 else "down"
    arrow     = "▲" if change >= 0 else "▼"
    cls       = "t-up" if change >= 0 else "t-dn"
    pstr      = f"{unit}{price:,.0f}" if name in ("NIFTY 50","SENSEX","BANK NIFTY") else f"{unit}{price:,.2f}"
    cstr      = f"{arrow} {abs(change):,.2f} ({abs(pct):.2f}%)"

    mhtml += f"""<div class="ticker-card {direction}">
      <div class="t-name">{name}</div>
      <div class="t-price">{pstr}</div>
      <div class="{cls}">{cstr}</div>
    </div>"""

mhtml += "</div></div>"
st.markdown(mhtml, unsafe_allow_html=True)

# ─────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────

st.markdown(f"""
<div class="dash-header">
  <div class="dash-title"><span class="live-dot"></span>JM FINANCIAL &nbsp;·&nbsp; RISK INTELLIGENCE DASHBOARD</div>
  <div class="dash-sub">Real-time news &nbsp;·&nbsp; RBI & NSE Circulars &nbsp;·&nbsp; Live Markets &nbsp;·&nbsp; {now_ist_str}</div>
</div>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────

with st.sidebar:
    st.markdown("## 📊 JM Risk Dashboard")
    st.markdown("---")

    # ── STOCK SEARCH ──
    st.markdown("### 🔍 Stock Search")
    st.markdown("<div style='font-size:0.68rem;color:#484f58;margin-bottom:6px;'>Search by Name · NSE Code · BSE Code · ISIN</div>", unsafe_allow_html=True)

    search_input = st.text_input(
        label="Stock Search",
        placeholder="e.g. RELIANCE or 500325",
        label_visibility="collapsed",
        key="stock_search_input"
    )

    if search_input.strip():
        with st.spinner("Fetching quote..."):
            result = fetch_stock_quote(search_input.strip())

        if result["found"]:
            price   = result["price"]
            change  = result["change"]
            pct     = result["pct"]
            arrow   = "▲" if change >= 0 else "▼"
            clr     = "#3fb950" if change >= 0 else "#f85149"
            st.markdown(f"""
            <div class="sq-card">
              <div class="sq-name">{result['name']}</div>
              <div class="sq-sym">{result['symbol']}</div>
              <div class="sq-price">₹{price:,.2f}</div>
              <div style="color:{clr};font-size:0.68rem;font-weight:600;">
                {arrow} {abs(change):,.2f} &nbsp;({abs(pct):.2f}%)
              </div>
            </div>
            """, unsafe_allow_html=True)

            # Also show news for this stock
            with st.spinner("Fetching news..."):
                # Use the company name for Google News search
                stock_name = result['name'].split(" ")[0]   # first word works well
                feed = feedparser.parse(
                    gn(f"{stock_name} stock NSE BSE India")
                )
                st.markdown(f"**Latest News — {result['name']}**")
                count = 0
                for entry in feed.entries[:5]:
                    t  = entry.get("title","").strip()
                    lk = entry.get("link","#")
                    dt = parse_dt(entry)
                    if t:
                        ago = time_ago(dt)
                        st.markdown(f"""
                        <div class="news-card">
                          <a class="card-title" href="{lk}" target="_blank">{t}</a>
                          <div class="card-meta"><span>{ago}</span></div>
                        </div>""", unsafe_allow_html=True)
                        count += 1
                if count == 0:
                    st.markdown("<div style='font-size:0.7rem;color:#484f58;'>No recent news found.</div>", unsafe_allow_html=True)
        else:
            st.error(f"Symbol not found: {result.get('symbol','')}. Try NSE code (e.g. RELIANCE) or BSE code (e.g. 500325).")

    st.markdown("---")

    # ── CONTROLS ──
    st.markdown("### ⚙️ Controls")
    auto_refresh = st.toggle("Auto Refresh (90s)", value=True)
    if st.button("🔄 Refresh Now", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")

    # ── ADMIN PANEL ──
    st.markdown("### 🔐 Admin Panel")
    pwd      = st.text_input("Password", type="password", key="admin_pwd")
    is_admin = (pwd == ADMIN_PASSWORD)

    if is_admin:
        st.success("✅ Access granted")
        st.markdown("**📢 Push Internal Headline**")

        new_title = st.text_area("Headline text", height=68, key="adm_title")
        new_link  = st.text_input("Source link (optional)", value="#", key="adm_link")
        new_cat   = st.selectbox(
            "Segment / Category",
            options=[
                "⚠️ Risk Alert",
                "📋 Compliance",
                "🏛️ Internal Memo",
                "🇮🇳 India Markets",
                "💵 Currency & Forex",
                "🛢️ Commodities & Oil",
                "🌍 Geopolitical Risk",
                "📊 Global Macro",
                "📈 Moneycontrol",
                "📰 Economic Times",
                "🗞️ Mint Markets",
            ],
            key="adm_cat"
        )
        if st.button("➕ Publish Headline", use_container_width=True):
            if new_title.strip():
                manual = load_manual_headlines()
                manual.insert(0, {
                    "title":     new_title.strip(),
                    "link":      new_link.strip() or "#",
                    "dt":        datetime.now(timezone.utc).isoformat(),
                    "category":  new_cat,
                    "manual":    True,
                    "priority":  is_priority(new_title),
                    "sentiment": get_sentiment(new_title),
                })
                save_manual_headlines(manual)
                st.cache_data.clear()
                st.success("Published!")
                st.rerun()

        manual_items = load_manual_headlines()
        if manual_items:
            st.markdown("**Manage Published Headlines**")
            for i, item in enumerate(manual_items):
                c1, c2 = st.columns([5, 1])
                c1.markdown(f"<div style='font-size:0.68rem;color:#c9d1d9;'>{item['title'][:55]}…</div>", unsafe_allow_html=True)
                if c2.button("🗑", key=f"del_{i}"):
                    manual_items.pop(i)
                    save_manual_headlines(manual_items)
                    st.rerun()
    elif pwd:
        st.error("Incorrect password")

# ─────────────────────────────────────────────
# DATA FETCH
# ─────────────────────────────────────────────

@st.cache_data(ttl=90, show_spinner=False)
def load_all_data():
    all_data      = fetch_all_feeds(FEED_SOURCES)
    rbi_circulars = fetch_rbi_circulars()
    nse_circulars = fetch_nse_circulars()
    portfolio_data = {}
    port_feeds = {s: [gn(f"{s} stock NSE BSE India")] for s in PORTFOLIO_STOCKS}
    raw_port   = fetch_all_feeds(port_feeds)
    for s in PORTFOLIO_STOCKS:
        portfolio_data[s] = raw_port.get(s, [])
    return all_data, rbi_circulars, nse_circulars, portfolio_data

with st.spinner("📡 Fetching latest news..."):
    all_data, rbi_circulars, nse_circulars, portfolio_data = load_all_data()

manual_headlines = load_manual_headlines()
for item in manual_headlines:
    if isinstance(item.get("dt"), str):
        try:    item["dt"] = datetime.fromisoformat(item["dt"])
        except: item["dt"] = datetime.now(timezone.utc)

all_articles = []
for cat, arts in all_data.items():
    for a in arts:
        all_articles.append({**a, "category": cat, "manual": False})
for item in manual_headlines:
    all_articles.append({**item, "manual": True})

all_articles.sort(
    key=lambda x: x["dt"] if isinstance(x["dt"], datetime) else datetime.now(timezone.utc),
    reverse=True
)

# Stats bar
total  = len(all_articles)
prio_n = sum(1 for a in all_articles if a.get("priority"))
pos_n  = sum(1 for a in all_articles if a.get("sentiment") == "positive")
neg_n  = sum(1 for a in all_articles if a.get("sentiment") == "negative")
c1,c2,c3,c4,c5 = st.columns(5)
c1.metric("📰 Total", total)
c2.metric("⚡ Priority", prio_n)
c3.metric("🟢 Positive", pos_n)
c4.metric("🔴 Negative", neg_n)
c5.metric("⚪ Neutral", total - pos_n - neg_n)
st.markdown("---")

# ─────────────────────────────────────────────
# RENDERERS
# ─────────────────────────────────────────────

def _card_class(is_prio, sentiment):
    if is_prio:               return "news-card priority"
    if sentiment == "positive": return "news-card sentiment-positive"
    if sentiment == "negative": return "news-card sentiment-negative"
    return "news-card"

def render_news_cards(articles: list):
    if not articles:
        st.markdown("<div style='font-size:0.78rem;color:#484f58;padding:16px 0;'>No news found — try Refresh Now.</div>", unsafe_allow_html=True)
        return
    for art in articles:
        is_prio   = art.get("priority", False)
        is_manual = art.get("manual", False)
        sentiment = art.get("sentiment", "neutral")
        cat_label = art.get("category", "")
        cls       = _card_class(is_prio, sentiment)
        dt_obj    = art["dt"]
        if isinstance(dt_obj, str):
            try:    dt_obj = datetime.fromisoformat(dt_obj)
            except: dt_obj = datetime.now(timezone.utc)
        ago = time_ago(dt_obj)
        p_badge = '<span class="bp">⚡ PRIORITY</span>' if is_prio else ""
        m_badge = '<span class="bm">📢 INTERNAL</span>' if is_manual else ""
        if not is_prio and not is_manual:
            s_badge = ('<span class="bpo">▲ POS</span>' if sentiment == "positive"
                       else '<span class="bne">▼ NEG</span>' if sentiment == "negative"
                       else "")
        else:
            s_badge = ""
        st.markdown(f"""<div class="{cls}">
          <a class="card-title" href="{art['link']}" target="_blank">{art['title']}</a>
          <div class="card-meta"><span>{ago}</span><span>·</span><span style="color:#30363d">{cat_label}</span>{p_badge}{m_badge}{s_badge}</div>
        </div>""", unsafe_allow_html=True)


def render_circular_cards(articles: list, badge_class: str, card_extra: str):
    if not articles:
        st.markdown("<div style='font-size:0.78rem;color:#484f58;padding:16px 0;'>No circulars found — check network or try Refresh Now.</div>", unsafe_allow_html=True)
        return
    label = "RBI" if "rbi" in card_extra else "NSE"
    for art in articles:
        is_prio   = art.get("priority", False)
        sentiment = art.get("sentiment", "neutral")
        cls       = f"news-card {card_extra}" + (" priority" if is_prio else "")
        ago       = time_ago(art["dt"])
        src       = art.get("source", "")
        t_badge   = f'<span class="{badge_class}">{label}</span>'
        p_badge   = '<span class="bp">⚡ PRIORITY</span>' if is_prio else ""
        src_badge = f'<span class="bsrc">{src}</span>' if src else ""
        s_badge   = ('<span class="bpo">▲ POS</span>' if sentiment == "positive"
                     else '<span class="bne">▼ NEG</span>' if sentiment == "negative"
                     else "")
        st.markdown(f"""<div class="{cls}">
          <a class="card-title" href="{art['link']}" target="_blank">{art['title']}</a>
          <div class="card-meta"><span>{ago}</span>{t_badge}{s_badge}{p_badge}{src_badge}</div>
        </div>""", unsafe_allow_html=True)


def render_portfolio_tab(portfolio_data: dict):
    for stock, articles in portfolio_data.items():
        pos = sum(1 for a in articles if a.get("sentiment") == "positive")
        neg = sum(1 for a in articles if a.get("sentiment") == "negative")
        neu = len(articles) - pos - neg
        st.markdown(f"""<div class="stock-hdr">📌 {stock.upper()}</div>
        <div class="sent-bar">
          <span>Headlines: <b style="color:#c9d1d9">{len(articles)}</b></span>
          <span>🟢 <span class="pos">{pos}</span></span>
          <span>🔴 <span class="neg">{neg}</span></span>
          <span>⚪ <span class="neu">{neu}</span></span>
        </div>""", unsafe_allow_html=True)
        if not articles:
            st.markdown("<div style='font-size:0.72rem;color:#484f58;padding:4px 0 10px;'>No recent news.</div>", unsafe_allow_html=True)
            continue
        for art in articles:
            is_prio   = art.get("priority", False)
            sentiment = art.get("sentiment", "neutral")
            cls       = _card_class(is_prio, sentiment)
            dt_obj    = art["dt"]
            if isinstance(dt_obj, str):
                try:    dt_obj = datetime.fromisoformat(dt_obj)
                except: dt_obj = datetime.now(timezone.utc)
            ago = time_ago(dt_obj)
            p_badge  = '<span class="bp">⚡ PRIORITY</span>' if is_prio else ""
            st_badge = f'<span class="bst">{stock.upper()}</span>'
            s_badge  = ('<span class="bpo">▲ POS</span>' if sentiment == "positive" and not is_prio
                        else '<span class="bne">▼ NEG</span>' if sentiment == "negative" and not is_prio
                        else "")
            st.markdown(f"""<div class="{cls}">
              <a class="card-title" href="{art['link']}" target="_blank">{art['title']}</a>
              <div class="card-meta"><span>{ago}</span>{st_badge}{s_badge}{p_badge}</div>
            </div>""", unsafe_allow_html=True)
        st.markdown("<div style='margin-bottom:6px'></div>", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# MAIN TABS
# ─────────────────────────────────────────────

news_cats = list(FEED_SOURCES.keys())

tab_all, tab_prio, tab_port, tab_rbi, tab_nse, *cat_tabs = st.tabs(
    ["All News", "🔴 Priority", "📂 Portfolio", "🏛️ RBI Circulars", "🔵 NSE Circulars"] + news_cats
)

with tab_all:
    render_news_cards(all_articles)

with tab_prio:
    prio_arts = [a for a in all_articles if a.get("priority")]
    if prio_arts:
        st.markdown(f"<div style='font-size:0.7rem;color:#f85149;margin-bottom:10px;'>⚡ {len(prio_arts)} priority items across all feeds</div>", unsafe_allow_html=True)
    render_news_cards(prio_arts)

with tab_port:
    total_port = sum(len(v) for v in portfolio_data.values())
    st.markdown(f"<div style='font-size:0.7rem;color:#a78bfa;margin-bottom:4px;'>Tracking <b style='color:#e6edf3'>{len(PORTFOLIO_STOCKS)} stocks</b> · {total_port} headlines</div>", unsafe_allow_html=True)
    st.markdown("---")
    render_portfolio_tab(portfolio_data)

with tab_rbi:
    st.markdown(f"""<div style='font-size:0.68rem;color:#d29922;margin-bottom:6px;'>
    Sources: rbi.org.in/notifications_rss.xml · pressreleases_rss.xml · BS_CircularIndexDisplay.aspx
    <br><span style='color:#484f58;'>Current FY only (Apr 2025 onwards) · {len(rbi_circulars)} circulars loaded</span></div>""",
    unsafe_allow_html=True)
    st.markdown("---")
    render_circular_cards(rbi_circulars, "br", "rbi")

with tab_nse:
    st.markdown(f"""<div style='font-size:0.68rem;color:#388bfd;margin-bottom:6px;'>
    Sources: nsearchives.nseindia.com/content/RSS/Circulars.xml · Online_announcements.xml
    <br><span style='color:#484f58;'>Current FY only (Apr 2025 onwards) · {len(nse_circulars)} circulars loaded</span></div>""",
    unsafe_allow_html=True)
    st.markdown("---")
    render_circular_cards(nse_circulars, "bn", "nse")

for tab, cat in zip(cat_tabs, news_cats):
    with tab:
        st.markdown(f'<div class="cat-header">{cat}</div>', unsafe_allow_html=True)
        arts = [{**a, "category": cat, "manual": False} for a in all_data.get(cat, [])]
        render_news_cards(arts)

# ─────────────────────────────────────────────
# AUTO-REFRESH
# ─────────────────────────────────────────────

if auto_refresh:
    ph = st.empty()
    for remaining in range(AUTO_REFRESH_SECONDS, 0, -1):
        ph.markdown(f"<div style='font-size:0.62rem;color:#484f58;text-align:right;padding-top:8px;'>⏱ Next refresh in {remaining}s</div>",
                    unsafe_allow_html=True)
        time.sleep(1)
    st.cache_data.clear()
    st.rerun()
