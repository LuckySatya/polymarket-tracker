"""
Polymarket Advanced Anomaly Tracker v3
- Auto-discovers token IDs via Gamma API at startup
- Volume velocity, orderbook depth, cross-market curve, price spikes
"""

import asyncio
import time
import requests
from datetime import datetime, timezone
from collections import deque

# ── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN  = "8775155035:AAEJkWSEoHdTGAp9_D-r5Q4mF8YBgha5BXQ"
CHAT_ID    = "498024378"

# Event slug — used to auto-discover all child markets
EVENT_SLUG = "us-x-iran-ceasefire-by"

# Date labels we care about (matched against market question text)
TARGET_DATES = ["April 15", "April 30", "May 31", "June 30"]

MARKET_URL = "https://polymarket.com/event/us-x-iran-ceasefire-by/us-x-iran-ceasefire-by-april-15-182-528-637"

# Tuning
VELOCITY_SPIKE_MULT  = 3.0
SPREAD_ALERT_CENTS   = 8
WHALE_SIZE_USDC      = 2000
CURVE_DEVIATION_PCT  = 25
SPIKE_CENTS          = 5
SUMMARY_HOURS        = 6
COOLDOWN_SEC         = 300

CLOB  = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"

# ── STATE ─────────────────────────────────────────────────────────────────────
price_history     = deque(maxlen=60)
last_alert_time   = {}
baseline_velocity = None
MARKETS           = []   # filled at startup: [(label, date_str, yes_token_id), ...]

# ── HELPERS ───────────────────────────────────────────────────────────────────
def tg(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10
        )
    except Exception as e:
        print(f"[TG] {e}")

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def can_alert(key):
    if time.time() - last_alert_time.get(key, 0) > COOLDOWN_SEC:
        last_alert_time[key] = time.time()
        return True
    return False

def days_to(date_str):
    target = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
    return max(0, (target - datetime.now(timezone.utc)).days)

# ── MARKET DISCOVERY ──────────────────────────────────────────────────────────
def discover_markets():
    """
    Use Gamma API to find all markets in the ceasefire event,
    extract YES token IDs and resolution dates for each date bucket.
    """
    log("Discovering markets via Gamma API...")
    try:
        r = requests.get(
            f"{GAMMA}/markets",
            params={"event_slug": EVENT_SLUG, "limit": 50},
            timeout=15
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log(f"Gamma API error (event_slug): {e}")
        # Fallback: try fetching by slug directly
        try:
            r = requests.get(
                f"{GAMMA}/markets",
                params={"slug": EVENT_SLUG, "limit": 50},
                timeout=15
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e2:
            log(f"Gamma API fallback error: {e2}")
            return []

    # Gamma returns list or dict with "markets" key
    if isinstance(data, dict):
        markets_raw = data.get("markets", data.get("data", []))
    else:
        markets_raw = data

    log(f"Got {len(markets_raw)} markets from Gamma API")

    found = []
    for m in markets_raw:
        question = m.get("question", "") or m.get("title", "")
        end_date = m.get("endDate", "") or m.get("end_date_iso", "")

        # Get clob_token_ids — list of [yes_token, no_token]
        tokens = m.get("clobTokenIds") or m.get("clob_token_ids") or []
        if isinstance(tokens, str):
            import json
            try:
                tokens = json.loads(tokens)
            except:
                tokens = []

        if not tokens:
            continue

        yes_token = tokens[0] if tokens else None
        if not yes_token:
            continue

        # Match against our target dates
        for label in TARGET_DATES:
            if label.lower() in question.lower():
                # Parse end_date to YYYY-MM-DD
                date_str = end_date[:10] if end_date else ""
                found.append((label, date_str, str(yes_token)))
                log(f"Found: {label} | token={yes_token[:20]}... | ends={date_str}")
                break

    # Sort by date
    found.sort(key=lambda x: x[1])
    return found

def discover_markets_v2():
    """Alternative: search by market slugs directly."""
    slugs = [
        "us-x-iran-ceasefire-by-april-15-182-528-637",
        "us-x-iran-ceasefire-by-april-30",
        "us-x-iran-ceasefire-by-may-31",
        "us-x-iran-ceasefire-by-june-30",
    ]
    labels = ["April 15", "April 30", "May 31", "June 30"]
    dates  = ["2026-04-15", "2026-04-30", "2026-05-31", "2026-06-30"]

    found = []
    for slug, label, date in zip(slugs, labels, dates):
        try:
            r = requests.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=10)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict):
                items = data.get("markets", data.get("data", [data]))
            else:
                items = data if isinstance(data, list) else [data]

            for m in items:
                tokens = m.get("clobTokenIds") or m.get("clob_token_ids") or []
                if isinstance(tokens, str):
                    import json
                    try: tokens = json.loads(tokens)
                    except: tokens = []
                if tokens:
                    yes_token = str(tokens[0])
                    found.append((label, date, yes_token))
                    log(f"v2 found: {label} token={yes_token[:24]}...")
                    break
        except Exception as e:
            log(f"v2 slug {slug} error: {e}")

    return found

# ── PRICE ─────────────────────────────────────────────────────────────────────
def get_price(token_id):
    try:
        r = requests.get(
            f"{CLOB}/price",
            params={"token_id": token_id, "side": "buy"},
            timeout=8
        )
        r.raise_for_status()
        val = float(r.json().get("price", 0))
        return round(val * 100, 1)
    except Exception as e:
        log(f"Price error ({token_id[:16]}...): {e}")
        return None

def get_midpoint(token_id):
    try:
        r = requests.get(f"{CLOB}/midpoint", params={"token_id": token_id}, timeout=8)
        r.raise_for_status()
        val = float(r.json().get("mid", 0))
        return round(val * 100, 1)
    except:
        return None

def get_orderbook(token_id):
    try:
        r = requests.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=8)
        r.raise_for_status()
        return r.json()
    except:
        return None

def get_recent_trades(token_id, limit=100):
    try:
        r = requests.get(f"{CLOB}/trades", params={"market": token_id, "limit": limit}, timeout=8)
        r.raise_for_status()
        return r.json().get("data", [])
    except:
        return []

# ── SIGNAL 1: VELOCITY ────────────────────────────────────────────────────────
def check_velocity():
    global baseline_velocity
    if not MARKETS:
        return
    trades = get_recent_trades(MARKETS[0][2])
    if not trades:
        return

    now_ts  = time.time()
    window  = 120
    recent  = sum(1 for t in trades
                  if now_ts - int(t.get("timestamp", 0)) / 1000 < window)
    tpm     = recent / (window / 60)

    if baseline_velocity is None:
        baseline_velocity = max(tpm, 0.1)
        log(f"Velocity baseline: {baseline_velocity:.1f} t/min")
        return

    baseline_velocity = 0.9 * baseline_velocity + 0.1 * tpm
    ratio = tpm / baseline_velocity
    log(f"Velocity: {tpm:.1f} t/min  base={baseline_velocity:.1f}  ratio={ratio:.1f}x")

    if ratio >= VELOCITY_SPIKE_MULT and can_alert("velocity"):
        p = get_price(MARKETS[0][2]) or get_midpoint(MARKETS[0][2])
        tg(
            f"🔥 <b>Volume velocity spike!</b>\n\n"
            f"Trade rate: <b>{tpm:.1f}/min</b> ({ratio:.1f}x normal)\n"
            f"YES: <b>{p}¢</b>  |  Days left: {days_to(MARKETS[0][1])}\n\n"
            f"Something moving before price reacts — check news.\n\n"
            f"👉 <a href='{MARKET_URL}'>Open market</a>"
        )

# ── SIGNAL 2: ORDERBOOK ───────────────────────────────────────────────────────
def check_orderbook():
    if not MARKETS:
        return
    book = get_orderbook(MARKETS[0][2])
    if not book:
        return

    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if not bids or not asks:
        return

    best_bid     = round(float(bids[0]["price"]) * 100, 1)
    best_ask     = round(float(asks[0]["price"]) * 100, 1)
    spread       = round(best_ask - best_bid, 1)
    mid          = round((best_bid + best_ask) / 2, 1)
    top_bid_sz   = float(bids[0].get("size", 0))
    top_ask_sz   = float(asks[0].get("size", 0))

    log(f"Book: bid={best_bid}¢ ask={best_ask}¢ spread={spread}¢ "
        f"bid_sz=${top_bid_sz:.0f} ask_sz=${top_ask_sz:.0f}")

    if spread >= SPREAD_ALERT_CENTS and can_alert("spread"):
        tg(
            f"📊 <b>Wide spread — market uncertain</b>\n\n"
            f"Bid: <b>{best_bid}¢</b>  |  Ask: <b>{best_ask}¢</b>\n"
            f"Spread: <b>{spread}¢</b>  (normal 1–3¢)  |  Mid: {mid}¢\n\n"
            f"Limit order near mid could fill at a discount.\n\n"
            f"👉 <a href='{MARKET_URL}'>Open market</a>"
        )

    if top_bid_sz >= WHALE_SIZE_USDC and can_alert("whale_bid"):
        tg(
            f"🐋 <b>Whale bid — large YES accumulation</b>\n\n"
            f"${top_bid_sz:,.0f} buy order at {best_bid}¢\n"
            f"Spread: {spread}¢  |  Days left: {days_to(MARKETS[0][1])}\n\n"
            f"Smart money accumulating YES quietly.\n\n"
            f"👉 <a href='{MARKET_URL}'>Open market</a>"
        )

    if top_ask_sz >= WHALE_SIZE_USDC and can_alert("whale_ask"):
        tg(
            f"🐋 <b>Whale ask — large YES sell wall</b>\n\n"
            f"${top_ask_sz:,.0f} sell order at {best_ask}¢\n"
            f"Spread: {spread}¢  |  Days left: {days_to(MARKETS[0][1])}\n\n"
            f"Good NO entry if ceasefire unlikely.\n\n"
            f"👉 <a href='{MARKET_URL}'>Open market</a>"
        )

# ── SIGNAL 3: CURVE ───────────────────────────────────────────────────────────
def check_curve():
    if len(MARKETS) < 3:
        return

    data = []
    for label, date_str, token_id in MARKETS:
        p = get_price(token_id) or get_midpoint(token_id)
        d = days_to(date_str)
        if p is not None and p > 0:
            data.append((label, p, d))
            log(f"Curve: {label}={p}¢ ({d}d)")

    if len(data) < 3:
        return

    anomalies = []
    for i in range(1, len(data)):
        if data[i][1] < data[i-1][1]:
            anomalies.append(
                f"⚠️ Inversion: {data[i][0]} ({data[i][1]}¢) < "
                f"{data[i-1][0]} ({data[i-1][1]}¢)"
            )

    rates = []
    for i in range(1, len(data)):
        gap = data[i][2] - data[i-1][2]
        if gap > 0:
            rates.append((data[i-1][0], data[i][0],
                          (data[i][1] - data[i-1][1]) / gap))

    for i in range(1, len(rates)):
        if rates[i-1][2] > 0:
            ratio = rates[i][2] / rates[i-1][2]
            if ratio > 2.5 or ratio < 0.2:
                anomalies.append(
                    f"📐 Rate inconsistency: "
                    f"{rates[i-1][0]}→{rates[i-1][1]} {rates[i-1][2]:.1f}¢/d vs "
                    f"{rates[i][0]}→{rates[i][1]} {rates[i][2]:.1f}¢/d"
                )

    if data[1][2] > 0:
        extrapolated = round(data[1][1] * (data[0][2] / data[1][2]), 1)
        dev_pct = abs(data[0][1] - extrapolated) / max(extrapolated, 1) * 100
        log(f"Curve dev: actual={data[0][1]}¢ expected={extrapolated}¢ dev={dev_pct:.0f}%")

        if (dev_pct > CURVE_DEVIATION_PCT or anomalies) and can_alert("curve"):
            direction = "above" if data[0][1] > extrapolated else "below"
            curve_lines = "\n".join(f"  {l}: <b>{p}¢</b>" for l, p, d in data)
            rate_lines  = "\n".join(f"  {a}→{b}: {r:.1f}¢/day" for a, b, r in rates)
            tg(
                f"📐 <b>Cross-market mispricing</b>\n\n"
                f"Apr 15 actual: <b>{data[0][1]}¢</b>\n"
                f"Curve-implied: <b>{extrapolated}¢</b>  ({dev_pct:.0f}% {direction})\n\n"
                + ("\n".join(anomalies) + "\n\n" if anomalies else "") +
                f"<b>Curve:</b>\n{curve_lines}\n\n"
                f"<b>Implied daily rate:</b>\n{rate_lines}\n\n"
                f"👉 <a href='{MARKET_URL}'>Open market</a>"
            )

# ── SIGNAL 4: PRICE SPIKE ─────────────────────────────────────────────────────
def check_price():
    if not MARKETS:
        return
    p = get_price(MARKETS[0][2]) or get_midpoint(MARKETS[0][2])
    if p is None or p == 0:
        return

    price_history.append(p)
    log(f"Price: {p}¢")

    if len(price_history) < 2:
        return

    prev  = price_history[-2]
    delta = round(p - prev, 1)

    if abs(delta) >= SPIKE_CENTS and can_alert("spike"):
        arrow = "📈" if delta > 0 else "📉"
        tg(
            f"{arrow} <b>Spike: {'+' if delta > 0 else ''}{delta}¢</b>\n\n"
            f"YES: <b>{p}¢</b>  (was {prev}¢)\n"
            f"Days left: {days_to(MARKETS[0][1])}\n\n"
            f"👉 <a href='{MARKET_URL}'>Open market</a>"
        )

    if p >= 25 and prev < 25 and can_alert("above25"):
        tg(f"🔔 <b>YES crossed 25¢ → {p}¢</b>\nPossible NO entry.\n"
           f"👉 <a href='{MARKET_URL}'>Open market</a>")

    if p <= 10 and prev > 10 and can_alert("below10"):
        tg(f"🔔 <b>YES dropped below 10¢ → {p}¢</b>\nAsymmetric YES opportunity?\n"
           f"Days left: {days_to(MARKETS[0][1])}\n"
           f"👉 <a href='{MARKET_URL}'>Open market</a>")

# ── ASYNC LOOPS ───────────────────────────────────────────────────────────────
async def price_loop():
    while True:
        check_price()
        await asyncio.sleep(30)

async def orderbook_loop():
    await asyncio.sleep(15)
    while True:
        check_orderbook()
        await asyncio.sleep(30)

async def velocity_loop():
    await asyncio.sleep(25)
    while True:
        check_velocity()
        await asyncio.sleep(60)

async def curve_loop():
    await asyncio.sleep(45)
    while True:
        check_curve()
        await asyncio.sleep(60)

async def summary_loop():
    await asyncio.sleep(60)
    while True:
        await asyncio.sleep(SUMMARY_HOURS * 3600)
        data = []
        for label, date_str, token_id in MARKETS:
            p = get_price(token_id) or get_midpoint(token_id)
            d = days_to(date_str)
            data.append((label, p, d))

        lines = "\n".join(
            f"  {label}: <b>{p}¢</b>  ({d}d)" if p else f"  {label}: —"
            for label, p, d in data
        )
        valid = [(l, p, d) for l, p, d in data if p and p > 0]
        rates = "\n".join(
            f"  {valid[i-1][0]}→{valid[i][0]}: "
            f"{(valid[i][1]-valid[i-1][1])/max(valid[i][2]-valid[i-1][2],1):.1f}¢/day"
            for i in range(1, len(valid))
            if valid[i][2] != valid[i-1][2]
        )
        tg(
            f"🕐 <b>{SUMMARY_HOURS}h Update</b>\n\n"
            f"<b>Ceasefire curve:</b>\n{lines}\n\n"
            f"<b>Implied daily rate:</b>\n{rates}"
        )

# ── STARTUP ───────────────────────────────────────────────────────────────────
async def main():
    global MARKETS

    log("Advanced tracker v3 starting...")

    # Try primary discovery method
    MARKETS = discover_markets()

    # Fallback to slug-based discovery
    if not MARKETS:
        log("Primary discovery failed, trying slug-based...")
        MARKETS = discover_markets_v2()

    if not MARKETS:
        err = "Could not discover any markets from Gamma API. Check event slug."
        log(err)
        tg(f"❌ <b>Tracker error</b>\n\n{err}")
        return

    market_list = "\n".join(f"  {l}: token ...{t[-8:]}" for l, d, t in MARKETS)
    log(f"Discovered {len(MARKETS)} markets:\n{market_list}")

    # Verify prices are working
    test_price = get_price(MARKETS[0][2]) or get_midpoint(MARKETS[0][2])
    price_status = f"YES: <b>{test_price}¢</b>" if test_price else "⚠️ Price API not responding"

    tg(
        f"🚀 <b>Advanced Polymarket Tracker v3</b>\n\n"
        f"<b>Markets discovered:</b> {len(MARKETS)}\n"
        + "\n".join(f"  • {l} ({d})" for l, d, t in MARKETS) +
        f"\n\n{price_status}\n"
        f"Days to Apr 15: <b>{days_to(MARKETS[0][1])}</b>\n\n"
        f"<b>Active signals:</b>\n"
        f"  🔥 Volume velocity\n"
        f"  🐋 Orderbook whale detection\n"
        f"  📐 Cross-market curve\n"
        f"  ⚡ Price spikes"
    )

    await asyncio.gather(
        price_loop(),
        orderbook_loop(),
        velocity_loop(),
        curve_loop(),
        summary_loop(),
    )

if __name__ == "__main__":
    asyncio.run(main())
