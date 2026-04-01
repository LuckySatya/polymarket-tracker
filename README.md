# Polymarket Advanced Anomaly Tracker

Watches all US x Iran ceasefire markets simultaneously with 4 signals:

| Signal | What it catches |
|--------|----------------|
| 🔥 Volume velocity | Unusual trade rate before price moves |
| 🐋 Orderbook depth | Whale bids/asks, wide spreads |
| 📐 Cross-market curve | Apr15 mispriced vs Apr30/May31/Jun30 |
| ⚡ Price spike | Classic threshold breach |

## Deploy to Railway (free, runs 24/7)

1. Create GitHub repo, upload all 4 files
2. Go to railway.app → New Project → Deploy from GitHub
3. Done — Telegram alerts start within 60 seconds

## Tune thresholds (top of polymarket_advanced.py)

```python
VELOCITY_SPIKE_MULT  = 3.0   # alert if trade rate > 3x normal
SPREAD_ALERT_CENTS   = 8     # alert if bid-ask > 8¢
WHALE_SIZE_USDC      = 2000  # alert if top order > $2000
CURVE_DEVIATION_PCT  = 25    # alert if Apr15 deviates >25% from curve
SPIKE_CENTS          = 5     # alert on ±5¢ price move
SUMMARY_HOURS        = 6     # summary every 6 hours
COOLDOWN_SEC         = 300   # 5 min between same-type alerts
```
