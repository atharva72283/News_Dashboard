"""
JM Financial | Risk Intelligence Dashboard
==========================================
Real-time news aggregator for risk officers.
- Live Market Monitor (Nifty, Sensex, Bank Nifty, Crude, Brent, Gold, Silver)
- Fixed RBI Circulars (official rbi.org.in RSS + ASPX scraping)
- Fixed NSE Circulars (official nsearchives RSS feed)
- Keyword-based sentiment (no AI dependency)

Run with:  streamlit run app.py
Install:   pip install streamlit requests beautifulsoup4 lxml feedparser yfinance
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
import yfinance as yf
import pytz

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
    PROXIES = {
        "http":  f"http://{eu}:{ep}@{PROXY_ADDR}",
        "https": f"http://{eu}:{ep}@{PROXY_ADDR}",
    }

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
# LIVE MARKET TICKERS (Yahoo Finance symbols)
# ─────────────────────────────────────────────
MARKET_TICKERS = {
    "NIFTY 50":    "^NSEI",
    "SENSEX":      "^BSESN",
    "BANK NIFTY":  "^NSEBANK",
    "CRUDE (WTI)": "CL=F",
    "BRENT":       "BZ=F",
    "GOLD":        "GC=F",
    "SILVER":      "SI=F",
}

TICKER_UNITS = {
    "NIFTY 50":    "₹",
    "SENSEX":      "₹",
    "BANK NIFTY":  "₹",
    "CRUDE (WTI)": "$",
    "BRENT":       "$",
    "GOLD":        "$",
    "SILVER":      "$",
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
# SENTIMENT KEYWORDS (improved)
# ─────────────────────────────────────────────
NEGATIVE_KEYWORDS = [
    "crash", "plunge", "fall", "drop", "decline", "loss", "losses", "slump",
    "selloff", "sell-off", "tumble", "sink", "sinks", "sank", "collapse",
    "collapses", "crisis", "recession", "default", "fraud", "scam", "ban",
    "penalty", "suspension", "warning", "threat", "attack", "war",
    "conflict", "sanctions", "halt", "circuit breaker", "devaluation",
    "inflation spike", "rate hike", "downgrade", "probe",
    "investigation", "sebi action", "npa", "writeoff", "write-off",
    "layoff", "layoffs", "fired", "bankrupt", "insolvency",
    "miss", "misses", "disappoints", "disappointing", "weak", "slowdown",
    "contraction", "bearish", "bear market", "correction",
    "volatility", "fear", "panic",
    "fii selling", "fpi selling", "outflow", "outflows", "dumped", "dumping",
    "exodus",
]

POSITIVE_KEYWORDS = [
    "rally", "surge", "gain", "gains", "rise", "rises", "rose", "jump",
    "jumps", "jumped", "soar", "soars", "soared", "record high",
    "all-time high", "ath", "profit", "profits", "revenue", "growth",
    "upgrade", "bullish", "bull", "outperform", "beat", "beats",
    "strong", "robust", "boost", "boosts", "boosted",
    "fii buying", "fpi buying", "dii buying", "institutional buying",
    "net buyer", "net inflow", "inflow", "inflows",
    "investment", "orders", "win", "wins",
    "expansion", "rate cut", "rate cuts", "easing",
    "recovery", "recovers", "rebound", "rebounds", "optimism",
    "opportunity", "partnership", "deal", "acquisition", "merger",
    "dividend", "buyback", "approval", "approved", "launches", "launch",
    "breakthrough", "innovation", "milestone", "commissioning",
    "capacity addition", "capex", "turnaround",
]

def get_sentiment(title: str) -> str:
    t = title.lower()
    neg_score = sum(1 for kw in NEGATIVE_KEYWORDS if kw in t)
    pos_score = sum(1 for kw in POSITIVE_KEYWORDS if kw in t)
    if neg_score > pos_score:
        return "negative"
    elif pos_score > neg_score:
        return "positive"
    return "neutral"

EXCLUDE_CIRCULAR_KEYWORDS = [
    "court", "tribunal", "writ", "petition", "judgment", "judgement",
    "annual report", "quarterly result", "q1 result", "q2 result",
    "q3 result", "q4 result", "balance sheet", "ipo filing", "drhp",
]

def is_priority(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in PRIORITY_KEYWORDS)

# ─────────────────────────────────────────────
# LIVE MARKET DATA — Yahoo Finance
# ─────────────────────────────────────────────

@st.cache_data(ttl=60, show_spinner=False)
def fetch_market_data() -> dict:
    """Fetch live prices for all tickers via yfinance. Cached 60s."""
    results = {}
    for name, sym in MARKET_TICKERS.items():
        try:
            ticker = yf.Ticker(sym)
            hist = ticker.history(period="2d", interval="1m")
            if hist.empty or len(hist) < 2:
                results[name] = {"price": None, "change": None, "pct": None}
                continue
            price  = float(hist["Close"].iloc[-1])
            prev   = float(hist["Close"].iloc[-2])
            change = price - prev
            pct    = (change / prev) * 100 if prev else 0.0
            results[name] = {"price": price, "change": change, "pct": pct}
        except Exception:
            results[name] = {"price": None, "change": None, "pct": None}
    return results

# ─────────────────────────────────────────────
# RSS SOURCES
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
# RBI CIRCULARS — Official rbi.org.in sources
# ─────────────────────────────────────────────
RBI_RSS_FEEDS = [
    "https://rbi.org.in/notifications_rss.xml",      # Notifications RSS
    "https://rbi.org.in/pressreleases_rss.xml",       # Press Releases RSS
]

RBI_ASPX_URLS = [
    "https://www.rbi.org.in/Scripts/BS_CircularIndexDisplay.aspx",
    "https://www.rbi.org.in/Scripts/NotificationUser.aspx",
    "https://www.rbi.org.in/Scripts/BS_ViewMasterDirections.aspx",
]

def fetch_rbi_aspx(url: str) -> list:
    try:
        from bs4 import BeautifulSoup
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        kwargs = dict(headers=headers, timeout=15, verify=False)
        if PROXIES:
            kwargs["proxies"] = PROXIES
        resp = requests.get(url, **kwargs)
        soup = BeautifulSoup(resp.content, "lxml")
        items = []
        for a in soup.find_all("a", href=True):
            href  = a["href"].strip()
            title = a.get_text(strip=True)
            if not title or len(title) < 15:
                continue
            if any(kw in href.lower() for kw in [
                "notification", "circular", "rdocs", "masters", "ntfication"
            ]):
                if not href.startswith("http"):
                    href = "https://www.rbi.org.in" + href
                items.append({
                    "title":     title,
                    "link":      href,
                    "dt":        datetime.now(timezone.utc),
                    "priority":  is_priority(title),
                    "sentiment": get_sentiment(title),
                    "source":    "RBI Direct",
                })
            if len(items) >= 20:
                break
        return items
    except Exception:
        return []


def fetch_rbi_circulars() -> list:
    all_items = []
    seen = set()

    # 1. Official RSS
    for url in RBI_RSS_FEEDS:
        try:
            kwargs = dict(timeout=12, verify=False)
            if PROXIES:
                kwargs["proxies"] = PROXIES
            resp = requests.get(url, **kwargs)
            feed = feedparser.parse(resp.content)
            for entry in feed.entries[:20]:
                title = entry.get("title", "").strip()
                link  = entry.get("link", "#")
                if not title or title in seen:
                    continue
                tl = title.lower()
                if any(kw in tl for kw in EXCLUDE_CIRCULAR_KEYWORDS):
                    continue
                seen.add(title)
                all_items.append({
                    "title":     title,
                    "link":      link,
                    "dt":        parse_dt(entry),
                    "priority":  is_priority(title),
                    "sentiment": get_sentiment(title),
                    "source":    "RBI RSS",
                })
        except Exception:
            pass

    # 2. ASPX scraping
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = [ex.submit(fetch_rbi_aspx, url) for url in RBI_ASPX_URLS]
        for future in as_completed(futures):
            for item in future.result():
                if item["title"] not in seen:
                    seen.add(item["title"])
                    all_items.append(item)

    all_items.sort(key=lambda x: x["dt"], reverse=True)
    return all_items[:40]


# ─────────────────────────────────────────────
# NSE CIRCULARS — Official nsearchives RSS
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
            kwargs = dict(headers=headers, timeout=15, verify=False)
            if PROXIES:
                kwargs["proxies"] = PROXIES
            resp = requests.get(url, **kwargs)
            feed = feedparser.parse(resp.content)
            for entry in feed.entries[:25]:
                title = entry.get("title", "").strip()
                link  = entry.get("link", "#")
                if not title or title in seen:
                    continue
                tl = title.lower()
                if any(kw in tl for kw in EXCLUDE_CIRCULAR_KEYWORDS):
                    continue
                seen.add(title)
                all_items.append({
                    "title":     title,
                    "link":      link,
                    "dt":        parse_dt(entry),
                    "priority":  is_priority(title),
                    "sentiment": get_sentiment(title),
                    "source":    "NSE RSS",
                })
        except Exception:
            pass

    # Fallback scrape
    if len(all_items) < 5:
        try:
            from bs4 import BeautifulSoup
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "https://www.nseindia.com/",
            }
            kwargs = dict(headers=headers, timeout=15, verify=False)
            if PROXIES:
                kwargs["proxies"] = PROXIES
            resp = requests.get(
                "https://www.nseindia.com/resources/exchange-communication-circulars",
                **kwargs
            )
            soup = BeautifulSoup(resp.content, "lxml")
            for a in soup.find_all("a", href=True):
                href  = a["href"].strip()
                title = a.get_text(strip=True)
                if not title or len(title) < 15 or title in seen:
                    continue
                if "circular" in href.lower() or "NSE/" in href:
                    if not href.startswith("http"):
                        href = "https://www.nseindia.com" + href
                    seen.add(title)
                    all_items.append({
                        "title":     title,
                        "link":      href,
                        "dt":        datetime.now(timezone.utc),
                        "priority":  is_priority(title),
                        "sentiment": get_sentiment(title),
                        "source":    "NSE Web",
                    })
                if len(all_items) >= 30:
                    break
        except Exception:
            pass

    all_items.sort(key=lambda x: x["dt"], reverse=True)
    return all_items[:40]


# ─────────────────────────────────────────────
# FEED HELPERS
# ─────────────────────────────────────────────

def parse_dt(entry) -> datetime:
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return datetime.now(timezone.utc)


def fetch_feed(url: str) -> list:
    try:
        kwargs = dict(timeout=10, verify=False)
        if PROXIES:
            kwargs["proxies"] = PROXIES
        resp = requests.get(url, **kwargs)
        feed = feedparser.parse(resp.content)
        items = []
        for entry in feed.entries[:15]:
            title = entry.get("title", "").strip()
            link  = entry.get("link", "#")
            if not title:
                continue
            items.append({
                "title":     title,
                "link":      link,
                "dt":        parse_dt(entry),
                "priority":  is_priority(title),
                "sentiment": get_sentiment(title),
            })
        return items
    except Exception:
        return []


def fetch_all_feeds(feed_dict: dict) -> dict:
    results = defaultdict(list)
    all_urls = [(cat, url) for cat, urls in feed_dict.items() for url in urls]
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(fetch_feed, url): (cat, url) for cat, url in all_urls}
        for future in as_completed(futures):
            cat, _ = futures[future]
            results[cat].extend(future.result())
    for cat in results:
        results[cat].sort(key=lambda x: x["dt"], reverse=True)
    return dict(results)


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


def time_ago(dt: datetime) -> str:
    if not isinstance(dt, datetime):
        return "unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = datetime.now(timezone.utc) - dt
    s = int(diff.total_seconds())
    if s < 60:    return f"{s}s ago"
    if s < 3600:  return f"{s//60}m ago"
    if s < 86400: return f"{s//3600}h ago"
    return f"{s//86400}d ago"

# ─────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────

st.set_page_config(
    page_title="JM Financial | Risk Intelligence",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ─────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────

st.markdown("""
<style>
html, body, [class*="css"] {
    font-family: 'IBM Plex Mono', 'Courier New', monospace !important;
    background-color: #0d1117 !important;
    color: #c9d1d9 !important;
}
.block-container { padding-top: 0.5rem !important; max-width: 1400px !important; }

/* ── MARKET MONITOR ── */
.market-monitor {
    background: linear-gradient(135deg, #0d1117 0%, #161b22 100%);
    border: 1px solid #21262d;
    border-radius: 10px;
    padding: 12px 16px;
    margin-bottom: 14px;
}
.market-monitor-title {
    font-size: 0.65rem;
    font-weight: 700;
    color: #484f58;
    letter-spacing: 0.12em;
    margin-bottom: 10px;
    text-transform: uppercase;
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
.ticker-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
}
.ticker-card {
    background: #161b22;
    border: 1px solid #21262d;
    border-radius: 8px;
    padding: 8px 14px;
    min-width: 120px;
    flex: 1;
    text-align: center;
}
.ticker-card.up   { border-bottom: 2px solid #3fb950; }
.ticker-card.down { border-bottom: 2px solid #f85149; }
.ticker-card.flat { border-bottom: 2px solid #484f58; }
.ticker-name  { font-size: 0.58rem; color: #484f58; letter-spacing: 0.06em; font-weight: 700; text-transform: uppercase; }
.ticker-price { font-size: 1.0rem; color: #e6edf3; font-weight: 700; margin: 3px 0; letter-spacing: -0.02em; }
.ticker-change-up   { font-size: 0.65rem; color: #3fb950; font-weight: 600; }
.ticker-change-down { font-size: 0.65rem; color: #f85149; font-weight: 600; }
.ticker-change-flat { font-size: 0.65rem; color: #484f58; font-weight: 600; }

/* ── HEADER ── */
.dashboard-header {
    background: linear-gradient(135deg, #161b22 0%, #0d1117 100%);
    border: 1px solid #21262d;
    border-radius: 8px;
    padding: 12px 20px;
    margin-bottom: 12px;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.dashboard-title  { font-size: 1.05rem; font-weight: 700; color: #e6edf3; letter-spacing: 0.05em; }
.dashboard-subtitle { font-size: 0.68rem; color: #484f58; margin-top: 2px; }
.live-dot {
    width: 8px; height: 8px; background: #3fb950;
    border-radius: 50%; display: inline-block; margin-right: 6px;
    animation: pulse 2s infinite;
}
@keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.3; } }

/* ── NEWS CARDS ── */
.news-card {
    background: #161b22;
    border: 1px solid #21262d;
    border-left: 3px solid #21262d;
    border-radius: 6px;
    padding: 10px 14px;
    margin-bottom: 6px;
}
.news-card:hover { border-left-color: #388bfd; }
.news-card.priority           { border-left-color: #f85149 !important; background: #1a0f0f; }
.news-card.sentiment-positive { border-left-color: #3fb950; background: #0d1a0f; }
.news-card.sentiment-negative { border-left-color: #f85149; background: #1a0d0d; }
.news-card.rbi  { border-left-color: #d29922; }
.news-card.nse  { border-left-color: #388bfd; }
.card-title {
    font-size: 0.82rem; font-weight: 500; color: #c9d1d9 !important;
    text-decoration: none !important; line-height: 1.4; display: block; margin-bottom: 6px;
}
.card-title:hover { color: #388bfd !important; }
.card-meta { font-size: 0.68rem; color: #484f58; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }

/* ── BADGES ── */
.badge-priority { background:#f85149; color:#fff; font-size:0.6rem; font-weight:700; padding:2px 6px; border-radius:3px; }
.badge-positive { background:#1a4a1f; color:#3fb950; font-size:0.6rem; font-weight:700; padding:2px 6px; border-radius:3px; border:1px solid #3fb950; }
.badge-negative { background:#4a1a1a; color:#f85149; font-size:0.6rem; font-weight:700; padding:2px 6px; border-radius:3px; border:1px solid #f85149; }
.badge-manual   { background:#1a2a4a; color:#388bfd; font-size:0.6rem; font-weight:700; padding:2px 6px; border-radius:3px; }
.badge-rbi      { background:#2a1f00; color:#d29922; font-size:0.6rem; font-weight:700; padding:2px 6px; border-radius:3px; border:1px solid #d29922; }
.badge-nse      { background:#001a3a; color:#388bfd; font-size:0.6rem; font-weight:700; padding:2px 6px; border-radius:3px; border:1px solid #388bfd; }
.badge-stock    { background:#2a1a4a; color:#a78bfa; font-size:0.6rem; font-weight:700; padding:2px 6px; border-radius:3px; }
.badge-source   { background:#21262d; color:#8b949e; font-size:0.58rem; padding:2px 5px; border-radius:3px; }
.cat-header {
    font-size:0.75rem; font-weight:700; color:#388bfd; letter-spacing:0.08em;
    padding:6px 0 10px; border-bottom:1px solid #21262d; margin-bottom:12px;
}
.stock-section-header {
    font-size:0.78rem; font-weight:700; color:#a78bfa; letter-spacing:0.06em;
    padding:8px 0 4px; border-bottom:1px solid #21262d; margin:12px 0 8px;
}
.stock-sentiment-bar { display:flex; gap:16px; font-size:0.68rem; color:#484f58; margin-bottom:10px; flex-wrap:wrap; }
.stock-sent-item .pos { color:#3fb950; font-weight:600; }
.stock-sent-item .neg { color:#f85149; font-weight:600; }
.stock-sent-item .neu { color:#484f58; font-weight:600; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# LIVE MARKET MONITOR
# ─────────────────────────────────────────────

market_data = fetch_market_data()

market_html = '<div class="market-monitor">'
market_html += '<div class="market-monitor-title">⚡ LIVE MARKET MONITOR &nbsp;·&nbsp; Data via Yahoo Finance &nbsp;·&nbsp; Refreshes every 60s</div>'
market_html += '<div class="ticker-grid">'

for name, data in market_data.items():
    price  = data.get("price")
    change = data.get("change")
    pct    = data.get("pct")
    unit   = TICKER_UNITS.get(name, "")

    if price is None:
        market_html += f"""
        <div class="ticker-card flat">
          <div class="ticker-name">{name}</div>
          <div class="ticker-price" style="font-size:0.8rem;color:#484f58;">—</div>
          <div class="ticker-change-flat">Unavailable</div>
        </div>"""
        continue

    direction  = "up" if change >= 0 else "down"
    arrow      = "▲" if change >= 0 else "▼"
    cls_chg    = "ticker-change-up" if change >= 0 else "ticker-change-down"
    price_str  = f"{unit}{price:,.0f}" if name in ("NIFTY 50", "SENSEX", "BANK NIFTY") else f"{unit}{price:,.2f}"
    change_str = f"{arrow} {abs(change):,.2f} ({abs(pct):.2f}%)"

    market_html += f"""
    <div class="ticker-card {direction}">
      <div class="ticker-name">{name}</div>
      <div class="ticker-price">{price_str}</div>
      <div class="{cls_chg}">{change_str}</div>
    </div>"""

market_html += "</div></div>"
st.markdown(market_html, unsafe_allow_html=True)

# ─────────────────────────────────────────────
# HEADER (Updated with pytz for IST)
# ─────────────────────────────────────────────

# 1. Define the India Timezone
ist = pytz.timezone('Asia/Kolkata')

# 2. Get current time in IST and format it
now_str = datetime.now(ist).strftime("%d %b %Y · %I:%M %p IST")

st.markdown(f"""
<div class="dashboard-header">
  <div>
    <div class="dashboard-title">
      <span class="live-dot"></span>JM FINANCIAL · RISK INTELLIGENCE DASHBOARD
    </div>
    <div class="dashboard-subtitle">Real-time news · RBI & NSE Circulars · Live Markets · {now_str}</div>
  </div>
</div>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# SIDEBAR — Stock lookup + Admin only
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
        # Using the gn() function already defined in your main code
        stock_feeds = [
            gn(sc + " stock NSE BSE India"),
            gn(sc + " share price earnings results"),
            gn(sc + " India company news"),
        ]
        with st.spinner(f"Fetching {sc}…"):
            # We pass filter_fn=None to ensure compatibility with your main script's definition
            stock_arts = fetch_urls_parallel(stock_feeds, filter_fn=None)

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
                
                # Using the time_ago() helper from your main script
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

    # ── Admin Panel ──
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
# DATA FETCHING
# ─────────────────────────────────────────────

@st.cache_data(ttl=90, show_spinner=False)
def load_all_data():
    all_data      = fetch_all_feeds(FEED_SOURCES)
    rbi_circulars = fetch_rbi_circulars()
    nse_circulars = fetch_nse_circulars()
    portfolio_data = {}
    port_feeds = {stock: [gn(f"{stock} stock NSE BSE India")] for stock in PORTFOLIO_STOCKS}
    raw_port   = fetch_all_feeds(port_feeds)
    for stock in PORTFOLIO_STOCKS:
        portfolio_data[stock] = raw_port.get(stock, [])
    return all_data, rbi_circulars, nse_circulars, portfolio_data


with st.spinner("📡 Fetching latest news..."):
    all_data, rbi_circulars, nse_circulars, portfolio_data = load_all_data()

manual_headlines = load_manual_headlines()
for item in manual_headlines:
    if "dt" in item and isinstance(item["dt"], str):
        try: item["dt"] = datetime.fromisoformat(item["dt"])
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
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("📰 Total Headlines", total)
c2.metric("⚡ Priority",  prio_n)
c3.metric("🟢 Positive",  pos_n)
c4.metric("🔴 Negative",  neg_n)
c5.metric("⚪ Neutral",   total - pos_n - neg_n)
st.markdown("---")

# ─────────────────────────────────────────────
# CARD RENDERERS
# ─────────────────────────────────────────────

def render_news_cards(articles: list):
    if not articles:
        st.markdown("""<div style='font-family:IBM Plex Mono,monospace;font-size:0.8rem;
        color:#484f58;padding:20px 0;'>No news found — try Refresh Now.</div>""",
        unsafe_allow_html=True)
        return
    for art in articles:
        is_prio   = art.get("priority", False)
        is_manual = art.get("manual", False)
        sentiment = art.get("sentiment", "neutral")
        cat_label = art.get("category", "")
        if is_prio:   card_class = "news-card priority"
        elif sentiment == "positive": card_class = "news-card sentiment-positive"
        elif sentiment == "negative": card_class = "news-card sentiment-negative"
        else: card_class = "news-card"
        dt_obj = art["dt"]
        if isinstance(dt_obj, str):
            try: dt_obj = datetime.fromisoformat(dt_obj)
            except: dt_obj = datetime.now(timezone.utc)
        ago = time_ago(dt_obj)
        prio_badge   = '<span class="badge-priority">⚡ PRIORITY</span>' if is_prio else ""
        manual_badge = '<span class="badge-manual">📢 INTERNAL</span>' if is_manual else ""
        if not is_prio and not is_manual:
            if sentiment == "positive":   sent_badge = '<span class="badge-positive">▲ POSITIVE</span>'
            elif sentiment == "negative": sent_badge = '<span class="badge-negative">▼ NEGATIVE</span>'
            else: sent_badge = ""
        else: sent_badge = ""
        st.markdown(f"""
        <div class="{card_class}">
          <a class="card-title" href="{art['link']}" target="_blank">{art['title']}</a>
          <div class="card-meta">
            <span>{ago}</span><span style="color:#30363d">·</span>
            <span>{cat_label}</span>{prio_badge}{manual_badge}{sent_badge}
          </div>
        </div>""", unsafe_allow_html=True)


def render_circular_cards(articles: list, badge_class: str, card_extra_class: str):
    if not articles:
        st.markdown("""<div style='font-family:IBM Plex Mono,monospace;font-size:0.8rem;
        color:#484f58;padding:20px 0;'>No circulars fetched — check network or try Refresh Now.</div>""",
        unsafe_allow_html=True)
        return
    label = "RBI" if "rbi" in card_extra_class else "NSE"
    for art in articles:
        is_prio   = art.get("priority", False)
        sentiment = art.get("sentiment", "neutral")
        card_cls  = f"news-card {card_extra_class}" + (" priority" if is_prio else "")
        ago       = time_ago(art["dt"])
        source    = art.get("source", "")
        prio_badge = '<span class="badge-priority">⚡ PRIORITY</span>' if is_prio else ""
        type_badge = f'<span class="{badge_class}">{label} CIRCULAR</span>'
        src_badge  = f'<span class="badge-source">{source}</span>' if source else ""
        if sentiment == "positive":   sent_badge = '<span class="badge-positive">▲ POSITIVE</span>'
        elif sentiment == "negative": sent_badge = '<span class="badge-negative">▼ NEGATIVE</span>'
        else: sent_badge = ""
        st.markdown(f"""
        <div class="{card_cls}">
          <a class="card-title" href="{art['link']}" target="_blank">{art['title']}</a>
          <div class="card-meta">
            <span>{ago}</span>{type_badge}{sent_badge}{prio_badge}{src_badge}
          </div>
        </div>""", unsafe_allow_html=True)


def render_portfolio_tab(portfolio_data: dict):
    if not portfolio_data:
        st.markdown("""<div style='font-family:IBM Plex Mono,monospace;font-size:0.8rem;
        color:#484f58;padding:20px 0;'>No portfolio stocks configured.</div>""",
        unsafe_allow_html=True)
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
        </div>""", unsafe_allow_html=True)
        if not articles:
            st.markdown("""<div style='font-family:IBM Plex Mono,monospace;
            font-size:0.78rem;color:#484f58;padding:6px 0 12px;'>No recent news found.</div>""",
            unsafe_allow_html=True)
            continue
        for art in articles:
            is_prio   = art.get("priority", False)
            sentiment = art.get("sentiment", "neutral")
            if is_prio:   card_class = "news-card priority"
            elif sentiment == "positive": card_class = "news-card sentiment-positive"
            elif sentiment == "negative": card_class = "news-card sentiment-negative"
            else: card_class = "news-card"
            dt_obj = art["dt"]
            if isinstance(dt_obj, str):
                try: dt_obj = datetime.fromisoformat(dt_obj)
                except: dt_obj = datetime.now(timezone.utc)
            ago = time_ago(dt_obj)
            prio_badge  = '<span class="badge-priority">⚡ PRIORITY</span>' if is_prio else ""
            stock_badge = f'<span class="badge-stock">{stock.upper()}</span>'
            if sentiment == "positive" and not is_prio:   sent_badge = '<span class="badge-positive">▲ POSITIVE</span>'
            elif sentiment == "negative" and not is_prio: sent_badge = '<span class="badge-negative">▼ NEGATIVE</span>'
            else: sent_badge = ""
            st.markdown(f"""
            <div class="{card_class}">
              <a class="card-title" href="{art['link']}" target="_blank">{art['title']}</a>
              <div class="card-meta">
                <span>{ago}</span>{stock_badge}{sent_badge}{prio_badge}
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
        st.markdown(f"""<div style='font-family:IBM Plex Mono,monospace;font-size:0.72rem;
        color:#f85149;margin-bottom:12px;'>⚡ {len(prio_arts)} priority items across all feeds</div>""",
        unsafe_allow_html=True)
    render_news_cards(prio_arts)

with tab_portfolio:
    total_port_articles = sum(len(v) for v in portfolio_data.values())
    st.markdown(f"""<div style='font-family:IBM Plex Mono,monospace;font-size:0.72rem;
    color:#a78bfa;margin-bottom:4px;'>Tracking <span style='color:#e6edf3;font-weight:600;'>
    {len(PORTFOLIO_STOCKS)} stocks</span> · {total_port_articles} total headlines
    <br><span style='color:#484f58;'>Edit PORTFOLIO_STOCKS in code to add/remove stocks.</span>
    </div>""", unsafe_allow_html=True)
    st.markdown("---")
    render_portfolio_tab(portfolio_data)

with tab_rbi:
    st.markdown(f"""<div style='font-family:IBM Plex Mono,monospace;font-size:0.72rem;
    color:#d29922;margin-bottom:4px;'>
    Sources: rbi.org.in/notifications_rss.xml · pressreleases_rss.xml · BS_CircularIndexDisplay.aspx
    <br><span style='color:#484f58;'>Direct from RBI official website · {len(rbi_circulars)} circulars loaded</span>
    </div>""", unsafe_allow_html=True)
    st.markdown("---")
    render_circular_cards(rbi_circulars, "badge-rbi", "rbi")

with tab_nse:
    st.markdown(f"""<div style='font-family:IBM Plex Mono,monospace;font-size:0.72rem;
    color:#388bfd;margin-bottom:4px;'>
    Sources: nsearchives.nseindia.com/content/RSS/Circulars.xml · Online_announcements.xml
    <br><span style='color:#484f58;'>Official NSE archives RSS · {len(nse_circulars)} circulars loaded</span>
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
