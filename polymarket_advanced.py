"""
Polymarket Advanced Anomaly Tracker
US x Iran ceasefire markets — multi-signal detection

Signals:
  1. Volume velocity  — unusual trade rate before price moves
  2. Orderbook depth  — whale positioning, spread anomalies
  3. Cross-market curve fit — mispricing vs sibling markets
  4. Price spike      — classic threshold breach
"""

import asyncio
import json
import time
import requests
from datetime import datetime, timezone
from collections import deque

# ── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN  = "8775155035:AAEJkWSEoHdTGAp9_D-r5Q4mF8YBgha5BXQ"
CHAT_ID    = "498024378"

# All ceasefire YES token IDs (ordered by resolution date)
MARKETS = [
    ("Apr 15", "2026-04-15",
     "110406456068829362276240506792118823760217441862547705615195186887988065588637"),
    ("Apr 30", "2026-04-30",
     "93513271614004093873589848080286856812074932070363702542344921986195150904832"),
    ("May 31", "2026-05-31",
     "55480560649953869474889564695893980899277985027154110743474741820157509374099"),
    ("Jun 30", "2026-06-30",
     "37778223631706244786891818434499241296591894062921218703919093519945536389454"),
]

PRIMARY_TOKEN = MARKETS[0][2]
MARKET_URL    = "https://polymarket.com/event/us-x-iran-ceasefire-by/us-x-iran-ceasefire-by-april-15-182-528-637"

# Tuning
VELOCITY_WINDOW_SEC  = 120   # rolling window for trade velocity
VELOCITY_SPIKE_MULT  = 3.0   # alert if trades/min > Nx baseline
SPREAD_ALERT_CENTS   = 8     # alert if bid-ask spread wider than this
WHALE_SIZE_USDC      = 2000  # alert if top-of-book order > this
CURVE_DEVIATION_PCT  = 25    # alert if Apr15 deviates >25% from curve
SPIKE_CENTS          = 5     # price spike threshold
SUMMARY_HOURS        = 6
COOLDOWN_SEC         = 300   # min seconds between same-type alerts

CLOB  = "https://clob.polymarket.com"

# ── STATE ─────────────────────────────────────────────────────────────────────
price_history      = deque(maxlen=60)
last_alert_time    = {}
baseline_velocity  = None

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
    print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] {msg}")

def can_alert(key):
    if time.time() - last_alert_time.get(key, 0) > COOLDOWN_SEC:
        last_alert_time[key] = time.time()
        return True
    return False

def days_to(date_str):
    target = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
    return max(0, (target - datetime.now(timezone.utc)).days)

def get_price(token_id):
    try:
        r = requests.get(f"{CLOB}/price?token_id={token_id}&side=buy", timeout=8)
        return round(float(r.json().get("price", 0)) * 100, 1)
    except:
        return None

def get_orderbook(token_id):
    try:
        r = requests.get(f"{CLOB}/book?token_id={token_id}", timeout=8)
        return r.json()
    except:
        return None

def get_recent_trades(token_id, limit=100):
    try:
        r = requests.get(f"{CLOB}/trades?market={token_id}&limit={limit}", timeout=8)
        return r.json().get("data", [])
    except:
        return []

# ── SIGNAL 1: VOLUME VELOCITY ─────────────────────────────────────────────────
def check_velocity():
    global baseline_velocity
    trades = get_recent_trades(PRIMARY_TOKEN)
    if not trades:
        return

    now_ts = time.time()
    recent = sum(
        1 for t in trades
        if now_ts - int(t.get("timestamp", 0)) / 1000 < VELOCITY_WINDOW_SEC
    )
    tpm = recent / (VELOCITY_WINDOW_SEC / 60)

    if baseline_velocity is None:
        baseline_velocity = max(tpm, 0.1)
        log(f"Velocity baseline: {baseline_velocity:.1f} t/min")
        return

    baseline_velocity = 0.9 * baseline_velocity + 0.1 * tpm
    ratio = tpm / baseline_velocity
    log(f"Velocity: {tpm:.1f} t/min  base={baseline_velocity:.1f}  ratio={ratio:.1f}x")

    if ratio >= VELOCITY_SPIKE_MULT and can_alert("velocity"):
        p = get_price(PRIMARY_TOKEN)
        tg(
            f"🔥 <b>Volume velocity spike!</b>\n\n"
            f"Trade rate: <b>{tpm:.1f}/min</b> ({ratio:.1f}x normal)\n"
            f"YES: <b>{p}¢</b>  |  Days left: {days_to(MARKETS[0][1])}\n\n"
            f"Something moving <i>before</i> price reacts — check news now.\n\n"
            f"👉 <a href='{MARKET_URL}'>Open market</a>"
        )

# ── SIGNAL 2: ORDERBOOK DEPTH ─────────────────────────────────────────────────
def check_orderbook():
    book = get_orderbook(PRIMARY_TOKEN)
    if not book:
        return

    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if not bids or not asks:
        return

    best_bid      = round(float(bids[0]["price"]) * 100, 1)
    best_ask      = round(float(asks[0]["price"]) * 100, 1)
    spread        = round(best_ask - best_bid, 1)
    mid           = round((best_bid + best_ask) / 2, 1)
    top_bid_size  = float(bids[0].get("size", 0))
    top_ask_size  = float(asks[0].get("size", 0))

    log(f"Book: bid={best_bid}¢ ask={best_ask}¢ spread={spread}¢ "
        f"bid_sz=${top_bid_size:.0f} ask_sz=${top_ask_size:.0f}")

    if spread >= SPREAD_ALERT_CENTS and can_alert("spread"):
        tg(
            f"📊 <b>Wide spread — market uncertain</b>\n\n"
            f"Bid: <b>{best_bid}¢</b>  |  Ask: <b>{best_ask}¢</b>\n"
            f"Spread: <b>{spread}¢</b>  (normal 1–3¢)  |  Mid: {mid}¢\n\n"
            f"Traders disagree on fair value. "
            f"Limit order near mid could get filled at a discount.\n\n"
            f"👉 <a href='{MARKET_URL}'>Open market</a>"
        )

    if top_bid_size >= WHALE_SIZE_USDC and can_alert("whale_bid"):
        tg(
            f"🐋 <b>Whale bid — large YES accumulation</b>\n\n"
            f"${top_bid_size:,.0f} buy order sitting at {best_bid}¢\n"
            f"Spread: {spread}¢  |  Days left: {days_to(MARKETS[0][1])}\n\n"
            f"Smart money accumulating YES quietly. "
            f"Price likely moves up once this absorbs asks.\n\n"
            f"👉 <a href='{MARKET_URL}'>Open market</a>"
        )

    if top_ask_size >= WHALE_SIZE_USDC and can_alert("whale_ask"):
        tg(
            f"🐋 <b>Whale ask — large YES sell wall</b>\n\n"
            f"${top_ask_size:,.0f} sell order at {best_ask}¢\n"
            f"Spread: {spread}¢  |  Days left: {days_to(MARKETS[0][1])}\n\n"
            f"Large seller suppressing YES. "
            f"Good NO entry if you think ceasefire is unlikely.\n\n"
            f"👉 <a href='{MARKET_URL}'>Open market</a>"
        )

# ── SIGNAL 3: CROSS-MARKET CURVE ─────────────────────────────────────────────
def check_curve():
    data = []
    for label, date_str, token_id in MARKETS:
        p = get_price(token_id)
        d = days_to(date_str)
        if p is not None:
            data.append((label, p, d))
            log(f"Curve: {label}={p}¢ ({d}d)")

    if len(data) < 3:
        return

    anomalies = []

    # Check monotonicity
    for i in range(1, len(data)):
        if data[i][1] < data[i-1][1]:
            anomalies.append(
                f"⚠️ Inversion: {data[i][0]} ({data[i][1]}¢) < "
                f"{data[i-1][0]} ({data[i-1][1]}¢)"
            )

    # Check implied daily rate consistency
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
                    f"📐 Rate jump: {rates[i-1][0]}→{rates[i-1][1]} "
                    f"{rates[i-1][2]:.1f}¢/d vs "
                    f"{rates[i][0]}→{rates[i][1]} {rates[i][2]:.1f}¢/d"
                )

    # Apr 15 vs curve extrapolation from Apr 30
    extrapolated = round(data[1][1] * (data[0][2] / max(data[1][2], 1)), 1)
    dev_pct = abs(data[0][1] - extrapolated) / max(extrapolated, 1) * 100
    log(f"Apr15: actual={data[0][1]}¢  extrapolated={extrapolated}¢  dev={dev_pct:.0f}%")

    if (dev_pct > CURVE_DEVIATION_PCT or anomalies) and can_alert("curve"):
        direction = "above" if data[0][1] > extrapolated else "below"
        curve_lines = "\n".join(
            f"  {label}: <b>{p}¢</b>" for label, p, d in data
        )
        rate_lines = "\n".join(
            f"  {a}→{b}: {r:.1f}¢/day" for a, b, r in rates
        )
        tg(
            f"📐 <b>Cross-market mispricing</b>\n\n"
            f"Apr 15 actual: <b>{data[0][1]}¢</b>\n"
            f"Curve-implied: <b>{extrapolated}¢</b>  "
            f"({dev_pct:.0f}% {direction})\n\n"
            + ("\n".join(anomalies) + "\n\n" if anomalies else "") +
            f"<b>Full curve:</b>\n{curve_lines}\n\n"
            f"<b>Implied daily rate:</b>\n{rate_lines}\n\n"
            f"👉 <a href='{MARKET_URL}'>Open market</a>"
        )

# ── SIGNAL 4: PRICE SPIKE ─────────────────────────────────────────────────────
def check_price():
    p = get_price(PRIMARY_TOKEN)
    if p is None:
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
        tg(f"🔔 <b>YES crossed 25¢ → {p}¢</b>\n"
           f"Possible NO entry or news catalyst.\n"
           f"👉 <a href='{MARKET_URL}'>Open market</a>")

    if p <= 10 and prev > 10 and can_alert("below10"):
        tg(f"🔔 <b>YES dropped below 10¢ → {p}¢</b>\n"
           f"Extreme asymmetric YES opportunity?\n"
           f"Days left: {days_to(MARKETS[0][1])}\n"
           f"👉 <a href='{MARKET_URL}'>Open market</a>")

# ── ASYNC LOOPS ───────────────────────────────────────────────────────────────
async def price_loop():
    while True:
        check_price()
        await asyncio.sleep(30)

async def orderbook_loop():
    await asyncio.sleep(10)
    while True:
        check_orderbook()
        await asyncio.sleep(30)

async def velocity_loop():
    await asyncio.sleep(20)
    while True:
        check_velocity()
        await asyncio.sleep(60)

async def curve_loop():
    await asyncio.sleep(40)
    while True:
        check_curve()
        await asyncio.sleep(60)

async def summary_loop():
    await asyncio.sleep(60)
    while True:
        await asyncio.sleep(SUMMARY_HOURS * 3600)
        data = []
        for label, date_str, token_id in MARKETS:
            p = get_price(token_id)
            d = days_to(date_str)
            data.append((label, p, d))

        lines = "\n".join(
            f"  {label}: <b>{p}¢</b>  ({d}d)" if p else f"  {label}: —"
            for label, p, d in data
        )
        valid = [(l, p, d) for l, p, d in data if p]
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

async def main():
    log("Advanced tracker starting...")
    tg(
        "🚀 <b>Advanced Polymarket Tracker v2</b>\n\n"
        "<b>Active signals:</b>\n"
        "  🔥 Volume velocity spike\n"
        "  🐋 Orderbook whale detection\n"
        "  📐 Cross-market curve mispricing\n"
        "  ⚡ Price threshold breach\n\n"
        f"<b>Watching:</b> Apr 15, Apr 30, May 31, Jun 30\n"
        f"<b>Primary:</b> Apr 15  |  Days left: <b>{days_to(MARKETS[0][1])}</b>"
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
