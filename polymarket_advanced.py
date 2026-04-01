"""
Polymarket Advanced Anomaly Tracker v4
Tracks NO prices across all ceasefire dates
- Auto-discovers token IDs via Gamma API
- Volume velocity, orderbook whale, cross-market curve, price spikes
- Telegram command: /prices — get all current NO prices on demand
"""

import asyncio
import time
import requests
import json
from datetime import datetime, timezone
from collections import deque

# ── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN  = "8775155035:AAEJkWSEoHdTGAp9_D-r5Q4mF8YBgha5BXQ"
CHAT_ID    = "498024378"
EVENT_SLUG = "us-x-iran-ceasefire-by"
TARGET_DATES = ["April 7", "April 15", "April 30", "May 31", "June 30"]
MARKET_URL = "https://polymarket.com/event/us-x-iran-ceasefire-by/us-x-iran-ceasefire-by-april-15-182-528-637"

VELOCITY_SPIKE_MULT  = 3.0
WHALE_SIZE_USDC      = 2000
CURVE_DEVIATION_PCT  = 25
SPIKE_CENTS          = 3     # tighter for NO (moves in smaller increments)
SUMMARY_HOURS        = 6
COOLDOWN_SEC         = 300
CLOB  = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"

# ── STATE ─────────────────────────────────────────────────────────────────────
price_history     = deque(maxlen=60)
last_alert_time   = {}
baseline_velocity = None
MARKETS           = []   # [(label, date_str, yes_token, no_token), ...]
last_update_id    = 0

# ── HELPERS ───────────────────────────────────────────────────────────────────
def tg(msg, chat_id=None):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id or CHAT_ID, "text": msg,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10
        )
    except Exception as e:
        print(f"[TG] {e}")

def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)

def can_alert(key):
    if time.time() - last_alert_time.get(key, 0) > COOLDOWN_SEC:
        last_alert_time[key] = time.time()
        return True
    return False

def days_to(date_str):
    target = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
    return max(0, (target - datetime.now(timezone.utc)).days)

def no_entry_signal(no_price):
    """Assess NO entry quality based on price."""
    if no_price <= 80:
        return "🟢 Strong NO entry — unusually cheap, implied YES probability high"
    elif no_price <= 86:
        return "🟡 Moderate NO entry — below typical range"
    elif no_price >= 96:
        return "⚪ NO very expensive — limited upside"
    return "⚪ NO within normal range"

# ── MARKET DISCOVERY ──────────────────────────────────────────────────────────
def discover_markets():
    log("Discovering markets via verified slugs...")
    found = []

    slugs = [
        ("April 7",  "2026-04-07", "us-x-iran-ceasefire-by-april-7-278"),
        ("April 15", "2026-04-15", "us-x-iran-ceasefire-by-april-15-182-528-637"),
        ("April 30", "2026-04-30", "us-x-iran-ceasefire-by-april-30-194-679-389"),
        ("May 31",   "2026-05-31", "us-x-iran-ceasefire-by-may-31-313-373-916"),
        ("June 30",  "2026-06-30", "us-x-iran-ceasefire-by-june-30-752-741-257"),
    ]

    for label, date, slug in slugs:
        try:
            r = requests.get(f"{GAMMA}/markets", params={"slug": slug}, timeout=10)
            r.raise_for_status()
            data = r.json()
            items = data.get("markets", data.get("data", [data])) if isinstance(data, dict) else (data if isinstance(data, list) else [data])
            for m in items:
                tokens = m.get("clobTokenIds") or m.get("clob_token_ids") or []
                if isinstance(tokens, str):
                    try: tokens = json.loads(tokens)
                    except: tokens = []
                if len(tokens) >= 2:
                    found.append((label, date, str(tokens[0]), str(tokens[1])))
                    log(f"  Found: {label} | YES=...{str(tokens[0])[-8:]} NO=...{str(tokens[1])[-8:]}")
                    break
        except Exception as e:
            log(f"  Slug error {label}: {e}")

    found.sort(key=lambda x: x[1])
    return found

# ── API CALLS ─────────────────────────────────────────────────────────────────
def get_no_price(no_token):
    """Get NO price — buy side."""
    try:
        r = requests.get(f"{CLOB}/price",
                         params={"token_id": no_token, "side": "buy"}, timeout=8)
        r.raise_for_status()
        val = float(r.json().get("price", 0))
        return round(val * 100, 1) if val > 0 else None
    except:
        return None

def get_no_midpoint(no_token):
    try:
        r = requests.get(f"{CLOB}/midpoint", params={"token_id": no_token}, timeout=8)
        r.raise_for_status()
        val = float(r.json().get("mid", 0))
        return round(val * 100, 1) if val > 0 else None
    except:
        return None

def get_price_no(no_token):
    return get_no_price(no_token) or get_no_midpoint(no_token)

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

# ── /prices COMMAND ───────────────────────────────────────────────────────────
def send_prices(chat_id):
    if not MARKETS:
        tg("⚠️ No markets loaded yet.", chat_id)
        return

    lines = []
    prev_no = None
    for label, date_str, yes_token, no_token in MARKETS:
        no  = get_price_no(no_token)
        yes = round(100 - no, 1) if no else None
        d   = days_to(date_str)

        if no:
            # Implied daily NO decay vs previous date
            rate_str = ""
            if prev_no and no < prev_no:
                rate_str = f"  ↘ {round(prev_no - no, 1)}¢ cheaper"
            lines.append(
                f"  <b>{label}</b> ({d}d)  NO: <b>{no}¢</b>  YES: {yes}¢{rate_str}"
            )
            prev_no = no
        else:
            lines.append(f"  <b>{label}</b> ({d}d)  —")

    tg(
        f"📊 <b>Live ceasefire NO prices</b>\n"
        f"<i>{datetime.now(timezone.utc).strftime('%H:%M UTC')}</i>\n\n"
        + "\n".join(lines) +
        f"\n\n<i>NO pays $1 if no ceasefire by that date</i>\n"
        f"👉 <a href='{MARKET_URL}'>Open market</a>",
        chat_id
    )

# ── TELEGRAM POLLING ──────────────────────────────────────────────────────────
async def command_loop():
    global last_update_id
    log("Command listener started — send /prices to bot anytime")
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params={"offset": last_update_id + 1, "timeout": 20},
                timeout=30
            )
            updates = r.json().get("result", [])
            for update in updates:
                last_update_id = update["update_id"]
                msg = update.get("message", {})
                text = msg.get("text", "").strip().lower()
                chat_id = msg.get("chat", {}).get("id")
                if text in ["/prices", "/p"]:
                    log(f"Command /prices from {chat_id}")
                    send_prices(chat_id)
        except Exception as e:
            log(f"Command loop error: {e}")
        await asyncio.sleep(2)

# ── SIGNAL 1: VELOCITY ────────────────────────────────────────────────────────
def check_velocity():
    global baseline_velocity
    if not MARKETS:
        return
    # Use YES token for trade history (more liquid side)
    trades = get_recent_trades(MARKETS[1][2] if len(MARKETS) > 1 else MARKETS[0][2])
    if not trades:
        return

    now_ts = time.time()
    recent = sum(1 for t in trades
                 if now_ts - int(t.get("timestamp", 0)) / 1000 < 120)
    tpm = recent / 2.0  # per minute over 2-min window

    if baseline_velocity is None:
        baseline_velocity = max(tpm, 0.1)
        log(f"Velocity baseline: {baseline_velocity:.1f} t/min")
        return

    baseline_velocity = 0.9 * baseline_velocity + 0.1 * tpm
    ratio = tpm / baseline_velocity
    log(f"Velocity: {tpm:.1f} t/min  base={baseline_velocity:.1f}  ratio={ratio:.1f}x")

    if ratio >= VELOCITY_SPIKE_MULT and can_alert("velocity"):
        # Get Apr 15 NO price
        no = get_price_no(MARKETS[1][3] if len(MARKETS) > 1 else MARKETS[0][3])
        tg(
            f"🔥 <b>Volume velocity spike!</b>\n\n"
            f"Trade rate: <b>{tpm:.1f}/min</b> ({ratio:.1f}x normal)\n"
            f"Apr 15 NO: <b>{no}¢</b>  |  Days left: {days_to(MARKETS[1][1] if len(MARKETS) > 1 else MARKETS[0][1])}\n\n"
            f"Something moving before price reacts — check news.\n\n"
            f"👉 <a href='{MARKET_URL}'>Open market</a>"
        )

# ── SIGNAL 2: ORDERBOOK (NO side) ─────────────────────────────────────────────
def check_orderbook():
    if not MARKETS:
        return
    # Watch Apr 15 NO token orderbook
    m = MARKETS[1] if len(MARKETS) > 1 else MARKETS[0]
    book = get_orderbook(m[3])  # NO token
    if not book:
        return

    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if not bids or not asks:
        return

    best_bid   = round(float(bids[0]["price"]) * 100, 1)
    best_ask   = round(float(asks[0]["price"]) * 100, 1)
    spread     = round(best_ask - best_bid, 1)
    top_bid_sz = float(bids[0].get("size", 0))

    log(f"Book NO: bid={best_bid}¢ ask={best_ask}¢ spread={spread}¢ bid_sz=${top_bid_sz:.0f}")

    if top_bid_sz >= WHALE_SIZE_USDC and can_alert("whale_bid"):
        tg(
            f"🐋 <b>Whale NO bid — large accumulation</b>\n\n"
            f"${top_bid_sz:,.0f} NO buy order at {best_bid}¢\n"
            f"Spread: {spread}¢  |  Days left: {days_to(m[1])}\n\n"
            f"Smart money loading up on NO — betting ceasefire unlikely.\n\n"
            f"👉 <a href='{MARKET_URL}'>Open market</a>"
        )

# ── SIGNAL 3: CROSS-MARKET CURVE (NO) ────────────────────────────────────────
def check_curve():
    if len(MARKETS) < 3:
        return

    data = []
    for label, date_str, yes_token, no_token in MARKETS:
        no = get_price_no(no_token)
        d  = days_to(date_str)
        if no and no > 0:
            data.append((label, no, d))
            log(f"Curve NO: {label}={no}¢ ({d}d)")

    if len(data) < 3:
        return

    anomalies = []

    # NO prices should DECREASE as date gets further out
    # (further date = more chance of ceasefire = cheaper NO)
    for i in range(1, len(data)):
        if data[i][1] > data[i-1][1]:
            anomalies.append(
                f"⚠️ Inversion: {data[i][0]} NO ({data[i][1]}¢) > "
                f"{data[i-1][0]} NO ({data[i-1][1]}¢) — one is mispriced"
            )

    # Check rate consistency between intervals
    rates = []
    for i in range(1, len(data)):
        gap = data[i][2] - data[i-1][2]
        if gap > 0:
            drop = data[i-1][1] - data[i][1]   # NO should drop as dates extend
            rates.append((data[i-1][0], data[i][0], drop / gap))

    for i in range(1, len(rates)):
        if rates[i-1][2] > 0:
            ratio = rates[i][2] / rates[i-1][2]
            if ratio > 2.5 or ratio < 0.2:
                anomalies.append(
                    f"📐 Inconsistent drop: "
                    f"{rates[i-1][0]}→{rates[i-1][1]} {rates[i-1][2]:.1f}¢/day vs "
                    f"{rates[i][0]}→{rates[i][1]} {rates[i][2]:.1f}¢/day"
                )

    # Apr 15 vs curve extrapolation from Apr 30
    if len(data) >= 2 and data[1][2] > 0:
        ratio_days = data[0][2] / data[1][2]
        # NO for Apr15 should be higher than Apr30 (less time = less chance of ceasefire)
        extrapolated = round(data[1][1] + (data[1][1] * (1 - ratio_days) * 0.5), 1)
        dev_pct = abs(data[0][1] - extrapolated) / max(extrapolated, 1) * 100
        log(f"Curve: Apr15 NO actual={data[0][1]}¢ implied={extrapolated}¢ dev={dev_pct:.0f}%")

        if (dev_pct > CURVE_DEVIATION_PCT or anomalies) and can_alert("curve"):
            direction = "above" if data[0][1] > extrapolated else "below"
            curve_lines = "\n".join(f"  {l}: <b>{p}¢</b> NO" for l, p, d in data)
            rate_lines  = "\n".join(
                f"  {a}→{b}: {r:.1f}¢/day drop" for a, b, r in rates
            )
            tg(
                f"📐 <b>NO curve mispricing</b>\n\n"
                f"Apr 15 NO actual: <b>{data[0][1]}¢</b>\n"
                f"Curve-implied:    <b>{extrapolated}¢</b>  ({dev_pct:.0f}% {direction})\n\n"
                + ("\n".join(anomalies) + "\n\n" if anomalies else "") +
                f"<b>NO curve:</b>\n{curve_lines}\n\n"
                f"<b>Daily drop rate:</b>\n{rate_lines}\n\n"
                f"👉 <a href='{MARKET_URL}'>Open market</a>"
            )

# ── SIGNAL 4: NO PRICE SPIKE ──────────────────────────────────────────────────
def check_price():
    if not MARKETS:
        return
    # Monitor Apr 7 and Apr 15 NO prices
    targets = MARKETS[:2] if len(MARKETS) >= 2 else MARKETS

    for label, date_str, yes_token, no_token in targets:
        no = get_price_no(no_token)
        if not no:
            continue

        log(f"NO price {label}: {no}¢")
        price_history.append((label, no, time.time()))

        # Find previous reading for this label
        prev_entries = [(l, p, t) for l, p, t in price_history
                        if l == label and time.time() - t > 25]
        if not prev_entries:
            continue

        prev_no = prev_entries[-1][1]
        delta   = round(no - prev_no, 1)

        if abs(delta) >= SPIKE_CENTS and can_alert(f"spike_{label}"):
            arrow = "📈" if delta > 0 else "📉"
            signal = no_entry_signal(no)
            tg(
                f"{arrow} <b>{label} NO spike: {'+' if delta > 0 else ''}{delta}¢</b>\n\n"
                f"NO: <b>{no}¢</b>  (was {prev_no}¢)\n"
                f"Days left: {days_to(date_str)}\n\n"
                f"{signal}\n\n"
                f"👉 <a href='{MARKET_URL}'>Open market</a>"
            )

        # Threshold alerts
        if no <= 80 and prev_no > 80 and can_alert(f"below80_{label}"):
            tg(
                f"🔔 <b>{label} NO dropped below 80¢ → {no}¢</b>\n\n"
                f"Market pricing ceasefire probability unusually high.\n"
                f"Strong NO entry if you disagree.\n\n"
                f"👉 <a href='{MARKET_URL}'>Open market</a>"
            )

        if no >= 96 and prev_no < 96 and can_alert(f"above96_{label}"):
            tg(
                f"🔔 <b>{label} NO above 96¢ → {no}¢</b>\n\n"
                f"Market near-certain no ceasefire. Limited NO upside.\n"
                f"Consider YES as lottery ticket.\n\n"
                f"👉 <a href='{MARKET_URL}'>Open market</a>"
            )

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
        lines = []
        for label, date_str, yes_token, no_token in MARKETS:
            no = get_price_no(no_token)
            d  = days_to(date_str)
            lines.append(
                f"  {label} ({d}d): <b>{no}¢</b> NO" if no else f"  {label}: —"
            )
        tg(
            f"🕐 <b>{SUMMARY_HOURS}h Update — NO prices</b>\n\n"
            + "\n".join(lines) +
            f"\n\n<i>Send /prices anytime for live snapshot</i>"
        )

# ── STARTUP ───────────────────────────────────────────────────────────────────
async def main():
    global MARKETS
    log("Advanced tracker v4 starting (NO-focused)...")

    MARKETS = discover_markets()

    if not MARKETS:
        tg("❌ <b>Could not discover markets.</b> Check Gamma API / slugs.")
        return

    # Test all NO prices
    price_lines = []
    for label, date_str, yes_token, no_token in MARKETS:
        no = get_price_no(no_token)
        price_lines.append(f"  {label}: <b>{no}¢</b> NO" if no else f"  {label}: —")
    price_status = "\n".join(price_lines)

    tg(
        f"🚀 <b>Advanced Tracker v4 — NO focused</b>\n\n"
        f"<b>Markets:</b>\n"
        + "\n".join(f"  • {l} ({days_to(d)}d left)" for l, d, yt, nt in MARKETS) +
        f"\n\n{price_status}\n\n"
        f"<b>Signals:</b>\n"
        f"  🔥 Volume velocity\n"
        f"  🐋 Whale NO bid\n"
        f"  📐 NO curve mispricing\n"
        f"  ⚡ NO price spikes\n\n"
        f"Send <b>/prices</b> anytime for a live snapshot 📊"
    )

    await asyncio.gather(
        price_loop(),
        orderbook_loop(),
        velocity_loop(),
        curve_loop(),
        summary_loop(),
        command_loop(),
    )

if __name__ == "__main__":
    asyncio.run(main())
