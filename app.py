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
import google.generativeai as genai 

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
DB_FILE              = "manual_headlines.json"
ADMIN_PASSWORD       = "JM_RISK_2026"
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
# PORTFOLIO STOCKS
# ─────────────────────────────────────────────
PORTFOLIO_STOCKS = [
    "Religare",
    "Valor Estate",
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

EXCLUDE_CIRCULAR_KEYWORDS = [
    "court", "tribunal", "writ", "petition", "judgment", "judgement",
    "filing", "filed", "stock exchange filing", "bse filing", "nse filing",
    "annual report", "quarterly result", "q1 result", "q2 result",
    "q3 result", "q4 result", "earnings", "balance sheet",
    "ipo filing", "drhp", "prospectus",
]

# ─────────────────────────────────────────────
# AI SENTIMENT via Google Gemini API
# ─────────────────────────────────────────────

@st.cache_data(ttl=3600, show_spinner=False)
def get_sentiment_ai(titles_tuple: tuple) -> dict:
    titles = list(titles_tuple)
    if not titles:
        return {}

    api_key = st.secrets.get("GOOGLE_API_KEY", "")
    if not api_key:
        return {t: _fallback_sentiment(t) for t in titles}

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash")

        numbered = "\n".join([f"{i+1}. {t}" for i, t in enumerate(titles)])

        prompt = f"""You are a financial news sentiment analyst for an Indian equity risk management team.
Analyze the sentiment of each headline below from the perspective of a RISK OFFICER.

Rules:
- "positive" = good for markets/investors
- "negative" = bad for markets/investors
- "neutral" = factual/no clear impact

Respond ONLY with a valid JSON object. No explanation. Format:
{{"1": "positive", "2": "negative", "3": "neutral", ...}}

Headlines:
{numbered}"""

        response = model.generate_content(prompt)
        raw = response.text.strip()
        raw = re.sub(r"```json|```", "", raw).strip()
        result_map = json.loads(raw)

        sentiment_dict = {}
        for i, title in enumerate(titles):
            key = str(i + 1)
            # Flag if this was truly classified by AI (not neutral)
            val = result_map.get(key, "neutral")
            sentiment_dict[title] = val
        return sentiment_dict

    except Exception as e:
        st.warning(f"⚠️ Gemini AI unavailable: {e}")
        return {t: _fallback_sentiment(t) for t in titles}

def _fallback_sentiment(title: str) -> str:
    t = title.lower()
    if any(kw in t for kw in ["fraud", "scam", "default", "crash"]): return "negative"
    if any(kw in t for kw in ["record high", "profit surge"]): return "positive"
    return "neutral"

def batch_sentiment(articles: list) -> list:
    if not articles: return articles
    BATCH_SIZE = 50
    all_titles = [a["title"] for a in articles]
    sentiment_map = {}
    for i in range(0, len(all_titles), BATCH_SIZE):
        batch = all_titles[i:i + BATCH_SIZE]
        batch_result = get_sentiment_ai(tuple(batch))
        sentiment_map.update(batch_result)
    for article in articles:
        article["sentiment"] = sentiment_map.get(article["title"], "neutral")
    return articles

def is_priority(title: str) -> bool:
    return any(kw in title.lower() for kw in PRIORITY_KEYWORDS)

# ─────────────────────────────────────────────
# RSS & FETCHING
# ─────────────────────────────────────────────

def gn(q): return f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=en-IN&gl=IN&ceid=IN:en"

FEED_SOURCES = {
    "🇮🇳 India Markets": [gn("NIFTY OR SENSEX OR NSE OR BSE India stock market"), gn("RBI policy rate India")],
    "💵 Currency & Forex": [gn("Indian Rupee USD exchange rate")],
    "🛢️ Commodities & Oil": [gn("crude oil price Brent WTI")],
    "🌍 Geopolitical Risk": [gn("Iran war sanctions Middle East")],
    "📊 Global Macro": [gn("Federal Reserve interest rate inflation")],
    "📰 Reuters Finance": ["https://feeds.reuters.com/reuters/businessNews"],
}

RBI_CIRCULAR_FEEDS = ["https://www.rbi.org.in/rss/RBINotificationsRSS.xml"]
NSE_CIRCULAR_FEEDS = [gn("NSE circular notice India")]

def parse_dt(entry) -> datetime:
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t: return datetime(*t[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)

def fetch_feed(url: str) -> list:
    try:
        resp = requests.get(url, timeout=10, verify=False, proxies=PROXIES)
        feed = feedparser.parse(resp.content)
        return [{"title": e.get("title", "").strip(), "link": e.get("link", "#"), "dt": parse_dt(e), "priority": is_priority(e.get("title", "")), "sentiment": "neutral"} for e in feed.entries[:15] if e.get("title")]
    except: return []

def fetch_all_feeds(feed_dict: dict) -> dict:
    results = defaultdict(list)
    all_urls = [(cat, url) for cat, urls in feed_dict.items() for url in urls]
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(fetch_feed, url): cat for cat, url in all_urls}
        for f in as_completed(futures): results[futures[f]].extend(f.result())
    for cat in results: results[cat].sort(key=lambda x: x["dt"], reverse=True)
    return dict(results)

def fetch_circulars(feeds: list, exclude_kw: list) -> list:
    items = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        for f in as_completed([ex.submit(fetch_feed, url) for url in feeds]):
            for item in f.result():
                if not any(kw in item["title"].lower() for kw in exclude_kw): items.append(item)
    items.sort(key=lambda x: x["dt"], reverse=True)
    seen, unique = set(), []
    for i in items:
        if i["title"] not in seen: seen.add(i["title"]); unique.append(i)
    return unique

def load_manual_headlines():
    if os.path.exists(DB_FILE):
        try: 
            with open(DB_FILE) as f: return json.load(f)
        except: return []
    return []

def save_manual_headlines(items):
    with open(DB_FILE, "w") as f: json.dump(items, f, default=str)

def time_ago(dt: datetime) -> str:
    diff = datetime.now(timezone.utc) - dt.replace(tzinfo=timezone.utc)
    s = int(diff.total_seconds())
    if s < 60: return f"{s}s ago"
    if s < 3600: return f"{s//60}m ago"
    if s < 86400: return f"{s//3600}h ago"
    return f"{s//86400}d ago"

# ─────────────────────────────────────────────
# UI CONFIG & CSS
# ─────────────────────────────────────────────
st.set_page_config(page_title="JM Financial | Risk Intelligence", page_icon="📊", layout="wide")

st.markdown("""
<style>
html, body, [class*="css"] { font-family: 'IBM Plex Mono', monospace !important; background-color: #0d1117 !important; color: #c9d1d9 !important; }
.news-card { background: #161b22; border: 1px solid #21262d; border-left: 3px solid #21262d; border-radius: 6px; padding: 10px 14px; margin-bottom: 6px; }
.news-card.priority { border-left-color: #f85149; background: #1a0f0f; }
.news-card.sentiment-positive { border-left-color: #3fb950; background: #0d1a0f; }
.news-card.sentiment-negative { border-left-color: #f85149; background: #1a0d0d; }
.card-title { font-size: 0.82rem; font-weight: 500; color: #c9d1d9 !important; text-decoration: none !important; display: block; margin-bottom: 6px; }
.card-meta { font-size: 0.68rem; color: #484f58; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.badge-ai-classified { background: #1a1a1a; color: #a78bfa; border: 1px solid #6e40c9; font-size: 0.55rem; padding: 1px 4px; border-radius: 3px; font-weight: bold; }
.badge-priority { background:#f85149; color:#fff; font-size:0.6rem; padding:2px 6px; border-radius:3px; }
.badge-positive { background:#1a4a1f; color:#3fb950; font-size:0.6rem; border:1px solid #3fb950; padding:2px 6px; border-radius:3px; }
.badge-negative { background:#4a1a1a; color:#f85149; font-size:0.6rem; border:1px solid #f85149; padding:2px 6px; border-radius:3px; }
.ai-badge { background: linear-gradient(135deg, #6e40c9, #388bfd); color: white; font-size: 0.62rem; padding: 3px 8px; border-radius: 20px; }
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────
api_key_status = bool(st.secrets.get("GOOGLE_API_KEY", ""))
st.markdown(f"""
<div style="display:flex;justify-content:space-between;padding:16px;background:#161b22;border-radius:8px;margin-bottom:16px;">
  <div><b>JM FINANCIAL · RISK INTELLIGENCE</b></div>
  <div class="ai-badge">{'✦ GEMINI AI ACTIVE' if api_key_status else '⚠️ AI OFF'}</div>
</div>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────
with st.sidebar:
    auto_refresh = st.toggle("Auto Refresh (90s)", value=True)
    if st.button("🔄 Refresh Now", use_container_width=True): st.cache_data.clear(); st.rerun()

# ─────────────────────────────────────────────
# DATA & RENDERING
# ─────────────────────────────────────────────
@st.cache_data(ttl=90, show_
