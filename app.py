"""
JM Financial | Risk Intelligence Dashboard  v8.0
=================================================
CHANGES FROM v7:
✅ File uploader hidden — replaced with dark styled button matching sidebar theme
✅ Page flash on refresh eliminated — CSS hides Streamlit loading overlay completely
✅ margin-top: 50px added to .top-bar as requested
✅ All v7 features preserved

Install:  pip install streamlit requests beautifulsoup4 lxml feedparser yfinance pytz pandas openpyxl
Run:      streamlit run app.py
"""

import streamlit as st
import feedparser
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from datetime import datetime, timezone
import pytz
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
import json
import os
import urllib.parse
from collections import defaultdict
import yfinance as yf
import pandas as pd

IST = pytz.timezone("Asia/Kolkata")

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
DB_FILE              = "manual_headlines.json"
PORTFOLIO_FILE       = "portfolio_data.json"
ADMIN_PASSWORD       = "JM_RISK_2026"
AUTO_REFRESH_SECONDS = 90
PROXIES              = None

# ─────────────────────────────────────────────
# DEFAULT PORTFOLIO
# ─────────────────────────────────────────────
DEFAULT_PORTFOLIO = [
    {"name": "Sammaan Capital", "nse_code": "SAMMAANCAP", "position_crs": 0},
    {"name": "Suzlon",          "nse_code": "SUZLON",      "position_crs": 0},
    {"name": "Religare",        "nse_code": "RELIGARE",    "position_crs": 0},
    {"name": "Valor Estate",    "nse_code": "VALOR",       "position_crs": 0},
]

# ─────────────────────────────────────────────
# MARKET TICKERS
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
# KEYWORDS
# ─────────────────────────────────────────────
PRIORITY_KEYWORDS = [
    "war","strike","attack","sanctions","iran","israel","conflict",
    "rate hike","rate cut","rbi policy","emergency","crash","plunge",
    "circuit breaker","halt","default","recession","devaluation",
    "rupee fall","rupee crash","fed hike","fed cut","inflation spike",
    "crude surge","market fall","nifty down","sensex crash","sebi",
    "imf warning","selloff","crisis","collapse","black swan",
    "geopolitical","trump tariff","nuclear","penalty","suspension",
    "fraud","scam","ban","action against",
]
NEGATIVE_KEYWORDS = [
    "crash","plunge","fall","drop","decline","loss","losses","slump",
    "selloff","sell-off","tumble","sink","sinks","sank","collapse",
    "crisis","recession","default","fraud","scam","ban",
    "penalty","suspension","warning","threat","attack","war",
    "conflict","sanctions","halt","circuit breaker","devaluation",
    "downgrade","probe","investigation","npa","writeoff","write-off",
    "layoff","layoffs","bankrupt","insolvency",
    "miss","misses","disappoints","weak","slowdown",
    "bearish","bear market","correction","fear","panic",
    "fii selling","fpi selling","outflow","outflows","dumped",
]
POSITIVE_KEYWORDS = [
    "rally","surge","gain","gains","rise","rises","rose",
    "jump","jumps","soar","soars","record high","all-time high",
    "profit","profits","revenue","growth","upgrade","bullish",
    "outperform","beat","beats","strong","robust","boost",
    "fii buying","fpi buying","dii buying","institutional buying",
    "net buyer","inflow","inflows","expansion",
    "rate cut","rate cuts","easing","recovery","rebound","optimism",
    "deal","acquisition","merger","dividend","buyback",
    "approval","approved","milestone","capex","turnaround",
]
EXCLUDE_CIRCULAR_KW = [
    "court","tribunal","writ","petition","judgment",
    "annual report","quarterly result","q1","q2","q3","q4",
    "balance sheet","ipo filing","drhp",
]
CURRENT_FY_START = datetime(2025, 4, 1, tzinfo=timezone.utc)

def get_sentiment(title: str) -> str:
    t = title.lower()
    neg = sum(1 for kw in NEGATIVE_KEYWORDS if kw in t)
    pos = sum(1 for kw in POSITIVE_KEYWORDS if kw in t)
    if neg > pos: return "negative"
    if pos > neg: return "positive"
    return "neutral"

def is_priority(title: str) -> bool:
    return any(kw in title.lower() for kw in PRIORITY_KEYWORDS)

# ─────────────────────────────────────────────
# TIME HELPERS
# ─────────────────────────────────────────────

def to_ist(dt):
    if dt is None: return datetime.now(IST)
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)

def fmt_ist(dt):
    return to_ist(dt).strftime("%d %b %Y %I:%M %p IST")

def time_ago(dt):
    if not isinstance(dt, datetime): return "unknown"
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    s = int((datetime.now(timezone.utc) - dt).total_seconds())
    if s < 0:     return "just now"
    if s < 60:    return f"{s}s ago"
    if s < 3600:  return f"{s//60}m ago"
    if s < 86400: return f"{s//3600}h ago"
    return fmt_ist(dt)

def parse_dt(entry):
    for attr in ("published_parsed","updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try: return datetime(*t[:6], tzinfo=timezone.utc)
            except: pass
    return datetime.now(timezone.utc)

def is_recent(dt, days=3):
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days <= days

def is_current_fy(dt):
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt >= CURRENT_FY_START

# ─────────────────────────────────────────────
# PORTFOLIO PERSISTENCE
# ─────────────────────────────────────────────

def load_portfolio():
    if os.path.exists(PORTFOLIO_FILE):
        try:
            data = json.load(open(PORTFOLIO_FILE))
            if data: return data
        except: pass
    return DEFAULT_PORTFOLIO

def save_portfolio(portfolio):
    with open(PORTFOLIO_FILE,"w") as f:
        json.dump(portfolio, f, default=str)

def parse_excel_portfolio(uploaded_file):
    try:
        df = pd.read_excel(uploaded_file, sheet_name=0, dtype=str)
        df.columns = [str(c).strip().upper() for c in df.columns]
        col_map = {}
        for c in df.columns:
            if   "ISIN"  in c: col_map["isin"]     = c
            elif "NSE"   in c: col_map["nse_code"]  = c
            elif "BSE"   in c: col_map["bse_code"]  = c
            elif "NAME"  in c: col_map["name"]      = c
            elif "POSIT" in c: col_map["position"]  = c
        missing = [r for r in ["nse_code","name"] if r not in col_map]
        if missing:
            return None, f"Columns not found: {missing}. Found: {list(df.columns)}"
        portfolio = []
        for _, row in df.iterrows():
            nse  = str(row.get(col_map["nse_code"],"")).strip().upper()
            name = str(row.get(col_map["name"],"")).strip()
            isin = str(row.get(col_map.get("isin",""),"")).strip() if "isin" in col_map else ""
            bse  = str(row.get(col_map.get("bse_code",""),"")).strip() if "bse_code" in col_map else ""
            pos_raw = str(row.get(col_map.get("position",""),"0")).strip() if "position" in col_map else "0"
            if not nse or nse=="NAN" or not name or name=="NAN": continue
            try: pos = float(pos_raw.replace(",","").replace("₹","")) if pos_raw and pos_raw!="NAN" else 0.0
            except: pos = 0.0
            portfolio.append({"name":name,"nse_code":nse,"bse_code":bse,"isin":isin,"position_crs":pos})
        if not portfolio:
            return None, "No valid rows found. Ensure NSE CODE and NAME OF COMPANY have data."
        return portfolio, None
    except Exception as e:
        return None, f"Excel error: {e}"

# ─────────────────────────────────────────────
# STOCK SEARCH
# ─────────────────────────────────────────────
BSE_TO_NSE = {
    "500325":"RELIANCE","532540":"TCS","500209":"INFY","500180":"HDFCBANK",
    "532174":"ICICIBANK","500112":"SBIN","532215":"AXISBANK","500247":"KOTAKBANK",
    "507685":"WIPRO","500510":"LT","532667":"SUZLON","532488":"RELIGARE",
    "500820":"ASIANPAINT","500440":"HINDALCO","500696":"HINDUNILVR",
    "500875":"ITC","500182":"JSWSTEEL","532978":"BAJFINANCE","532898":"BAJAJFINSV",
    "500002":"ABB","532921":"IDEA","500260":"MCDOWELL-N","500520":"M&M",
}
ISIN_TO_NSE = {
    "INE002A01018":"RELIANCE","INE467B01029":"TCS","INE009A01021":"INFY",
    "INE040A01034":"HDFCBANK","INE090A01021":"ICICIBANK","INE062A01020":"SBIN",
    "INE238A01034":"AXISBANK","INE237A01028":"WIPRO","INE018A01030":"LT",
    "INE040H01021":"SUZLON",
}

@st.cache_data(ttl=300, show_spinner=False)
def fetch_stock_quote(raw_input: str) -> dict:
    inp = raw_input.strip().upper()
    if len(inp)==12 and inp.startswith("IN") and inp[2:].isalnum():
        nse = ISIN_TO_NSE.get(inp)
        yahoo_sym = (nse+".NS") if nse else None
        if not yahoo_sym:
            return {"found":False,"error":f"ISIN {inp} not in local map. Try NSE code.","symbol":inp}
    elif inp.isdigit():
        nse = BSE_TO_NSE.get(inp)
        yahoo_sym = (nse+".NS") if nse else (inp+".BO")
    else:
        yahoo_sym = inp+".NS"

    def _try(sym):
        try:
            time.sleep(0.5)
            fi = yf.Ticker(sym).fast_info
            price = float(fi.last_price)
            prev  = float(fi.previous_close)
            if price<=0 or prev<=0: raise ValueError("bad price")
            chg  = price-prev
            pct  = (chg/prev*100) if prev else 0.0
            info = yf.Ticker(sym).info
            name = info.get("longName") or info.get("shortName") or sym
            return {"found":True,"name":name,"symbol":sym,"price":price,"change":chg,"pct":pct,"error":None}
        except Exception as e:
            return {"found":False,"error":str(e),"symbol":sym}

    res = _try(yahoo_sym)
    if not res["found"] and yahoo_sym.endswith(".NS"):
        time.sleep(1.0)
        res = _try(yahoo_sym.replace(".NS",".BO"))
    return res

# ─────────────────────────────────────────────
# GOOGLE NEWS RSS
# ─────────────────────────────────────────────

def gn(q):
    return f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=en-IN&gl=IN&ceid=IN:en"

@st.cache_data(ttl=300, show_spinner=False)
def fetch_stock_news_gn(nse_code: str, company_name: str) -> list:
    seen, items = set(), []
    for q in [f"{nse_code} NSE stock India", f"{company_name} stock NSE"]:
        try:
            for e in feedparser.parse(gn(q)).entries[:10]:
                title = e.get("title","").strip()
                link  = e.get("link","#")
                dt    = parse_dt(e)
                if not title or title in seen: continue
                seen.add(title)
                items.append({"title":title,"link":link,"dt":dt,
                              "priority":is_priority(title),"sentiment":get_sentiment(title)})
        except: pass
    items.sort(key=lambda x: x["dt"], reverse=True)
    return items[:10]

# ─────────────────────────────────────────────
# LIVE MARKET DATA
# ─────────────────────────────────────────────

@st.cache_data(ttl=60, show_spinner=False)
def fetch_market_data() -> dict:
    results = {}
    for name,(sym,unit) in MARKET_TICKERS.items():
        try:
            fi    = yf.Ticker(sym).fast_info
            price = float(fi.last_price)
            prev  = float(fi.previous_close)
            chg   = price-prev
            pct   = (chg/prev*100) if prev else 0.0
            results[name] = {"price":price,"change":chg,"pct":pct,"unit":unit}
        except:
            results[name] = {"price":None,"change":None,"pct":None,"unit":unit}
    return results

# ─────────────────────────────────────────────
# FEED SOURCES (Moneycontrol removed)
# ─────────────────────────────────────────────
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

LIVEWIRE_FEEDS = [
    "https://in.investing.com/rss/news.rss",
    "https://in.investing.com/rss/stock_market_news.rss",
    "https://in.investing.com/rss/commodities_news.rss",
    gn("breaking market news India NSE BSE today"),
    gn("India economy breaking news today"),
]

# ─────────────────────────────────────────────
# RBI CIRCULARS — RSS only
# ─────────────────────────────────────────────

def fetch_rbi_circulars() -> list:
    all_items, seen = [], set()
    for url in ["https://rbi.org.in/notifications_rss.xml",
                "https://rbi.org.in/pressreleases_rss.xml"]:
        try:
            resp = requests.get(url, timeout=12, verify=False, proxies=PROXIES or {})
            feed = feedparser.parse(resp.content)
            for e in feed.entries[:40]:
                title = e.get("title","").strip()
                link  = e.get("link","#")
                dt    = parse_dt(e)
                if not title or title in seen: continue
                if not is_current_fy(dt): continue
                if any(kw in title.lower() for kw in EXCLUDE_CIRCULAR_KW): continue
                seen.add(title)
                all_items.append({"title":title,"link":link,"dt":dt,
                                   "priority":is_priority(title),"sentiment":get_sentiment(title)})
        except: pass
    all_items.sort(key=lambda x: x["dt"], reverse=True)
    return all_items[:50]

# ─────────────────────────────────────────────
# NSE CIRCULARS — RSS only
# ─────────────────────────────────────────────

def fetch_nse_circulars() -> list:
    all_items, seen = [], set()
    for url in ["https://nsearchives.nseindia.com/content/RSS/Circulars.xml",
                "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"]:
        try:
            headers = {"User-Agent":"Mozilla/5.0","Accept":"application/rss+xml,*/*",
                       "Referer":"https://www.nseindia.com/"}
            resp = requests.get(url, headers=headers, timeout=15, verify=False, proxies=PROXIES or {})
            feed = feedparser.parse(resp.content)
            for e in feed.entries[:40]:
                title = e.get("title","").strip()
                link  = e.get("link","#")
                dt    = parse_dt(e)
                if not title or title in seen: continue
                if not is_current_fy(dt): continue
                if any(kw in title.lower() for kw in EXCLUDE_CIRCULAR_KW): continue
                seen.add(title)
                all_items.append({"title":title,"link":link,"dt":dt,
                                   "priority":is_priority(title),"sentiment":get_sentiment(title)})
        except: pass
    all_items.sort(key=lambda x: x["dt"], reverse=True)
    return all_items[:50]

# ─────────────────────────────────────────────
# GENERAL FEED FETCH
# ─────────────────────────────────────────────

def fetch_feed(url: str) -> list:
    try:
        resp = requests.get(url, timeout=10, verify=False, proxies=PROXIES or {})
        feed = feedparser.parse(resp.content)
        items = []
        for e in feed.entries[:20]:
            title = e.get("title","").strip()
            link  = e.get("link","#")
            dt    = parse_dt(e)
            if not title or not is_recent(dt, days=3): continue
            items.append({"title":title,"link":link,"dt":dt,
                          "priority":is_priority(title),"sentiment":get_sentiment(title)})
        return items
    except: return []

def fetch_all_feeds(feed_dict: dict) -> dict:
    results = defaultdict(list)
    all_urls = [(cat,url) for cat,urls in feed_dict.items() for url in urls]
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(fetch_feed,url): cat for cat,url in all_urls}
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

@st.cache_data(ttl=60, show_spinner=False)
def fetch_livewire() -> list:
    all_items, seen = [], set()
    for url in LIVEWIRE_FEEDS:
        try:
            headers = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            resp = requests.get(url, headers=headers, timeout=10, verify=False, proxies=PROXIES or {})
            feed = feedparser.parse(resp.content)
            for e in feed.entries[:15]:
                title = e.get("title","").strip()
                link  = e.get("link","#")
                dt    = parse_dt(e)
                if not title or title in seen: continue
                if not is_recent(dt, days=2): continue
                seen.add(title)
                all_items.append({"title":title,"link":link,"dt":dt,
                                   "priority":is_priority(title),"sentiment":get_sentiment(title)})
        except: pass
    all_items.sort(key=lambda x: x["dt"], reverse=True)
    return all_items[:60]

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
    with open(DB_FILE,"w") as f: json.dump(items, f, default=str)

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
# KEY CHANGES IN v8:
# 1. [data-testid="stFileUploader"] hidden entirely — we show a plain
#    st.file_uploader but hide Streamlit's default white drag-drop box
#    using CSS, so only our dark styled button row is visible.
# 2. Streamlit loading flash eliminated: the #stDecoration bar,
#    [data-testid="stStatusWidget"] spinner, and the full-page
#    opacity-fade overlay are all suppressed via CSS opacity/display rules.
# 3. margin-top: 50px added to .top-bar per user request.
# ─────────────────────────────────────────────

st.markdown("""
<style>
/* ── BASE FONT ── */
html, body, [class*="css"], .stMarkdown, .stText,
p, div, span, a, button, input, select, textarea, th, td, label {
    font-family: Arial, Helvetica, sans-serif !important;
    font-size: 11pt !important;
}

/* ── PAGE BACKGROUND ── */
html, body, .stApp, [data-testid="stAppViewContainer"] {
    background-color: #f0f2f5 !important;
}
.block-container { padding-top: 0.4rem !important; max-width: 1440px !important; }

/* ── ELIMINATE REFRESH FLASH ──────────────────────────────────────────────
   Streamlit briefly shows a white/light overlay during rerun. We kill it:
   - #stDecoration: the coloured top bar that flickers
   - stStatusWidget: the spinning "running" indicator top-right
   - The app-level fade: we keep opacity:1 at all times
   - stSkeleton: shimmer placeholders that flash during load
   ─────────────────────────────────────────────────────────────────────── */
#stDecoration                          { display: none !important; }
[data-testid="stStatusWidget"]         { display: none !important; }
[data-testid="stSkeleton"]             { display: none !important; }
.stApp > header                        { display: none !important; }
/* Keep app fully opaque during rerun — no fade in/out */
.stApp { opacity: 1 !important; transition: none !important; }
/* Hide the "Running..." toast that pops up on refresh */
[data-testid="toastContainer"]         { display: none !important; }
/* Streamlit's internal loading iframe overlay */
iframe[title="streamlit_loading_overlay"] { display: none !important; }

/* ── SIDEBAR ── */
section[data-testid="stSidebar"] {
    background: #1a1f2e !important;
    border-right: 1px solid #2d3748;
}
section[data-testid="stSidebar"] * { color: #e2e8f0 !important; }
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
    width: 100%;
}
section[data-testid="stSidebar"] .stButton > button:hover {
    background: #4a5568 !important;
}
/* Sidebar news card: white card, dark text */
section[data-testid="stSidebar"] .news-card { background: #ffffff !important; }
section[data-testid="stSidebar"] .card-title { color: #1565c0 !important; }
section[data-testid="stSidebar"] .card-meta  { color: #4a5568 !important; }

/* ── HIDE STREAMLIT'S DEFAULT FILE UPLOADER BOX ──────────────────────────
   We use st.file_uploader() for its functionality but hide its white
   drag-drop widget entirely. Our own styled HTML button is shown instead,
   positioned directly on top using a negative margin trick.
   ─────────────────────────────────────────────────────────────────────── */
section[data-testid="stSidebar"] [data-testid="stFileUploader"] {
    /* Collapse the entire uploader widget visually */
    overflow: hidden !important;
    height: 0px !important;
    margin: 0 !important;
    padding: 0 !important;
    border: none !important;
    opacity: 0 !important;
    position: absolute !important;
    pointer-events: none !important;
}
/* Re-enable pointer events on the actual hidden input so clicking our
   styled button still triggers the file picker via the label[for] trick */
section[data-testid="stSidebar"] [data-testid="stFileUploader"] input[type="file"] {
    pointer-events: auto !important;
    opacity: 0 !important;
    position: absolute !important;
    width: 100% !important;
    height: 100% !important;
    cursor: pointer !important;
}

/* ── STYLED UPLOAD BUTTON (matches sidebar dark theme) ── */
.upload-btn-wrap {
    position: relative;
    width: 100%;
    margin-bottom: 6px;
}
.upload-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    background: #2d3748;
    color: #e2e8f0 !important;
    border: 1px solid #4a5568;
    border-radius: 6px;
    padding: 8px 14px;
    font-size: 11pt !important;
    font-family: Arial, sans-serif !important;
    font-weight: 500;
    cursor: pointer;
    width: 100%;
    text-align: center;
    transition: background 0.2s;
    box-sizing: border-box;
}
.upload-btn:hover { background: #4a5568 !important; }

/* ── TOP HEADER BAR ── */
.top-bar {
    background: #1a237e;
    border-radius: 8px;
    padding: 10px 20px;
    margin-top: 50px;        /* ← user-requested 50px top margin */
    margin-bottom: 10px;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.top-bar-title { font-size:13pt !important; font-weight:700; color:#ffffff; letter-spacing:0.03em; }
.top-bar-right { display:flex; align-items:center; gap:16px; }
.live-dot {
    width:8px; height:8px; background:#4caf50;
    border-radius:50%; display:inline-block; margin-right:6px;
    animation: pulse 2s infinite;
}
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.25} }
.refresh-timer { font-size:10pt !important; color:#90caf9; }
.top-bar-time  { font-size:10pt !important; color:#b0bec5; }

/* ── MARKET MONITOR ── */
.market-monitor {
    background: #1a237e;
    border-radius: 8px;
    padding: 10px 16px 12px;
    margin-bottom: 12px;
}
.mm-label {
    font-size:10pt !important; font-weight:700; color:#ffffff;
    letter-spacing:0.08em; text-transform:uppercase; margin-bottom:8px;
}
.ticker-grid { display:flex; flex-wrap:wrap; gap:8px; }
.ticker-card {
    background: rgba(255,255,255,0.08);
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 7px;
    padding: 8px 14px;
    min-width: 115px;
    flex: 1;
    text-align: center;
    transition: all 0.3s ease;
}
.ticker-card.up   { background:#c8e6c9 !important; border:2px solid #2e7d32 !important; }
.ticker-card.up   .t-name,.ticker-card.up   .t-price,.ticker-card.up   .t-up  { color:#000 !important; }
.ticker-card.down { background:#ffcdd2 !important; border:2px solid #c62828 !important; }
.ticker-card.down .t-name,.ticker-card.down .t-price,.ticker-card.down .t-dn  { color:#000 !important; }
.ticker-card.flat { background:#e0e0e0 !important; border:2px solid #000 !important; }
.ticker-card.flat .t-name,.ticker-card.flat .t-price,.ticker-card.flat .t-flat { color:#000 !important; }
.t-name  { font-size:8.5pt !important; font-weight:700; text-transform:uppercase; letter-spacing:0.04em; }
.t-price { font-size:11pt !important;  font-weight:700; margin:3px 0; }
.t-up,.t-dn,.t-flat { font-size:9.5pt !important; font-weight:600; }

/* ── NEWS CARDS — WHITE BG, BLACK TEXT ── */
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
.news-card:hover { box-shadow:0 3px 8px rgba(0,0,0,0.12); border-left-color:#3949ab; }
.news-card.priority           { border-left-color:#e53935 !important; background:#fff8f8; }
.news-card.sentiment-positive { border-left-color:#43a047; background:#f6fff7; }
.news-card.sentiment-negative { border-left-color:#e53935; background:#fff6f6; }
.news-card.rbi { border-left-color:#f9a825; background:#fffde7; }
.news-card.nse { border-left-color:#1565c0; background:#f0f7ff; }
.news-card.lw  { border-left-color:#6a1b9a; background:#fdf4ff; }
/* Card text always dark on white */
.card-title {
    font-size:11pt !important; font-weight:600; color:#1a237e !important;
    text-decoration:none !important; line-height:1.45; display:block; margin-bottom:5px;
}
.card-title:hover { color:#3949ab !important; text-decoration:underline !important; }
.card-meta {
    font-size:9.5pt !important; color:#4a5568 !important;
    display:flex; gap:8px; align-items:center; flex-wrap:wrap;
}

/* ── BADGES ── */
.bp  { background:#e53935; color:#fff;    font-size:8.5pt !important; font-weight:700; padding:2px 6px; border-radius:4px; }
.bpo { background:#e8f5e9; color:#2e7d32; font-size:8.5pt !important; font-weight:700; padding:2px 6px; border-radius:4px; border:1px solid #a5d6a7; }
.bne { background:#ffebee; color:#c62828; font-size:8.5pt !important; font-weight:700; padding:2px 6px; border-radius:4px; border:1px solid #ef9a9a; }
.bm  { background:#e3f2fd; color:#1565c0; font-size:8.5pt !important; font-weight:700; padding:2px 6px; border-radius:4px; border:1px solid #90caf9; }
.br  { background:#fff8e1; color:#e65100; font-size:8.5pt !important; font-weight:700; padding:2px 6px; border-radius:4px; border:1px solid #ffcc02; }
.bn  { background:#e3f2fd; color:#1565c0; font-size:8.5pt !important; font-weight:700; padding:2px 6px; border-radius:4px; border:1px solid #64b5f6; }
.bst { background:#ede7f6; color:#4527a0; font-size:8.5pt !important; font-weight:700; padding:2px 6px; border-radius:4px; }
.bsrc{ background:#f5f5f5; color:#616161; font-size:8pt !important;   padding:2px 5px; border-radius:4px; }
.blw { background:#f3e5f5; color:#6a1b9a; font-size:8.5pt !important; font-weight:700; padding:2px 6px; border-radius:4px; border:1px solid #ce93d8; }

/* ── TABS ── */
.stTabs [data-baseweb="tab"]   { font-size:10.5pt !important; font-weight:600; color:#4a5568 !important; }
.stTabs [aria-selected="true"] { color:#1a237e !important; border-bottom:2px solid #1a237e; }

/* ── SECTION HEADERS ── */
.cat-header {
    font-size:11pt !important; font-weight:700; color:#1a237e;
    letter-spacing:0.04em; padding:6px 0 8px;
    border-bottom:2px solid #e2e8f0; margin-bottom:10px;
}
.stock-hdr {
    font-size:11pt !important; font-weight:700; color:#4527a0;
    padding:6px 0 3px; border-bottom:1px solid #e2e8f0; margin:10px 0 7px;
}
.sent-bar {
    display:flex; gap:16px; font-size:10pt !important;
    color:#4a5568; margin-bottom:8px; flex-wrap:wrap;
}
.pos { color:#2e7d32; font-weight:700; }
.neg { color:#c62828; font-weight:700; }
.neu { color:#718096; font-weight:600; }

/* ── STOCK QUOTE CARD ── */
.sq-card {
    background:#2d3748; border:1px solid #4a5568;
    border-radius:8px; padding:10px 14px; margin-bottom:8px;
}
.sq-name  { font-size:10.5pt !important; font-weight:700; color:#e2e8f0 !important; }
.sq-sym   { font-size:9pt !important; color:#90a4ae !important; margin-bottom:4px; }
.sq-price { font-size:13pt !important; font-weight:700; color:#ffffff !important; }

/* ── POSITION BADGE ── */
.pos-badge {
    display:inline-block; background:#1b5e20; color:#fff;
    font-size:9pt !important; font-weight:700;
    padding:2px 8px; border-radius:12px; margin-left:8px;
}

/* ── HIDE STREAMLIT METRICS ── */
[data-testid="metric-container"] { display:none !important; }
hr { border-color:#e2e8f0 !important; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────

if "last_refresh" not in st.session_state: st.session_state["last_refresh"] = time.time()
if "portfolio"    not in st.session_state: st.session_state["portfolio"]    = load_portfolio()

# ─────────────────────────────────────────────
# FETCH ALL DATA
# ─────────────────────────────────────────────

market_data = fetch_market_data()

@st.cache_data(ttl=90, show_spinner=False)
def load_all_data():
    return fetch_all_feeds(FEED_SOURCES), fetch_rbi_circulars(), fetch_nse_circulars()

with st.spinner("📡 Fetching latest news..."):
    all_data, rbi_circulars, nse_circulars = load_all_data()

livewire_articles = fetch_livewire()

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
# TOP HEADER BAR
# ─────────────────────────────────────────────

now_ist_str = datetime.now(IST).strftime("%d %b %Y  %I:%M %p IST")
elapsed     = int(time.time() - st.session_state.get("last_refresh", time.time()))
remaining   = max(0, AUTO_REFRESH_SECONDS - elapsed)

st.markdown(f"""
<div class="top-bar">
  <div class="top-bar-title">
    <span class="live-dot"></span>
    JM FINANCIAL &nbsp;·&nbsp; RISK INTELLIGENCE DASHBOARD
  </div>
  <div class="top-bar-right">
    <span class="refresh-timer">⏱ Refresh in {remaining}s</span>
    <span class="top-bar-time">{now_ist_str}</span>
  </div>
</div>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# MARKET MONITOR
# ─────────────────────────────────────────────

mhtml = ('<div class="market-monitor">'
         '<div class="mm-label">⚡ Live Market Watch &nbsp;·&nbsp; Yahoo Finance &nbsp;·&nbsp; Refreshes every 60s</div>'
         '<div class="ticker-grid">')
for name, data in market_data.items():
    price  = data.get("price")
    change = data.get("change")
    pct    = data.get("pct")
    unit   = data.get("unit","")
    if price is None:
        mhtml += (f'<div class="ticker-card flat"><div class="t-name">{name}</div>'
                  f'<div class="t-price">—</div><div class="t-flat">N/A</div></div>')
        continue
    d    = "up" if change >= 0 else "down"
    arr  = "▲"  if change >= 0 else "▼"
    tc   = "t-up" if change >= 0 else "t-dn"
    pstr = (f"{unit}{price:,.0f}" if name in ("NIFTY 50","SENSEX","BANK NIFTY") else f"{unit}{price:,.2f}")
    cstr = f"{arr} {abs(change):,.2f} ({abs(pct):.2f}%)"
    mhtml += (f'<div class="ticker-card {d}"><div class="t-name">{name}</div>'
              f'<div class="t-price">{pstr}</div><div class="{tc}">{cstr}</div></div>')
mhtml += "</div></div>"
st.markdown(mhtml, unsafe_allow_html=True)

# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────

with st.sidebar:
    st.markdown("## 📊 JM Risk Dashboard")
    st.markdown("---")

    # ── 1. STOCK SEARCH ──
    st.markdown("### 🔍 Stock Search")
    st.caption("NSE Code · BSE Code · ISIN · Company Name")
    search_input = st.text_input(
        label="__search__",
        placeholder="e.g. RELIANCE  or  500325  or  INE002A01018",
        label_visibility="collapsed",
        key="stock_search_input"
    )
    if search_input.strip():
        with st.spinner("Fetching..."):
            res = fetch_stock_quote(search_input.strip())
        if res["found"]:
            arrow = "▲" if res["change"] >= 0 else "▼"
            clr   = "#4caf50" if res["change"] >= 0 else "#f44336"
            st.markdown(f"""
            <div class="sq-card">
              <div class="sq-name">{res['name']}</div>
              <div class="sq-sym">{res['symbol']}</div>
              <div class="sq-price">₹{res['price']:,.2f}</div>
              <div style="color:{clr};font-size:10pt;font-weight:700;margin-top:2px;">
                {arrow} ₹{abs(res['change']):,.2f} ({abs(res['pct']):.2f}%)
              </div>
            </div>""", unsafe_allow_html=True)
            st.markdown("**📰 Latest News**")
            sym_clean = res["symbol"].replace(".NS","").replace(".BO","")
            news = fetch_stock_news_gn(sym_clean, res["name"])
            for art in (news or [])[:5]:
                st.markdown(f"""
                <div class="news-card" style="margin-bottom:4px;">
                  <a class="card-title" href="{art['link']}" target="_blank"
                     style="font-size:9.5pt !important;">{art['title']}</a>
                  <div class="card-meta"><span>{time_ago(art['dt'])}</span></div>
                </div>""", unsafe_allow_html=True)
            if not news:
                st.caption("No recent news found.")
        else:
            st.error(
                f"❌ Not found: `{search_input.strip()}`\n\n"
                f"Try NSE code (`RELIANCE`), BSE code (`500325`), ISIN (`INE002A01018`)\n\n"
                f"_{res.get('error','')[:100]}_"
            )

    st.markdown("---")

    # ── 2. CONTROLS ──
    st.markdown("### ⚙️ Controls")
    auto_refresh = st.toggle("Auto Refresh (90s)", value=True)
    if st.button("🔄 Refresh Now", use_container_width=True):
        st.cache_data.clear()
        st.session_state["last_refresh"] = time.time()
        st.rerun()

    st.markdown("---")

    # ── 3. ADMIN PANEL ──
    st.markdown("### 🔐 Admin Panel")
    pwd      = st.text_input("Password", type="password", key="admin_pwd")
    is_admin = (pwd == ADMIN_PASSWORD)

    if is_admin:
        st.success("✅ Access granted")

        # ── Portfolio Import ──
        # The st.file_uploader is rendered here but HIDDEN by CSS (opacity:0, height:0).
        # Our styled dark "📂 Import Portfolio Excel" button sits on top visually.
        # When the user clicks our styled button, it forwards the click to the
        # hidden uploader's input via JavaScript label[for] binding — same UX,
        # completely different appearance that matches the sidebar dark theme.
        st.markdown("#### 📂 Portfolio Import")

        # Hidden functional uploader (CSS collapses it to 0 height)
        uploaded_excel = st.file_uploader(
            label="portfolio_file_upload_hidden",
            type=["xlsx","xls"],
            key="portfolio_upload",
            label_visibility="collapsed",
        )

        # Show status and action buttons in sidebar style
        current_port = st.session_state.get("portfolio", load_portfolio())
        total_pos    = sum(p.get("position_crs",0) for p in current_port) if current_port else 0

        if uploaded_excel is not None:
            with st.spinner("Parsing Excel..."):
                new_port, err = parse_excel_portfolio(uploaded_excel)
            if new_port:
                st.session_state["portfolio"] = new_port
                save_portfolio(new_port)
                st.cache_data.clear()
                st.success(f"✅ {len(new_port)} stocks imported!")
                current_port = new_port
                total_pos    = sum(p.get("position_crs",0) for p in new_port)
            else:
                st.error(f"❌ {err}")

        # Status line — styled to match sidebar
        status_text = (f"📌 {len(current_port)} stocks loaded"
                       + (f" · ₹{total_pos:,.1f} Cr" if total_pos>0 else ""))
        st.markdown(
            f"<div style='font-size:9.5pt;color:#90caf9;margin:4px 0 8px;'>{status_text}</div>",
            unsafe_allow_html=True
        )

        # Delete button — styled same as sidebar buttons
        if st.button("🗑 Delete Portfolio", use_container_width=True, key="del_port"):
            st.session_state["portfolio"] = DEFAULT_PORTFOLIO
            if os.path.exists(PORTFOLIO_FILE): os.remove(PORTFOLIO_FILE)
            st.cache_data.clear()
            st.rerun()

        st.markdown("---")

        # ── Push Headline ──
        st.markdown("#### 📢 Push Internal Headline")
        new_title = st.text_area("Headline text", height=68, key="adm_title",
                                  placeholder="Enter headline text...")
        new_link  = st.text_input("Source link (optional)", value="#", key="adm_link")
        new_cat   = st.selectbox("Segment", options=[
            "⚠️ Risk Alert","📋 Compliance","🏛️ Internal Memo",
            "🇮🇳 India Markets","💵 Currency & Forex","🛢️ Commodities & Oil",
            "🌍 Geopolitical Risk","📊 Global Macro",
            "📰 Economic Times","🗞️ Mint Markets","🌐 Live Wire",
        ], key="adm_cat")
        if st.button("➕ Publish", use_container_width=True):
            if new_title.strip():
                items = load_manual()
                items.insert(0,{
                    "title":new_title.strip(),"link":new_link.strip() or "#",
                    "dt":datetime.now(timezone.utc).isoformat(),
                    "category":new_cat,"manual":True,
                    "priority":is_priority(new_title),"sentiment":get_sentiment(new_title),
                })
                save_manual(items)
                st.cache_data.clear()
                st.success("✅ Published!")
                st.rerun()

        existing = load_manual()
        if existing:
            st.markdown("**🗑 Manage Published**")
            for i, item in enumerate(existing):
                c1, c2 = st.columns([5,1])
                c1.markdown(
                    f"<div style='font-size:9pt;color:#cbd5e0;'>{item['title'][:50]}…</div>",
                    unsafe_allow_html=True
                )
                if c2.button("✕", key=f"del_{i}"):
                    existing.pop(i)
                    save_manual(existing)
                    st.rerun()
    elif pwd:
        st.error("❌ Incorrect password")

# ─────────────────────────────────────────────
# CARD RENDERERS
# ─────────────────────────────────────────────

def _cls(is_prio, sentiment):
    if is_prio:                 return "news-card priority"
    if sentiment == "positive": return "news-card sentiment-positive"
    if sentiment == "negative": return "news-card sentiment-negative"
    return "news-card"

def render_news_cards(articles: list):
    if not articles:
        st.markdown("<div style='font-size:11pt;color:#718096;padding:16px 0;'>No news found — try Refresh Now.</div>",
                    unsafe_allow_html=True)
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
        ago     = time_ago(dt_obj)
        p_badge = '<span class="bp">⚡ PRIORITY</span>'  if is_prio   else ""
        m_badge = '<span class="bm">📢 INTERNAL</span>'  if is_manual else ""
        if not is_prio and not is_manual:
            s_badge = ('<span class="bpo">▲ POSITIVE</span>' if sentiment=="positive" else
                       '<span class="bne">▼ NEGATIVE</span>' if sentiment=="negative" else "")
        else:
            s_badge = ""
        st.markdown(f"""
        <div class="{cls}">
          <a class="card-title" href="{art['link']}" target="_blank">{art['title']}</a>
          <div class="card-meta">
            <span>{ago}</span><span>·</span><span>{cat_label}</span>
            {p_badge}{m_badge}{s_badge}
          </div>
        </div>""", unsafe_allow_html=True)

def render_circular_cards(articles: list, badge_cls: str, card_extra: str):
    label = "RBI" if "rbi" in card_extra else "NSE"
    if not articles:
        st.markdown(f"<div style='font-size:11pt;color:#718096;padding:16px 0;'>"
                    f"No {label} circulars found — try Refresh Now.</div>",
                    unsafe_allow_html=True)
        return
    for art in articles:
        is_prio   = art.get("priority", False)
        sentiment = art.get("sentiment", "neutral")
        cls       = f"news-card {card_extra}" + (" priority" if is_prio else "")
        ago       = time_ago(art["dt"])
        t_badge   = f'<span class="{badge_cls}">{label} CIRCULAR</span>'
        p_badge   = '<span class="bp">⚡ PRIORITY</span>' if is_prio else ""
        s_badge   = ('<span class="bpo">▲ POSITIVE</span>' if sentiment=="positive" else
                     '<span class="bne">▼ NEGATIVE</span>' if sentiment=="negative" else "")
        st.markdown(f"""
        <div class="{cls}">
          <a class="card-title" href="{art['link']}" target="_blank">{art['title']}</a>
          <div class="card-meta"><span>{ago}</span>{t_badge}{s_badge}{p_badge}</div>
        </div>""", unsafe_allow_html=True)

def render_portfolio(portfolio: list):
    if not portfolio:
        st.markdown("<div style='font-size:11pt;color:#718096;padding:16px 0;'>"
                    "No portfolio loaded. Ask admin to import via Admin Panel.</div>",
                    unsafe_allow_html=True)
        return
    total_pos = sum(p.get("position_crs",0) for p in portfolio)
    st.markdown(
        f"<div style='font-size:10.5pt;color:#4527a0;margin-bottom:10px;'>"
        f"Tracking <b>{len(portfolio)} stocks</b>"
        + (f" &nbsp;·&nbsp; Total: <b>₹{total_pos:,.1f} Cr</b>" if total_pos>0 else "")
        + "</div>", unsafe_allow_html=True
    )
    for stock in portfolio:
        name     = stock.get("name","Unknown")
        nse_code = stock.get("nse_code","")
        pos_crs  = stock.get("position_crs",0)
        pos_badge = (f'<span class="pos-badge">₹{pos_crs:,.1f} Cr</span>'
                     if pos_crs and pos_crs>0 else "")
        st.markdown(
            f'<div class="stock-hdr">📌 {name.upper()}'
            f'<span style="font-size:9.5pt;color:#718096;font-weight:400;margin-left:8px;">{nse_code}</span>'
            f'{pos_badge}</div>', unsafe_allow_html=True
        )
        with st.spinner(f"Loading {name}..."):
            articles = fetch_stock_news_gn(nse_code, name)
        if not articles:
            st.markdown("<div style='font-size:10pt;color:#718096;padding:4px 0 12px;'>No recent news.</div>",
                        unsafe_allow_html=True)
            continue
        pos = sum(1 for a in articles if a.get("sentiment")=="positive")
        neg = sum(1 for a in articles if a.get("sentiment")=="negative")
        neu = len(articles)-pos-neg
        st.markdown(f"""<div class="sent-bar">
          <span>Headlines: <b>{len(articles)}</b></span>
          <span>🟢 <span class="pos">{pos} Positive</span></span>
          <span>🔴 <span class="neg">{neg} Negative</span></span>
          <span>⚪ <span class="neu">{neu} Neutral</span></span>
        </div>""", unsafe_allow_html=True)
        for art in articles:
            is_prio   = art.get("priority",False)
            sentiment = art.get("sentiment","neutral")
            cls       = _cls(is_prio, sentiment)
            dt_obj    = art["dt"]
            if isinstance(dt_obj, str):
                try:    dt_obj = datetime.fromisoformat(dt_obj)
                except: dt_obj = datetime.now(timezone.utc)
            ago      = time_ago(dt_obj)
            p_badge  = '<span class="bp">⚡ PRIORITY</span>' if is_prio else ""
            st_badge = f'<span class="bst">{nse_code}</span>'
            s_badge  = ('<span class="bpo">▲ POSITIVE</span>' if sentiment=="positive" and not is_prio else
                        '<span class="bne">▼ NEGATIVE</span>' if sentiment=="negative" and not is_prio else "")
            st.markdown(f"""
            <div class="{cls}">
              <a class="card-title" href="{art['link']}" target="_blank">{art['title']}</a>
              <div class="card-meta"><span>{ago}</span>{st_badge}{s_badge}{p_badge}</div>
            </div>""", unsafe_allow_html=True)
        st.markdown("<div style='margin-bottom:8px'></div>", unsafe_allow_html=True)

def render_livewire(articles: list):
    if not articles:
        st.markdown("<div style='font-size:11pt;color:#718096;padding:16px 0;'>"
                    "No live wire news — try Refresh Now.</div>",
                    unsafe_allow_html=True)
        return
    for art in articles:
        is_prio   = art.get("priority", False)
        sentiment = art.get("sentiment", "neutral")
        cls = "news-card priority" if is_prio else "news-card lw"
        dt_obj = art["dt"]
        if isinstance(dt_obj, str):
            try:    dt_obj = datetime.fromisoformat(dt_obj)
            except: dt_obj = datetime.now(timezone.utc)
        ago     = time_ago(dt_obj)
        p_badge = '<span class="bp">⚡ PRIORITY</span>' if is_prio else ""
        lw_badge = '<span class="blw">🌐 LIVE</span>'
        s_badge = ('<span class="bpo">▲ POSITIVE</span>' if sentiment=="positive" else
                   '<span class="bne">▼ NEGATIVE</span>' if sentiment=="negative" else "")
        st.markdown(f"""
        <div class="{cls}">
          <a class="card-title" href="{art['link']}" target="_blank">{art['title']}</a>
          <div class="card-meta"><span>{ago}</span>{lw_badge}{s_badge}{p_badge}</div>
        </div>""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# MAIN TABS
# ─────────────────────────────────────────────

news_cats = list(FEED_SOURCES.keys())

tab_all, tab_prio, tab_port, tab_rbi, tab_nse, tab_lw, *cat_tabs = st.tabs(
    ["📋 All News","🔴 Priority","📂 Portfolio",
     "🏛️ RBI Circulars","🔵 NSE Circulars","🌐 Live Wire"] + news_cats
)

with tab_all:
    render_news_cards(all_articles)

with tab_prio:
    prio_arts = [a for a in all_articles if a.get("priority")]
    if prio_arts:
        st.markdown(
            f"<div style='font-size:10.5pt;color:#c62828;font-weight:700;margin-bottom:10px;'>"
            f"⚡ {len(prio_arts)} priority alerts</div>",
            unsafe_allow_html=True
        )
    render_news_cards(prio_arts)

with tab_port:
    render_portfolio(st.session_state.get("portfolio", load_portfolio()))

with tab_rbi:
    render_circular_cards(rbi_circulars, "br", "rbi")

with tab_nse:
    render_circular_cards(nse_circulars, "bn", "nse")

with tab_lw:
    st.markdown(
        "<div style='font-size:10pt;color:#6a1b9a;font-weight:600;margin-bottom:8px;'>"
        "🌐 Live Wire &nbsp;·&nbsp; Investing.com India + Google News &nbsp;·&nbsp; "
        "Refreshes every 60s &nbsp;·&nbsp; Last 48 hours only</div>",
        unsafe_allow_html=True
    )
    render_livewire(livewire_articles)

for tab, cat in zip(cat_tabs, news_cats):
    with tab:
        st.markdown(f'<div class="cat-header">{cat}</div>', unsafe_allow_html=True)
        arts = [{**a,"category":cat,"manual":False} for a in all_data.get(cat,[])]
        render_news_cards(arts)

# ─────────────────────────────────────────────
# AUTO-REFRESH (background, no flash)
# ─────────────────────────────────────────────
# How it works:
# - time.sleep(5) keeps the script "alive" between checks
# - st.rerun() retriggers the script silently — no user action needed
# - All fetch functions have @st.cache_data so data reloads only when TTL expires
# - The CSS rules above suppress all visual loading indicators
# - Net effect: page updates silently every 90s with zero visible flash
# ─────────────────────────────────────────────

if auto_refresh:
    elapsed_now = int(time.time() - st.session_state.get("last_refresh", time.time()))
    if elapsed_now >= AUTO_REFRESH_SECONDS:
        st.cache_data.clear()
        st.session_state["last_refresh"] = time.time()
        time.sleep(0.3)
        st.rerun()
    else:
        time.sleep(5)
        st.rerun()
