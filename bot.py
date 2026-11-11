"""
Daniel's market alerts bot -> Telegram.

What it does on every run:
1. Universe: the ~100 largest US stocks, split evenly across the 11 GICS
   sectors (top N by market cap in each sector). Refreshed weekly.
2. Price moves: during US market hours, alerts when a stock is up/down
   3% or more on the day, and again if it reaches 5%. Each level fires once per day.
3. News: every new news item that mentions any of the stocks.

Secrets come from environment variables: TELEGRAM_TOKEN, CHAT_ID.
State (what was already sent) is kept in state.json.
"""

import datetime as dt
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import yfinance as yf

# ---------------- settings ----------------
TOTAL_STOCKS = 100
THRESHOLDS = [3.0, 5.0]          # % daily move levels that trigger an alert
UNIVERSE_REFRESH_DAYS = 7        # rebuild the top-100 list once a week
MAX_SEEN_NEWS = 5000             # how many news ids to remember
MAX_SUMMARY_CHARS = 600          # max length of each news summary
STATE_FILE = "state.json"
NY = ZoneInfo("America/New_York")

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

# Large-cap candidates per GICS sector. The bot ranks them by live market cap
# and keeps the biggest ones in each sector. Order of sectors matters only for
# who gets the leftover slot (100 / 11 = 9 each, +1 to the first sector).
CANDIDATES = {
    "Information Technology": ["AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "ADBE", "AMD", "CSCO", "ACN",
                               "IBM", "INTU", "TXN", "QCOM", "NOW", "AMAT", "MU", "PLTR", "ANET", "LRCX"],
    "Communication Services": ["GOOGL", "META", "NFLX", "TMUS", "DIS", "VZ", "T", "CMCSA", "CHTR", "EA",
                               "TTWO", "WBD", "LYV", "OMC"],
    "Consumer Discretionary": ["AMZN", "TSLA", "HD", "MCD", "BKNG", "TJX", "LOW", "SBUX", "NKE", "ORLY",
                               "CMG", "MAR", "GM", "ABNB", "RCL", "AZO", "F"],
    "Financials": ["BRK-B", "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "AXP", "SPGI",
                   "BLK", "C", "SCHW", "PGR", "CB", "MMC", "BX"],
    "Health Care": ["LLY", "UNH", "JNJ", "ABBV", "MRK", "TMO", "ABT", "ISRG", "AMGN", "DHR",
                    "PFE", "GILD", "BSX", "SYK", "VRTX", "MDT", "BMY", "CVS", "ELV"],
    "Industrials": ["GE", "CAT", "RTX", "UBER", "HON", "BA", "UNP", "ETN", "DE", "LMT",
                    "ADP", "GEV", "PH", "TT", "WM", "UPS", "GD", "NOC"],
    "Consumer Staples": ["WMT", "COST", "PG", "KO", "PEP", "PM", "MO", "MDLZ", "CL", "TGT",
                         "KMB", "KDP", "MNST", "GIS", "KR", "SYY", "STZ"],
    "Energy": ["XOM", "CVX", "COP", "EOG", "SLB", "WMB", "KMI", "PSX", "MPC", "OKE",
               "OXY", "VLO", "BKR", "FANG"],
    "Utilities": ["NEE", "SO", "DUK", "CEG", "VST", "AEP", "SRE", "D", "EXC", "PCG",
                  "XEL", "PEG", "ED"],
    "Real Estate": ["PLD", "AMT", "EQIX", "WELL", "SPG", "O", "PSA", "DLR", "CCI", "CBRE",
                    "VICI", "EXR", "AVB"],
    "Materials": ["LIN", "SHW", "APD", "ECL", "FCX", "NEM", "CTVA", "DOW", "NUE", "MLM",
                  "VMC", "PPG", "DD", "IFF"],
}

SECTOR_HE = {
    "Information Technology": "טכנולוגיה",
    "Communication Services": "תקשורת",
    "Consumer Discretionary": "צריכה מחזורית",
    "Financials": "פיננסים",
    "Health Care": "בריאות",
    "Industrials": "תעשייה",
    "Consumer Staples": "צריכה בסיסית",
    "Energy": "אנרגיה",
    "Utilities": "תשתיות",
    "Real Estate": "נדל\"ן",
    "Materials": "חומרים",
}


# ---------------- helpers ----------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def send(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, data=data, timeout=20) as r:
                return json.load(r).get("ok", False)
        except urllib.error.HTTPError as e:
            if e.code == 429:          # Telegram rate limit
                time.sleep(5)
                continue
            print("telegram error", e.code, e.read()[:300])
            return False
        except Exception as e:
            print("telegram error", e)
            time.sleep(2)
    return False


def send_lines(header, lines, limit=3900):
    """Send many lines, split into several messages under Telegram's size limit."""
    chunk = header
    for line in lines:
        if len(chunk) + len(line) + 1 > limit:
            send(chunk)
            time.sleep(1)
            chunk = header
        chunk += "\n" + line
    if chunk != header:
        send(chunk)


def market_cap(ticker):
    try:
        fi = yf.Ticker(ticker).fast_info
        val = getattr(fi, "market_cap", None)
        if val is None and hasattr(fi, "get"):
            val = fi.get("marketCap")
        return float(val or 0)
    except Exception as e:
        print("market cap failed", ticker, e)
        return 0.0


# ---------------- 1. universe ----------------
def build_universe():
    sectors = list(CANDIDATES)
    base, extra = divmod(TOTAL_STOCKS, len(sectors))
    universe = {}
    for i, sector in enumerate(sectors):
        quota = base + (1 if i < extra else 0)
        caps = sorted(((t, market_cap(t)) for t in CANDIDATES[sector]),
                      key=lambda x: x[1], reverse=True)
        for t, cap in caps[:quota]:
            if cap > 0:
                universe[t] = sector
    return universe


def refresh_universe_if_needed(state):
    info = state.get("universe")
    today = dt.date.today()
    if info:
        age = (today - dt.date.fromisoformat(info["date"])).days
        if age < UNIVERSE_REFRESH_DAYS and info.get("tickers"):
            return info["tickers"]

    universe = build_universe()
    if not universe:
        return info["tickers"] if info else {}
    state["universe"] = {"date": today.isoformat(), "tickers": universe}

    lines = []
    for sector in CANDIDATES:
        tks = [t for t, s in universe.items() if s == sector]
        if tks:
            lines.append(f"<b>{SECTOR_HE[sector]}</b>: {', '.join(tks)}")
    send_lines(f"📋 <b>רשימת המעקב עודכנה ({len(universe)} מניות)</b>", lines)
    return universe


# ---------------- 2. price moves ----------------
def check_moves(state, universe):
    now = dt.datetime.now(NY)
    if now.weekday() >= 5:
        return
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=10, second=0, microsecond=0)
    if not (market_open <= now <= market_close):
        return

    today = now.date().isoformat()
    alerts = state.get("alerts")
    if not alerts or alerts.get("date") != today:
        alerts = {"date": today, "levels": {}}
        state["alerts"] = alerts
    levels = alerts["levels"]

    tickers = list(universe)
    data = yf.download(tickers, period="5d", interval="1d",
                       progress=False, auto_adjust=False, threads=True)
    closes = data["Close"]

    lines = []
    for t in tickers:
        if t not in closes:
            continue
        series = closes[t].dropna()
        if len(series) < 2 or series.index[-1].date() != now.date():
            continue  # no trading today (holiday) or missing data
        last, prev = float(series.iloc[-1]), float(series.iloc[-2])
        pct = (last / prev - 1) * 100
        hit = max((th for th in THRESHOLDS if abs(pct) >= th), default=0)
        if hit > levels.get(t, 0):
            levels[t] = hit
            arrow = "🟢" if pct > 0 else "🔴"
            strong = arrow * 2 if hit >= THRESHOLDS[-1] else arrow
            sector = SECTOR_HE.get(universe[t], universe[t])
            lines.append(f"{strong} <b>{t}</b> {pct:+.2f}% | ${last:,.2f} ({sector})")

    if lines:
        send_lines("⚡ <b>תנועה חדה</b>", lines)


# ---------------- 3. news ----------------
def parse_news_item(item):
    c = item.get("content") or item          # newer / older yfinance formats
    title = c.get("title")
    link = ((c.get("canonicalUrl") or {}).get("url")
            or (c.get("clickThroughUrl") or {}).get("url")
            or c.get("link"))
    nid = item.get("id") or c.get("id") or c.get("uuid") or link
    publisher = (c.get("provider") or {}).get("displayName") or c.get("publisher") or ""
    summary = c.get("summary") or c.get("description") or ""
    summary = re.sub(r"<[^>]+>", " ", summary)          # strip any HTML tags
    summary = re.sub(r"\s+", " ", html.unescape(summary)).strip()
    if summary == (title or "").strip():
        summary = ""
    if len(summary) > MAX_SUMMARY_CHARS:
        cut = summary[:MAX_SUMMARY_CHARS]
        summary = cut[:cut.rfind(" ")] + "…" if " " in cut else cut + "…"
    return nid, title, link, publisher, summary


def check_news(state, universe):
    seen_list = state.get("seen_news", [])
    seen = set(seen_list)
    new_items = {}  # id -> [title, link, publisher, summary, [tickers]]

    for t in universe:
        try:
            items = yf.Ticker(t).news or []
        except Exception as e:
            print("news failed", t, e)
            continue
        for it in items:
            nid, title, link, pub, summary = parse_news_item(it)
            if not nid or not title or nid in seen:
                continue
            if nid in new_items:
                if t not in new_items[nid][4]:
                    new_items[nid][4].append(t)
            else:
                new_items[nid] = [title, link, pub, summary, [t]]

    state["seen_news"] = (seen_list + list(new_items))[-MAX_SEEN_NEWS:]

    # First run: just remember existing news so you don't get flooded with old items.
    if not state.get("news_seeded"):
        state["news_seeded"] = True
        send("✅ הבוט פעיל. מעכשיו תקבל התראות על תנועות חדות וחדשות על המניות ברשימה.")
        return

    lines = []
    for title, link, pub, summary, tks in new_items.values():
        source = f" <i>({html.escape(pub)})</i>" if pub else ""
        block = f"🔹 <b>{', '.join(tks)}</b> | <b>{html.escape(title)}</b>{source}"
        if summary:
            block += f"\n{html.escape(summary)}"
        if link:
            block += f"\n<a href=\"{html.escape(link)}\">לכתבה המלאה ←</a>"
        lines.append(block + "\n")
    if lines:
        send_lines("📰 <b>חדשות</b>\n", lines)


# ---------------- main ----------------
def main():
    state = load_state()
    universe = refresh_universe_if_needed(state)
    if not universe:
        print("no universe, aborting")
        save_state(state)
        return
    for step in (check_moves, check_news):
        try:
            step(state, universe)
        except Exception as e:
            print(f"{step.__name__} failed:", e)
    save_state(state)


if __name__ == "__main__":
    main()
