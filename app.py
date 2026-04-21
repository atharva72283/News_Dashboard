"""
JM Financial | Risk Intelligence Dashboard  v13.0
==================================================
KEY CHANGES FROM v12:
✅ nselib used for stock quote prices (fetch_stock_quote)
✅ nselib used for FII/DII data (capital_market.fii_dii_trade_react)
✅ nselib used for NSE Circulars (capital_market.exchange_circulars)
✅ yfinance kept ONLY for live market monitor tickers (NIFTY/SENSEX/VIX/commodities)
✅ Investing.com feeds removed from Live Wire (irrelevant international news)
✅ Live Wire now: Al Jazeera RSS + Google News India/geopolitical only
✅ RBI Circulars: RSS only (no ASPX, no Google News) — clean and reliable
✅ Deduplication fixed: ALL tabs use local dedup — no cross-tab interference
✅ Live Wire articles merged into All News tab correctly
✅ Live dot animation slowed to 4s

Install:
  pip install streamlit feedparser requests urllib3 beautifulsoup4 lxml
              yfinance pytz pandas openpyxl fpdf2 nselib
Run:
  streamlit run app.py
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
import io
import csv
import re
import urllib.parse
from collections import defaultdict
import yfinance as yf
import pandas as pd

IST = pytz.timezone("Asia/Kolkata")

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
DB_FILE        = "manual_headlines.json"
PORTFOLIO_FILE = "portfolio_data.json"
LOGIN_LOG_FILE = "login_log.json"
ADMIN_PASSWORD = "JM_RISK_2026"
PROXIES        = None

REFRESH_MARKET_HOURS = 30
REFRESH_OFF_HOURS    = 300

# ─────────────────────────────────────────────────────────────
# DEFAULT PORTFOLIO
# ─────────────────────────────────────────────────────────────
DEFAULT_PORTFOLIO = [
    {"name":"Sammaan Capital","nse_code":"SAMMAANCAP","position_crs":0},
    {"name":"Suzlon",         "nse_code":"SUZLON",     "position_crs":0},
    {"name":"Religare",       "nse_code":"RELIGARE",   "position_crs":0},
    {"name":"Valor Estate",   "nse_code":"VALOR",      "position_crs":0},
]

# ─────────────────────────────────────────────────────────────
# MARKET TICKERS — yfinance only (indices + commodities)
# ─────────────────────────────────────────────────────────────
MARKET_TICKERS = {
    "NIFTY 50":    ("^NSEI",     "₹"),
    "SENSEX":      ("^BSESN",    "₹"),
    "BANK NIFTY":  ("^NSEBANK",  "₹"),
    "INDIA VIX":   ("^INDIAVIX", ""),
    "CRUDE (WTI)": ("CL=F",      "$"),
    "BRENT":       ("BZ=F",      "$"),
    "GOLD":        ("GC=F",      "$"),
    "SILVER":      ("SI=F",      "$"),
}

# ─────────────────────────────────────────────────────────────
# KEYWORDS
# ─────────────────────────────────────────────────────────────
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

def get_sentiment(title):
    t = title.lower()
    n = sum(1 for kw in NEGATIVE_KEYWORDS if kw in t)
    p = sum(1 for kw in POSITIVE_KEYWORDS if kw in t)
    if n > p: return "negative"
    if p > n: return "positive"
    return "neutral"

def is_priority(title):
    return any(kw in title.lower() for kw in PRIORITY_KEYWORDS)

# ─────────────────────────────────────────────────────────────
# TIME HELPERS
# ─────────────────────────────────────────────────────────────

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

def age_days(dt):
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days

def is_market_open():
    n = datetime.now(IST)
    if n.weekday() >= 5: return False
    return n.replace(hour=9, minute=15, second=0) <= n <= n.replace(hour=15, minute=30, second=0)

def get_refresh_interval():
    return REFRESH_MARKET_HOURS if is_market_open() else REFRESH_OFF_HOURS

# ─────────────────────────────────────────────────────────────
# LOGIN LOG
# ─────────────────────────────────────────────────────────────

def record_login():
    if st.session_state.get("_login_recorded"): return
    st.session_state["_login_recorded"] = True
    ip, ua = "unknown", "unknown"
    try:
        h  = st.context.headers
        ip = h.get("X-Forwarded-For","").split(",")[0].strip() or h.get("X-Real-Ip","") or "unknown"
        ua = h.get("User-Agent","unknown")[:200]
    except: pass
    entry = {"timestamp": datetime.now(IST).strftime("%d %b %Y %I:%M:%S %p IST"),
             "ip": ip, "user_agent": ua}
    logs = []
    if os.path.exists(LOGIN_LOG_FILE):
        try:
            with open(LOGIN_LOG_FILE) as f: logs = json.load(f)
        except: pass
    logs.insert(0, entry); logs = logs[:500]
    with open(LOGIN_LOG_FILE,"w") as f: json.dump(logs, f, default=str)

def load_login_logs():
    if not os.path.exists(LOGIN_LOG_FILE): return []
    try:
        with open(LOGIN_LOG_FILE) as f: return json.load(f)
    except: return []

def logs_to_csv_bytes(logs):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Timestamp (IST)","IP Address","Browser / Device"])
    for log in logs: w.writerow([log.get("timestamp",""), log.get("ip",""), log.get("user_agent","")])
    return buf.getvalue().encode("utf-8")

# ─────────────────────────────────────────────────────────────
# PORTFOLIO PERSISTENCE
# ─────────────────────────────────────────────────────────────

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
        return (out, None) if out else (None, "No valid rows found.")
    except Exception as e:
        return None, f"Excel error: {e}"

# ─────────────────────────────────────────────────────────────
# NSELIB HELPER — safe import with fallback flag
# ─────────────────────────────────────────────────────────────

def _nselib_available():
    try:
        import nselib  # noqa
        return True
    except ImportError:
        return False

NSELIB_OK = _nselib_available()

# ─────────────────────────────────────────────────────────────
# STOCK QUOTE — nselib primary, yfinance fallback
# ─────────────────────────────────────────────────────────────

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

@st.cache_data(ttl=120, show_spinner=False)
def fetch_stock_quote(raw: str) -> dict:
    """
    Primary: nselib equity.equity_history() for NSE live price.
    nselib hits NSE's own API — no rate limiting issues like Yahoo Finance.
    Fallback: yfinance download (daily OHLCV, no .info call).
    TTL=120s — fresh enough for risk monitoring without hammering APIs.
    """
    inp = raw.strip().upper()

    # Resolve to NSE symbol
    if len(inp)==12 and inp.startswith("IN") and inp[2:].isalnum():
        nse_sym = ISIN_TO_NSE.get(inp)
        if not nse_sym:
            return {"found":False,"error":f"ISIN {inp} not in local map. Try NSE code.","symbol":inp}
    elif inp.isdigit():
        nse_sym = BSE_TO_NSE.get(inp, inp)
    else:
        nse_sym = inp

    yahoo_sym = nse_sym + ".NS"

    # ── PRIMARY: nselib ──────────────────────────────────────────
    if NSELIB_OK:
        try:
            from nselib import capital_market
            # get_quote returns a dict with lastPrice, change, pChange, companyName
            quote = capital_market.market_watch_all_indices()  # not right — use below
        except: pass

        try:
            from nselib import capital_market
            # equity_history gives OHLCV for a date range — use last 2 trading days
            from datetime import date, timedelta as td
            today = date.today().strftime("%d-%m-%Y")
            week_ago = (date.today() - td(days=7)).strftime("%d-%m-%Y")
            df = capital_market.price_volume_and_deliverable_position_data(
                symbol=nse_sym, from_date=week_ago, to_date=today
            )
            if df is not None and not df.empty:
                df = df.sort_values("Date") if "Date" in df.columns else df
                price = float(df["ClosePrice"].iloc[-1]) if "ClosePrice" in df.columns else float(df.iloc[-1,-1])
                prev  = float(df["ClosePrice"].iloc[-2]) if "ClosePrice" in df.columns and len(df)>1 else price
                c = price - prev; pct = (c/prev*100) if prev else 0.0
                nm = nse_sym
                # Try to get company name
                try:
                    nm_col = [c for c in df.columns if "symbol" in c.lower() or "name" in c.lower()]
                    if nm_col: nm = str(df[nm_col[0]].iloc[-1])
                except: nm = nse_sym
                return {"found":True,"name":nm,"symbol":nse_sym,"price":price,"change":c,"pct":pct,"error":None,"source":"NSE"}
        except: pass

    # ── FALLBACK: yfinance download (no .info — no rate limit) ──
    def _yf_fetch(sym):
        try:
            df = yf.download(sym, period="5d", interval="1d", progress=False, auto_adjust=True)
            if df is None or df.empty: return None
            if hasattr(df.columns, "levels"): df.columns = df.columns.droplevel(1)
            close = df["Close"].dropna()
            if len(close) < 2: return None
            price = float(close.iloc[-1]); prev = float(close.iloc[-2])
            if price<=0 or prev<=0: return None
            c=price-prev; pct=(c/prev*100)
            try: nm = getattr(yf.Ticker(sym).fast_info,"name",None) or sym.replace(".NS","")
            except: nm = sym.replace(".NS","").replace(".BO","")
            return {"found":True,"name":nm,"symbol":sym,"price":price,"change":c,"pct":pct,"error":None,"source":"Yahoo"}
        except: return None

    res = _yf_fetch(yahoo_sym)
    if not res: res = _yf_fetch(nse_sym+".BO")
    if not res: return {"found":False,"error":"Could not fetch quote. Try again.","symbol":nse_sym}
    return res

# ─────────────────────────────────────────────────────────────
# GOOGLE NEWS HELPER
# ─────────────────────────────────────────────────────────────

def gn(q):
    return f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=en-IN&gl=IN&ceid=IN:en"

# ─────────────────────────────────────────────────────────────
# STOCK NEWS (Google News RSS)
# ─────────────────────────────────────────────────────────────

@st.cache_data(ttl=300, show_spinner=False)
def fetch_stock_news_gn(nse, name):
    seen, items = set(), []
    for q in [f"{nse} NSE stock India", f"{name} stock NSE"]:
        try:
            for e in feedparser.parse(gn(q)).entries[:20]:
                t,l,dt = e.get("title","").strip(), e.get("link","#"), parse_dt(e)
                if not t or t in seen or not is_recent(dt, days=30): continue
                seen.add(t)
                items.append({"title":t,"link":l,"dt":dt,"priority":is_priority(t),"sentiment":get_sentiment(t)})
        except: pass
    items.sort(key=lambda x: x["dt"], reverse=True)
    return items[:5]

# ─────────────────────────────────────────────────────────────
# LIVE MARKET DATA — yfinance only (NIFTY/VIX/commodities)
# ─────────────────────────────────────────────────────────────

@st.cache_data(ttl=60, show_spinner=False)
def fetch_market_data():
    """
    Uses yfinance 1-minute data for real intraday prices.
    Flattens MultiIndex columns (yfinance 0.2.x issue).
    Falls back to fast_info if download fails.
    """
    res = {}
    for name, (sym, unit) in MARKET_TICKERS.items():
        price, prev, ok = None, None, False
        try:
            df = yf.download(sym, period="5d", interval="1m", progress=False, auto_adjust=True)
            if df is not None and not df.empty:
                if hasattr(df.columns, "levels"):
                    df.columns = df.columns.droplevel(1)
                if "Close" in df.columns:
                    series = df["Close"].dropna()
                    if len(series) >= 2:
                        price = float(series.iloc[-1])
                        today = datetime.now(IST).strftime("%Y-%m-%d")
                        prev_s = series[series.index.strftime("%Y-%m-%d") < today]
                        prev   = float(prev_s.iloc[-1]) if not prev_s.empty else float(series.iloc[-2])
                        ok = True
        except: pass
        if not ok:
            try:
                fi = yf.Ticker(sym).fast_info
                price = float(fi.last_price); prev = float(fi.previous_close)
                if price>0 and prev>0: ok=True
            except: pass
        if ok and price and prev and prev>0:
            c=price-prev; pct=(c/prev)*100
            res[name] = {"price":price,"change":c,"pct":pct,"unit":unit}
        else:
            res[name] = {"price":None,"change":None,"pct":None,"unit":unit}
    return res

# ─────────────────────────────────────────────────────────────
# FII / DII — nselib primary, NSE JSON API fallback, Google News last
# ─────────────────────────────────────────────────────────────

@st.cache_data(ttl=1800, show_spinner=False)
def fetch_fii_flow():
    """
    Primary: nselib capital_market.fii_dii_trade_react()
    nselib wraps NSE's internal JSON API correctly with proper session handling.
    Fallback 1: Direct NSE JSON API with session cookie.
    Fallback 2: Google News headline.
    """
    result = {"fii_net":None,"dii_net":None,"headline":None,
              "dt":None,"data_date":None,"source":None}

    # ── PRIMARY: nselib ──────────────────────────────────────
    if NSELIB_OK:
        try:
            from nselib import capital_market
            df = capital_market.fii_dii_trade_react()
            if df is not None and not df.empty:
                row = df.iloc[0]
                # Column names vary across nselib versions — try all
                def _get(cols):
                    for c in cols:
                        for col in df.columns:
                            if c.lower() in col.lower():
                                try: return float(str(row[col]).replace(",",""))
                                except: pass
                    return None
                date_v = str(row.get("date", row.get("Date", row.get("TRADE_DATE","")))).strip()[:12]
                fii_v  = _get(["fiiNet","fii_net","FII_NET","fiiNetTrade","FII"])
                dii_v  = _get(["diiNet","dii_net","DII_NET","diiNetTrade","DII"])
                if fii_v is not None:
                    fp = "+" if fii_v>=0 else ""; dp = "+" if (dii_v or 0)>=0 else ""
                    result.update({
                        "fii_net":fii_v,"dii_net":dii_v,"data_date":date_v,
                        "dt":datetime.now(timezone.utc),"source":"NSE (nselib)",
                        "headline":f"FII: {fp}{fii_v:,.2f} Cr  |  DII: {dp}{(dii_v or 0):,.2f} Cr  (as of {date_v})"
                    })
                    return result
        except: pass

    # ── FALLBACK 1: Direct NSE JSON API with session ──────────
    try:
        session = requests.Session()
        bh = {
            "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language":"en-US,en;q=0.9",
        }
        session.get("https://www.nseindia.com",
                    headers={**bh,"Accept":"text/html,*/*;q=0.9"},
                    timeout=10, verify=False)
        resp = session.get(
            "https://www.nseindia.com/api/fiidiiTradeReact",
            headers={**bh,"Accept":"application/json","Referer":"https://www.nseindia.com/reports/fii-dii"},
            timeout=12, verify=False, proxies=PROXIES or {}
        )
        data = resp.json()
        if isinstance(data, list) and data:
            row = data[0]
            date_v = str(row.get("date") or row.get("Date") or row.get("TRADE_DATE",""))[:12]
            def _v(keys):
                for k in keys:
                    v = row.get(k)
                    if v is None and "cashMarket" in row:
                        v = row["cashMarket"].get(k)
                    if v is not None:
                        try: return float(str(v).replace(",",""))
                        except: pass
                return None
            fii_v = _v(["fiiNetTrade","FII_NET_TRADE","fii_net_trade","FII"])
            dii_v = _v(["diiNetTrade","DII_NET_TRADE","dii_net_trade","DII"])
            if fii_v is not None:
                fp = "+" if fii_v>=0 else ""; dp = "+" if (dii_v or 0)>=0 else ""
                result.update({
                    "fii_net":fii_v,"dii_net":dii_v,"data_date":date_v,
                    "dt":datetime.now(timezone.utc),"source":"NSE API",
                    "headline":f"FII: {fp}{fii_v:,.2f} Cr  |  DII: {dp}{(dii_v or 0):,.2f} Cr  (as of {date_v})"
                })
                return result
    except: pass

    # ── FALLBACK 2: Google News ────────────────────────────────
    try:
        for q in ["FII DII net NSE India provisional crore today",
                  "FII buying selling India crore today NSE"]:
            feed = feedparser.parse(gn(q))
            for e in feed.entries[:8]:
                t = e.get("title","").strip(); dt = parse_dt(e)
                tl = t.lower()
                if ("fii" in tl or "fpi" in tl) and "dii" in tl and is_recent(dt, days=1):
                    nums = re.findall(r"[\-\+]?[\d,]+(?:\.\d+)?", t)
                    valid = []
                    for n in nums[:6]:
                        try: valid.append(float(n.replace(",","")))
                        except: pass
                    result.update({
                        "headline":t,"dt":dt,"source":"Google News",
                        "data_date":to_ist(dt).strftime("%d %b %Y"),
                        "fii_net":valid[0] if len(valid)>0 else None,
                        "dii_net":valid[1] if len(valid)>1 else None,
                    })
                    return result
    except: pass

    return result

# ─────────────────────────────────────────────────────────────
# FEED SOURCES — news tabs
# ─────────────────────────────────────────────────────────────

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
    "🛢️ Commodities": [
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

# Live Wire — Al Jazeera + Google News India/geopolitical
# Investing.com REMOVED (only international news, not relevant)
LIVEWIRE_FEEDS = [
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://www.aljazeera.com/xml/rss/middleeast.xml",
    gn("Iran war Israel Middle East breaking news today"),
    gn("India breaking news market NSE BSE today"),
    gn("RBI SEBI NSE news India today"),
]

# ─────────────────────────────────────────────────────────────
# RBI CIRCULARS — RSS only (clean and reliable)
# ─────────────────────────────────────────────────────────────

def fetch_rbi_circulars():
    """RBI official RSS feeds only. No ASPX scraping, no Google News."""
    all_items, seen = [], set()
    for url in ["https://rbi.org.in/notifications_rss.xml",
                "https://rbi.org.in/pressreleases_rss.xml"]:
        try:
            resp = requests.get(url, timeout=12, verify=False, proxies=PROXIES or {})
            for e in feedparser.parse(resp.content).entries[:40]:
                t,l,dt = e.get("title","").strip(), e.get("link","#"), parse_dt(e)
                if not t or t in seen or age_days(dt) > 90: continue
                if any(kw in t.lower() for kw in EXCLUDE_CIRCULAR_KW): continue
                seen.add(t)
                all_items.append({"title":t,"link":l,"dt":dt,
                                   "priority":is_priority(t),"sentiment":get_sentiment(t),
                                   "source":"RBI RSS"})
        except: pass
    all_items.sort(key=lambda x: x["dt"], reverse=True)
    return all_items[:60]

# ─────────────────────────────────────────────────────────────
# NSE CIRCULARS — nselib primary, RSS fallback
# ─────────────────────────────────────────────────────────────

def fetch_nse_circulars():
    """
    Primary: nselib capital_market.exchange_circulars(period='1M')
    Returns a DataFrame with circular details directly from NSE API.
    Fallback: NSE RSS archives.
    """
    all_items, seen = [], set()

    # ── PRIMARY: nselib ──────────────────────────────────────
    if NSELIB_OK:
        try:
            from nselib import capital_market
            df = capital_market.exchange_circulars(period="1M")
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    # Try to get title/subject from common column names
                    t = str(row.get("subject") or row.get("Subject") or
                            row.get("circularSubject") or row.get("heading") or
                            row.get("title") or row.get("Title") or "").strip()
                    l = str(row.get("link") or row.get("Link") or
                            row.get("url") or row.get("circularUrl") or "#").strip()
                    # Parse date
                    date_raw = str(row.get("date") or row.get("Date") or
                                   row.get("circularDate") or "").strip()
                    try:
                        dt = datetime.strptime(date_raw[:10], "%d-%m-%Y").replace(tzinfo=timezone.utc)
                    except:
                        try: dt = datetime.strptime(date_raw[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        except: dt = datetime.now(timezone.utc)
                    if not t or t in seen or age_days(dt) > 90: continue
                    if any(kw in t.lower() for kw in EXCLUDE_CIRCULAR_KW): continue
                    seen.add(t)
                    all_items.append({"title":t,"link":l,"dt":dt,
                                       "priority":is_priority(t),"sentiment":get_sentiment(t),
                                       "source":"NSE (nselib)"})
        except: pass

    # ── FALLBACK: NSE RSS archives ────────────────────────────
    for url in ["https://nsearchives.nseindia.com/content/RSS/Circulars.xml",
                "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"]:
        try:
            h = {"User-Agent":"Mozilla/5.0","Accept":"application/rss+xml,*/*",
                 "Referer":"https://www.nseindia.com/"}
            resp = requests.get(url, headers=h, timeout=15, verify=False, proxies=PROXIES or {})
            for e in feedparser.parse(resp.content).entries[:40]:
                t,l,dt = e.get("title","").strip(), e.get("link","#"), parse_dt(e)
                if not t or t in seen or age_days(dt) > 90: continue
                if any(kw in t.lower() for kw in EXCLUDE_CIRCULAR_KW): continue
                seen.add(t)
                all_items.append({"title":t,"link":l,"dt":dt,
                                   "priority":is_priority(t),"sentiment":get_sentiment(t),
                                   "source":"NSE RSS"})
        except: pass

    all_items.sort(key=lambda x: x["dt"], reverse=True)
    return all_items[:60]

# ─────────────────────────────────────────────────────────────
# SEBI CIRCULARS
# ─────────────────────────────────────────────────────────────

def fetch_sebi_circulars():
    all_items, seen = [], set()
    try:
        resp = requests.get("https://www.sebi.gov.in/sebi_data/rss.xml",
                            timeout=12, verify=False, proxies=PROXIES or {})
        for e in feedparser.parse(resp.content).entries[:40]:
            t,l,dt = e.get("title","").strip(), e.get("link","#"), parse_dt(e)
            if not t or t in seen or age_days(dt) > 90: continue
            if any(kw in t.lower() for kw in EXCLUDE_CIRCULAR_KW): continue
            seen.add(t)
            all_items.append({"title":t,"link":l,"dt":dt,"priority":is_priority(t),
                               "sentiment":get_sentiment(t),"source":"SEBI RSS"})
    except: pass
    for q in ["SEBI circular notification India 2025 2026",
              "SEBI interim order penalty suspension India"]:
        try:
            for e in feedparser.parse(gn(q)).entries[:10]:
                t,l,dt = e.get("title","").strip(), e.get("link","#"), parse_dt(e)
                if not t or t in seen: continue
                seen.add(t)
                enf = any(kw in t.lower() for kw in ["interim order","suspension","penalty","ban","enforcement"])
                all_items.append({"title":t,"link":l,"dt":dt,"priority":is_priority(t) or enf,
                                   "sentiment":"negative" if enf else get_sentiment(t),
                                   "source":"SEBI Enforcement" if enf else "Google News"})
        except: pass
    all_items.sort(key=lambda x: x["dt"], reverse=True)
    return all_items[:60]

# ─────────────────────────────────────────────────────────────
# GENERAL FEED FETCH
# ─────────────────────────────────────────────────────────────

def fetch_feed(url):
    try:
        resp = requests.get(url, timeout=10, verify=False, proxies=PROXIES or {})
        items = []
        for e in feedparser.parse(resp.content).entries[:20]:
            t,l,dt = e.get("title","").strip(), e.get("link","#"), parse_dt(e)
            if not t or not is_recent(dt, days=7): continue
            items.append({"title":t,"link":l,"dt":dt,"priority":is_priority(t),"sentiment":get_sentiment(t)})
        return items
    except: return []

def fetch_all_feeds(fd):
    res = defaultdict(list)
    urls = [(cat,url) for cat,urls in fd.items() for url in urls]
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(fetch_feed,url): cat for cat,url in urls}
        for fut in as_completed(futs): res[futs[fut]].extend(fut.result())
    for cat in res:
        seen, dd = set(), []
        for a in sorted(res[cat], key=lambda x: x["dt"], reverse=True):
            if a["title"] not in seen: seen.add(a["title"]); dd.append(a)
        res[cat] = dd
    return dict(res)

@st.cache_data(ttl=60, show_spinner=False)
def fetch_livewire():
    """
    Al Jazeera + targeted Google News for India/geopolitical breaking news.
    Investing.com removed — was only showing international commodity/FX news.
    Refreshes every 60s.
    """
    all_items, seen = [], set()
    for url in LIVEWIRE_FEEDS:
        try:
            h = {"User-Agent":"Mozilla/5.0","Accept":"application/rss+xml,*/*"}
            resp = requests.get(url, headers=h, timeout=12, verify=False, proxies=PROXIES or {})
            for e in feedparser.parse(resp.content).entries[:20]:
                t,l,dt = e.get("title","").strip(), e.get("link","#"), parse_dt(e)
                if not t or t in seen or not is_recent(dt, days=2): continue
                seen.add(t)
                all_items.append({"title":t,"link":l,"dt":dt,
                                   "priority":is_priority(t),"sentiment":get_sentiment(t)})
        except: pass
    all_items.sort(key=lambda x: x["dt"], reverse=True)
    return all_items[:60]

# ─────────────────────────────────────────────────────────────
# MANUAL HEADLINES
# ─────────────────────────────────────────────────────────────

def load_manual():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE) as f: return json.load(f)
        except: return []
    return []

def save_manual(items):
    with open(DB_FILE,"w") as f: json.dump(items, f, default=str)

# ─────────────────────────────────────────────────────────────
# RISK SCORE
# ─────────────────────────────────────────────────────────────

def calc_risk_score(articles, position_crs, total_crs):
    if total_crs<=0 or position_crs<=0: return 0.0
    neg = sum(1 for a in articles if a.get("sentiment")=="negative")
    pos = sum(1 for a in articles if a.get("sentiment")=="positive")
    return max(0.0, min(100.0, (neg-pos)*(position_crs/total_crs)*100))

# ─────────────────────────────────────────────────────────────
# PDF EXPORT
# ─────────────────────────────────────────────────────────────

def _safe(text, limit=200):
    return str(text)[:limit].encode("latin-1", errors="replace").decode("latin-1")

def generate_briefing_pdf(all_articles, rbi_items, nse_items, sebi_items, portfolio, market_data):
    try:
        from fpdf import FPDF
    except ImportError:
        return b""
    now_ist = datetime.now(IST).strftime("%d %B %Y  %I:%M %p IST")
    pdf = FPDF(); pdf.add_page(); pdf.set_auto_page_break(auto=True, margin=14)
    pdf.set_fill_color(26,35,126); pdf.rect(0,0,210,22,"F")
    pdf.set_text_color(255,255,255); pdf.set_font("Helvetica","B",13)
    pdf.set_xy(10,7); pdf.cell(130,8,"JM FINANCIAL  |  RISK INTELLIGENCE BRIEFING")
    pdf.set_font("Helvetica","",8); pdf.set_xy(140,9); pdf.cell(60,6,_safe(now_ist),align="R")
    pdf.set_text_color(0,0,0); pdf.ln(16)
    def section(title, fr, fg, fb):
        pdf.set_font("Helvetica","B",10); pdf.set_fill_color(fr,fg,fb)
        pdf.cell(190,7,"  "+title,fill=True); pdf.ln(8); pdf.set_font("Helvetica","",9)
    section("MARKET SNAPSHOT",225,230,255)
    for name,data in market_data.items():
        p=data.get("price"); c=data.get("change") or 0; pct=data.get("pct") or 0; unit=data.get("unit","")
        if p is None: continue
        arrow="+" if c>=0 else "-"
        ps=f"{unit}{p:,.0f}" if name in ("NIFTY 50","SENSEX","BANK NIFTY") else f"{unit}{p:,.2f}"
        pdf.set_text_color(0,120,0) if c>=0 else pdf.set_text_color(180,0,0)
        pdf.cell(47,5,_safe(f"{name}: {ps}  ({arrow}{abs(pct):.2f}%)"))
        if pdf.get_x()>160: pdf.ln(6)
    pdf.set_text_color(0,0,0); pdf.ln(8)
    section("PRIORITY ALERTS  (last 24 hours)",255,230,230)
    prio=[a for a in all_articles if a.get("priority") and is_recent(a.get("dt",datetime.now(timezone.utc)),days=1)][:10]
    if prio:
        for a in prio:
            pdf.set_text_color(160,0,0); pdf.cell(6,5,">"); pdf.set_text_color(0,0,0)
            pdf.multi_cell(184,5,_safe(a.get("title",""),120))
    else:
        pdf.set_text_color(130,130,130); pdf.cell(190,5,"No priority alerts in the last 24 hours."); pdf.ln(6)
    pdf.set_text_color(0,0,0); pdf.ln(3)
    section("NEW CIRCULARS  (RBI / NSE / SEBI  -  last 24 hours)",255,252,220)
    circs=[(a,"RBI") for a in rbi_items]+[(a,"NSE") for a in nse_items]+[(a,"SEBI") for a in sebi_items]
    recent=[x for x in circs if is_recent(x[0].get("dt",datetime.now(timezone.utc)),days=1)][:12]
    if recent:
        for a,src in recent:
            pdf.set_font("Helvetica","B",9); pdf.set_text_color(180,60,0); pdf.cell(14,5,f"[{src}]")
            pdf.set_font("Helvetica","",9); pdf.set_text_color(0,0,0); pdf.multi_cell(176,5,_safe(a.get("title",""),110))
    else:
        pdf.set_text_color(130,130,130); pdf.cell(190,5,"No new circulars in the last 24 hours."); pdf.ln(6)
    pdf.set_text_color(0,0,0); pdf.ln(3)
    section("PORTFOLIO RISK SUMMARY",230,245,230)
    tp=sum(p.get("position_crs",0) for p in portfolio)
    pdf.set_font("Helvetica","B",9); pdf.cell(190,5,f"Total Exposure: INR {tp:,.1f} Cr  |  Stocks: {len(portfolio)}"); pdf.ln(7)
    pdf.set_font("Helvetica","",9)
    for stock in sorted(portfolio, key=lambda x: x.get("position_crs",0), reverse=True)[:20]:
        nm=_safe(stock.get("name",""),30); nse=_safe(stock.get("nse_code","")); pos=stock.get("position_crs",0)
        pct_p=(pos/tp*100) if tp>0 else 0
        pdf.cell(65,5,nm); pdf.cell(25,5,nse); pdf.cell(40,5,f"INR {pos:,.1f} Cr")
        pdf.set_text_color(100,100,100); pdf.cell(40,5,f"({pct_p:.1f}%)"); pdf.ln(5); pdf.set_text_color(0,0,0)
    pdf.set_y(-12); pdf.set_font("Helvetica","I",7); pdf.set_text_color(150,150,150)
    pdf.cell(190,4,_safe(f"JM Financial Risk Intelligence Dashboard  |  {now_ist}  |  CONFIDENTIAL"),align="C")
    raw = pdf.output()
    return bytes(raw) if isinstance(raw,(bytes,bytearray)) else raw.encode("latin-1")

# ─────────────────────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="JM Financial | Risk Intelligence",
    page_icon="📊", layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────────────────────

st.markdown("""
<style>
html,body,[class*="css"],.stMarkdown,.stText,
p,div,span,a,button,input,select,textarea,th,td,label {
    font-family: Arial, Helvetica, sans-serif !important;
    font-size: 11pt !important;
}
html,body,.stApp,[data-testid="stAppViewContainer"] { background-color:#f0f2f5 !important; }
.block-container { padding-top:0.4rem !important; max-width:1440px !important; }

/* FLASH ELIMINATION */
#stDecoration,[data-testid="stStatusWidget"],[data-testid="stSkeleton"],
.stApp > header,[data-testid="toastContainer"] { display:none !important; }
.stApp,[data-testid="stAppViewContainer"],.main,
[data-testid="stAppViewBlockContainer"] { opacity:1 !important; }
[data-stale="true"],[data-stale="false"] { opacity:1 !important; }
.stApp,.stApp *,[data-testid="stAppViewContainer"],
[data-testid="stAppViewContainer"] * { transition:none !important; animation-duration:0.001s !important; }
/* Exception: live dot keeps its own animation */
.live-dot { animation:pulse 4s infinite !important; }

/* SIDEBAR */
section[data-testid="stSidebar"] { background:#1a1f2e !important; border-right:1px solid #2d3748; }
section[data-testid="stSidebar"] * { color:#e2e8f0 !important; }
section[data-testid="stSidebar"] input,section[data-testid="stSidebar"] textarea {
    background:#2d3748 !important; border:1px solid #4a5568 !important;
    color:#e2e8f0 !important; border-radius:6px !important;
}
section[data-testid="stSidebar"] .stButton > button {
    background:#2d3748 !important; color:#e2e8f0 !important;
    border:1px solid #4a5568 !important; border-radius:6px !important;
    width:100%; font-size:11pt !important; padding:6px 12px !important;
}
section[data-testid="stSidebar"] .stButton > button:hover { background:#4a5568 !important; }
section[data-testid="stSidebar"] .news-card  { background:#ffffff !important; }
section[data-testid="stSidebar"] .card-title { color:#1565c0 !important; }
section[data-testid="stSidebar"] .card-meta  { color:#4a5568 !important; }
/* File uploader — styled dark, visible and functional */
section[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] {
    background:#2d3748 !important; border:1px dashed #4a5568 !important; border-radius:6px !important;
}
section[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] p,
section[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] span,
section[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] small { color:#90caf9 !important; }
section[data-testid="stSidebar"] [data-testid="stFileUploaderDropzone"] button {
    background:#3d4f63 !important; color:#e2e8f0 !important;
    border:1px solid #4a5568 !important; border-radius:5px !important;
}

/* HEADER */
.top-bar {
    background:#1a237e; border-radius:8px; padding:10px 20px;
    margin-top:50px; margin-bottom:4px;
    display:flex; justify-content:space-between; align-items:center;
}
.top-bar-title { font-size:13pt !important; font-weight:700; color:#fff; letter-spacing:0.03em; }
.top-bar-right { display:flex; align-items:center; gap:16px; }
.live-dot {
    width:8px; height:8px; background:#4caf50; border-radius:50%;
    display:inline-block; margin-right:6px;
}
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.25} }
.refresh-timer { font-size:10pt !important; color:#90caf9; }
.top-bar-time  { font-size:10pt !important; color:#b0bec5; }
.mkt-open  { background:#1b5e20;color:#fff;font-size:8.5pt !important;font-weight:700;padding:2px 8px;border-radius:10px; }
.mkt-close { background:#424242;color:#ccc;font-size:8.5pt !important;font-weight:600;padding:2px 8px;border-radius:10px; }

/* VIX BANNER */
.vix-banner {
    background:#b71c1c; color:#fff; border-radius:6px;
    padding:6px 16px; margin-bottom:8px; font-size:10.5pt !important;
    font-weight:700; text-align:center; letter-spacing:0.04em;
}

/* FII BAR */
.fii-bar {
    background:#0d47a1; border-radius:6px; padding:6px 16px;
    margin-bottom:10px; display:flex; gap:24px; align-items:center; flex-wrap:wrap;
    font-size:9.5pt !important;
}
.fii-bar span { color:#b3c5e8 !important; }
.fii-pos { color:#80e27e !important; font-weight:700; }
.fii-neg { color:#ff8a80 !important; font-weight:700; }
.fii-neu { color:#b3c5e8 !important; font-weight:600; }

/* MARKET MONITOR */
.market-monitor { background:#1a237e; border-radius:8px; padding:10px 16px 12px; margin-bottom:8px; }
.mm-label { font-size:10pt !important; font-weight:700; color:#fff; letter-spacing:0.08em; text-transform:uppercase; margin-bottom:8px; }
.ticker-grid { display:flex; flex-wrap:wrap; gap:8px; }
.ticker-card { border-radius:7px; padding:8px 14px; min-width:105px; flex:1; text-align:center; }
.ticker-card.up   { background:#c8e6c9 !important; border:2px solid #2e7d32 !important; }
.ticker-card.up   .t-name,.ticker-card.up   .t-price,.ticker-card.up   .t-up  { color:#000 !important; }
.ticker-card.down { background:#ffcdd2 !important; border:2px solid #c62828 !important; }
.ticker-card.down .t-name,.ticker-card.down .t-price,.ticker-card.down .t-dn  { color:#000 !important; }
.ticker-card.flat { background:#e0e0e0 !important; border:2px solid #555 !important; }
.ticker-card.flat .t-name,.ticker-card.flat .t-price,.ticker-card.flat .t-flat { color:#000 !important; }
.ticker-card.vix-hi { background:#ffcdd2 !important; border:3px solid #b71c1c !important; }
.ticker-card.vix-hi .t-name,.ticker-card.vix-hi .t-price,.ticker-card.vix-hi .t-vix { color:#b71c1c !important; }
.ticker-card.vix-ok { background:#e8f5e9 !important; border:2px solid #388e3c !important; }
.ticker-card.vix-ok .t-name,.ticker-card.vix-ok .t-price,.ticker-card.vix-ok .t-vix { color:#1b5e20 !important; }
.t-name  { font-size:8.5pt !important; font-weight:700; text-transform:uppercase; letter-spacing:0.04em; }
.t-price { font-size:11pt !important;  font-weight:700; margin:3px 0; }
.t-up,.t-dn,.t-flat,.t-vix { font-size:9.5pt !important; font-weight:600; }

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
.news-card.rbi  { border-left-color:#f9a825; background:#fffde7; }
.news-card.nse  { border-left-color:#1565c0; background:#f0f7ff; }
.news-card.sebi { border-left-color:#6a0dad; background:#faf0ff; }
.news-card.lw   { border-left-color:#6a1b9a; background:#fdf4ff; }
.news-card.aj   { border-left-color:#c62828; background:#fff5f5; }
.card-title {
    font-size:11pt !important; font-weight:600; color:#1a237e !important;
    text-decoration:none !important; line-height:1.45; display:block; margin-bottom:5px;
}
.card-title:hover { color:#3949ab !important; text-decoration:underline !important; }
.card-meta { font-size:9.5pt !important; color:#4a5568 !important; display:flex; gap:8px; align-items:center; flex-wrap:wrap; }

/* BADGES */
.bp   { background:#e53935;color:#fff;  font-size:8.5pt !important;font-weight:700;padding:2px 6px;border-radius:4px; }
.bpo  { background:#e8f5e9;color:#2e7d32;font-size:8.5pt !important;font-weight:700;padding:2px 6px;border-radius:4px;border:1px solid #a5d6a7; }
.bne  { background:#ffebee;color:#c62828;font-size:8.5pt !important;font-weight:700;padding:2px 6px;border-radius:4px;border:1px solid #ef9a9a; }
.bm   { background:#e3f2fd;color:#1565c0;font-size:8.5pt !important;font-weight:700;padding:2px 6px;border-radius:4px;border:1px solid #90caf9; }
.br   { background:#fff8e1;color:#e65100;font-size:8.5pt !important;font-weight:700;padding:2px 6px;border-radius:4px;border:1px solid #ffcc02; }
.bn   { background:#e3f2fd;color:#1565c0;font-size:8.5pt !important;font-weight:700;padding:2px 6px;border-radius:4px;border:1px solid #64b5f6; }
.bsbi { background:#f3e5f5;color:#6a0dad;font-size:8.5pt !important;font-weight:700;padding:2px 6px;border-radius:4px;border:1px solid #ce93d8; }
.bst  { background:#ede7f6;color:#4527a0;font-size:8.5pt !important;font-weight:700;padding:2px 6px;border-radius:4px; }
.blw  { background:#f3e5f5;color:#6a1b9a;font-size:8.5pt !important;font-weight:700;padding:2px 6px;border-radius:4px;border:1px solid #ce93d8; }
.baj  { background:#ffebee;color:#c62828;font-size:8.5pt !important;font-weight:700;padding:2px 6px;border-radius:4px;border:1px solid #ef9a9a; }

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

/* RISK SCORE BAR */
.risk-bar-wrap { background:#e0e0e0;border-radius:4px;height:6px;width:100%;margin-top:3px; }
.risk-bar-fill { height:6px;border-radius:4px; }

.pos-badge { display:inline-block;background:#1b5e20;color:#fff;font-size:9pt !important;font-weight:700;padding:2px 8px;border-radius:12px;margin-left:8px; }
[data-testid="metric-container"] { display:none !important; }
hr { border-color:#e2e8f0 !important; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────────────────────

if "last_refresh"    not in st.session_state: st.session_state["last_refresh"]    = time.time()
if "portfolio"       not in st.session_state: st.session_state["portfolio"]       = load_portfolio()
if "_login_recorded" not in st.session_state: st.session_state["_login_recorded"] = False
if "show_uploader"   not in st.session_state: st.session_state["show_uploader"]   = False

record_login()

# ─────────────────────────────────────────────────────────────
# FETCH ALL DATA
# ─────────────────────────────────────────────────────────────

market_data = fetch_market_data()
fii_data    = fetch_fii_flow()

@st.cache_data(ttl=90, show_spinner=False)
def load_all_data():
    return (fetch_all_feeds(FEED_SOURCES),
            fetch_rbi_circulars(),
            fetch_nse_circulars(),
            fetch_sebi_circulars())

with st.spinner("📡 Fetching latest news..."):
    all_data, rbi_circulars, nse_circulars, sebi_circulars = load_all_data()

livewire_articles = fetch_livewire()

manual_items = load_manual()
for item in manual_items:
    if isinstance(item.get("dt"), str):
        try:    item["dt"] = datetime.fromisoformat(item["dt"])
        except: item["dt"] = datetime.now(timezone.utc)

# Build all_articles — FEED_SOURCES + Live Wire + manual, sorted newest first
all_articles = []
for cat, arts in all_data.items():
    for a in arts: all_articles.append({**a,"category":cat,"manual":False})
for a in livewire_articles:
    all_articles.append({**a,"category":"🌐 Live Wire","manual":False})
for item in manual_items:
    all_articles.append({**item,"manual":True})
all_articles.sort(
    key=lambda x: x["dt"] if isinstance(x["dt"],datetime) else datetime.now(timezone.utc),
    reverse=True
)

# ─────────────────────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────────────────────

now_ist_str  = datetime.now(IST).strftime("%d %b %Y  %I:%M %p IST")
refresh_int  = get_refresh_interval()
elapsed      = int(time.time() - st.session_state.get("last_refresh", time.time()))
remaining    = max(0, refresh_int - elapsed)
mkt_badge    = '<span class="mkt-open">● MARKET OPEN</span>' if is_market_open() else '<span class="mkt-close">● MARKET CLOSED</span>'

st.markdown(f"""
<div class="top-bar">
  <div class="top-bar-title">
    <span class="live-dot"></span>JM FINANCIAL &nbsp;·&nbsp; RISK INTELLIGENCE DASHBOARD
    &nbsp;&nbsp;{mkt_badge}
  </div>
  <div class="top-bar-right">
    <span class="refresh-timer">⏱ {remaining}s &nbsp;·&nbsp; {'Live 30s' if is_market_open() else 'Off-hours 5min'}</span>
    <span class="top-bar-time">{now_ist_str}</span>
  </div>
</div>""", unsafe_allow_html=True)

# VIX Warning Banner
vix_price = (market_data.get("INDIA VIX") or {}).get("price")
if vix_price and vix_price >= 20:
    level = "🔴 EXTREME FEAR" if vix_price >= 25 else "🟡 ELEVATED FEAR"
    st.markdown(f'<div class="vix-banner">⚠️ INDIA VIX = {vix_price:.2f} — {level} · Elevated market risk — review positions immediately</div>', unsafe_allow_html=True)

# FII/DII Flow Bar
fii_html = '<div class="fii-bar"><span>📊 FII/DII Provisional'
dd = fii_data.get("data_date"); src = fii_data.get("source","")
if dd:  fii_html += f' &nbsp;·&nbsp; <span style="color:#ffd54f !important;font-weight:700;">As of {dd}</span>'
if src: fii_html += f' &nbsp;·&nbsp; <span style="color:#90caf9 !important;font-size:8.5pt !important;">{src}</span>'
fii_html += ':</span>'
fn = fii_data.get("fii_net"); dn = fii_data.get("dii_net")
if fn is not None:
    fc="fii-pos" if fn>=0 else "fii-neg"; dc="fii-pos" if (dn or 0)>=0 else "fii-neg"
    fii_html += f'<span>FII: <span class="{fc}">{"+" if fn>=0 else ""}&#8377;{fn:,.0f} Cr</span></span>'
    if dn is not None:
        fii_html += f'<span>DII: <span class="{dc}">{"+" if dn>=0 else ""}&#8377;{dn:,.0f} Cr</span></span>'
    if fn<0 and (dn or 0)<0:   fii_html += '<span style="color:#ff8a80 !important;font-weight:700;">⚠ Both Selling</span>'
    elif fn>=0 and (dn or 0)>=0: fii_html += '<span style="color:#80e27e !important;font-weight:700;">✓ Both Buying</span>'
else:
    fii_html += '<span class="fii-neu">Provisional data available after 5 PM IST &nbsp;|&nbsp; <a href="https://www.nseindia.com/reports/fii-dii" target="_blank" style="color:#90caf9 !important;">NSE Page ↗</a></span>'
fii_html += '</div>'
st.markdown(fii_html, unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# MARKET MONITOR
# ─────────────────────────────────────────────────────────────

mhtml = '<div class="market-monitor"><div class="mm-label">⚡ Live Market Watch &nbsp;·&nbsp; Yahoo Finance &nbsp;·&nbsp; Refreshes every 60s</div><div class="ticker-grid">'
for name, data in market_data.items():
    p,c,pct,unit = data.get("price"),data.get("change"),data.get("pct"),data.get("unit","")
    if p is None:
        mhtml += f'<div class="ticker-card flat"><div class="t-name">{name}</div><div class="t-price">—</div><div class="t-flat">N/A</div></div>'
        continue
    if name == "INDIA VIX":
        vcls = "vix-hi" if p>=20 else "vix-ok"
        vlbl = "⚠ HIGH" if p>=20 else "✓ LOW"
        mhtml += f'<div class="ticker-card {vcls}"><div class="t-name">{name}</div><div class="t-price">{p:.2f}</div><div class="t-vix">{vlbl} ({pct:+.2f}%)</div></div>'
        continue
    d="up" if c>=0 else "down"; arr="▲" if c>=0 else "▼"; tc="t-up" if c>=0 else "t-dn"
    ps=f"{unit}{p:,.0f}" if name in ("NIFTY 50","SENSEX","BANK NIFTY") else f"{unit}{p:,.2f}"
    mhtml += f'<div class="ticker-card {d}"><div class="t-name">{name}</div><div class="t-price">{ps}</div><div class="{tc}">{arr} {abs(c):,.2f} ({abs(pct):.2f}%)</div></div>'
mhtml += "</div></div>"
st.markdown(mhtml, unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## 📊 JM Risk Dashboard")
    st.markdown("---")
    st.markdown("### 🔍 Stock Search")
    st.caption("NSE Code · BSE Code · ISIN · Name")
    search_input = st.text_input(
        label="__s__", placeholder="e.g. RELIANCE  or  500325",
        label_visibility="collapsed", key="stock_search_input"
    )
    if search_input.strip():
        with st.spinner("Fetching..."):
            res = fetch_stock_quote(search_input.strip())
        if res["found"]:
            arrow="▲" if res["change"]>=0 else "▼"; clr="#4caf50" if res["change"]>=0 else "#f44336"
            src_lbl = res.get("source","")
            st.markdown(f"""<div class="sq-card">
              <div class="sq-name">{res['name']}</div>
              <div class="sq-sym">{res['symbol']} &nbsp;·&nbsp; <span style="color:#4caf50 !important;font-size:8pt !important;">{src_lbl}</span></div>
              <div class="sq-price">&#8377;{res['price']:,.2f}</div>
              <div style="color:{clr};font-size:10pt;font-weight:700;margin-top:2px;">
                {arrow} &#8377;{abs(res['change']):,.2f} ({abs(res['pct']):.2f}%)</div>
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
            if not news: st.caption("No recent news found.")
        else:
            st.error(f"❌ Not found: `{search_input.strip()}`\n\n{res.get('error','')[:120]}")

    st.markdown("---")
    st.markdown("### ⚙️ Controls")
    auto_refresh = st.toggle("Auto Refresh", value=True)
    if st.button("🔄 Refresh Now", use_container_width=True):
        st.cache_data.clear(); st.session_state["last_refresh"]=time.time(); st.rerun()

    st.markdown("---")
    st.markdown("### 🔐 Admin Panel")
    pwd      = st.text_input("Password", type="password", key="admin_pwd")
    is_admin = (pwd == ADMIN_PASSWORD)

    if is_admin:
        st.success("✅ Access granted")
        st.markdown("#### 📂 Portfolio")
        current_port = st.session_state.get("portfolio", load_portfolio())
        total_pos    = sum(p.get("position_crs",0) for p in current_port)
        st.markdown(
            f"<div style='font-size:9.5pt;color:#90caf9;margin:2px 0 8px;'>"
            f"📌 {len(current_port)} stocks"
            + (f" · ₹{total_pos:,.1f} Cr" if total_pos>0 else "")
            + "</div>", unsafe_allow_html=True
        )
        col_a,col_b = st.columns(2)
        with col_a:
            if st.button("📂 Import", use_container_width=True, key="btn_import"):
                st.session_state["show_uploader"] = not st.session_state.get("show_uploader",False)
        with col_b:
            if st.button("🗑 Delete", use_container_width=True, key="btn_del"):
                st.session_state["portfolio"] = DEFAULT_PORTFOLIO
                st.session_state["show_uploader"] = False
                if os.path.exists(PORTFOLIO_FILE): os.remove(PORTFOLIO_FILE)
                st.cache_data.clear(); st.rerun()

        if st.session_state.get("show_uploader",False):
            st.caption("📎 Select Excel (.xlsx):")
            uploaded_excel = st.file_uploader(
                label="portfolio_excel", type=["xlsx","xls"],
                key="portfolio_upload", label_visibility="collapsed",
            )
            if uploaded_excel is not None:
                with st.spinner("Parsing..."):
                    new_port, err = parse_excel_portfolio(uploaded_excel)
                if new_port:
                    st.session_state["portfolio"] = new_port
                    st.session_state["show_uploader"] = False
                    save_portfolio(new_port); st.cache_data.clear()
                    st.success(f"✅ {len(new_port)} stocks imported!"); st.rerun()
                else:
                    st.error(f"❌ {err}")

        st.markdown("---")
        st.markdown("#### 📄 Daily Briefing")
        if st.button("⬇ Download Morning Briefing (PDF)", use_container_width=True):
            with st.spinner("Generating PDF..."):
                pdf_bytes = generate_briefing_pdf(
                    all_articles, rbi_circulars, nse_circulars, sebi_circulars,
                    st.session_state.get("portfolio", load_portfolio()), market_data
                )
            if pdf_bytes:
                fname = f"JM_RiskBriefing_{datetime.now(IST).strftime('%Y%m%d_%H%M')}.pdf"
                st.download_button("📥 Save PDF", data=pdf_bytes, file_name=fname,
                                   mime="application/pdf", use_container_width=True, key="save_pdf")
            else:
                st.warning("⚠ Install fpdf2: add fpdf2 to requirements.txt")

        st.markdown("---")
        st.markdown("#### 📢 Push Headline")
        new_title = st.text_area("Headline", height=68, key="adm_title", placeholder="Enter headline text...")
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
            for i,item in enumerate(existing):
                c1,c2=st.columns([5,1])
                c1.markdown(f"<div style='font-size:9pt;color:#cbd5e0;'>{item['title'][:50]}…</div>",unsafe_allow_html=True)
                if c2.button("✕",key=f"del_{i}"):
                    existing.pop(i); save_manual(existing); st.rerun()

        st.markdown("---")
        st.markdown("#### 🛡️ Access Log")
        logs=load_login_logs()
        st.markdown(f"<div style='font-size:9.5pt;color:#90caf9;margin:2px 0 8px;'>📋 {len(logs)} sessions</div>",unsafe_allow_html=True)
        if logs:
            st.download_button("⬇ Download Log (CSV)", data=logs_to_csv_bytes(logs),
                               file_name=f"jm_log_{datetime.now(IST).strftime('%Y%m%d')}.csv",
                               mime="text/csv", use_container_width=True, key="dl_log")
            if st.button("🗑 Clear Log", use_container_width=True, key="clear_log"):
                if os.path.exists(LOGIN_LOG_FILE): os.remove(LOGIN_LOG_FILE); st.rerun()
        else:
            st.caption("No sessions yet.")
    elif pwd:
        st.error("❌ Incorrect password")

# ─────────────────────────────────────────────────────────────
# RENDERERS
# Each renderer uses LOCAL dedup only — no cross-tab interference.
# This is the correct fix for blank category tabs.
# ─────────────────────────────────────────────────────────────

def _cls(ip,sent):
    if ip:               return "news-card priority"
    if sent=="positive": return "news-card sentiment-positive"
    if sent=="negative": return "news-card sentiment-negative"
    return "news-card"

def _local_dedup(articles):
    """Deduplicate within a single list. Never touches a global set."""
    seen, out = set(), []
    for a in articles:
        t = a.get("title","")
        if t and t not in seen: seen.add(t); out.append(a)
    return out

def render_news_cards(articles):
    items = _local_dedup(articles)
    if not items:
        st.markdown("<div style='font-size:11pt;color:#718096;padding:16px 0;'>No news found — try Refresh Now.</div>",unsafe_allow_html=True)
        return
    for art in items:
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
    label="RBI" if "rbi" in card_extra else ("SEBI" if "sebi" in card_extra else "NSE")
    items = _local_dedup(articles)
    if not items:
        st.markdown(f"<div style='font-size:11pt;color:#718096;padding:16px 0;'>No {label} circulars — try Refresh Now.</div>",unsafe_allow_html=True)
        return
    for art in items:
        ip=art.get("priority",False); sent=art.get("sentiment","neutral")
        is_enf = art.get("source","")=="SEBI Enforcement"
        cls=f"news-card {card_extra}"+(" priority" if ip else ""); ago=time_ago(art["dt"])
        src=art.get("source","")
        tb=f'<span class="{badge_cls}">{label} {"ENFORCEMENT" if is_enf else "CIRCULAR"}</span>'
        pb='<span class="bp">⚡ PRIORITY</span>' if ip else ""
        sb=('<span class="bpo">▲ POSITIVE</span>' if sent=="positive" else '<span class="bne">▼ NEGATIVE</span>' if sent=="negative" else "")
        src_b=f'<span style="background:#21262d;color:#8b949e;font-size:7.5pt;padding:1px 5px;border-radius:3px;">{src}</span>' if src else ""
        st.markdown(f'<div class="{cls}"><a class="card-title" href="{art["link"]}" target="_blank">{art["title"]}</a><div class="card-meta"><span>{ago}</span>{tb}{sb}{pb}{src_b}</div></div>',unsafe_allow_html=True)

def render_portfolio(portfolio):
    if not portfolio:
        st.markdown("<div style='font-size:11pt;color:#718096;padding:16px 0;'>No portfolio loaded. Admin → Import.</div>",unsafe_allow_html=True)
        return
    tp=sum(p.get("position_crs",0) for p in portfolio)
    month_label=datetime.now(timezone.utc).strftime("%B %Y")
    st.markdown(f"<div style='font-size:10.5pt;color:#4527a0;margin-bottom:10px;'>Tracking <b>{len(portfolio)} stocks</b>"+(f" &nbsp;·&nbsp; Total: <b>₹{tp:,.1f} Cr</b>" if tp>0 else "")+f" &nbsp;·&nbsp; <span style='font-size:9.5pt;color:#718096;'>{month_label} · last 30 days</span></div>",unsafe_allow_html=True)
    for stock in sorted(portfolio, key=lambda x: x.get("position_crs",0), reverse=True):
        name=stock.get("name",""); nse=stock.get("nse_code",""); pos=stock.get("position_crs",0)
        pb=f'<span class="pos-badge">₹{pos:,.1f} Cr</span>' if pos and pos>0 else ""
        arts=fetch_stock_news_gn(nse, name)
        score=calc_risk_score(arts, pos, tp)
        if score>=15:    rs_col="#c62828"; rs_lbl=f"⚠ HIGH ({score:.0f})"
        elif score>=5:   rs_col="#e65100"; rs_lbl=f"△ MEDIUM ({score:.0f})"
        else:            rs_col="#2e7d32"; rs_lbl=f"✓ LOW ({score:.0f})"
        rs_badge=f'<span style="font-size:8.5pt;font-weight:700;color:{rs_col};margin-left:10px;">{rs_lbl}</span>'
        st.markdown(f'<div class="stock-hdr">📌 {name.upper()}<span style="font-size:9.5pt;color:#718096;font-weight:400;margin-left:8px;">{nse}</span>{pb}{rs_badge}</div>',unsafe_allow_html=True)
        if not arts:
            st.markdown(f"<div style='font-size:10pt;color:#718096;padding:4px 0 12px;'>No recent news.</div>",unsafe_allow_html=True)
            continue
        pn=sum(1 for a in arts if a.get("sentiment")=="positive")
        nn=sum(1 for a in arts if a.get("sentiment")=="negative")
        un=len(arts)-pn-nn
        bw=min(int(score),100); bc="#c62828" if score>=15 else "#e65100" if score>=5 else "#2e7d32"
        st.markdown(f'<div class="sent-bar"><span>Headlines:<b>{len(arts)}</b></span><span>🟢<span class="pos">{pn}</span></span><span>🔴<span class="neg">{nn}</span></span><span>⚪<span class="neu">{un}</span></span></div><div class="risk-bar-wrap"><div class="risk-bar-fill" style="width:{bw}%;background:{bc};"></div></div>',unsafe_allow_html=True)
        for art in arts:
            ip=art.get("priority",False); sent=art.get("sentiment","neutral"); cls=_cls(ip,sent)
            dt_o=art["dt"]
            if isinstance(dt_o,str):
                try:    dt_o=datetime.fromisoformat(dt_o)
                except: dt_o=datetime.now(timezone.utc)
            ago=time_ago(dt_o)
            pb2='<span class="bp">⚡ PRIORITY</span>' if ip else ""
            sb2=('<span class="bpo">▲ POSITIVE</span>' if sent=="positive" and not ip else '<span class="bne">▼ NEGATIVE</span>' if sent=="negative" and not ip else "")
            st.markdown(f'<div class="{cls}"><a class="card-title" href="{art["link"]}" target="_blank">{art["title"]}</a><div class="card-meta"><span>{ago}</span><span class="bst">{nse}</span>{sb2}{pb2}</div></div>',unsafe_allow_html=True)
        st.markdown("<div style='margin-bottom:8px'></div>",unsafe_allow_html=True)

def render_livewire(articles):
    items = _local_dedup(articles)
    if not items:
        st.markdown("<div style='font-size:11pt;color:#718096;padding:16px 0;'>No live wire news — try Refresh Now.</div>",unsafe_allow_html=True)
        return
    for art in items:
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

# ─────────────────────────────────────────────────────────────
# TABS
# ─────────────────────────────────────────────────────────────

news_cats=list(FEED_SOURCES.keys())
tab_all,tab_prio,tab_port,tab_rbi,tab_nse,tab_sebi,tab_lw,*cat_tabs=st.tabs(
    ["📋 All News","🔴 Priority","📂 Portfolio",
     "🏛️ RBI Circulars","🔵 NSE Circulars","⚖️ SEBI","🌐 Live Wire"]+news_cats
)

with tab_all:   render_news_cards(all_articles)

with tab_prio:
    pa=[a for a in all_articles if a.get("priority")]
    if pa: st.markdown(f"<div style='font-size:10.5pt;color:#c62828;font-weight:700;margin-bottom:10px;'>⚡ {len(pa)} priority alerts</div>",unsafe_allow_html=True)
    render_news_cards(pa)

with tab_port:  render_portfolio(st.session_state.get("portfolio",load_portfolio()))

with tab_rbi:
    st.markdown("<div style='font-size:9.5pt;color:#e65100;margin-bottom:8px;'>Source: rbi.org.in official RSS feeds only</div>",unsafe_allow_html=True)
    render_circular_cards(rbi_circulars,"br","rbi")

with tab_nse:
    nse_src = "NSE (nselib)" if NSELIB_OK else "NSE RSS Archives"
    st.markdown(f"<div style='font-size:9.5pt;color:#1565c0;margin-bottom:8px;'>Source: {nse_src}</div>",unsafe_allow_html=True)
    render_circular_cards(nse_circulars,"bn","nse")

with tab_sebi:
    st.markdown("<div style='font-size:9.5pt;color:#6a0dad;margin-bottom:8px;'>Source: sebi.gov.in RSS + Google News enforcement orders</div>",unsafe_allow_html=True)
    render_circular_cards(sebi_circulars,"bsbi","sebi")

with tab_lw:
    lw_count = len(_local_dedup(livewire_articles))
    st.markdown(f"<div style='font-size:9.5pt;color:#6a1b9a;margin-bottom:8px;'>🌐 Live Wire &nbsp;·&nbsp; Al Jazeera + Google News India/Geopolitical &nbsp;·&nbsp; Last 48h &nbsp;·&nbsp; {lw_count} articles</div>",unsafe_allow_html=True)
    render_livewire(livewire_articles)

# Category tabs — each uses local dedup, completely independent
for tab,cat in zip(cat_tabs,news_cats):
    with tab:
        st.markdown(f'<div class="cat-header">{cat}</div>',unsafe_allow_html=True)
        render_news_cards([{**a,"category":cat,"manual":False} for a in all_data.get(cat,[])])

# ─────────────────────────────────────────────────────────────
# AUTO-REFRESH
# ─────────────────────────────────────────────────────────────

if auto_refresh:
    elapsed_now = int(time.time()-st.session_state.get("last_refresh",time.time()))
    if elapsed_now >= get_refresh_interval():
        st.cache_data.clear(); st.session_state["last_refresh"]=time.time(); time.sleep(0.3); st.rerun()
    else:
        time.sleep(5); st.rerun()
