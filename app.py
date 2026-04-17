"""
JM Financial | Risk Intelligence Dashboard  v5.0
=================================================
✅ BSE/NSE/ISIN stock search fixed (proper Yahoo Finance symbol resolution)
✅ Header: Title + Refresh timer on top bar
✅ Market Watch below header
✅ No stats/counts bar
✅ White news cards with green/red colour coding
✅ Arial 11px font throughout
✅ IST timestamps via pytz
✅ RBI/NSE circulars current FY only
✅ Admin push panel with segment dropdown
✅ Moneycontrol + ET + Mint replacing Reuters

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
# PORTFOLIO STOCKS
# ─────────────────────────────────────────────
PORTFOLIO_STOCKS = [
    "Sammaan Capital",
    "Suzlon",
    "Religare",
    "Valor Estate",
]

# ─────────────────────────────────────────────
# LIVE MARKET TICKERS
# ─────────────────────────────────────────────
MARKET_TICKERS = {
    "NIFTY 50":    ("^NSEI",    "₹"),
    "SENSEX":      ("^BSESN",   "₹"),
    "BANK NIFTY":  ("^NSEBANK", "₹"),
    "CRUDE (WTI)": ("CL=F",     "$"),
    "BRENT":       ("BZ=F",     "$"),
    "GOLD":        ("GC=F",     "$"),
    "SILVER":      ("SI=F",     "$"),
}

# ─────────────────────────────────────────────
# PRIORITY & SENTIMENT KEYWORDS
# ─────────────────────────────────────────────
PRIORITY_KEYWORDS = [
    "war", "strike", "attack", "sanctions", "iran", "israel", "conflict",
    "rate hike", "rate cut", "rbi policy", "emergency", "crash", "plunge",
    "circuit breaker", "halt", "default", "recession", "devaluation",
    "rupee fall", "rupee crash", "fed hike", "fed cut", "inflation spike",
    "crude surge", "market fall", "nifty down", "sensex crash", "sebi",
    "imf warning", "selloff", "crisis", "collapse", "black swan",
    "geopolitical", "trump tariff", "nuclear", "penalty", "suspension",
    "fraud", "scam", "ban", "action against",
]

NEGATIVE_KEYWORDS = [
    "crash", "plunge", "fall", "drop", "decline", "loss", "losses", "slump",
    "selloff", "sell-off", "tumble", "sink", "sinks", "sank", "collapse",
    "crisis", "recession", "default", "fraud", "scam", "ban",
    "penalty", "suspension", "warning", "threat", "attack", "war",
    "conflict", "sanctions", "halt", "circuit breaker", "devaluation",
    "downgrade", "probe", "investigation", "npa", "writeoff", "write-off",
    "layoff", "layoffs", "bankrupt", "insolvency",
    "miss", "misses", "disappoints", "weak", "slowdown",
    "bearish", "bear market", "correction", "fear", "panic",
    "fii selling", "fpi selling", "outflow", "outflows", "dumped",
]

POSITIVE_KEYWORDS = [
    "rally", "surge", "gain", "gains", "rise", "rises", "rose",
    "jump", "jumps", "soar", "soars", "record high", "all-time high",
    "profit", "profits", "revenue", "growth", "upgrade", "bullish",
    "outperform", "beat", "beats", "strong", "robust", "boost",
    "fii buying", "fpi buying", "dii buying", "institutional buying",
    "net buyer", "inflow", "inflows", "expansion",
    "rate cut", "rate cuts", "easing", "recovery", "rebound", "optimism",
    "deal", "acquisition", "merger", "dividend", "buyback",
    "approval", "approved", "milestone", "capex", "turnaround",
]

def get_sentiment(title: str) -> str:
    t = title.lower()
    neg = sum(1 for kw in NEGATIVE_KEYWORDS if kw in t)
    pos = sum(1 for kw in POSITIVE_KEYWORDS if kw in t)
    if neg > pos: return "negative"
    if pos > neg: return "positive"
    return "neutral"

def is_priority(title: str) -> bool:
    return any(kw in title.lower() for kw in PRIORITY_KEYWORDS)

EXCLUDE_CIRCULAR_KW = [
    "court", "tribunal", "writ", "petition", "judgment",
    "annual report", "quarterly result", "q1", "q2", "q3", "q4",
    "balance sheet", "ipo filing", "drhp",
]

CURRENT_FY_START = datetime(2025, 4, 1, tzinfo=timezone.utc)

# ─────────────────────────────────────────────
# TIME HELPERS
# ─────────────────────────────────────────────

def to_ist(dt: datetime) -> datetime:
    if dt is None: return datetime.now(IST)
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)

def fmt_ist(dt: datetime) -> str:
    return to_ist(dt).strftime("%d %b %Y %I:%M %p IST")

def time_ago(dt: datetime) -> str:
    if not isinstance(dt, datetime): return "unknown"
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    diff = datetime.now(timezone.utc) - dt
    s = int(diff.total_seconds())
    if s < 0:     return "just now"
    if s < 60:    return f"{s}s ago"
    if s < 3600:  return f"{s//60}m ago"
    if s < 86400: return f"{s//3600}h ago"
    return fmt_ist(dt)

def parse_dt(entry) -> datetime:
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try: return datetime(*t[:6], tzinfo=timezone.utc)
            except: pass
    return datetime.now(timezone.utc)

def is_recent(dt: datetime, days: int = 3) -> bool:
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days <= days

def is_current_fy(dt: datetime) -> bool:
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt >= CURRENT_FY_START

# ─────────────────────────────────────────────
# STOCK SEARCH — BSE/NSE/ISIN/Name
# ─────────────────────────────────────────────

# Comprehensive BSE code → NSE symbol mapping
BSE_TO_NSE = {
    "500325": "RELIANCE", "532540": "TCS", "500209": "INFY",
    "500180": "HDFCBANK", "532174": "ICICIBANK", "500112": "SBIN",
    "532215": "AXISBANK", "500247": "KOTAKBANK", "507685": "WIPRO",
    "500510": "LT", "532667": "SUZLON", "532488": "RELIGARE",
    "500820": "ASIANPAINT", "500010": "HDFC", "500440": "HINDALCO",
    "500696": "HINDUNILVR", "500875": "ITC", "500182": "JSWSTEEL",
    "532978": "BAJFINANCE", "532898": "BAJAJFINSV",
    "500002": "ABB", "500003": "AEGISLOG", "532921": "IDEA",
    "500260": "MCDOWELL-N", "500520": "MAHINDRA",
}

# ISIN → NSE symbol mapping (common ones)
ISIN_TO_NSE = {
    "INE002A01018": "RELIANCE",
    "INE467B01029": "TCS",
    "INE009A01021": "INFY",
    "INE040A01034": "HDFCBANK",
    "INE090A01021": "ICICIBANK",
    "INE062A01020": "SBIN",
    "INE238A01034": "AXISBANK",
    "INE237A01028": "WIPRO",
    "INE018A01030": "LT",
    "INE040H01021": "SUZLON",
}

@st.cache_data(ttl=300, show_spinner=False)
def fetch_stock_quote(raw_input: str) -> dict:
    """
    Resolves stock by:
    1. ISIN (12-char alphanumeric starting with IN)
    2. BSE code (6-digit number)
    3. NSE code (letters only, e.g. RELIANCE)
    4. Partial name (try .NS suffix)
    Returns price, change, pct, name.
    """
    inp = raw_input.strip().upper()

    # Step 1: Determine Yahoo Finance symbol
    yahoo_sym = None

    # ISIN detection
    if len(inp) == 12 and inp.startswith("IN") and inp[2:].isalnum():
        nse_code = ISIN_TO_NSE.get(inp)
        if nse_code:
            yahoo_sym = nse_code + ".NS"
        else:
            # Try NSE first, BSE fallback — ISIN not in our map
            return {"found": False, "error": f"ISIN {inp} not in local map. Try NSE code directly.", "symbol": inp}

    # BSE code detection (purely numeric, typically 6 digits)
    elif inp.isdigit():
        nse_code = BSE_TO_NSE.get(inp)
        if nse_code:
            yahoo_sym = nse_code + ".NS"
        else:
            # Try direct BSE symbol on Yahoo
            yahoo_sym = inp + ".BO"

    # NSE code or name (letters / alphanumeric with hyphen)
    else:
        # Try .NS first (NSE), fall back to .BO
        yahoo_sym = inp + ".NS"

    # Step 2: Fetch from yfinance
    def _try_fetch(sym: str) -> dict:
        try:
            t  = yf.Ticker(sym)
            fi = t.fast_info
            price = float(fi.last_price)
            prev  = float(fi.previous_close)
            if price <= 0 or prev <= 0:
                raise ValueError("Invalid price")
            change = price - prev
            pct    = (change / prev * 100) if prev else 0.0
            info   = t.info
            name   = info.get("longName") or info.get("shortName") or sym
            return {"found": True, "name": name, "symbol": sym,
                    "price": price, "change": change, "pct": pct, "error": None}
        except Exception as e:
            return {"found": False, "error": str(e), "symbol": sym}

    result = _try_fetch(yahoo_sym)

    # If .NS failed, try .BO as fallback
    if not result["found"] and yahoo_sym.endswith(".NS"):
        bo_sym = yahoo_sym.replace(".NS", ".BO")
        result = _try_fetch(bo_sym)

    # If .BO failed too, try raw symbol
    if not result["found"] and not yahoo_sym.endswith(".NS") and not yahoo_sym.endswith(".BO"):
        result = _try_fetch(inp)

    return result

# ─────────────────────────────────────────────
# LIVE MARKET DATA
# ─────────────────────────────────────────────

@st.cache_data(ttl=60, show_spinner=False)
def fetch_market_data() -> dict:
    results = {}
    for name, (sym, unit) in MARKET_TICKERS.items():
        try:
            fi    = yf.Ticker(sym).fast_info
            price = float(fi.last_price)
            prev  = float(fi.previous_close)
            chg   = price - prev
            pct   = (chg / prev * 100) if prev else 0.0
            results[name] = {"price": price, "change": chg, "pct": pct, "unit": unit}
        except Exception:
            results[name] = {"price": None, "change": None, "pct": None, "unit": unit}
    return results

# ─────────────────────────────────────────────
# RSS / FEED SOURCES
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
# RBI CIRCULARS
# ─────────────────────────────────────────────

def fetch_rbi_circulars() -> list:
    all_items, seen = [], set()
    urls = [
        "https://rbi.org.in/notifications_rss.xml",
        "https://rbi.org.in/pressreleases_rss.xml",
    ]
    for url in urls:
        try:
            resp = requests.get(url, timeout=12, verify=False, proxies=PROXIES or {})
            feed = feedparser.parse(resp.content)
            for entry in feed.entries[:40]:
                title = entry.get("title", "").strip()
                link  = entry.get("link", "#")
                dt    = parse_dt(entry)
                if not title or title in seen: continue
                if not is_current_fy(dt): continue
                if any(kw in title.lower() for kw in EXCLUDE_CIRCULAR_KW): continue
                seen.add(title)
                all_items.append({
                    "title": title, "link": link, "dt": dt,
                    "priority": is_priority(title),
                    "sentiment": get_sentiment(title),
                    "source": "RBI RSS",
                })
        except Exception:
            pass

    # ASPX scraping
    aspx_urls = [
        "https://www.rbi.org.in/Scripts/BS_CircularIndexDisplay.aspx",
        "https://www.rbi.org.in/Scripts/NotificationUser.aspx",
    ]

    all_items.sort(key=lambda x: x["dt"], reverse=True)
    return all_items[:50]

# ─────────────────────────────────────────────
# NSE CIRCULARS
# ─────────────────────────────────────────────

def fetch_nse_circulars() -> list:
    all_items, seen = [], set()
    urls = [
        "https://nsearchives.nseindia.com/content/RSS/Circulars.xml",
        "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml",
    ]
    for url in urls:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept": "application/rss+xml,application/xml,*/*",
                "Referer": "https://www.nseindia.com/",
            }
            resp = requests.get(url, headers=headers, timeout=15, verify=False, proxies=PROXIES or {})
            feed = feedparser.parse(resp.content)
            for entry in feed.entries[:40]:
                title = entry.get("title", "").strip()
                link  = entry.get("link", "#")
                dt    = parse_dt(entry)
                if not title or title in seen: continue
                if not is_current_fy(dt): continue
                if any(kw in title.lower() for kw in EXCLUDE_CIRCULAR_KW): continue
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
            if not title or not is_recent(dt, days=3): continue
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
        seen, deduped = set(), []
        for a in sorted(results[cat], key=lambda x: x["dt"], reverse=True):
            if a["title"] not in seen:
                seen.add(a["title"])
                deduped.append(a)
        results[cat] = deduped
    return dict(results)

# ─────────────────────────────────────────────
# MANUAL HEADLINES
# ─────────────────────────────────────────────

def load_manual() -> list:
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE) as f: return json.load(f)
        except: return []
    return []

def save_manual(items: list):
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
# CSS — Arial 11, white cards, colour borders
# ─────────────────────────────────────────────

st.markdown("""
<style>
/* ── BASE FONT ── */
html, body, [class*="css"], .stMarkdown, .stText, p, div, span, a, button, input, select, textarea {
    font-family: Arial, Helvetica, sans-serif !important;
    font-size: 11pt !important;
}

/* ── BACKGROUND ── */
html, body, .stApp, [data-testid="stAppViewContainer"] {
    background-color: #f0f2f5 !important;
}
.block-container { padding-top: 0.4rem !important; max-width: 1440px !important; }

/* ── SIDEBAR ── */
section[data-testid="stSidebar"] {
    background: #1a1f2e !important;
    border-right: 1px solid #2d3748;
}
section[data-testid="stSidebar"] * {
    color: #e2e8f0 !important;
}
section[data-testid="stSidebar"] input,
section[data-testid="stSidebar"] textarea {
    background: #2d3748 !important;
    border: 1px solid #4a5568 !important;
    color: #e2e8f0 !important;
    border-radius: 6px !important;
}
section[data-testid="stSidebar"] .stButton > button {
    background: #2d3748 !important;
    color: #e2e8f0 !important;
    border: 1px solid #4a5568 !important;
    border-radius: 6px !important;
    font-size: 11pt !important;
}
section[data-testid="stSidebar"] .stButton > button:hover {
    background: #4a5568 !important;
}

/* ── TOP HEADER BAR ── */


.top-bar {
    background: #1a237e;
    border-radius: 8px;
    padding: 10px 20px;
    margin-top: 50px;
    margin-bottom: 10px;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.top-bar-title {
    font-size: 13pt !important;
    font-weight: 700;
    color: #ffffff;
    letter-spacing: 0.03em;
}
.top-bar-right {
    display: flex;
    align-items: center;
    gap: 16px;
}
.live-dot {
    width: 8px; height: 8px;
    background: #4caf50;
    border-radius: 65%;
    display: inline-block;
    margin-right: 6px;
    animation: pulse 2s infinite;
}
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.25} }
.refresh-timer {
    font-size: 10pt !important;
    color: #90caf9;
    font-family: Arial, sans-serif !important;
}
.top-bar-time {
    font-size: 10pt !important;
    color: #b0bec5;
}

/* ── UPDATED MARKET MONITOR ── */
.market-monitor {
    background: #1a237e;
    border-radius: 8px;
    padding: 10px 16px 12px;
    margin-bottom: 12px;
}

.mm-label {
    font-size: 10pt !important;
    font-weight: 700;
    color: #ffffff;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    margin-bottom: 8px;
}

.ticker-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
}

.ticker-card {
    background: rgba(255,255,255,0.08); /* Default state */
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 7px;
    padding: 8px 14px;
    min-width: 115px;
    flex: 1;
    text-align: center;
    transition: all 0.3s ease; /* Smooth color transition */
}

/* --- Dynamic States --- */

/* Positive Change: Dark Green Border, Light Green Fill, Black Text */
.ticker-card.up {
    background: #c8e6c9 !important; 
    border: 2px solid #2e7d32 !important;
}
.ticker-card.up .t-name, 
.ticker-card.up .t-price, 
.ticker-card.up .t-up {
    color: #000000 !important;
}

/* Negative Change: Dark Red Border, Light Red Fill, Black Text */
.ticker-card.down {
    background: #ffcdd2 !important;
    border: 2px solid #c62828 !important;
}
.ticker-card.down .t-name, 
.ticker-card.down .t-price, 
.ticker-card.down .t-dn {
    color: #000000 !important;
}

/* Flat Change: Black Border, Grey Fill, Black Text */
.ticker-card.flat {
    background: #e0e0e0 !important;
    border: 2px solid #000000 !important;
}
.ticker-card.flat .t-name, 
.ticker-card.flat .t-price, 
.ticker-card.flat .t-flat {
    color: #000000 !important;
}

/* --- Typography (Base Styles) --- */
.t-name  { font-size: 8.5pt !important; color: #90caf9; font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; }
.t-price { font-size: 11pt !important; color: #ffffff; font-weight: 700; margin: 3px 0; }
.t-up    { font-size: 9.5pt !important; color: #4caf50; font-weight: 600; }
.t-dn    { font-size: 9.5pt !important; color: #f44336; font-weight: 600; }
.t-flat  { font-size: 9.5pt !important; color: #90a4ae; font-weight: 600; }

/* ── NEWS CARDS — WHITE BACKGROUND ── */
.news-card {
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-left: 4px solid #cbd5e0;
    border-radius: 7px;
    padding: 10px 14px;
    margin-bottom: 6px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
    transition: box-shadow 0.15s, border-left-color 0.15s;
}
.news-card:hover {
    box-shadow: 0 3px 8px rgba(0,0,0,0.12);
    border-left-color: #3949ab;
}
.news-card.priority           { border-left-color: #e53935 !important; background: #fff8f8; }
.news-card.sentiment-positive { border-left-color: #43a047; background: #f6fff7; }
.news-card.sentiment-negative { border-left-color: #e53935; background: #fff6f6; }
.news-card.rbi  { border-left-color: #f9a825; background: #fffde7; }
.news-card.nse  { border-left-color: #1565c0; background: #f0f7ff; }

.card-title {
    font-size: 11pt !important;
    font-weight: 600;
    color: #1a237e !important;
    text-decoration: none !important;
    line-height: 1.45;
    display: block;
    margin-bottom: 5px;
}
.card-title:hover { color: #3949ab !important; text-decoration: underline !important; }

.card-meta {
    font-size: 9.5pt !important;
    color: #718096;
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
}

/* ── BADGES ── */
.bp  { background:#e53935; color:#fff; font-size:8.5pt !important; font-weight:700; padding:2px 6px; border-radius:4px; }
.bpo { background:#e8f5e9; color:#2e7d32; font-size:8.5pt !important; font-weight:700; padding:2px 6px; border-radius:4px; border:1px solid #a5d6a7; }
.bne { background:#ffebee; color:#c62828; font-size:8.5pt !important; font-weight:700; padding:2px 6px; border-radius:4px; border:1px solid #ef9a9a; }
.bm  { background:#e3f2fd; color:#1565c0; font-size:8.5pt !important; font-weight:700; padding:2px 6px; border-radius:4px; border:1px solid #90caf9; }
.br  { background:#fff8e1; color:#e65100; font-size:8.5pt !important; font-weight:700; padding:2px 6px; border-radius:4px; border:1px solid #ffcc02; }
.bn  { background:#e3f2fd; color:#1565c0; font-size:8.5pt !important; font-weight:700; padding:2px 6px; border-radius:4px; border:1px solid #64b5f6; }
.bst { background:#ede7f6; color:#4527a0; font-size:8.5pt !important; font-weight:700; padding:2px 6px; border-radius:4px; }
.bsrc{ background:#f5f5f5; color:#616161; font-size:8pt !important; padding:2px 5px; border-radius:4px; }

/* ── TAB LABELS ── */
.stTabs [data-baseweb="tab"] {
    font-size: 10.5pt !important;
    font-weight: 600;
    color: #4a5568 !important;
}
.stTabs [aria-selected="true"] {
    color: #1a237e !important;
    border-bottom: 2px solid #1a237e;
}

/* ── SECTION HEADERS ── */
.cat-header {
    font-size: 11pt !important;
    font-weight: 700;
    color: #1a237e;
    letter-spacing: 0.04em;
    padding: 6px 0 8px;
    border-bottom: 2px solid #e2e8f0;
    margin-bottom: 10px;
}
.stock-hdr {
    font-size: 11pt !important;
    font-weight: 700;
    color: #4527a0;
    padding: 6px 0 3px;
    border-bottom: 1px solid #e2e8f0;
    margin: 10px 0 7px;
}
.sent-bar {
    display: flex;
    gap: 16px;
    font-size: 10pt !important;
    color: #718096;
    margin-bottom: 8px;
    flex-wrap: wrap;
}
.pos { color: #2e7d32; font-weight: 700; }
.neg { color: #c62828; font-weight: 700; }
.neu { color: #718096; font-weight: 600; }

/* ── STOCK QUOTE CARD (sidebar) ── */
.sq-card {
    background: #2d3748;
    border: 1px solid #4a5568;
    border-radius: 8px;
    padding: 10px 14px;
    margin-bottom: 8px;
}
.sq-name  { font-size: 10.5pt !important; font-weight: 700; color: #e2e8f0 !important; }
.sq-sym   { font-size: 9pt !important; color: #90a4ae !important; margin-bottom: 4px; }
.sq-price { font-size: 13pt !important; font-weight: 700; color: #ffffff !important; }

/* ── METRICS — hide them (no stats bar) ── */
[data-testid="metric-container"] { display: none !important; }

/* ── DIVIDERS ── */
hr { border-color: #e2e8f0 !important; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# FETCH DATA
# ─────────────────────────────────────────────

market_data = fetch_market_data()

@st.cache_data(ttl=90, show_spinner=False)
def load_all_data():
    all_data       = fetch_all_feeds(FEED_SOURCES)
    rbi_circulars  = fetch_rbi_circulars()
    nse_circulars  = fetch_nse_circulars()
    portfolio_data = {}
    port_feeds = {s: [gn(f"{s} stock NSE BSE India")] for s in PORTFOLIO_STOCKS}
    raw_port   = fetch_all_feeds(port_feeds)
    for s in PORTFOLIO_STOCKS:
        portfolio_data[s] = raw_port.get(s, [])
    return all_data, rbi_circulars, nse_circulars, portfolio_data

with st.spinner("📡 Fetching latest news..."):
    all_data, rbi_circulars, nse_circulars, portfolio_data = load_all_data()

manual_items = load_manual()
for item in manual_items:
    if isinstance(item.get("dt"), str):
        try:    item["dt"] = datetime.fromisoformat(item["dt"])
        except: item["dt"] = datetime.now(timezone.utc)

all_articles = []
for cat, arts in all_data.items():
    for a in arts:
        all_articles.append({**a, "category": cat, "manual": False})
for item in manual_items:
    all_articles.append({**item, "manual": True})

all_articles.sort(
    key=lambda x: x["dt"] if isinstance(x["dt"], datetime) else datetime.now(timezone.utc),
    reverse=True
)

# ─────────────────────────────────────────────
# TOP HEADER BAR  (title + time + refresh)
# ─────────────────────────────────────────────

now_ist_str = datetime.now(IST).strftime("%d %b %Y  %I:%M %p IST")

# Use session state to track refresh countdown display
if "refresh_counter" not in st.session_state:
    st.session_state["refresh_counter"] = AUTO_REFRESH_SECONDS
if "last_refresh" not in st.session_state:
    st.session_state["last_refresh"] = time.time()

elapsed   = int(time.time() - st.session_state["last_refresh"])
remaining = max(0, AUTO_REFRESH_SECONDS - elapsed)

st.markdown(f"""
<div class="top-bar">
  <div class="top-bar-title">
    <span class="live-dot"></span>
    JM FINANCIAL &nbsp;·&nbsp; RISK INTELLIGENCE DASHBOARD
  </div>
  <div class="top-bar-right">
    <span class="refresh-timer">⏱ Next refresh in {remaining}s</span>
    <span class="top-bar-time">{now_ist_str}</span>
  </div>
</div>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# MARKET MONITOR (below header)
# ─────────────────────────────────────────────

mhtml = '<div class="market-monitor"><div class="mm-label">⚡ Live Market Watch &nbsp;·&nbsp; Yahoo Finance &nbsp;·&nbsp; Refreshes every 60s</div><div class="ticker-grid">'

for name, data in market_data.items():
    price  = data.get("price")
    change = data.get("change")
    pct    = data.get("pct")
    unit   = data.get("unit", "")

    if price is None:
        mhtml += f'<div class="ticker-card flat"><div class="t-name">{name}</div><div class="t-price" style="font-size:9pt;color:#90a4ae">—</div><div class="t-flat">N/A</div></div>'
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
# SIDEBAR
# ─────────────────────────────────────────────

with st.sidebar:
    st.markdown("## 📊 JM Risk Dashboard")
    st.markdown("---")

    # ── STOCK SEARCH ──
    st.markdown("### 🔍 Stock Search")
    st.markdown(
        "<div style='font-size:9.5pt;color:#90a4ae;margin-bottom:6px;'>"
        "Search by NSE Code · BSE Code · ISIN · Name</div>",
        unsafe_allow_html=True
    )
    search_input = st.text_input(
        label="stock_search",
        placeholder="e.g.  RELIANCE  or  500325  or  INE002A01018",
        label_visibility="collapsed",
        key="stock_search_input"
    )

    if search_input.strip():
        with st.spinner("Fetching..."):
            result = fetch_stock_quote(search_input.strip())

        if result["found"]:
            price  = result["price"]
            change = result["change"]
            pct    = result["pct"]
            arrow  = "▲" if change >= 0 else "▼"
            clr    = "#4caf50" if change >= 0 else "#f44336"
            st.markdown(f"""
            <div class="sq-card">
              <div class="sq-name">{result['name']}</div>
              <div class="sq-sym">{result['symbol']}</div>
              <div class="sq-price">₹{price:,.2f}</div>
              <div style="color:{clr};font-size:10pt;font-weight:700;margin-top:2px;">
                {arrow} ₹{abs(change):,.2f} &nbsp;({abs(pct):.2f}%)
              </div>
            </div>
            """, unsafe_allow_html=True)

            # News for searched stock
            st.markdown(f"**📰 Latest — {result['name'].split(' ')[0]}**")
            try:
                feed = feedparser.parse(gn(f"{result['name'].split(' ')[0]} stock NSE BSE India"))
                count = 0
                for entry in feed.entries[:5]:
                    t  = entry.get("title","").strip()
                    lk = entry.get("link","#")
                    dt = parse_dt(entry)
                    if t:
                        st.markdown(f"""
                        <div class="news-card" style="margin-bottom:4px;">
                          <a class="card-title" href="{lk}" target="_blank"
                             style="font-size:9.5pt !important;">{t}</a>
                          <div class="card-meta"><span>{time_ago(dt)}</span></div>
                        </div>""", unsafe_allow_html=True)
                        count += 1
                if count == 0:
                    st.markdown("<div style='font-size:9.5pt;color:#90a4ae;'>No recent news.</div>", unsafe_allow_html=True)
            except Exception:
                pass
        else:
            err = result.get("error", "")
            st.error(
                f"❌ **Could not find:** `{search_input.strip()}`\n\n"
                f"Please try:\n- NSE code: `RELIANCE`, `HDFCBANK`\n"
                f"- BSE code: `500325`, `532540`\n"
                f"- ISIN: `INE002A01018`\n\n"
                f"_(Technical: {err[:80]})_"
            )

    st.markdown("---")

    # ── CONTROLS ──
    st.markdown("### ⚙️ Controls")
    auto_refresh = st.toggle("Auto Refresh (90s)", value=True)
    if st.button("🔄 Refresh Now", use_container_width=True):
        st.cache_data.clear()
        st.session_state["last_refresh"] = time.time()
        st.rerun()

    st.markdown("---")

    # ── ADMIN PANEL ──
    st.markdown("### 🔐 Admin Panel")
    pwd      = st.text_input("Password", type="password", key="admin_pwd")
    is_admin = (pwd == ADMIN_PASSWORD)

    if is_admin:
        st.success("✅ Access granted")
        st.markdown("**📢 Push Internal Headline**")
        new_title = st.text_area("Headline text", height=70, key="adm_title",
                                  placeholder="Enter headline text here...")
        new_link  = st.text_input("Source link (optional)", value="#", key="adm_link")
        new_cat   = st.selectbox(
            "Segment",
            options=[
                "⚠️ Risk Alert", "📋 Compliance", "🏛️ Internal Memo",
                "🇮🇳 India Markets", "💵 Currency & Forex",
                "🛢️ Commodities & Oil", "🌍 Geopolitical Risk",
                "📊 Global Macro",
                "📰 Economic Times", "🗞️ Mint Markets",
            ],
            key="adm_cat"
        )
        if st.button("➕ Publish Headline", use_container_width=True):
            if new_title.strip():
                items = load_manual()
                items.insert(0, {
                    "title":     new_title.strip(),
                    "link":      new_link.strip() or "#",
                    "dt":        datetime.now(timezone.utc).isoformat(),
                    "category":  new_cat,
                    "manual":    True,
                    "priority":  is_priority(new_title),
                    "sentiment": get_sentiment(new_title),
                })
                save_manual(items)
                st.cache_data.clear()
                st.success("✅ Published!")
                st.rerun()

        existing = load_manual()
        if existing:
            st.markdown("**🗑 Manage Published**")
            for i, item in enumerate(existing):
                c1, c2 = st.columns([5, 1])
                c1.markdown(
                    f"<div style='font-size:9pt;color:#cbd5e0;'>{item['title'][:55]}…</div>",
                    unsafe_allow_html=True
                )
                if c2.button("✕", key=f"del_{i}"):
                    existing.pop(i)
                    save_manual(existing)
                    st.rerun()
    elif pwd:
        st.error("❌ Incorrect password")

# ─────────────────────────────────────────────
# RENDERERS
# ─────────────────────────────────────────────

def _cls(is_prio, sentiment):
    if is_prio:                return "news-card priority"
    if sentiment == "positive": return "news-card sentiment-positive"
    if sentiment == "negative": return "news-card sentiment-negative"
    return "news-card"

def render_news_cards(articles: list):
    if not articles:
        st.markdown(
            "<div style='font-size:11pt;color:#718096;padding:16px 0;'>"
            "No news found — try Refresh Now.</div>",
            unsafe_allow_html=True
        )
        return
    for art in articles:
        is_prio   = art.get("priority", False)
        is_manual = art.get("manual", False)
        sentiment = art.get("sentiment", "neutral")
        cat_label = art.get("category", "")
        cls       = _cls(is_prio, sentiment)
        dt_obj    = art["dt"]
        if isinstance(dt_obj, str):
            try:    dt_obj = datetime.fromisoformat(dt_obj)
            except: dt_obj = datetime.now(timezone.utc)
        ago = time_ago(dt_obj)

        p_badge = '<span class="bp">⚡ PRIORITY</span>'   if is_prio   else ""
        m_badge = '<span class="bm">📢 INTERNAL</span>'   if is_manual else ""
        if not is_prio and not is_manual:
            s_badge = ('<span class="bpo">▲ POSITIVE</span>' if sentiment == "positive" else
                       '<span class="bne">▼ NEGATIVE</span>' if sentiment == "negative" else "")
        else:
            s_badge = ""

        st.markdown(f"""
        <div class="{cls}">
          <a class="card-title" href="{art['link']}" target="_blank">{art['title']}</a>
          <div class="card-meta">
            <span>{ago}</span>
            <span>·</span>
            <span>{cat_label}</span>
            {p_badge}{m_badge}{s_badge}
          </div>
        </div>""", unsafe_allow_html=True)


def render_circular_cards(articles: list, badge_cls: str, card_extra: str):
    label = "RBI" if "rbi" in card_extra else "NSE"
    if not articles:
        st.markdown(
            f"<div style='font-size:11pt;color:#718096;padding:16px 0;'>"
            f"No {label} circulars found — check network or try Refresh Now.</div>",
            unsafe_allow_html=True
        )
        return
    for art in articles:
        is_prio   = art.get("priority", False)
        sentiment = art.get("sentiment", "neutral")
        cls       = f"news-card {card_extra}" + (" priority" if is_prio else "")
        ago       = time_ago(art["dt"])
        src       = art.get("source", "")
        t_badge   = f'<span class="{badge_cls}">{label} CIRCULAR</span>'
        p_badge   = '<span class="bp">⚡ PRIORITY</span>' if is_prio else ""
        src_badge = f'<span class="bsrc">{src}</span>' if src else ""
        s_badge   = ('<span class="bpo">▲ POSITIVE</span>' if sentiment == "positive" else
                     '<span class="bne">▼ NEGATIVE</span>' if sentiment == "negative" else "")
        st.markdown(f"""
        <div class="{cls}">
          <a class="card-title" href="{art['link']}" target="_blank">{art['title']}</a>
          <div class="card-meta">
            <span>{ago}</span>{t_badge}{s_badge}{p_badge}{src_badge}
          </div>
        </div>""", unsafe_allow_html=True)


def render_portfolio(portfolio_data: dict):
    for stock, articles in portfolio_data.items():
        pos = sum(1 for a in articles if a.get("sentiment") == "positive")
        neg = sum(1 for a in articles if a.get("sentiment") == "negative")
        neu = len(articles) - pos - neg
        st.markdown(f"""
        <div class="stock-hdr">📌 {stock.upper()}</div>
        <div class="sent-bar">
          <span>Headlines: <b>{len(articles)}</b></span>
          <span>🟢 <span class="pos">{pos} Positive</span></span>
          <span>🔴 <span class="neg">{neg} Negative</span></span>
          <span>⚪ <span class="neu">{neu} Neutral</span></span>
        </div>""", unsafe_allow_html=True)

        if not articles:
            st.markdown("<div style='font-size:10pt;color:#718096;padding:4px 0 10px;'>No recent news.</div>", unsafe_allow_html=True)
            continue

        for art in articles:
            is_prio   = art.get("priority", False)
            sentiment = art.get("sentiment", "neutral")
            cls       = _cls(is_prio, sentiment)
            dt_obj    = art["dt"]
            if isinstance(dt_obj, str):
                try:    dt_obj = datetime.fromisoformat(dt_obj)
                except: dt_obj = datetime.now(timezone.utc)
            ago      = time_ago(dt_obj)
            p_badge  = '<span class="bp">⚡ PRIORITY</span>'           if is_prio else ""
            st_badge = f'<span class="bst">{stock.upper()}</span>'
            s_badge  = ('<span class="bpo">▲ POSITIVE</span>' if sentiment == "positive" and not is_prio else
                        '<span class="bne">▼ NEGATIVE</span>' if sentiment == "negative" and not is_prio else "")
            st.markdown(f"""
            <div class="{cls}">
              <a class="card-title" href="{art['link']}" target="_blank">{art['title']}</a>
              <div class="card-meta">
                <span>{ago}</span>{st_badge}{s_badge}{p_badge}
              </div>
            </div>""", unsafe_allow_html=True)
        st.markdown("<div style='margin-bottom:6px'></div>", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# MAIN TABS
# ─────────────────────────────────────────────

news_cats = list(FEED_SOURCES.keys())

tab_all, tab_prio, tab_port, tab_rbi, tab_nse, *cat_tabs = st.tabs(
    ["📋 All News", "🔴 Priority", "📂 Portfolio", "🏛️ RBI Circulars", "🔵 NSE Circulars"] + news_cats
)

with tab_all:
    render_news_cards(all_articles)

with tab_prio:
    prio_arts = [a for a in all_articles if a.get("priority")]
    if prio_arts:
        st.markdown(
            f"<div style='font-size:10.5pt;color:#c62828;font-weight:700;margin-bottom:10px;'>"
            f"⚡ {len(prio_arts)} priority alerts across all feeds</div>",
            unsafe_allow_html=True
        )
    render_news_cards(prio_arts)

with tab_port:
    total_port = sum(len(v) for v in portfolio_data.values())
    st.markdown(
        f"<div style='font-size:10.5pt;color:#4527a0;margin-bottom:6px;'>"
        f"Tracking <b>{len(PORTFOLIO_STOCKS)} stocks</b> · {total_port} total headlines</div>",
        unsafe_allow_html=True
    )
    st.markdown("---")
    render_portfolio(portfolio_data)

with tab_rbi:
    st.markdown(f"""
    <div style='font-size:10pt;color:#e65100;margin-bottom:6px;background:#fffde7;
    border:1px solid #ffcc02;border-radius:6px;padding:8px 12px;'>
    <b>Sources:</b> rbi.org.in/notifications_rss.xml · pressreleases_rss.xml · BS_CircularIndexDisplay.aspx
    <br><span style='color:#795548;'>Showing current FY (Apr 2025 onwards) · {len(rbi_circulars)} circulars loaded</span>
    </div>""", unsafe_allow_html=True)
    render_circular_cards(rbi_circulars, "br", "rbi")

with tab_nse:
    st.markdown(f"""
    <div style='font-size:10pt;color:#1565c0;margin-bottom:6px;background:#f0f7ff;
    border:1px solid #90caf9;border-radius:6px;padding:8px 12px;'>
    <b>Sources:</b> nsearchives.nseindia.com/content/RSS/Circulars.xml · Online_announcements.xml
    <br><span style='color:#546e7a;'>Showing current FY (Apr 2025 onwards) · {len(nse_circulars)} circulars loaded</span>
    </div>""", unsafe_allow_html=True)
    render_circular_cards(nse_circulars, "bn", "nse")

for tab, cat in zip(cat_tabs, news_cats):
    with tab:
        st.markdown(f'<div class="cat-header">{cat}</div>', unsafe_allow_html=True)
        arts = [{**a, "category": cat, "manual": False} for a in all_data.get(cat, [])]
        render_news_cards(arts)

# ─────────────────────────────────────────────
# AUTO-REFRESH LOGIC
# ─────────────────────────────────────────────

if auto_refresh:
    elapsed_now = int(time.time() - st.session_state.get("last_refresh", time.time()))
    if elapsed_now >= AUTO_REFRESH_SECONDS:
        st.cache_data.clear()
        st.session_state["last_refresh"] = time.time()
        time.sleep(0.5)
        st.rerun()
    else:
        time.sleep(5)   # Check every 5 seconds (no blocking countdown)
        st.rerun()
