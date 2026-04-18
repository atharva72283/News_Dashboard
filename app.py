"""
JM Financial | Risk Intelligence Dashboard  v11.0
==================================================
CHANGES FROM v10:
✅ Portfolio section: Two clean dark buttons [📂 Import] [🗑 Delete]
   Import button uses st.session_state toggle to show/hide file_uploader
   File uploader appears below buttons (styled dark) only when Import is clicked
   Clicking Import again hides it → clean toggle UX
✅ Login log: replaced table display with [⬇ Download Log] button (CSV download)
   Log table removed from admin panel entirely — clean and private
✅ All flash suppression, AJ RSS, current-month portfolio, margin-top:50px preserved

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
import io
import csv
import urllib.parse
from collections import defaultdict
import yfinance as yf
import pandas as pd

IST = pytz.timezone("Asia/Kolkata")

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
DB_FILE        = "manual_headlines.json"
PORTFOLIO_FILE = "portfolio_data.json"
LOGIN_LOG_FILE = "login_log.json"
ADMIN_PASSWORD = "JM_RISK_2026"
AUTO_REFRESH   = 90
PROXIES        = None

DEFAULT_PORTFOLIO = [
    {"name":"Sammaan Capital","nse_code":"SAMMAANCAP","position_crs":0},
    {"name":"Suzlon",         "nse_code":"SUZLON",     "position_crs":0},
    {"name":"Religare",       "nse_code":"RELIGARE",   "position_crs":0},
    {"name":"Valor Estate",   "nse_code":"VALOR",      "position_crs":0},
]

MARKET_TICKERS = {
    "NIFTY 50":    ("^NSEI",   "₹"),
    "SENSEX":      ("^BSESN",  "₹"),
    "BANK NIFTY":  ("^NSEBANK","₹"),
    "CRUDE (WTI)": ("CL=F",   "$"),
    "BRENT":       ("BZ=F",   "$"),
    "GOLD":        ("GC=F",   "$"),
    "SILVER":      ("SI=F",   "$"),
}

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

def get_sentiment(title):
    t = title.lower()
    n = sum(1 for kw in NEGATIVE_KEYWORDS if kw in t)
    p = sum(1 for kw in POSITIVE_KEYWORDS if kw in t)
    if n > p: return "negative"
    if p > n: return "positive"
    return "neutral"

def is_priority(title):
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
    for a in ("published_parsed","updated_parsed"):
        t = getattr(entry, a, None)
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

def is_current_month(dt):
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return dt.year == now.year and dt.month == now.month

# ─────────────────────────────────────────────
# IP LOGIN LOG
# ─────────────────────────────────────────────

def record_login():
    if st.session_state.get("_login_recorded"): return
    st.session_state["_login_recorded"] = True
    ip, ua = "unknown", "unknown"
    try:
        headers = st.context.headers
        ip = (headers.get("X-Forwarded-For","").split(",")[0].strip()
              or headers.get("X-Real-Ip","")
              or headers.get("Remote-Addr","")
              or "unknown")
        ua = headers.get("User-Agent","unknown")[:200]
    except: pass
    entry = {
        "timestamp": datetime.now(IST).strftime("%d %b %Y %I:%M:%S %p IST"),
        "ip": ip if ip else "unknown",
        "user_agent": ua,
    }
    logs = []
    if os.path.exists(LOGIN_LOG_FILE):
        try:
            with open(LOGIN_LOG_FILE) as f: logs = json.load(f)
        except: logs = []
    logs.insert(0, entry)
    logs = logs[:500]
    with open(LOGIN_LOG_FILE,"w") as f: json.dump(logs, f, default=str)

def load_login_logs():
    if not os.path.exists(LOGIN_LOG_FILE): return []
    try:
        with open(LOGIN_LOG_FILE) as f: return json.load(f)
    except: return []

def logs_to_csv_bytes(logs):
    """Convert login log list to CSV bytes for download."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Timestamp (IST)", "IP Address", "Browser / Device"])
    for log in logs:
        writer.writerow([log.get("timestamp",""), log.get("ip",""), log.get("user_agent","")])
    return buf.getvalue().encode("utf-8")

# ─────────────────────────────────────────────
# PORTFOLIO PERSISTENCE
# ─────────────────────────────────────────────

def load_portfolio():
    if os.path.exists(PORTFOLIO_FILE):
        try:
            d = json.load(open(PORTFOLIO_FILE))
            if d: return d
        except: pass
    return DEFAULT_PORTFOLIO

def save_portfolio(p):
    with open(PORTFOLIO_FILE,"w") as f: json.dump(p, f, default=str)

def parse_excel_portfolio(f):
    try:
        df = pd.read_excel(f, sheet_name=0, dtype=str)
        df.columns = [str(c).strip().upper() for c in df.columns]
        cm = {}
        for c in df.columns:
            if   "ISIN"  in c: cm["isin"]    = c
            elif "NSE"   in c: cm["nse"]      = c
            elif "BSE"   in c: cm["bse"]      = c
            elif "NAME"  in c: cm["name"]     = c
            elif "POSIT" in c: cm["position"] = c
        miss = [r for r in ["nse","name"] if r not in cm]
        if miss: return None, f"Columns missing: {miss}. Found: {list(df.columns)}"
        out = []
        for _, row in df.iterrows():
            nse  = str(row.get(cm["nse"],"")).strip().upper()
            name = str(row.get(cm["name"],"")).strip()
            bse  = str(row.get(cm.get("bse",""),"")).strip() if "bse" in cm else ""
            isin = str(row.get(cm.get("isin",""),"")).strip() if "isin" in cm else ""
            pr   = str(row.get(cm.get("position",""),"0")).strip() if "position" in cm else "0"
            if not nse or nse=="NAN" or not name or name=="NAN": continue
            try: pos = float(pr.replace(",","").replace("₹","")) if pr and pr!="NAN" else 0.0
            except: pos = 0.0
            out.append({"name":name,"nse_code":nse,"bse_code":bse,"isin":isin,"position_crs":pos})
        return (out, None) if out else (None, "No valid rows found. Check NSE CODE and NAME columns.")
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
    "INE238A01034":"AXISBANK","INE237A01028":"WIPRO","INE018A01030":"LT","INE040H01021":"SUZLON",
}

@st.cache_data(ttl=300, show_spinner=False)
def fetch_stock_quote(raw):
    inp = raw.strip().upper()
    if len(inp)==12 and inp.startswith("IN") and inp[2:].isalnum():
        nse = ISIN_TO_NSE.get(inp)
        sym = (nse+".NS") if nse else None
        if not sym: return {"found":False,"error":f"ISIN {inp} not in map.","symbol":inp}
    elif inp.isdigit():
        nse = BSE_TO_NSE.get(inp)
        sym = (nse+".NS") if nse else (inp+".BO")
    else:
        sym = inp+".NS"
    def _t(s):
        try:
            time.sleep(0.5)
            fi = yf.Ticker(s).fast_info
            p,v = float(fi.last_price), float(fi.previous_close)
            if p<=0 or v<=0: raise ValueError()
            c=p-v; pct=(c/v*100) if v else 0.0
            info = yf.Ticker(s).info
            nm = info.get("longName") or info.get("shortName") or s
            return {"found":True,"name":nm,"symbol":s,"price":p,"change":c,"pct":pct,"error":None}
        except Exception as e:
            return {"found":False,"error":str(e),"symbol":s}
    res = _t(sym)
    if not res["found"] and sym.endswith(".NS"):
        time.sleep(1.0); res = _t(sym.replace(".NS",".BO"))
    return res

def gn(q):
    return f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=en-IN&gl=IN&ceid=IN:en"

@st.cache_data(ttl=300, show_spinner=False)
def fetch_stock_news_gn(nse, name):
    """Max 5 articles, current month only."""
    seen, items = set(), []
    for q in [f"{nse} NSE stock India", f"{name} stock NSE"]:
        try:
            for e in feedparser.parse(gn(q)).entries[:20]:
                t,l,dt = e.get("title","").strip(), e.get("link","#"), parse_dt(e)
                if not t or t in seen or not is_current_month(dt): continue
                seen.add(t)
                items.append({"title":t,"link":l,"dt":dt,"priority":is_priority(t),"sentiment":get_sentiment(t)})
        except: pass
    items.sort(key=lambda x: x["dt"], reverse=True)
    return items[:5]

@st.cache_data(ttl=60, show_spinner=False)
def fetch_market_data():
    res = {}
    for name,(sym,unit) in MARKET_TICKERS.items():
        try:
            fi=yf.Ticker(sym).fast_info; p,v=float(fi.last_price),float(fi.previous_close)
            c=p-v; pct=(c/v*100) if v else 0.0
            res[name]={"price":p,"change":c,"pct":pct,"unit":unit}
        except:
            res[name]={"price":None,"change":None,"pct":None,"unit":unit}
    return res

# ─────────────────────────────────────────────
# FEED SOURCES
# ─────────────────────────────────────────────

FEED_SOURCES = {
    "🇮🇳 India Markets":   [gn("NIFTY OR SENSEX OR NSE OR BSE India stock market"),gn("RBI policy rate India"),gn("India stock market today")],
    "💵 Currency & Forex": [gn("Indian Rupee USD exchange rate"),gn("Dollar index DXY Rupee"),gn("RBI forex intervention currency")],
    "🛢️ Commodities":      [gn("crude oil price Brent WTI"),gn("gold price silver commodity India"),gn("OPEC oil production today")],
    "🌍 Geopolitical Risk":[gn("Iran war sanctions Middle East"),gn("Russia Ukraine war economy"),gn("US sanctions tariff trade war India")],
    "📊 Global Macro":     [gn("Federal Reserve interest rate inflation"),gn("US economy recession GDP"),gn("IMF World Bank global economy")],
    "📰 Economic Times":   [
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
        "https://economictimes.indiatimes.com/news/economy/rssfeeds/4719148.cms",
    ],
    "🗞️ Mint Markets":     [
        "https://www.livemint.com/rss/markets",
        "https://www.livemint.com/rss/economy",
        "https://www.livemint.com/rss/companies",
    ],
}

LIVEWIRE_FEEDS = [
    "https://in.investing.com/rss/news.rss",
    "https://in.investing.com/rss/stock_market_news.rss",
    "https://in.investing.com/rss/commodities_news.rss",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://www.aljazeera.com/xml/rss/middleeast.xml",
    gn("Iran war Israel Middle East breaking news"),
    gn("breaking market news India NSE BSE today"),
]

def fetch_rbi_circulars():
    all_items, seen = [], set()
    for url in ["https://rbi.org.in/notifications_rss.xml","https://rbi.org.in/pressreleases_rss.xml"]:
        try:
            resp=requests.get(url,timeout=12,verify=False,proxies=PROXIES or {})
            for e in feedparser.parse(resp.content).entries[:40]:
                t,l,dt=e.get("title","").strip(),e.get("link","#"),parse_dt(e)
                if not t or t in seen or not is_current_fy(dt): continue
                if any(kw in t.lower() for kw in EXCLUDE_CIRCULAR_KW): continue
                seen.add(t)
                all_items.append({"title":t,"link":l,"dt":dt,"priority":is_priority(t),"sentiment":get_sentiment(t)})
        except: pass
    all_items.sort(key=lambda x: x["dt"], reverse=True)
    return all_items[:50]

def fetch_nse_circulars():
    all_items, seen = [], set()
    for url in ["https://nsearchives.nseindia.com/content/RSS/Circulars.xml",
                "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"]:
        try:
            h={"User-Agent":"Mozilla/5.0","Accept":"application/rss+xml,*/*","Referer":"https://www.nseindia.com/"}
            resp=requests.get(url,headers=h,timeout=15,verify=False,proxies=PROXIES or {})
            for e in feedparser.parse(resp.content).entries[:40]:
                t,l,dt=e.get("title","").strip(),e.get("link","#"),parse_dt(e)
                if not t or t in seen or not is_current_fy(dt): continue
                if any(kw in t.lower() for kw in EXCLUDE_CIRCULAR_KW): continue
                seen.add(t)
                all_items.append({"title":t,"link":l,"dt":dt,"priority":is_priority(t),"sentiment":get_sentiment(t)})
        except: pass
    all_items.sort(key=lambda x: x["dt"], reverse=True)
    return all_items[:50]

def fetch_feed(url):
    try:
        resp=requests.get(url,timeout=10,verify=False,proxies=PROXIES or {})
        items=[]
        for e in feedparser.parse(resp.content).entries[:20]:
            t,l,dt=e.get("title","").strip(),e.get("link","#"),parse_dt(e)
            if not t or not is_recent(dt,days=3): continue
            items.append({"title":t,"link":l,"dt":dt,"priority":is_priority(t),"sentiment":get_sentiment(t)})
        return items
    except: return []

def fetch_all_feeds(fd):
    res=defaultdict(list)
    urls=[(cat,url) for cat,urls in fd.items() for url in urls]
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs={ex.submit(fetch_feed,url):cat for cat,url in urls}
        for fut in as_completed(futs): res[futs[fut]].extend(fut.result())
    for cat in res:
        seen,dd=set(),[]
        for a in sorted(res[cat],key=lambda x:x["dt"],reverse=True):
            if a["title"] not in seen: seen.add(a["title"]); dd.append(a)
        res[cat]=dd
    return dict(res)

@st.cache_data(ttl=60, show_spinner=False)
def fetch_livewire():
    all_items, seen = [], set()
    for url in LIVEWIRE_FEEDS:
        try:
            h={"User-Agent":"Mozilla/5.0","Accept":"application/rss+xml,*/*"}
            resp=requests.get(url,headers=h,timeout=12,verify=False,proxies=PROXIES or {})
            for e in feedparser.parse(resp.content).entries[:15]:
                t,l,dt=e.get("title","").strip(),e.get("link","#"),parse_dt(e)
                if not t or t in seen or not is_recent(dt,days=2): continue
                seen.add(t)
                all_items.append({"title":t,"link":l,"dt":dt,"priority":is_priority(t),"sentiment":get_sentiment(t)})
        except: pass
    all_items.sort(key=lambda x: x["dt"], reverse=True)
    return all_items[:60]

def load_manual():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE) as f: return json.load(f)
        except: return []
    return []

def save_manual(items):
    with open(DB_FILE,"w") as f: json.dump(items,f,default=str)

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
/* BASE FONT */
html,body,[class*="css"],.stMarkdown,.stText,
p,div,span,a,button,input,select,textarea,th,td,label {
    font-family: Arial, Helvetica, sans-serif !important;
    font-size: 11pt !important;
}
html,body,.stApp,[data-testid="stAppViewContainer"] { background-color:#f0f2f5 !important; }
.block-container { padding-top:0.4rem !important; max-width:1440px !important; }

/* FLASH ELIMINATION */
#stDecoration { display:none !important; }
[data-testid="stStatusWidget"] { display:none !important; }
[data-testid="stSkeleton"] { display:none !important; }
.stApp > header { display:none !important; }
[data-testid="toastContainer"] { display:none !important; }
.stApp,[data-testid="stAppViewContainer"],
.main,[data-testid="stAppViewBlockContainer"] { opacity:1 !important; }
[data-stale="true"],[data-stale="false"] { opacity:1 !important; }
.stApp,.stApp *,
[data-testid="stAppViewContainer"],
[data-testid="stAppViewContainer"] * { transition:none !important; animation-duration:0.001s !important; }

/* SIDEBAR */
section[data-testid="stSidebar"] { background:#1a1f2e !important; border-right:1px solid #2d3748; }
section[data-testid="stSidebar"] * { color:#e2e8f0 !important; }
section[data-testid="stSidebar"] input,
section[data-testid="stSidebar"] textarea {
    background:#2d3748 !important; border:1px solid #4a5568 !important;
    color:#e2e8f0 !important; border-radius:6px !important;
}
/* All sidebar buttons — uniform dark style */
section[data-testid="stSidebar"] .stButton > button {
    background:#2d3748 !important; color:#e2e8f0 !important;
    border:1px solid #4a5568 !important; border-radius:6px !important;
    width:100%; transition:none !important; font-size:11pt !important;
    padding:6px 12px !important;
}
section[data-testid="stSidebar"] .stButton > button:hover { background:#4a5568 !important; }
/* Sidebar news cards */
section[data-testid="stSidebar"] .news-card  { background:#ffffff !important; }
section[data-testid="stSidebar"] .card-title { color:#1565c0 !important; }
section[data-testid="stSidebar"] .card-meta  { color:#4a5568 !important; }

/* FILE UPLOADER — completely hidden, only st.button shows */
section[data-testid="stSidebar"] [data-testid="stFileUploader"] {
    height:0 !important; overflow:hidden !important; opacity:0 !important;
    position:absolute !important; pointer-events:none !important;
    margin:0 !important; padding:0 !important;
}

/* TOP HEADER BAR */
.top-bar {
    background:#1a237e; border-radius:8px;
    padding:10px 20px; margin-top:50px; margin-bottom:10px;
    display:flex; justify-content:space-between; align-items:center;
}
.top-bar-title { font-size:13pt !important; font-weight:700; color:#fff; letter-spacing:0.03em; }
.top-bar-right { display:flex; align-items:center; gap:16px; }
.live-dot {
    width:8px; height:8px; background:#4caf50; border-radius:50%;
    display:inline-block; margin-right:6px; animation:pulse 2s infinite;
}
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.25} }
.refresh-timer { font-size:10pt !important; color:#90caf9; }
.top-bar-time  { font-size:10pt !important; color:#b0bec5; }

/* MARKET MONITOR */
.market-monitor { background:#1a237e; border-radius:8px; padding:10px 16px 12px; margin-bottom:12px; }
.mm-label { font-size:10pt !important; font-weight:700; color:#fff; letter-spacing:0.08em; text-transform:uppercase; margin-bottom:8px; }
.ticker-grid { display:flex; flex-wrap:wrap; gap:8px; }
.ticker-card { border-radius:7px; padding:8px 14px; min-width:115px; flex:1; text-align:center; }
.ticker-card.up   { background:#c8e6c9 !important; border:2px solid #2e7d32 !important; }
.ticker-card.up   .t-name,.ticker-card.up   .t-price,.ticker-card.up   .t-up  { color:#000 !important; }
.ticker-card.down { background:#ffcdd2 !important; border:2px solid #c62828 !important; }
.ticker-card.down .t-name,.ticker-card.down .t-price,.ticker-card.down .t-dn  { color:#000 !important; }
.ticker-card.flat { background:#e0e0e0 !important; border:2px solid #555 !important; }
.ticker-card.flat .t-name,.ticker-card.flat .t-price,.ticker-card.flat .t-flat{ color:#000 !important; }
.t-name  { font-size:8.5pt !important; font-weight:700; text-transform:uppercase; letter-spacing:0.04em; }
.t-price { font-size:11pt !important; font-weight:700; margin:3px 0; }
.t-up,.t-dn,.t-flat { font-size:9.5pt !important; font-weight:600; }

/* NEWS CARDS */
.news-card {
    background:#fff; border:1px solid #e2e8f0; border-left:4px solid #cbd5e0;
    border-radius:7px; padding:10px 14px; margin-bottom:6px;
    box-shadow:0 1px 3px rgba(0,0,0,0.06);
}
.news-card:hover { box-shadow:0 3px 8px rgba(0,0,0,0.12); border-left-color:#3949ab; }
.news-card.priority           { border-left-color:#e53935 !important; background:#fff8f8; }
.news-card.sentiment-positive { border-left-color:#43a047; background:#f6fff7; }
.news-card.sentiment-negative { border-left-color:#e53935; background:#fff6f6; }
.news-card.rbi { border-left-color:#f9a825; background:#fffde7; }
.news-card.nse { border-left-color:#1565c0; background:#f0f7ff; }
.news-card.lw  { border-left-color:#6a1b9a; background:#fdf4ff; }
.news-card.aj  { border-left-color:#c62828; background:#fff5f5; }
.card-title {
    font-size:11pt !important; font-weight:600; color:#1a237e !important;
    text-decoration:none !important; line-height:1.45; display:block; margin-bottom:5px;
}
.card-title:hover { color:#3949ab !important; text-decoration:underline !important; }
.card-meta { font-size:9.5pt !important; color:#4a5568 !important; display:flex; gap:8px; align-items:center; flex-wrap:wrap; }

/* BADGES */
.bp  { background:#e53935;color:#fff;  font-size:8.5pt !important;font-weight:700;padding:2px 6px;border-radius:4px; }
.bpo { background:#e8f5e9;color:#2e7d32;font-size:8.5pt !important;font-weight:700;padding:2px 6px;border-radius:4px;border:1px solid #a5d6a7; }
.bne { background:#ffebee;color:#c62828;font-size:8.5pt !important;font-weight:700;padding:2px 6px;border-radius:4px;border:1px solid #ef9a9a; }
.bm  { background:#e3f2fd;color:#1565c0;font-size:8.5pt !important;font-weight:700;padding:2px 6px;border-radius:4px;border:1px solid #90caf9; }
.br  { background:#fff8e1;color:#e65100;font-size:8.5pt !important;font-weight:700;padding:2px 6px;border-radius:4px;border:1px solid #ffcc02; }
.bn  { background:#e3f2fd;color:#1565c0;font-size:8.5pt !important;font-weight:700;padding:2px 6px;border-radius:4px;border:1px solid #64b5f6; }
.bst { background:#ede7f6;color:#4527a0;font-size:8.5pt !important;font-weight:700;padding:2px 6px;border-radius:4px; }
.blw { background:#f3e5f5;color:#6a1b9a;font-size:8.5pt !important;font-weight:700;padding:2px 6px;border-radius:4px;border:1px solid #ce93d8; }
.baj { background:#ffebee;color:#c62828;font-size:8.5pt !important;font-weight:700;padding:2px 6px;border-radius:4px;border:1px solid #ef9a9a; }

/* TABS */
.stTabs [data-baseweb="tab"]   { font-size:10.5pt !important;font-weight:600;color:#4a5568 !important; }
.stTabs [aria-selected="true"] { color:#1a237e !important;border-bottom:2px solid #1a237e; }

/* SECTION HEADERS */
.cat-header { font-size:11pt !important;font-weight:700;color:#1a237e;letter-spacing:0.04em;padding:6px 0 8px;border-bottom:2px solid #e2e8f0;margin-bottom:10px; }
.stock-hdr  { font-size:11pt !important;font-weight:700;color:#4527a0;padding:6px 0 3px;border-bottom:1px solid #e2e8f0;margin:10px 0 7px; }
.sent-bar   { display:flex;gap:16px;font-size:10pt !important;color:#4a5568;margin-bottom:8px;flex-wrap:wrap; }
.pos { color:#2e7d32;font-weight:700; } .neg { color:#c62828;font-weight:700; } .neu { color:#718096;font-weight:600; }

/* STOCK QUOTE CARD */
.sq-card  { background:#2d3748;border:1px solid #4a5568;border-radius:8px;padding:10px 14px;margin-bottom:8px; }
.sq-name  { font-size:10.5pt !important;font-weight:700;color:#e2e8f0 !important; }
.sq-sym   { font-size:9pt !important;color:#90a4ae !important;margin-bottom:4px; }
.sq-price { font-size:13pt !important;font-weight:700;color:#fff !important; }

.pos-badge { display:inline-block;background:#1b5e20;color:#fff;font-size:9pt !important;font-weight:700;padding:2px 8px;border-radius:12px;margin-left:8px; }
[data-testid="metric-container"] { display:none !important; }
hr { border-color:#e2e8f0 !important; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────

if "last_refresh"    not in st.session_state: st.session_state["last_refresh"]    = time.time()
if "portfolio"       not in st.session_state: st.session_state["portfolio"]       = load_portfolio()
if "_login_recorded" not in st.session_state: st.session_state["_login_recorded"] = False
if "show_uploader"   not in st.session_state: st.session_state["show_uploader"]   = False

record_login()

# ─────────────────────────────────────────────
# FETCH DATA
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
    for a in arts: all_articles.append({**a,"category":cat,"manual":False})
for item in manual_items: all_articles.append({**item,"manual":True})
all_articles.sort(key=lambda x: x["dt"] if isinstance(x["dt"],datetime) else datetime.now(timezone.utc), reverse=True)

# ─────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────

now_ist_str = datetime.now(IST).strftime("%d %b %Y  %I:%M %p IST")
elapsed     = int(time.time() - st.session_state.get("last_refresh", time.time()))
remaining   = max(0, AUTO_REFRESH - elapsed)

st.markdown(f"""
<div class="top-bar">
  <div class="top-bar-title"><span class="live-dot"></span>JM FINANCIAL &nbsp;·&nbsp; RISK INTELLIGENCE DASHBOARD</div>
  <div class="top-bar-right">
    <span class="refresh-timer">⏱ Refresh in {remaining}s</span>
    <span class="top-bar-time">{now_ist_str}</span>
  </div>
</div>""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# MARKET MONITOR
# ─────────────────────────────────────────────

mhtml = '<div class="market-monitor"><div class="mm-label">⚡ Live Market Watch &nbsp;·&nbsp; Yahoo Finance &nbsp;·&nbsp; Refreshes every 60s</div><div class="ticker-grid">'
for name, data in market_data.items():
    p,c,pct,unit = data.get("price"),data.get("change"),data.get("pct"),data.get("unit","")
    if p is None:
        mhtml += f'<div class="ticker-card flat"><div class="t-name">{name}</div><div class="t-price">—</div><div class="t-flat">N/A</div></div>'
        continue
    d="up" if c>=0 else "down"; arr="▲" if c>=0 else "▼"; tc="t-up" if c>=0 else "t-dn"
    ps=f"{unit}{p:,.0f}" if name in ("NIFTY 50","SENSEX","BANK NIFTY") else f"{unit}{p:,.2f}"
    mhtml += f'<div class="ticker-card {d}"><div class="t-name">{name}</div><div class="t-price">{ps}</div><div class="{tc}">{arr} {abs(c):,.2f} ({abs(pct):.2f}%)</div></div>'
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
    st.caption("NSE Code · BSE Code · ISIN · Company Name")
    search_input = st.text_input(
        label="__s__", placeholder="e.g. RELIANCE  or  500325  or  INE002A01018",
        label_visibility="collapsed", key="stock_search_input"
    )
    if search_input.strip():
        with st.spinner("Fetching..."):
            res = fetch_stock_quote(search_input.strip())
        if res["found"]:
            arrow="▲" if res["change"]>=0 else "▼"; clr="#4caf50" if res["change"]>=0 else "#f44336"
            st.markdown(f"""<div class="sq-card">
              <div class="sq-name">{res['name']}</div><div class="sq-sym">{res['symbol']}</div>
              <div class="sq-price">₹{res['price']:,.2f}</div>
              <div style="color:{clr};font-size:10pt;font-weight:700;margin-top:2px;">
                {arrow} ₹{abs(res['change']):,.2f} ({abs(res['pct']):.2f}%)</div>
            </div>""", unsafe_allow_html=True)
            st.markdown("**📰 Latest News**")
            sym_c = res["symbol"].replace(".NS","").replace(".BO","")
            news  = fetch_stock_news_gn(sym_c, res["name"])
            for art in (news or [])[:5]:
                st.markdown(f"""<div class="news-card" style="margin-bottom:4px;">
                  <a class="card-title" href="{art['link']}" target="_blank"
                     style="font-size:9.5pt !important;">{art['title']}</a>
                  <div class="card-meta"><span>{time_ago(art['dt'])}</span></div>
                </div>""", unsafe_allow_html=True)
            if not news: st.caption("No news this month.")
        else:
            st.error(f"❌ Not found: `{search_input.strip()}`\n\nTry NSE (`RELIANCE`), BSE (`500325`), ISIN (`INE002A01018`)\n\n_{res.get('error','')[:80]}_")

    st.markdown("---")

    # ── CONTROLS ──
    st.markdown("### ⚙️ Controls")
    auto_refresh = st.toggle("Auto Refresh (90s)", value=True)
    if st.button("🔄 Refresh Now", use_container_width=True):
        st.cache_data.clear(); st.session_state["last_refresh"] = time.time(); st.rerun()

    st.markdown("---")

    # ── ADMIN PANEL ──
    st.markdown("### 🔐 Admin Panel")
    pwd      = st.text_input("Password", type="password", key="admin_pwd")
    is_admin = (pwd == ADMIN_PASSWORD)

    if is_admin:
        st.success("✅ Access granted")
        st.markdown("#### 📂 Portfolio")

        # Status line
        current_port = st.session_state.get("portfolio", load_portfolio())
        total_pos    = sum(p.get("position_crs",0) for p in current_port)
        st.markdown(
            f"<div style='font-size:9.5pt;color:#90caf9;margin:2px 0 8px;'>"
            f"📌 {len(current_port)} stocks loaded"
            + (f" · ₹{total_pos:,.1f} Cr" if total_pos>0 else "")
            + "</div>", unsafe_allow_html=True
        )

        # ── TWO CLEAN DARK BUTTONS side by side ──────────────────────────
        # APPROACH: st.file_uploader is hidden via CSS (height:0, opacity:0).
        # A normal st.button "📂 Import Portfolio" toggles show_uploader flag.
        # When True, the file_uploader appears BELOW the buttons row,
        # completely styled dark via CSS — this is the ONLY approach that
        # guarantees both correct appearance AND correct functionality.
        # ─────────────────────────────────────────────────────────────────
        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("📂 Import", use_container_width=True, key="btn_import_port"):
                st.session_state["show_uploader"] = not st.session_state.get("show_uploader", False)
        with col_b:
            if st.button("🗑 Delete", use_container_width=True, key="btn_del_port"):
                st.session_state["portfolio"]   = DEFAULT_PORTFOLIO
                st.session_state["show_uploader"] = False
                if os.path.exists(PORTFOLIO_FILE): os.remove(PORTFOLIO_FILE)
                st.cache_data.clear(); st.rerun()

        # File uploader — shown only after Import is clicked
        # CSS completely hides Streamlit's default white box rendering
        # but the widget itself is still functional when visible=True in session
        if st.session_state.get("show_uploader", False):
            st.caption("📎 Choose your Excel file (.xlsx):")
            uploaded_excel = st.file_uploader(
                label="portfolio_excel",
                type=["xlsx","xls"],
                key="portfolio_upload",
                label_visibility="collapsed",
            )
            if uploaded_excel is not None:
                with st.spinner("Parsing Excel..."):
                    new_port, err = parse_excel_portfolio(uploaded_excel)
                if new_port:
                    st.session_state["portfolio"]     = new_port
                    st.session_state["show_uploader"] = False
                    save_portfolio(new_port); st.cache_data.clear()
                    st.success(f"✅ {len(new_port)} stocks imported!")
                    st.rerun()
                else:
                    st.error(f"❌ {err}")

        st.markdown("---")

        # ── PUSH HEADLINE ──
        st.markdown("#### 📢 Push Internal Headline")
        new_title = st.text_area("Headline text", height=68, key="adm_title",
                                  placeholder="Enter headline text...")
        new_link  = st.text_input("Source link (optional)", value="#", key="adm_link")
        new_cat   = st.selectbox("Segment", options=[
            "⚠️ Risk Alert","📋 Compliance","🏛️ Internal Memo",
            "🇮🇳 India Markets","💵 Currency & Forex","🛢️ Commodities",
            "🌍 Geopolitical Risk","📊 Global Macro",
            "📰 Economic Times","🗞️ Mint Markets","🌐 Live Wire",
        ], key="adm_cat")
        if st.button("➕ Publish", use_container_width=True):
            if new_title.strip():
                items = load_manual()
                items.insert(0,{"title":new_title.strip(),"link":new_link.strip() or "#",
                    "dt":datetime.now(timezone.utc).isoformat(),"category":new_cat,
                    "manual":True,"priority":is_priority(new_title),"sentiment":get_sentiment(new_title)})
                save_manual(items); st.cache_data.clear(); st.success("✅ Published!"); st.rerun()

        existing = load_manual()
        if existing:
            st.markdown("**🗑 Manage Published**")
            for i, item in enumerate(existing):
                c1,c2=st.columns([5,1])
                c1.markdown(f"<div style='font-size:9pt;color:#cbd5e0;'>{item['title'][:50]}…</div>",unsafe_allow_html=True)
                if c2.button("✕",key=f"del_{i}"):
                    existing.pop(i); save_manual(existing); st.rerun()

        st.markdown("---")

        # ── LOGIN LOG — DOWNLOAD ONLY ──────────────────────────────────
        # No table displayed. Just a count + download button.
        # Admin downloads the CSV privately — no info shown on screen.
        # ──────────────────────────────────────────────────────────────
        st.markdown("#### 🛡️ Access Log")
        logs = load_login_logs()
        log_count = len(logs)
        st.markdown(
            f"<div style='font-size:9.5pt;color:#090E41;margin:2px 0 8px;'>"
            f"📋 {log_count} session{'s' if log_count!=1 else ''} recorded</div>",
            unsafe_allow_html=True
        )
        if logs:
            csv_bytes = logs_to_csv_bytes(logs)
            fname = f"jm_access_log_{datetime.now(IST).strftime('%Y%m%d_%H%M')}.csv"
            st.download_button(
                label="⬇ Download Access Log (CSV)",
                data=csv_bytes,
                file_name=fname,
                mime="text/csv",
                use_container_width=True,
                key="dl_log",
            )
            if st.button("🗑 Clear Log", use_container_width=True, key="clear_log"):
                if os.path.exists(LOGIN_LOG_FILE): os.remove(LOGIN_LOG_FILE)
                st.rerun()
        else:
            st.caption("No sessions recorded yet.")
    elif pwd:
        st.error("❌ Incorrect password")

# ─────────────────────────────────────────────
# RENDERERS
# ─────────────────────────────────────────────

def _cls(ip, sent):
    if ip:               return "news-card priority"
    if sent=="positive": return "news-card sentiment-positive"
    if sent=="negative": return "news-card sentiment-negative"
    return "news-card"

def render_news_cards(articles):
    if not articles:
        st.markdown("<div style='font-size:11pt;color:#718096;padding:16px 0;'>No news found — try Refresh Now.</div>",unsafe_allow_html=True)
        return
    for art in articles:
        ip=art.get("priority",False); im=art.get("manual",False)
        sent=art.get("sentiment","neutral"); cat=art.get("category",""); cls=_cls(ip,sent)
        dt_o=art["dt"]
        if isinstance(dt_o,str):
            try:    dt_o=datetime.fromisoformat(dt_o)
            except: dt_o=datetime.now(timezone.utc)
        ago=time_ago(dt_o)
        pb='<span class="bp">⚡ PRIORITY</span>' if ip else ""
        mb='<span class="bm">📢 INTERNAL</span>' if im else ""
        sb=('<span class="bpo">▲ POSITIVE</span>' if sent=="positive" else
            '<span class="bne">▼ NEGATIVE</span>' if sent=="negative" else "") if not ip and not im else ""
        st.markdown(f'<div class="{cls}"><a class="card-title" href="{art["link"]}" target="_blank">{art["title"]}</a><div class="card-meta"><span>{ago}</span><span>·</span><span>{cat}</span>{pb}{mb}{sb}</div></div>',unsafe_allow_html=True)

def render_circular_cards(articles, badge_cls, card_extra):
    label="RBI" if "rbi" in card_extra else "NSE"
    if not articles:
        st.markdown(f"<div style='font-size:11pt;color:#718096;padding:16px 0;'>No {label} circulars — try Refresh Now.</div>",unsafe_allow_html=True)
        return
    for art in articles:
        ip=art.get("priority",False); sent=art.get("sentiment","neutral")
        cls=f"news-card {card_extra}"+(" priority" if ip else ""); ago=time_ago(art["dt"])
        tb=f'<span class="{badge_cls}">{label} CIRCULAR</span>'
        pb='<span class="bp">⚡ PRIORITY</span>' if ip else ""
        sb=('<span class="bpo">▲ POSITIVE</span>' if sent=="positive" else '<span class="bne">▼ NEGATIVE</span>' if sent=="negative" else "")
        st.markdown(f'<div class="{cls}"><a class="card-title" href="{art["link"]}" target="_blank">{art["title"]}</a><div class="card-meta"><span>{ago}</span>{tb}{sb}{pb}</div></div>',unsafe_allow_html=True)

def render_portfolio(portfolio):
    if not portfolio:
        st.markdown("<div style='font-size:11pt;color:#718096;padding:16px 0;'>No portfolio loaded. Ask admin to import via Admin Panel.</div>",unsafe_allow_html=True)
        return
    now=datetime.now(timezone.utc); month_label=now.strftime("%B %Y")
    tp=sum(p.get("position_crs",0) for p in portfolio)
    st.markdown(f"<div style='font-size:10.5pt;color:#4527a0;margin-bottom:10px;'>Tracking <b>{len(portfolio)} stocks</b>"+(f" &nbsp;·&nbsp; Total: <b>₹{tp:,.1f} Cr</b>" if tp>0 else "")+f" &nbsp;·&nbsp; <span style='font-size:9.5pt;color:#718096;'>Showing {month_label} news only</span></div>",unsafe_allow_html=True)
    for stock in portfolio:
        name=stock.get("name",""); nse=stock.get("nse_code",""); pos=stock.get("position_crs",0)
        pb=f'<span class="pos-badge">₹{pos:,.1f} Cr</span>' if pos and pos>0 else ""
        st.markdown(f'<div class="stock-hdr">📌 {name.upper()}<span style="font-size:9.5pt;color:#718096;font-weight:400;margin-left:8px;">{nse}</span>{pb}</div>',unsafe_allow_html=True)
        with st.spinner(f"Loading {name}..."):
            arts=fetch_stock_news_gn(nse, name)
        if not arts:
            st.markdown(f"<div style='font-size:10pt;color:#718096;padding:4px 0 12px;'>No news for {name} in {month_label}.</div>",unsafe_allow_html=True)
            continue
        pn=sum(1 for a in arts if a.get("sentiment")=="positive")
        nn=sum(1 for a in arts if a.get("sentiment")=="negative")
        un=len(arts)-pn-nn
        st.markdown(f'<div class="sent-bar"><span>Headlines: <b>{len(arts)}</b></span><span>🟢 <span class="pos">{pn} Positive</span></span><span>🔴 <span class="neg">{nn} Negative</span></span><span>⚪ <span class="neu">{un} Neutral</span></span></div>',unsafe_allow_html=True)
        for art in arts:
            ip=art.get("priority",False); sent=art.get("sentiment","neutral"); cls=_cls(ip,sent)
            dt_o=art["dt"]
            if isinstance(dt_o,str):
                try:    dt_o=datetime.fromisoformat(dt_o)
                except: dt_o=datetime.now(timezone.utc)
            ago=time_ago(dt_o)
            pb2='<span class="bp">⚡ PRIORITY</span>' if ip else ""
            sb2=('<span class="bpo">▲ POSITIVE</span>' if sent=="positive" and not ip else
                 '<span class="bne">▼ NEGATIVE</span>' if sent=="negative" and not ip else "")
            st.markdown(f'<div class="{cls}"><a class="card-title" href="{art["link"]}" target="_blank">{art["title"]}</a><div class="card-meta"><span>{ago}</span><span class="bst">{nse}</span>{sb2}{pb2}</div></div>',unsafe_allow_html=True)
        st.markdown("<div style='margin-bottom:8px'></div>",unsafe_allow_html=True)

def render_livewire(articles):
    if not articles:
        st.markdown("<div style='font-size:11pt;color:#718096;padding:16px 0;'>No live wire news — try Refresh Now.</div>",unsafe_allow_html=True)
        return
    for art in articles:
        ip=art.get("priority",False); sent=art.get("sentiment","neutral"); link=art.get("link","#")
        is_aj="aljazeera" in link.lower()
        cls="news-card priority" if ip else ("news-card aj" if is_aj else "news-card lw")
        dt_o=art["dt"]
        if isinstance(dt_o,str):
            try:    dt_o=datetime.fromisoformat(dt_o)
            except: dt_o=datetime.now(timezone.utc)
        ago=time_ago(dt_o)
        pb='<span class="bp">⚡ PRIORITY</span>' if ip else ""
        src='<span class="baj">📡 AL JAZEERA</span>' if is_aj else '<span class="blw">🌐 LIVE</span>'
        sb=('<span class="bpo">▲ POSITIVE</span>' if sent=="positive" else '<span class="bne">▼ NEGATIVE</span>' if sent=="negative" else "")
        st.markdown(f'<div class="{cls}"><a class="card-title" href="{link}" target="_blank">{art["title"]}</a><div class="card-meta"><span>{ago}</span>{src}{sb}{pb}</div></div>',unsafe_allow_html=True)

# ─────────────────────────────────────────────
# TABS
# ─────────────────────────────────────────────

news_cats=list(FEED_SOURCES.keys())
tab_all,tab_prio,tab_port,tab_rbi,tab_nse,tab_lw,*cat_tabs=st.tabs(
    ["📋 All News","🔴 Priority","📂 Portfolio","🏛️ RBI Circulars","🔵 NSE Circulars","🌐 Live Wire"]+news_cats
)

with tab_all:   render_news_cards(all_articles)
with tab_prio:
    pa=[a for a in all_articles if a.get("priority")]
    if pa: st.markdown(f"<div style='font-size:10.5pt;color:#c62828;font-weight:700;margin-bottom:10px;'>⚡ {len(pa)} priority alerts</div>",unsafe_allow_html=True)
    render_news_cards(pa)
with tab_port:  render_portfolio(st.session_state.get("portfolio",load_portfolio()))
with tab_rbi:   render_circular_cards(rbi_circulars,"br","rbi")
with tab_nse:   render_circular_cards(nse_circulars,"bn","nse")
with tab_lw:
    st.markdown("<div style='font-size:10pt;color:#6a1b9a;font-weight:600;margin-bottom:8px;'>🌐 Live Wire &nbsp;·&nbsp; Investing.com India &nbsp;+&nbsp; Al Jazeera Middle East &nbsp;·&nbsp; Refreshes every 60s &nbsp;·&nbsp; Last 48h only</div>",unsafe_allow_html=True)
    render_livewire(livewire_articles)
for tab,cat in zip(cat_tabs,news_cats):
    with tab:
        st.markdown(f'<div class="cat-header">{cat}</div>',unsafe_allow_html=True)
        render_news_cards([{**a,"category":cat,"manual":False} for a in all_data.get(cat,[])])

# ─────────────────────────────────────────────
# AUTO-REFRESH
# ─────────────────────────────────────────────

if auto_refresh:
    elapsed_now=int(time.time()-st.session_state.get("last_refresh",time.time()))
    if elapsed_now>=AUTO_REFRESH:
        st.cache_data.clear(); st.session_state["last_refresh"]=time.time(); time.sleep(0.3); st.rerun()
    else:
        time.sleep(5); st.rerun()
