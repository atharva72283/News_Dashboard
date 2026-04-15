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
import google.generativeai as genai  # Swapped from anthropic

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
# PORTFOLIO STOCKS — edit this list as needed
# ─────────────────────────────────────────────
PORTFOLIO_STOCKS = [
    "Sammaan Capital",
    "Suzlon",
    "Religare",
    "Valor Estate",
    # Add more stocks here — up to 10 recommended
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

# Keywords that indicate filings / court orders — filter these OUT from RBI/NSE tabs
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
    """
    Uses Google Gemini AI to analyze sentiment for a batch of news headlines.
    Returns a dict: { headline -> 'positive' | 'negative' | 'neutral' }
    """
    titles = list(titles_tuple)
    if not titles:
        return {}

    # Get API key from Streamlit secrets
    api_key = st.secrets.get("GOOGLE_API_KEY", "")
    if not api_key:
        return {t: _fallback_sentiment(t) for t in titles}

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash")

        numbered = "\n".join([f"{i+1}. {t}" for i, t in enumerate(titles)])

        prompt = f"""You are a financial news sentiment analyst for an Indian equity risk management team.
Analyze the sentiment of each headline below from the perspective of a RISK OFFICER at an Indian financial firm.

Rules:
- "positive" = good for markets, investors, or the subject company
- "negative" = bad for markets, investors, or the subject company
- "neutral" = factual/informational with no clear market impact

IMPORTANT context rules:
- "FII buying" or "DII buying" = POSITIVE
- "FII selling" or "outflows" = NEGATIVE
- "rate cut" = POSITIVE for equities
- "rate hike" = NEGATIVE for equities

Respond ONLY with a valid JSON object. No explanation. No markdown. Format:
{{"1": "positive", "2": "negative", "3": "neutral", ...}}

Headlines:
{numbered}"""

        response = model.generate_content(prompt)
        raw = response.text.strip()
        
        # Clean potential markdown fences
        raw = re.sub(r"```json|```", "", raw).strip()
        result_map = json.loads(raw)

        sentiment_dict = {}
        for i, title in enumerate(titles):
            key = str(i + 1)
            sentiment_dict[title] = result_map.get(key, "neutral")
        return sentiment_dict

    except Exception as e:
        st.warning(f"⚠️ AI sentiment temporarily unavailable: {e}. Using basic analysis.", icon="⚠️")
        return {t: _fallback_sentiment(t) for t in titles}


def _fallback_sentiment(title: str) -> str:
    """
    Very minimal fallback used ONLY if Google API is unavailable.
    """
    t = title.lower()
    obvious_negative = ["fraud", "scam", "default", "bankrupt", "crash", "plunge", "ban", "penalty", "suspend"]
    obvious_positive = ["record high", "all-time high", "profit surge", "strong growth", "rate cut"]
    if any(kw in t for kw in obvious_negative):
        return "negative"
    if any(kw in t for kw in obvious_positive):
        return "positive"
    return "neutral"


def batch_sentiment(articles: list) -> list:
    """
    Enriches a list of article dicts with AI sentiment.
    Batches headlines in groups of 50 for efficiency.
    """
    if not articles:
        return articles

    BATCH_SIZE = 50
    all_titles = [a["title"] for a in articles]

    sentiment_map = {}
    for i in range(0, len(all_titles), BATCH_SIZE):
        batch = all_titles[i:i + BATCH_SIZE]
        batch_result = get_sentiment_ai(tuple(batch))  # tuple for cache hashing
        sentiment_map.update(batch_result)

    for article in articles:
        article["sentiment"] = sentiment_map.get(article["title"], "neutral")

    return articles


def is_priority(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in PRIORITY_KEYWORDS)

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
# FEED FETCHING
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
                "title":    title,
                "link":     link,
                "dt":       parse_dt(entry),
                "priority": is_priority(title),
                "sentiment": "neutral",  # will be filled by AI
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
    # Sort each category by datetime descending
    for cat in results:
        results[cat].sort(key=lambda x: x["dt"], reverse=True)
    return dict(results)


def fetch_circulars(feeds: list, exclude_kw: list) -> list:
    items = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(fetch_feed, url) for url in feeds]
        for future in as_completed(futures):
            for item in future.result():
                t = item["title"].lower()
                if not any(kw in t for kw in exclude_kw):
                    items.append(item)
    items.sort(key=lambda x: x["dt"], reverse=True)
    # Deduplicate by title
    seen = set()
    unique = []
    for item in items:
        if item["title"] not in seen:
            seen.add(item["title"])
            unique.append(item)
    return unique


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
    if s < 60:   return f"{s}s ago"
    if s < 3600: return f"{s//60}m ago"
    if s < 86400:return f"{s//3600}h ago"
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
/* Base */
html, body, [class*="css"] {
    font-family: 'IBM Plex Mono', 'Courier New', monospace !important;
    background-color: #0d1117 !important;
    color: #c9d1d9 !important;
}
.block-container { padding-top: 1rem !important; max-width: 1400px !important; }

/* Header */
.dashboard-header {
    background: linear-gradient(135deg, #161b22 0%, #0d1117 100%);
    border: 1px solid #21262d;
    border-radius: 8px;
    padding: 16px 24px;
    margin-bottom: 16px;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
.dashboard-title {
    font-size: 1.1rem;
    font-weight: 700;
    color: #e6edf3;
    letter-spacing: 0.05em;
}
.dashboard-subtitle {
    font-size: 0.7rem;
    color: #484f58;
    margin-top: 2px;
}
.live-dot {
    width: 8px; height: 8px;
    background: #3fb950;
    border-radius: 50%;
    display: inline-block;
    margin-right: 6px;
    animation: pulse 2s infinite;
}
@keyframes pulse {
    0%,100% { opacity:1; } 50% { opacity:0.3; }
}
.ai-badge {
    background: linear-gradient(135deg, #6e40c9, #388bfd);
    color: white;
    font-size: 0.62rem;
    font-weight: 700;
    padding: 3px 8px;
    border-radius: 20px;
    letter-spacing: 0.05em;
}

/* News Cards */
.news-card {
    background: #161b22;
    border: 1px solid #21262d;
    border-left: 3px solid #21262d;
    border-radius: 6px;
    padding: 10px 14px;
    margin-bottom: 6px;
    transition: border-color 0.2s;
}
.news-card:hover { border-left-color: #388bfd; }
.news-card.priority { border-left-color: #f85149 !important; background: #1a0f0f; }
.news-card.sentiment-positive { border-left-color: #3fb950; background: #0d1a0f; }
.news-card.sentiment-negative { border-left-color: #f85149; background: #1a0d0d; }
.news-card.rbi { border-left-color: #d29922; }
.news-card.nse { border-left-color: #388bfd; }

.card-title {
    font-size: 0.82rem;
    font-weight: 500;
    color: #c9d1d9 !important;
    text-decoration: none !important;
    line-height: 1.4;
    display: block;
    margin-bottom: 6px;
}
.card-title:hover { color: #388bfd !important; }
.card-meta {
    font-size: 0.68rem;
    color: #484f58;
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
}

/* Badges */
.badge-priority { background:#f85149; color:#fff; font-size:0.6rem; font-weight:700; padding:2px 6px; border-radius:3px; }
.badge-positive { background:#1a4a1f; color:#3fb950; font-size:0.6rem; font-weight:700; padding:2px 6px; border-radius:3px; border:1px solid #3fb950; }
.badge-negative { background:#4a1a1a; color:#f85149; font-size:0.6rem; font-weight:700; padding:2px 6px; border-radius:3px; border:1px solid #f85149; }
.badge-manual   { background:#1a2a4a; color:#388bfd; font-size:0.6rem; font-weight:700; padding:2px 6px; border-radius:3px; }
.badge-rbi      { background:#2a1f00; color:#d29922; font-size:0.6rem; font-weight:700; padding:2px 6px; border-radius:3px; border:1px solid #d29922; }
.badge-nse      { background:#001a3a; color:#388bfd; font-size:0.6rem; font-weight:700; padding:2px 6px; border-radius:3px; border:1px solid #388bfd; }
.badge-stock    { background:#2a1a4a; color:#a78bfa; font-size:0.6rem; font-weight:700; padding:2px 6px; border-radius:3px; }
.badge-ai       { background:linear-gradient(135deg,#1a0f3a,#0f1a3a); color:#a78bfa; font-size:0.58rem; font-weight:700; padding:2px 6px; border-radius:3px; border:1px solid #6e40c9; }

/* Category header */
.cat-header {
    font-size: 0.75rem;
    font-weight: 700;
    color: #388bfd;
    letter-spacing: 0.08em;
    padding: 6px 0 10px;
    border-bottom: 1px solid #21262d;
    margin-bottom: 12px;
}
.stock-section-header {
    font-size: 0.78rem;
    font-weight: 700;
    color: #a78bfa;
    letter-spacing: 0.06em;
    padding: 8px 0 4px;
    border-bottom: 1px solid #21262d;
    margin: 12px 0 8px;
}
.stock-sentiment-bar {
    display: flex;
    gap: 16px;
    font-size: 0.68rem;
    color: #484f58;
    margin-bottom: 10px;
    flex-wrap: wrap;
}
.stock-sent-item .pos { color: #3fb950; font-weight: 600; }
.stock-sent-item .neg { color: #f85149; font-weight: 600; }
.stock-sent-item .neu { color: #484f58; font-weight: 600; }

/* AI loading indicator */
.ai-loading {
    font-size: 0.72rem;
    color: #a78bfa;
    padding: 8px 0;
    font-style: italic;
}
</style>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────

now_str = datetime.now(timezone.utc).strftime("%d %b %Y · %H:%M UTC")
api_configured = bool(st.secrets.get("GOOGLE_API_KEY", ""))

st.markdown(f"""
<div class="dashboard-header">
  <div>
    <div class="dashboard-title">
      <span class="live-dot"></span>JM FINANCIAL · RISK INTELLIGENCE DASHBOARD
    </div>
    <div class="dashboard-subtitle">Real-time news · AI-powered sentiment · {now_str}</div>
  </div>
  <div style="display:flex;gap:8px;align-items:center;">
    <span class="ai-badge">{'✦ AI SENTIMENT ON' if api_configured else '⚠ AI SENTIMENT OFF'}</span>
  </div>
</div>
""", unsafe_allow_html=True)

# ─────────────────────────────────────────────
# SIDEBAR — Admin + Controls
# ─────────────────────────────────────────────

with st.sidebar:
    st.markdown("### ⚙️ Controls")
    auto_refresh = st.toggle("Auto Refresh (90s)", value=True)
    if st.button("🔄 Refresh Now", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.markdown("---")
    st.markdown("### 🔐 Admin Panel")
    pwd = st.text_input("Password", type="password")
    is_admin = (pwd == ADMIN_PASSWORD)

    if is_admin:
        st.success("Access granted")
        st.markdown("**Add Internal Headline**")
        new_title = st.text_input("Headline")
        new_link  = st.text_input("Link (optional)", value="#")
        new_cat   = st.selectbox("Category", ["Internal", "Risk Alert", "Compliance"] + list(FEED_SOURCES.keys()))
        if st.button("➕ Add Headline", use_container_width=True):
            if new_title:
                manual = load_manual_headlines()
                manual.append({
                    "title": new_title, "link": new_link,
                    "dt": datetime.now(timezone.utc).isoformat(),
                    "category": new_cat, "manual": True,
                    "priority": is_priority(new_title),
                    "sentiment": "neutral",
                })
                save_manual_headlines(manual)
                st.cache_data.clear()
                st.success("Added!")
                st.rerun()

        manual_items = load_manual_headlines()
        if manual_items:
            st.markdown("**Manage Headlines**")
            for i, item in enumerate(manual_items):
                col1, col2 = st.columns([4, 1])
                col1.markdown(f"<div style='font-size:0.7rem;color:#c9d1d9'>{item['title'][:50]}...</div>", unsafe_allow_html=True)
                if col2.button("🗑", key=f"del_{i}"):
                    manual_items.pop(i)
                    save_manual_headlines(manual_items)
                    st.rerun()

    st.markdown("---")
    if not api_configured:
        st.markdown("""
        <div style='font-size:0.7rem;color:#d29922;background:#1a1400;border:1px solid #d29922;
        border-radius:6px;padding:10px;'>
        ⚠️ <b>AI Sentiment Disabled</b><br><br>
        To enable Gemini AI sentiment, add your Google API key to Streamlit secrets:<br><br>
        <code>GOOGLE_API_KEY = "AIzaSy..."</code><br><br>
        Go to: Streamlit Cloud → Your App → Settings → Secrets
        </div>
        """, unsafe_allow_html=True)

# ─────────────────────────────────────────────
# DATA FETCHING
# ─────────────────────────────────────────────

@st.cache_data(ttl=90, show_spinner=False)
def load_all_data():
    all_data       = fetch_all_feeds(FEED_SOURCES)
    rbi_circulars  = fetch_circulars(RBI_CIRCULAR_FEEDS, EXCLUDE_CIRCULAR_KEYWORDS)
    nse_circulars  = fetch_circulars(NSE_CIRCULAR_FEEDS, EXCLUDE_CIRCULAR_KEYWORDS)

    # Portfolio stocks
    portfolio_data = {}
    port_feeds = {stock: [gn(f"{stock} stock NSE BSE India")] for stock in PORTFOLIO_STOCKS}
    raw_port   = fetch_all_feeds(port_feeds)
    for stock in PORTFOLIO_STOCKS:
        portfolio_data[stock] = raw_port.get(stock, [])

    return all_data, rbi_circulars, nse_circulars, portfolio_data


with st.spinner("📡 Fetching latest news..."):
    all_data, rbi_circulars, nse_circulars, portfolio_data = load_all_data()

# Combine all articles
manual_headlines = load_manual_headlines()
for item in manual_headlines:
    if "dt" in item and isinstance(item["dt"], str):
        try: item["dt"] = datetime.fromisoformat(item["dt"])
        except: item["dt"] = datetime.now(timezone.utc)

all_articles_raw = []
for cat, arts in all_data.items():
    for a in arts:
        all_articles_raw.append({**a, "category": cat, "manual": False})
for item in manual_headlines:
    all_articles_raw.append({**item, "manual": True})

all_articles_raw.sort(key=lambda x: x["dt"] if isinstance(x["dt"], datetime) else datetime.now(timezone.utc), reverse=True)

# ─────────────────────────────────────────────
# AI SENTIMENT — run on all articles at once
# ─────────────────────────────────────────────

with st.spinner("🤖 Analyzing sentiment with Gemini AI..."):
    all_articles = batch_sentiment(all_articles_raw)
    for stock in portfolio_data:
        portfolio_data[stock] = batch_sentiment(portfolio_data[stock])
    rbi_circulars = batch_sentiment(rbi_circulars)
    nse_circulars = batch_sentiment(nse_circulars)

# ─────────────────────────────────────────────
# STATS BAR
# ─────────────────────────────────────────────

total   = len(all_articles)
prio_n  = sum(1 for a in all_articles if a.get("priority"))
pos_n   = sum(1 for a in all_articles if a.get("sentiment") == "positive")
neg_n   = sum(1 for a in all_articles if a.get("sentiment") == "negative")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("📰 Total Headlines", total)
c2.metric("⚡ Priority", prio_n)
c3.metric("🟢 Positive", pos_n)
c4.metric("🔴 Negative", neg_n)
c5.metric("⚪ Neutral", total - pos_n - neg_n)

st.markdown("---")

# (Rest of the rendering functions and tabs remain identical to original)
def render_news_cards(articles: list):
    if not articles:
        st.markdown("<div style='font-family:IBM Plex Mono,monospace; font-size:0.8rem;color:#484f58;padding:20px 0;'>No news found — try Refresh Now.</div>", unsafe_allow_html=True)
        return
    for art in articles:
        is_prio   = art.get("priority", False)
        is_manual = art.get("manual", False)
        sentiment = art.get("sentiment", "neutral")
        cat_label = art.get("category", "")
        card_class = "news-card"
        if is_prio: card_class = "news-card priority"
        elif sentiment == "positive": card_class = "news-card sentiment-positive"
        elif sentiment == "negative": card_class = "news-card sentiment-negative"
        dt_obj = art["dt"]
        if isinstance(dt_obj, str):
            try: dt_obj = datetime.fromisoformat(dt_obj)
            except: dt_obj = datetime.now(timezone.utc)
        ago = time_ago(dt_obj)
        prio_badge = '<span class="badge-priority">⚡ PRIORITY</span>' if is_prio else ""
        manual_badge = '<span class="badge-manual">📢 INTERNAL</span>' if is_manual else ""
        ai_badge = '<span class="badge-ai">✦ AI</span>'
        sent_badge = ""
        if not is_prio and not is_manual:
            if sentiment == "positive": sent_badge = '<span class="badge-positive">▲ POSITIVE</span>'
            elif sentiment == "negative": sent_badge = '<span class="badge-negative">▼ NEGATIVE</span>'
        st.markdown(f'<div class="{card_class}"><a class="card-title" href="{art["link"]}" target="_blank">{art["title"]}</a><div class="card-meta"><span>{ago}</span><span style="color:#30363d">·</span><span>{cat_label}</span>{prio_badge}{manual_badge}{sent_badge}{ai_badge}</div></div>', unsafe_allow_html=True)

def render_circular_cards(articles: list, badge_class: str, card_extra_class: str):
    if not articles:
        st.markdown("<div style='font-family:IBM Plex Mono,monospace; font-size:0.8rem;color:#484f58;padding:20px 0;'>No circulars fetched.</div>", unsafe_allow_html=True)
        return
    for art in articles:
        is_prio = art.get("priority", False)
        card_cls = f"news-card {card_extra_class}" + (" priority" if is_prio else "")
        ago = time_ago(art["dt"])
        prio_badge = '<span class="badge-priority">⚡ PRIORITY</span>' if is_prio else ""
        type_badge = f'<span class="{badge_class}">{"RBI" if "rbi" in card_extra_class else "NSE"} CIRCULAR</span>'
        ai_badge = '<span class="badge-ai">✦ AI</span>'
        sentiment = art.get("sentiment", "neutral")
        sent_badge = f'<span class="badge-positive">▲ POSITIVE</span>' if sentiment == "positive" else (f'<span class="badge-negative">▼ NEGATIVE</span>' if sentiment == "negative" else "")
        st.markdown(f'<div class="{card_cls}"><a class="card-title" href="{art["link"]}" target="_blank">{art["title"]}</a><div class="card-meta"><span>{ago}</span>{type_badge}{sent_badge}{prio_badge}{ai_badge}</div></div>', unsafe_allow_html=True)

def render_portfolio_tab(portfolio_data: dict):
    for stock, articles in portfolio_data.items():
        pos = sum(1 for a in articles if a.get("sentiment") == "positive")
        neg = sum(1 for a in articles if a.get("sentiment") == "negative")
        neu = len(articles) - pos - neg
        st.markdown(f'<div class="stock-section-header">📌 {stock.upper()}</div><div class="stock-sentiment-bar"><div class="stock-sent-item">Headlines: <span style="color:#c9d1d9;font-weight:600;">{len(articles)}</span></div><div class="stock-sent-item">🟢 Positive: <span class="pos">{pos}</span></div><div class="stock-sent-item">🔴 Negative: <span class="neg">{neg}</span></div><div class="stock-sent-item">⚪ Neutral: <span class="neu">{neu}</span></div></div>', unsafe_allow_html=True)
        for art in articles:
            is_prio = art.get("priority", False)
            sentiment = art.get("sentiment", "neutral")
            card_class = "news-card priority" if is_prio else ("news-card sentiment-positive" if sentiment == "positive" else ("news-card sentiment-negative" if sentiment == "negative" else "news-card"))
            ago = time_ago(art["dt"])
            stock_badge = f'<span class="badge-stock">{stock.upper()}</span>'
            sent_badge = f'<span class="badge-positive">▲ POSITIVE</span>' if sentiment == "positive" and not is_prio else (f'<span class="badge-negative">▼ NEGATIVE</span>' if sentiment == "negative" and not is_prio else "")
            st.markdown(f'<div class="{card_class}"><a class="card-title" href="{art["link"]}" target="_blank">{art["title"]}</a><div class="card-meta"><span>{ago}</span>{stock_badge}{sent_badge}{prio_badge if is_prio else ""}<span class="badge-ai">✦ AI</span></div></div>', unsafe_allow_html=True)

tab_all, tab_priority, tab_portfolio, tab_rbi, tab_nse, *cat_tabs = st.tabs(["All News", "🔴 Priority", "📂 Portfolio Stocks", "🏛️ RBI Circulars", "🔵 NSE Circulars"] + list(FEED_SOURCES.keys()))

with tab_all: render_news_cards(all_articles)
with tab_priority: render_news_cards([a for a in all_articles if a.get("priority")])
with tab_portfolio: render_portfolio_tab(portfolio_data)
with tab_rbi: render_circular_cards(rbi_circulars, "badge-rbi", "rbi")
with tab_nse: render_circular_cards(nse_circulars, "badge-nse", "nse")
for tab, cat in zip(cat_tabs, FEED_SOURCES.keys()):
    with tab:
        arts = [{**a, "category": cat, "manual": False} for a in all_data.get(cat, [])]
        render_news_cards(batch_sentiment(arts))

if auto_refresh:
    placeholder = st.empty()
    for remaining in range(AUTO_REFRESH_SECONDS, 0, -1):
        placeholder.markdown(f"<div style='font-family:IBM Plex Mono,monospace; font-size:0.68rem;color:#484f58;text-align:right;padding-top:10px;'>Next refresh in {remaining}s</div>", unsafe_allow_html=True)
        time.sleep(1)
    st.cache_data.clear()
    st.rerun()
