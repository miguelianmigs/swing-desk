"""
Swing scanner: finds small tech stocks breaking out early, builds the trade
(entry, stop, target, breakeven trigger, share size), texts it to Telegram,
updates the dashboard, and keeps a scorecard of how past alerts played out.

It does NOT place orders. You review the alert and decide.
"""
import os
import json
import math
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import yfinance as yf

# ---------- Your account ----------
ACCOUNT = 243.00          # account value in $ (update as it grows)
RISK_PCT = 2.0            # max $ risk per trade, as % of account
MAX_POSITION_PCT = 40.0   # never put more than this % of the account in one stock

# ---------- Stock filters ----------
MIN_PRICE, MAX_PRICE = 2.0, 20.0
MIN_AVG_VOLUME = 500_000
MIN_REL_VOLUME = 1.5      # today's volume vs 20-day average (partial day counts low)
SECTORS = {"Technology", "Communication Services"}

# Early-stage filters: catch the breakout, not the spike
MIN_DAY_GAIN, MAX_DAY_GAIN = 2.0, 12.0   # % move today
MAX_5DAY_GAIN = 20.0       # skip if already up more than this over 5 days
MAX_ABOVE_SMA20 = 15.0     # skip if price is stretched this % above its 20-day average
BREAKOUT_BUFFER = 3.0      # price must be within this % of (or above) the prior 20-day high

# ---------- Trade plan ----------
MAX_STOP_PCT = 10.0       # skip setups whose natural stop is further than this below entry
REWARD_RISK = 2.0         # target = entry + 2x the risk
TOP_N = 5

# ---------- Safety filters ----------
MARKET_TICKER = "QQQ"     # Nasdaq 100 fund; market is "healthy" when above its 50-day average
EARNINGS_DAYS = 7         # skip stocks reporting earnings within this many days

# ---------- Scorecard ----------
FILL_WINDOW_DAYS = 1      # buy-stop must trigger within this many trading days, or it's "no fill"
MAX_HOLD_DAYS = 15        # trading days before an open trade is closed as "timeout"

# ---------- Early-theme scan (weekly, Monday morning + any manual run) ----------
THEMES_FILE = "themes.txt"
THEME_BUCKET = 100.0       # $ set aside for early-catch bets
THEME_BETS = 5             # split the bucket into this many starter positions
THEME_MIN_PRICE, THEME_MAX_PRICE = 1.0, 5.0     # the "catch it under $5" zone
THEME_MIN_MCAP = 150e6     # skip tiny shells: real companies only
THEME_EXTRA_SCREENS = ["aggressive_small_caps", "small_cap_gainers", "undervalued_growth_stocks"]
THEME_MIN_AVG_VOLUME = 200_000
THEME_MAX_OFF_HIGH = 40.0  # must still be at least this % below its 1-year high (early, not extended)
THEME_CROSS_DAYS = 10      # price reclaimed its 50-day average within this many days
THEME_VOL_PICKUP = 1.3     # 10-day volume vs 60-day volume
THEME_TOP_N = 5
THEME_HALF_AT = 100.0      # sell half once the position is up this %, let the rest run
THEME_MIN_GRADE = 3        # early-catch picks need at least 3 of 5 financial checks (grade C)
COIL_RANGE_PCT = 15.0      # "coiling": last 10 days traded in a range tighter than this %
COIL_VOL_DRY = 0.8         # "coiling": 10-day volume below this x the 60-day (sellers dried up)

YAHOO_SCREENS = ["small_cap_gainers", "aggressive_small_caps",
                 "growth_technology_stocks", "most_actives"]
EXTRA_TICKERS_FILE = "watchlist.txt"
DATA_FILE = "docs/data.json"
HISTORY_KEEP = 60
TRACK_KEEP = 300

ET = ZoneInfo("America/New_York")


# ================= Universe & data =================
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
    return sorted(t for t in tickers if t.isalpha() and len(t) <= 5)


def download(tickers):
    if not tickers:
        return {}
    data = yf.download(sorted(tickers), period="6mo", interval="1d", group_by="ticker",
                       auto_adjust=True, threads=True, progress=False)
    out = {}
    for t in tickers:
        try:
            df = data[t] if len(tickers) > 1 else data
            df = df.dropna()
            if len(df):
                out[t] = df
        except Exception:
            pass
    return out


# ================= Market filter =================
def market_status(frames):
    df = frames.get(MARKET_TICKER)
    if df is None or len(df) < 50:
        return {"ok": True, "note": "Market check unavailable"}
    price = float(df["Close"].iloc[-1])
    sma50 = float(df["Close"].iloc[-50:].mean())
    ok = price > sma50
    pct = (price / sma50 - 1) * 100
    word = "healthy" if ok else "weak"
    return {"ok": ok, "note": f"Market {word}: {MARKET_TICKER} {pct:+.1f}% vs its 50-day average"}


# ================= Setup logic =================
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
    sma20 = float(close.iloc[-20:].mean())
    change = price / float(close.iloc[-2]) - 1
    gain_5d = price / float(close.iloc[-6]) - 1
    prior_high = float(high.iloc[-21:-1].max())

    if not (MIN_PRICE <= price <= MAX_PRICE): return None
    if avg_vol < MIN_AVG_VOLUME: return None
    if rvol < MIN_REL_VOLUME: return None
    if price <= sma50: return None
    if not (MIN_DAY_GAIN <= change * 100 <= MAX_DAY_GAIN): return None
    if gain_5d * 100 > MAX_5DAY_GAIN: return None
    if (price / sma20 - 1) * 100 > MAX_ABOVE_SMA20: return None
    if price < prior_high * (1 - BREAKOUT_BUFFER / 100): return None

    entry = round(float(high.iloc[-1]) + 0.01, 2)
    stop = round(float(low.iloc[-5:].min()) - 0.01, 2)
    if stop >= entry or (entry - stop) / entry * 100 > MAX_STOP_PCT:
        return None
    risk_ps = entry - stop
    target = round(entry + REWARD_RISK * risk_ps, 2)
    breakeven = round(entry + (target - entry) / 2, 2)   # halfway: move stop up to entry here

    risk_budget = ACCOUNT * RISK_PCT / 100
    shares = math.floor(min(risk_budget / risk_ps, ACCOUNT * MAX_POSITION_PCT / 100 / entry))
    if shares < 1:
        return None
    return dict(price=price, change=change * 100, rvol=rvol, entry=entry, stop=stop,
                target=target, breakeven=breakeven, shares=shares, cost=shares * entry,
                risk=shares * risk_ps, reward=shares * (target - entry),
                stop_pct=risk_ps / entry * 100)


def earnings_within(tk, days):
    """Returns (date_str or None, True if within `days`)."""
    try:
        cal = tk.calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if not dates:
            return None, False
        d = dates[0]
        if isinstance(d, datetime):
            d = d.date()
        today = datetime.now(ET).date()
        return d.isoformat(), 0 <= (d - today).days <= days
    except Exception:
        return None, False


def find_setups(frames):
    found = []
    for t, df in frames.items():
        if t == MARKET_TICKER:
            continue
        try:
            s = setup_for(df)
        except Exception:
            s = None
        if s:
            s["ticker"] = t
            found.append(s)
    found.sort(key=lambda s: s["rvol"], reverse=True)

    picks, skipped = [], []
    for s in found:
        try:
            tk = yf.Ticker(s["ticker"])
            info = tk.info
        except Exception:
            continue
        s["sector"] = info.get("sector", "")
        s["name"] = info.get("shortName", s["ticker"])
        if s["sector"] not in SECTORS:
            continue
        s["earnings"], soon = earnings_within(tk, EARNINGS_DAYS)
        if soon:
            skipped.append(f"{s['ticker']} (earnings {s['earnings']})")
            continue
        s["news"] = f"https://finance.yahoo.com/quote/{s['ticker']}/news"
        try:
            s.update(fundamentals(tk, info))
        except Exception:
            pass
        picks.append(s)
        if len(picks) >= TOP_N:
            break
    return picks, skipped


# ================= Scorecard =================
def update_scorecard(tracked, frames):
    """Replays each tracked alert against daily prices after its alert date."""
    for a in tracked:
        if a["status"] not in ("waiting", "open"):
            continue
        df = frames.get(a["ticker"])
        if df is None:
            continue
        bars = df[df.index.date > date.fromisoformat(a["date"])]
        entry, stop0, target, be = a["entry"], a["stop"], a["target"], a["breakeven"]
        fill = a.get("fill")
        stop = a.get("live_stop", stop0)
        days_in = 0
        for i, (idx, b) in enumerate(bars.iterrows()):
            o, h, l, c = float(b["Open"]), float(b["High"]), float(b["Low"]), float(b["Close"])
            d = idx.date().isoformat()
            if fill is None:
                if i >= FILL_WINDOW_DAYS:
                    a["status"] = "no_fill"
                    break
                if h < entry:
                    continue
                fill = max(o, entry)            # gapped up = filled at the open
                a.update(fill=round(fill, 2), filled=d, status="open")
            days_in += 1
            risk = fill - stop0
            # conservative: if stop and target both touched the same day, count the stop
            if l <= stop:
                exit_px = min(o, stop) if o < stop else stop
                a.update(status="loss" if exit_px < fill - 0.005 else "breakeven",
                         exit=round(exit_px, 2), closed=d)
                break
            if h >= target:
                a.update(status="win", exit=round(max(o, target), 2), closed=d)
                break
            if h >= be and stop < fill:
                stop = fill                     # breakeven rule
                a["live_stop"] = round(stop, 2)
            if days_in >= MAX_HOLD_DAYS:
                a.update(status="timeout", exit=round(c, 2), closed=d)
                break
        if a.get("exit") is not None and a.get("fill"):
            a["r"] = round((a["exit"] - a["fill"]) / (a["fill"] - a["stop"]), 2)
    return tracked


def scorecard_summary(tracked):
    done = [a for a in tracked if a["status"] in ("win", "loss", "breakeven", "timeout")]
    wins = [a for a in done if a.get("r", 0) > 0]
    avg_r = sum(a.get("r", 0) for a in done) / len(done) if done else 0
    return {
        "alerts": len(tracked),
        "filled": len([a for a in tracked if a.get("fill")]),
        "closed": len(done),
        "wins": len(wins),
        "win_rate": round(len(wins) / len(done) * 100) if done else None,
        "avg_r": round(avg_r, 2),
        "per_trade": round(avg_r * ACCOUNT * RISK_PCT / 100, 2),
        "open": len([a for a in tracked if a["status"] == "open"]),
        "waiting": len([a for a in tracked if a["status"] == "waiting"]),
    }


# ================= Fundamentals =================
def _row(df, names):
    for n in names:
        if df is not None and n in df.index:
            vals = df.loc[n].dropna()
            if len(vals):
                return vals
    return None


def fundamentals(tk, info):
    """Scores 5 financial health checks. Returns grade A-D, score, strengths and red flags."""
    score, good, bad = 0, [], []
    g = info.get("revenueGrowth")
    if g is not None:
        if g >= 0.10:
            score += 1; good.append(f"revenue +{g*100:.0f}%")
        elif g < 0:
            bad.append(f"revenue {g*100:.0f}%")
    gm = info.get("grossMargins")
    if gm is not None:
        if gm >= 0.30:
            score += 1; good.append(f"{gm*100:.0f}% margins")
        elif gm < 0.15:
            bad.append(f"thin margins {gm*100:.0f}%")
    cash, debt = info.get("totalCash") or 0, info.get("totalDebt") or 0
    if cash >= debt:
        score += 1; good.append("more cash than debt")
    elif debt > 3 * max(cash, 1):
        bad.append(f"heavy debt ${debt/1e6:,.0f}M vs ${cash/1e6:,.0f}M cash")
    fcf = info.get("freeCashflow")
    if fcf is not None:
        if fcf >= 0:
            score += 1; good.append("cash-flow positive")
        else:
            runway = cash / -fcf if fcf else 0
            if runway >= 2:
                score += 1; good.append(f"{runway:.0f}+ yrs of cash runway")
            else:
                bad.append(f"burning cash, ~{runway:.1f} yrs runway")
    try:
        sh = _row(tk.quarterly_balance_sheet, ["Ordinary Shares Number", "Share Issued"])
        if sh is not None and len(sh) >= 4:
            change = float(sh.iloc[0]) / float(sh.iloc[min(4, len(sh) - 1)]) - 1
            if change <= 0.105:
                score += 1
            else:
                bad.append(f"shares up {change*100:.0f}% (dilution)")
    except Exception:
        pass
    grade = {5: "A", 4: "B", 3: "C"}.get(score, "D")
    return {"grade": grade, "fscore": score, "good": good[:3], "bad": bad[:3]}


def fund_line(p):
    if "grade" not in p:
        return ""
    parts = ", ".join(p["good"]) or "no clear strengths"
    flags = f" | watch out: {', '.join(p['bad'])}" if p["bad"] else ""
    return f"  Financials {p['grade']} ({p['fscore']}/5): {parts}{flags}\n"


# ================= Early-theme scan =================
def load_themes():
    themes = {}
    if not os.path.exists(THEMES_FILE):
        return themes
    with open(THEMES_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            name, tickers = line.split(":", 1)
            for t in tickers.split(","):
                t = t.strip().upper()
                if t.isalpha():
                    themes[t] = name.strip()
    return themes


def theme_setup(df):
    """Beaten-down stock turning up from a long base.
    Returns dict with status "pick" (all signals), "watch" (close), or None."""
    df = df.dropna()
    if len(df) < 120:
        return None
    close, low, vol = df["Close"], df["Low"], df["Volume"]
    price = float(close.iloc[-1])
    if not (THEME_MIN_PRICE <= price <= THEME_MAX_PRICE):
        return None
    if float(vol.iloc[-60:].mean()) < THEME_MIN_AVG_VOLUME:
        return None
    year_high = float(df["High"].max())
    off_high = (1 - price / year_high) * 100
    if off_high < THEME_MAX_OFF_HIGH:
        return None                                    # already ran: not early anymore
    sma50 = close.rolling(50).mean()
    s_now = float(sma50.iloc[-1])
    was_below = (close.iloc[-THEME_CROSS_DAYS - 1:-1] < sma50.iloc[-THEME_CROSS_DAYS - 1:-1]).any()
    not_falling = s_now >= float(sma50.iloc[-11]) * 0.98
    reclaimed = price > s_now and was_below and not_falling
    near = price >= s_now * 0.95 and not_falling      # within 5% of the 50-day, or above
    vol_pickup = float(vol.iloc[-10:].mean()) / float(vol.iloc[-60:].mean())
    buyers = vol_pickup >= THEME_VOL_PICKUP

    if reclaimed and buyers:
        status, missing = "pick", ""
    elif reclaimed:
        status, missing = "watch", "waiting for volume to pick up"
    elif buyers and near:
        status = "watch"
        missing = ("needs to close above its 50-day avg "
                   f"({s_now:.2f})" if price <= s_now else "needs a fresh push after holding above its 50-day")
    else:
        hi10, lo10 = float(df["High"].iloc[-10:].max()), float(low.iloc[-10:].min())
        tight = (hi10 / lo10 - 1) * 100 <= COIL_RANGE_PCT
        dry = vol_pickup <= COIL_VOL_DRY
        if near and tight and dry:
            status = "watch"
            missing = (f"coiling: tight range, sellers dried up. Trigger = close above "
                       f"{max(hi10, s_now):.2f} on heavy volume")
        else:
            return None
    base_low = round(float(low.iloc[-60:].min()), 2)
    return dict(status=status, missing=missing, price=round(price, 2), off_high=round(off_high, 1),
                vol_pickup=round(vol_pickup, 1), sma50=round(s_now, 2), base_low=base_low,
                cut_pct=round((1 - base_low / price) * 100, 1),
                half_at=round(price * (1 + THEME_HALF_AT / 100), 2),
                bet=round(THEME_BUCKET / THEME_BETS, 2))


def run_theme_scan():
    themes = load_themes()
    for name in THEME_EXTRA_SCREENS:          # also hunt beyond your theme list
        try:
            res = yf.screen(name, count=100)
            for q in res.get("quotes", []):
                t = q.get("symbol", "")
                if t.isalpha() and len(t) <= 5 and t not in themes:
                    themes[t] = "Market-wide"
        except Exception as e:
            print(f"theme screen {name} failed: {e}")
    if not themes:
        return [], 0
    data = yf.download(sorted(themes), period="1y", interval="1d", group_by="ticker",
                       auto_adjust=True, threads=True, progress=False)
    picks = []
    for t, theme in themes.items():
        try:
            df = data[t] if len(themes) > 1 else data
            s = theme_setup(df)
        except Exception:
            s = None
        if s:
            s.update(ticker=t, theme=theme,
                     news=f"https://finance.yahoo.com/quote/{t}/news")
            picks.append(s)
    picks.sort(key=lambda p: (p["status"] != "pick", p["theme"] == "Market-wide", -p["vol_pickup"]))

    final, watch = [], []
    for p in picks:                           # quality check only on the few that passed
        bucket = final if p["status"] == "pick" else watch
        if len(bucket) >= THEME_TOP_N:
            continue
        try:
            tk = yf.Ticker(p["ticker"])
            info = tk.info
        except Exception:
            continue
        mcap = info.get("marketCap") or 0
        if mcap < THEME_MIN_MCAP:
            continue
        p.update(fundamentals(tk, info))
        if p["status"] == "pick" and p["fscore"] < THEME_MIN_GRADE:
            p["status"], p["missing"] = "watch", f"chart triggered, but financials grade {p['grade']}"
            bucket = watch
            if len(watch) >= THEME_TOP_N:
                continue
        rev = info.get("totalRevenue") or 0
        growth = info.get("revenueGrowth")
        p.update(name=info.get("shortName", p["ticker"]), mcap=round(mcap / 1e6),
                 revenue=round(rev / 1e6, 1),
                 rev_growth=None if growth is None else round(growth * 100))
        bucket.append(p)
        if len(final) >= THEME_TOP_N and len(watch) >= THEME_TOP_N:
            break
    watch.sort(key=lambda w: -w.get("fscore", 0))        # healthiest companies first
    return final, watch, len(themes)


def format_theme_alert(picks, watch, checked):
    head = (f"Early-catch scan (weekly)  |  {checked} names checked, under ${THEME_MAX_PRICE:.0f}\n"
            f"Long-shot bucket: ${THEME_BUCKET:.0f}, about ${THEME_BUCKET/THEME_BETS:.0f} per bet\n")
    lines = [head]
    if not picks:
        lines.append("No full signals this week.\n")
    for p in picks:
        growth = "" if p.get("rev_growth") is None else f" ({p['rev_growth']:+d}% yr/yr)"
        lines.append(
            f"{p['ticker']} ({p.get('name', p['ticker'])}, {p['theme']}) ${p['price']:.2f}\n"
            f"  Size ${p.get('mcap', 0):,}M | revenue ${p.get('revenue', 0)}M{growth}\n"
            f"{fund_line(p)}"
            f"  {p['off_high']:.0f}% below its 1-year high, just reclaimed its 50-day average\n"
            f"  Volume {p['vol_pickup']:.1f}x normal over 10 days\n"
            f"  Starter: ~${p['bet']:.0f} | Cut if it closes below {p['base_low']:.2f} (-{p['cut_pct']:.0f}%)\n"
            f"  Sell half at {p['half_at']:.2f} (+{THEME_HALF_AT:.0f}%): your money back, move it to swing trades\n"
            f"  Let the other half run for months. News: {p['news']}\n")
    if watch:
        lines.append("CLOSE TO TRIGGERING (watch, don't buy yet):")
        for w in watch:
            lines.append(f"{w['ticker']} ({w.get('name', w['ticker'])}, {w['theme']}) ${w['price']:.2f}, "
                         f"{w['off_high']:.0f}% below 1-yr high, ${w.get('mcap', 0):,}M size\n"
                         f"{fund_line(w)}"
                         f"  Missing: {w['missing']}\n")
    elif not picks:
        lines.append("Nothing close either. Patience is the strategy.")
    return "\n".join(lines)


def theme_scan_due(now):
    if os.getenv("GITHUB_EVENT_NAME") == "workflow_dispatch":
        return True                                   # any manual run
    return now.weekday() == 0 and now.hour < 12        # Monday morning scan


# ================= Alerts =================
def format_alert(picks, market, skipped):
    risk = ACCOUNT * RISK_PCT / 100
    head = f"Swing scan  |  risk ${risk:.2f}/trade\n{market['note']}"
    if not market["ok"]:
        return (head + "\n\nSitting out: breakouts fail more often when the market is "
                "below its 50-day average. No trades today.")
    if not picks:
        msg = head + "\n\nNo setups passed your rules. No trade is a fine trade."
    else:
        lines = [head + f"\n\n{len(picks)} setup(s):\n"]
        for s in picks:
            earn = f" | earnings {s['earnings']}" if s.get("earnings") else ""
            lines.append(
                f"{s['ticker']} ({s['name']}) ${s['price']:.2f}, +{s['change']:.1f}%, {s['rvol']:.1f}x vol{earn}\n"
                f"  Buy-stop {s['entry']:.2f} | Stop {s['stop']:.2f} (-{s['stop_pct']:.1f}%) | Target {s['target']:.2f}\n"
                f"  Move stop to {s['entry']:.2f} once price hits {s['breakeven']:.2f}\n"
                f"  {s['shares']} sh = ${s['cost']:.2f} | risk ${s['risk']:.2f} | reward ${s['reward']:.2f}\n"
                f"{fund_line(s)}"
                f"  News: {s['news']}\n")
        msg = "\n".join(lines)
    if skipped:
        msg += "\nSkipped for earnings soon: " + ", ".join(skipped)
    return msg


def send(text):
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    print(text)
    if token and chat:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": text,
                                "disable_web_page_preview": "true"}, timeout=20)
        print("Telegram:", r.status_code)


# ================= Dashboard data =================
def load_data():
    try:
        with open(DATA_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=1)


def clean(d):
    return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in d.items()}


def main():
    data = load_data()
    tracked = data.get("tracked", [])

    universe = build_universe()
    active = {a["ticker"] for a in tracked if a["status"] in ("waiting", "open")}
    print(f"Scanning {len(universe)} tickers, tracking {len(active)}")
    frames = download(set(universe) | active | {MARKET_TICKER})

    market = market_status(frames)
    tracked = update_scorecard(tracked, frames)

    picks, skipped = find_setups({t: frames[t] for t in universe if t in frames})
    picks = [clean(p) for p in picks]

    now = datetime.now(ET)
    today = now.date().isoformat()
    for p in picks:            # every alert joins the scorecard, even on weak-market days
        if p["ticker"] in active:
            continue
        tracked.append({"ticker": p["ticker"], "date": today, "entry": p["entry"],
                        "stop": p["stop"], "target": p["target"], "breakeven": p["breakeven"],
                        "market_ok": market["ok"], "status": "waiting"})
        active.add(p["ticker"])
    tracked = tracked[-TRACK_KEEP:]

    stamp = now.strftime("%Y-%m-%d %H:%M ET")
    shown = picks if market["ok"] else []
    data.update(updated=stamp, account=ACCOUNT, risk_pct=RISK_PCT, market=market,
                latest=shown, skipped=skipped, tracked=tracked,
                scorecard=scorecard_summary(tracked))
    data["history"] = ([{"date": stamp, "picks": shown}] + data.get("history", []))[:HISTORY_KEEP]

    theme_msg = None
    if theme_scan_due(now):
        try:
            tpicks, twatch, checked = run_theme_scan()
            data["theme"] = {"date": stamp, "checked": checked, "picks": tpicks, "watch": twatch}
            theme_msg = format_theme_alert(tpicks, twatch, checked)
        except Exception as e:
            print("theme scan failed:", e)
    save_data(data)

    send(format_alert(picks, market, skipped))
    if theme_msg:
        send(theme_msg)


if __name__ == "__main__":
    main()
