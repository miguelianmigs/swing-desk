"""
Swing scanner: a free Finviz-style screen for small tech names waking up.
Finds candidates, builds an entry/stop/target and share size for each,
and sends them to your phone via Telegram (or prints them).

It does NOT place orders. You review the alert and place the trade yourself.
"""
import os
import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
import pandas as pd
import yfinance as yf

# ---------- Your rules ----------
ACCOUNT = 243.00          # account value in $
RISK_PCT = 2.0            # max $ risk per trade, as % of account
MAX_POSITION_PCT = 40.0   # never put more than this % of the account in one stock
MIN_PRICE, MAX_PRICE = 2.0, 20.0
MIN_AVG_VOLUME = 500_000
MIN_REL_VOLUME = 1.5      # today's volume vs 20-day average (partial day counts low, so 1.5 midday ~ 2+ by close)
MAX_STOP_PCT = 12.0       # stop can't be further than this below entry
REWARD_RISK = 2.0         # target = entry + 2x the risk
SECTORS = {"Technology", "Communication Services"}
TOP_N = 5

# Yahoo's built-in screens used to build the universe each run
YAHOO_SCREENS = ["small_cap_gainers", "aggressive_small_caps",
                 "growth_technology_stocks", "most_actives"]
EXTRA_TICKERS_FILE = "watchlist.txt"   # one ticker per line, always scanned


def build_universe():
    tickers = set()
    for name in YAHOO_SCREENS:
        try:
            res = yf.screen(name, count=100)
            tickers.update(q["symbol"] for q in res.get("quotes", []) if "symbol" in q)
        except Exception as e:
            print(f"screen {name} failed: {e}")
    if os.path.exists(EXTRA_TICKERS_FILE):
        with open(EXTRA_TICKERS_FILE) as f:
            tickers.update(l.strip().upper() for l in f if l.strip() and not l.startswith("#"))
    # US common stocks only (skip ADRs with dots, warrants, units)
    return sorted(t for t in tickers if t.isalpha() and len(t) <= 5)


def setup_for(df):
    """df: daily OHLCV for one ticker, oldest first. Returns a setup dict or None."""
    df = df.dropna()
    if len(df) < 60:
        return None
    close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"]
    price = float(close.iloc[-1])
    avg_vol = float(vol.iloc[-21:-1].mean())
    if avg_vol <= 0:
        return None
    rvol = float(vol.iloc[-1]) / avg_vol
    sma50 = float(close.iloc[-50:].mean())
    change = price / float(close.iloc[-2]) - 1

    if not (MIN_PRICE <= price <= MAX_PRICE): return None
    if avg_vol < MIN_AVG_VOLUME: return None
    if rvol < MIN_REL_VOLUME: return None
    if price <= sma50 or change <= 0: return None

    entry = round(float(high.iloc[-1]) + 0.01, 2)             # buy-stop just above today's high
    swing_low = float(low.iloc[-5:].min()) - 0.01             # below the last 5 days' low
    stop = round(max(swing_low, entry * (1 - MAX_STOP_PCT / 100)), 2)
    if stop >= entry:
        return None
    risk_ps = entry - stop
    target = round(entry + REWARD_RISK * risk_ps, 2)

    risk_budget = ACCOUNT * RISK_PCT / 100
    max_cost = ACCOUNT * MAX_POSITION_PCT / 100
    shares = math.floor(min(risk_budget / risk_ps, max_cost / entry))
    if shares < 1:
        return None
    return dict(price=price, change=change * 100, rvol=rvol, entry=entry, stop=stop,
                target=target, shares=shares, cost=shares * entry,
                risk=shares * risk_ps, reward=shares * (target - entry),
                stop_pct=risk_ps / entry * 100)


def scan():
    universe = build_universe()
    print(f"Scanning {len(universe)} tickers")
    if not universe:
        return []
    data = yf.download(universe, period="6mo", interval="1d", group_by="ticker",
                       auto_adjust=True, threads=True, progress=False)
    found = []
    for t in universe:
        try:
            df = data[t] if len(universe) > 1 else data
            s = setup_for(df)
        except Exception:
            continue
        if s:
            s["ticker"] = t
            found.append(s)
    found.sort(key=lambda s: s["rvol"], reverse=True)

    picks = []
    for s in found:                      # sector check only on the few that passed
        try:
            info = yf.Ticker(s["ticker"]).info
            s["sector"] = info.get("sector", "")
            s["name"] = info.get("shortName", s["ticker"])
        except Exception:
            continue
        if s["sector"] in SECTORS:
            picks.append(s)
        if len(picks) >= TOP_N:
            break
    return picks


def format_alert(picks):
    if not picks:
        return "Swing scan: no setups passed your rules today. No trade is a fine trade."
    lines = [f"Swing scan: {len(picks)} setup(s)  |  risk ${ACCOUNT*RISK_PCT/100:.2f}/trade\n"]
    for s in picks:
        lines.append(
            f"{s['ticker']} ({s['name']}) ${s['price']:.2f}, +{s['change']:.1f}%, {s['rvol']:.1f}x vol\n"
            f"  Buy-stop {s['entry']:.2f} | Stop {s['stop']:.2f} (-{s['stop_pct']:.1f}%) | Target {s['target']:.2f}\n"
            f"  {s['shares']} sh = ${s['cost']:.2f} | risk ${s['risk']:.2f} | reward ${s['reward']:.2f}\n"
            f"  Check the news before you buy."
        )
    return "\n".join(lines)


def send(text):
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    print(text)
    if token and chat:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": text}, timeout=20)
        print("Telegram:", r.status_code)


DATA_FILE = "docs/data.json"
HISTORY_KEEP = 60   # scans kept on the dashboard (~6 weeks at 2 a day)


def save_dashboard(picks):
    """Write the results the dashboard page reads. Runs every scan."""
    try:
        with open(DATA_FILE) as f:
            data = json.load(f)
    except Exception:
        data = {"history": []}
    now = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
    clean = [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in p.items()} for p in picks]
    data.update(updated=now, account=ACCOUNT, risk_pct=RISK_PCT, latest=clean)
    data["history"] = ([{"date": now, "picks": clean}] + data.get("history", []))[:HISTORY_KEEP]
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=1)


if __name__ == "__main__":
    picks = scan()
    save_dashboard(picks)
    send(format_alert(picks))
