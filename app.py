"""
JM Financial | Risk Intelligence Dashboard  v13.1
==================================================
KEY CHANGES:
✅ UPDATED: FII/DII logic now specifically pulls Combined (NSE+BSE+MSEI) data
✅ nselib used for stock quote prices (fetch_stock_quote)
✅ nselib used for FII/DII data (capital_market.fii_dii_trading_activity)
✅ nselib used for NSE Circulars (capital_market.exchange_circulars)
✅ yfinance kept ONLY for live market monitor tickers (NIFTY/SENSEX/VIX/commodities)
✅ RBI Circulars: RSS only — clean and reliable
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
DB_FILE         = "manual_headlines.json"
PORTFOLIO_FILE = "portfolio_data.json"
LOGIN_LOG_FILE = "login_log.json"
ADMIN_PASSWORD = "JM_RISK_2026"
PROXIES         = None

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
    inp = raw.strip().upper()

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
                try:
                    nm_col = [c for c in df.columns if "symbol" in c.lower() or "name" in c.lower()]
                    if nm_col: nm = str(df[nm_col[0]].iloc[-1])
                except: nm = nse_sym
                return {"found":True,"name":nm,"symbol":nse_sym,"price":price,"change":c,"pct":pct,"error":None,"source":"NSE"}
        except: pass

    # ── FALLBACK: yfinance ──────────────────────────────────────
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
# FII / DII — UPDATED: Combined NSE+BSE Data via nselib
# ─────────────────────────────────────────────────────────────

@st.cache_data(ttl=1800, show_spinner=False)
def fetch_fii_flow():
    """
    Primary: nselib capital_market.fii_dii_trading_activity()
    Fetches the 'Combined' (NSE, BSE, MSEI) data as per user requirements.
    """
    result = {"fii_net":None,"dii_net":None,"headline":None,
              "dt":None,"data_date":None,"source":None}

    if NSELIB_OK:
        try:
            from nselib import capital_market
            df = capital_market.fii_dii_trading_activity()
            
            if df is not None and not df.empty:
                # Column names in nselib: category, date, buyValue, sellValue, netValue
                dii_row = df[df['category'].str.contains('DII', case=False, na=False)]
                fii_row = df[df['category'].str.contains('FII', case=False, na=False)]
                
                if not fii_row.empty and not dii_row.empty:
                    fii_v = float(str(fii_row['netValue'].iloc[0]).replace(",",""))
                    dii_v = float(str(dii_row['netValue'].iloc[0]).replace(",",""))
                    date_v = str(fii_row['date'].iloc[0])
                    
                    fp = "+" if fii_v >= 0 else ""
                    dp = "+" if dii_v >= 0 else ""
                    
                    result.update({
                        "fii_net": fii_v,
                        "dii_net": dii_v,
                        "data_date": date_v,
                        "dt": datetime.now(timezone.utc),
                        "source": "NSE Combined (nselib)",
                        "headline": f"FII: {fp}{fii_v:,.2f} Cr | DII: {dp}{dii_v:,.2f} Cr (as of {date_v})"
                    })
                    return result
        except: pass

    # FALLBACK 1: Direct NSE JSON
    try:
        session = requests.Session()
        bh = {"User-Agent":"Mozilla/5.0","Accept-Language":"en-US,en;q=0.9"}
        session.get("https://www.nseindia.com", headers=bh, timeout=10, verify=False)
        resp = session.get("https://www.nseindia.com/api/fiidiiTradeReact", 
                           headers={**bh, "Referer":"https://www.nseindia.com/reports/fii-dii"},
                           timeout=12, verify=False)
        data = resp.json()
        if isinstance(data, list) and data:
            row = data[0]
            date_v = str(row.get("date", ""))[:12]
            fii_v = float(str(row.get("fiiNetTrade", 0)).replace(",",""))
            dii_v = float(str(row.get("diiNetTrade", 0)).replace(",",""))
            result.update({
                "fii_net":fii_v, "dii_net":dii_v, "data_date":date_v,
                "dt":datetime.now(timezone.utc), "source":"NSE API",
                "headline":f"FII: {fii_v:,.0f} Cr | DII: {dii_v:,.0f} Cr"
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

LIVEWIRE_FEEDS = [
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://www.aljazeera.com/xml/rss/middleeast.xml",
    gn("Iran war Israel Middle East breaking news today"),
    gn("India breaking news market NSE BSE today"),
    gn("RBI SEBI NSE news India today"),
]

# ─────────────────────────────────────────────────────────────
# CIRCULAR FETCHERS
# ─────────────────────────────────────────────────────────────

def fetch_rbi_circulars():
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

def fetch_nse_circulars():
    all_items, seen = [], set()
    if NSELIB_OK:
        try:
            from nselib import capital_market
            df = capital_market.exchange_circulars(period="1M")
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    t = str(row.get("subject") or row.get("Subject") or "").strip()
                    l = str(row.get("link") or row.get("Link") or "#").strip()
                    date_raw = str(row.get("date") or row.get("Date") or "").strip()
                    try: dt = datetime.strptime(date_raw[:10], "%d-%m-%Y").replace(tzinfo=timezone.utc)
                    except: dt = datetime.now(timezone.utc)
                    if not t or t in seen or age_days(dt) > 90: continue
                    seen.add(t)
                    all_items.append({"title":t,"link":l,"dt":dt,"priority":is_priority(t),
                                       "sentiment":get_sentiment(t),"source":"NSE (nselib)"})
        except: pass

    for url in ["https://nsearchives.nseindia.com/content/RSS/Circulars.xml"]:
        try:
            resp = requests.get(url, timeout=15, verify=False)
            for e in feedparser.parse(resp.content).entries[:40]:
                t,l,dt = e.get("title","").strip(), e.get("link","#"), parse_dt(e)
                if not t or t in seen: continue
                seen.add(t)
                all_items.append({"title":t,"link":l,"dt":dt,"priority":is_priority(t),
                                   "sentiment":get_sentiment(t),"source":"NSE RSS"})
        except: pass
    all_items.sort(key=lambda x: x["dt"], reverse=True)
    return all_items[:60]

def fetch_sebi_circulars():
    all_items, seen = [], set()
    try:
        resp = requests.get("https://www.sebi.gov.in/sebi_data/rss.xml", timeout=12, verify=False)
        for e in feedparser.parse(resp.content).entries[:40]:
            t,l,dt = e.get("title","").strip(), e.get("link","#"), parse_dt(e)
            if not t or t in seen: continue
            seen.add(t)
            all_items.append({"title":t,"link":l,"dt":dt,"priority":is_priority(t),
                               "sentiment":get_sentiment(t),"source":"SEBI RSS"})
    except: pass
    all_items.sort(key=lambda x: x["dt"], reverse=True)
    return all_items[:60]

# ─────────────────────────────────────────────────────────────
# GENERAL HELPERS
# ─────────────────────────────────────────────────────────────

def fetch_feed(url):
    try:
        resp = requests.get(url, timeout=10, verify=False)
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
    all_items, seen = [], set()
    for url in LIVEWIRE_FEEDS:
        try:
            resp = requests.get(url, timeout=12, verify=False)
            for e in feedparser.parse(resp.content).entries[:20]:
                t,l,dt = e.get("title","").strip(), e.get("link","#"), parse_dt(e)
                if not t or t in seen: continue
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
    with open(DB_FILE,"w") as f: json.dump(items, f, default=str)

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
    try: from fpdf import FPDF
    except: return b""
    now_ist = datetime.now(IST).strftime("%d %b %Y %I:%M %p IST")
    pdf = FPDF(); pdf.add_page(); pdf.set_auto_page_break(auto=True, margin=14)
    pdf.set_fill_color(26,35,126); pdf.rect(0,0,210,22,"F")
    pdf.set_text_color(255,255,255); pdf.set_font("Helvetica","B",13)
    pdf.set_xy(10,7); pdf.cell(130,8,"JM FINANCIAL | RISK INTELLIGENCE BRIEFING")
    pdf.set_text_color(0,0,0); pdf.ln(16)
    # Market Data
    pdf.set_font("Helvetica","B",10); pdf.cell(190,7," MARKET SNAPSHOT",fill=False); pdf.ln(8)
    for n,d in market_data.items():
        if d.get("price"): pdf.set_font("Helvetica","",9); pdf.cell(90,5,f"{n}: {d.get('price'):,.2f} ({d.get('pct'):+.2f}%)"); pdf.ln(5)
    raw = pdf.output()
    return bytes(raw) if isinstance(raw,(bytes,bytearray)) else raw.encode("latin-1")

# ─────────────────────────────────────────────────────────────
# PAGE CONFIG & CSS
# ─────────────────────────────────────────────────────────────

st.set_page_config(page_title="JM Financial | Risk Intelligence", page_icon="📊", layout="wide")

st.markdown("""
<style>
    html,body,p,div,span,a,button,input,label { font-family: Arial, sans-serif !important; font-size: 11pt !important; }
    .top-bar { background:#1a237e; border-radius:8px; padding:10px 20px; margin-top:50px; display:flex; justify-content:space-between; align-items:center; color:#fff; }
    .live-dot { width:8px; height:8px; background:#4caf50; border-radius:50%; display:inline-block; margin-right:6px; animation:pulse 4s infinite; }
    @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.25} }
    .fii-bar { background:#0d47a1; border-radius:6px; padding:8px 16px; margin:10px 0; color:#fff; display:flex; gap:20px; font-size:10pt !important; }
    .fii-pos { color:#80e27e !important; font-weight:700; }
    .fii-neg { color:#ff8a80 !important; font-weight:700; }
    .market-monitor { background:#1a237e; border-radius:8px; padding:15px; margin-bottom:10px; }
    .ticker-grid { display:flex; flex-wrap:wrap; gap:10px; }
    .ticker-card { background:#fff; border-radius:7px; padding:10px; flex:1; min-width:120px; text-align:center; border:1px solid #ddd; }
    .news-card { background:#fff; border-left:4px solid #ddd; border-radius:7px; padding:12px; margin-bottom:8px; box-shadow:0 1px 3px rgba(0,0,0,0.1); }
    .card-title { font-weight:600; color:#1a237e !important; text-decoration:none; }
    .priority { border-left-color:#e53935 !important; background:#fff8f8; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────────────────────
# DATA LOADING & RENDER
# ─────────────────────────────────────────────────────────────

if "last_refresh" not in st.session_state: st.session_state["last_refresh"] = time.time()
if "portfolio" not in st.session_state: st.session_state["portfolio"] = load_portfolio()

market_data = fetch_market_data()
fii_data    = fetch_fii_flow()

with st.spinner("📡 Syncing Risk Data..."):
    all_data = fetch_all_feeds(FEED_SOURCES)
    rbi_circulars = fetch_rbi_circulars()
    nse_circulars = fetch_nse_circulars()
    sebi_circulars = fetch_sebi_circulars()
    livewire_articles = fetch_livewire()

# Build Article List
all_articles = []
for cat, arts in all_data.items():
    for a in arts: all_articles.append({**a,"category":cat})
for a in livewire_articles: all_articles.append({**a,"category":"🌐 Live Wire"})
all_articles.sort(key=lambda x: x["dt"], reverse=True)

# Header
st.markdown(f'<div class="top-bar"><div><span class="live-dot"></span><b>JM FINANCIAL</b> · RISK INTELLIGENCE</div><div>{datetime.now(IST).strftime("%d %b %Y %I:%M %p")}</div></div>', unsafe_allow_html=True)

# FII Flow Bar
fn, dn = fii_data.get("fii_net"), fii_data.get("dii_net")
date_v = fii_data.get("data_date", "Today")
fii_html = f'<div class="fii-bar"><b>INSTITUTIONAL FLOW (Combined)</b> · {date_v} · '
if fn is not None:
    fc="fii-pos" if fn>=0 else "fii-neg"; dc="fii-pos" if dn>=0 else "fii-neg"
    fii_html += f'FII: <span class="{fc}">₹{fn:,.2f} Cr</span> | DII: <span class="{dc}">₹{dn:,.2f} Cr</span>'
else:
    fii_html += "Data updating..."
fii_html += '</div>'
st.markdown(fii_html, unsafe_allow_html=True)

# Market Monitor
mhtml = '<div class="market-monitor"><div class="ticker-grid">'
for n, d in market_data.items():
    p, pct = d.get("price"), d.get("pct", 0)
    if p:
        clr = "#2e7d32" if pct>=0 else "#c62828"
        mhtml += f'<div class="ticker-card"><b>{n}</b><br><span style="font-size:12pt;">{p:,.2f}</span><br><span style="color:{clr};">{pct:+.2f}%</span></div>'
mhtml += "</div></div>"
st.markdown(mhtml, unsafe_allow_html=True)

# Sidebar
with st.sidebar:
    st.title("Settings")
    search = st.text_input("Stock Lookup")
    if search:
        q = fetch_stock_quote(search)
        if q["found"]: st.success(f"{q['name']}: ₹{q['price']}")
    st.divider()
    if st.button("Refresh Dashboard"):
        st.cache_data.clear(); st.rerun()

# Tabs
t1, t2, t3, t4, t5 = st.tabs(["📋 News Feed", "🔴 Priority", "📂 Portfolio", "🏛️ Circulars", "🌐 Live Wire"])

with t1:
    for a in all_articles[:40]:
        st.markdown(f'<div class="news-card"><a class="card-title" href="{a["link"]}">{a["title"]}</a><br><small>{a["category"]} · {time_ago(a["dt"])}</small></div>', unsafe_allow_html=True)

with t2:
    for a in [x for x in all_articles if x.get("priority")][:20]:
        st.markdown(f'<div class="news-card priority"><a class="card-title" href="{a["link"]}">{a["title"]}</a></div>', unsafe_allow_html=True)

with t3:
    for s in st.session_state["portfolio"]:
        st.subheader(f"📌 {s['name']}")
        st.caption(f"NSE: {s['nse_code']} | Exposure: ₹{s['position_crs']} Cr")

with t4:
    st.write("### RBI / NSE / SEBI")
    for c in (rbi_circulars + nse_circulars + sebi_circulars)[:30]:
        st.markdown(f'**[{c["source"]}]** {c["title"]} - [View]({c["link"]})')

with t5:
    for a in livewire_articles[:30]:
        st.markdown(f'**{a["title"]}** ({time_ago(a["dt"])})')

# Auto-Refresh Logic
time.sleep(30)
st.rerun()
