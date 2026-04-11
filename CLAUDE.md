# EMA 50/200 1H Touch Bot — Development Log

Lightweight watchlist alert bot. No trading. No DB.
Two modes: manual watchlist (any EMA touch) + auto-screener (trending coins, pullback alerts only).
Deployed on Heroku as a worker process. KuCoin spot.

---

## Architecture

```
bot.py               — Main loop, Telegram polling, EMA check, alert logic
screener.py          — Auto-screener: scans top 100 KuCoin USDT pairs for trending coins
logger.py            — Telegram send/receive
watchlist.json       — Manual watchlist (user-added coins, auto-created)
auto_watchlist.json  — Auto-screener list (auto-created)
price_state.json     — Per-symbol EMA position state for pullback detection (auto-created)
```

No database. All state in plain JSON files. No trade management.

---

## Two Watchlist Modes

### Manual watchlist
User adds coins via Telegram. Alerts on **any** EMA50 or EMA200 touch (from above or below) with good volume.

### Auto-screener
Runs every hour. Scans top 100 KuCoin USDT pairs by 24h volume. Adds coins where:
- 1h close > EMA50 (price is in uptrend)
- 1h EMA50 > EMA200 (bullish macro alignment — faster average above slower)
- 1h volume > vol_ma × 1.5 (volume-confirmed move)

Monitors auto-added coins for **pullbacks only**:
- **EMA50 pullback**: price was above EMA50 → now wicks into/near EMA50 → alert
- **EMA200 breakdown touch**: price broke below EMA50 → now testing EMA200 → alert
- **Auto-remove**: if price falls >3% below EMA200 the coin is dropped from the auto-list

---

## How It Works

**Every 5 minutes:**
1. Fetch 1h OHLCV (220 candles) for each watched pair from KuCoin
2. Compute EMA50, EMA200 (exponential), vol_ma (20-period median)
3. **Manual watchlist**: check if candle touched either EMA (any direction)
4. **Auto-screener list**: check for directional pullback using stored price state
5. Volume gate applies to both: current 1h volume > vol_ma × 1.3
6. If conditions pass: send Telegram alert (4h cooldown per symbol/EMA)

**Every 1 hour:**
1. Fetch 24h tickers → sort USDT pairs by quote volume → take top 100
2. For each candidate: fetch 1h OHLCV, check close > EMA50 + volume gate
3. Add newly qualifying coins to auto-list; notify via Telegram

---

## Telegram Commands

| Input | Action |
|---|---|
| `BTC` | Add BTC/USDT to manual watchlist |
| `SOL/USDT` | Add SOL/USDT to manual watchlist |
| `/unwatch BTC` | Remove from manual watchlist |
| `/list` | Show manual watchlist |
| `/autolist` | Show auto-screener list |
| `/autounwatch BTC` | Remove from auto-screener list |
| `/scan` | Run screener immediately |
| `/help` | Command list |

**Symbol normalisation:** accepts bare tickers (BTC), slash pairs (BTC/USDT), no-slash (BTCUSDT), hyphen (BTC-USDT). Bare tickers default to /USDT. Symbol is verified against KuCoin before being added.

---

## Alert Message Formats

**Manual watchlist — any touch:**
```
──────────────────────
📍 EMA50 TOUCH — BTC/USDT
──────────────────────
Price   42350.0000
EMA50   42180.0000
(touching from above)

Volume  1.8× avg
1h candle · 14:00 UTC
```

**Auto-screener — EMA50 pullback:**
```
──────────────────────
🔄 EMA50 PULLBACK — BTC/USDT
──────────────────────
Price   82350.0000
EMA50   79180.0000
(pulling back from above)

Volume  1.8× avg
1h candle · 14:00 UTC
[auto-screener]
```

**Auto-screener — EMA200 breakdown touch:**
```
──────────────────────
⚠️ EMA200 BREAKDOWN TOUCH — ZEC/USDT
──────────────────────
Price   102.5000
EMA200   98.4000
(broke below EMA50 → testing EMA200)

Volume  2.1× avg
1h candle · 15:00 UTC
[auto-screener]
```

---

## Touch / Pullback Detection Logic

Both modes inspect **`df.iloc[-2]`** — the last *closed* 1h candle. `df.iloc[-1]` is the still-forming candle whose high/low/close are not final and would produce false positives. Checking a closed candle's full body/wick gives a definitive answer with no noise.

```python
# candle = df.iloc[-2]  (last closed)
candle_touched = low <= ema_val <= high          # wick or body passed through the level
close_near     = abs(close - ema_val) / ema_val <= 0.001   # closed within 0.1% of EMA
good_volume    = volume > vol_ma * 1.3         # manual watchlist (VOLUME_MULT)
good_vol_auto  = volume > vol_ma * 0.3         # auto-screener pullbacks (PULLBACK_VOLUME_MULT)

# Manual: fires if (candle_touched OR close_near) AND good_volume

# Auto — EMA50 pullback: additionally requires was_above_ema50 = True (from price_state.json)
#         uses good_vol_auto (0.3×) not good_volume — pullbacks naturally have lower volume
# Auto — EMA200 breakdown: requires was_above_ema50 AND now close < EMA50
```

EMA200 requires 200 candles to fully converge — fetches 220 to ensure accuracy.
`price_state.json` is updated after every 5-min check and persisted across restarts.

**Why this means 5-min check and 1-hour screener are the right intervals:**
A 1h candle closes once per hour. The 5-min check loop detects the close within 5 min of it happening — no faster polling is needed. The screener runs hourly, naturally aligned with candle closes. Neither interval is about rate limits (we use <2% of KuCoin's allowance); they're about how often 1h candles produce new information.

---

## Configuration

| Parameter | Value | Notes |
|---|---|---|
| `CHECK_INTERVAL` | 300s (5 min) | How often to check each pair |
| `AUTO_SCAN_INTERVAL` | 3600s (1 hour) | How often screener runs |
| `ALERT_COOLDOWN` | 4 hours | Per (symbol, EMA) pair |
| `VOLUME_MULT` | 1.3× | Volume gate for manual-watchlist touch alerts |
| `PULLBACK_VOLUME_MULT` | 0.3× | Volume gate for auto-screener pullback alerts (lower — pullbacks have less volume than the breakout that qualified entry) |
| `AUTO_VOLUME_MULT` | 1.5× | Volume gate for screener entry (stricter) |
| `TOUCH_BUFFER` | 0.1% | Close-to-EMA proximity threshold |
| `AUTO_REMOVE_THRESH` | 0.97 | Remove from auto-list if close < EMA200 × 0.97 |
| `SCREENER_TOP_N` | 100 | Number of pairs screener checks (by 24h volume) |

---

## Environment Variables

| Var | Purpose |
|---|---|
| `TOKEN` | Telegram bot token |
| `CHAT_ID` | Telegram chat ID |
| `KUCOIN_API_KEY` | KuCoin API key (read-only) |
| `KUCOIN_SECRET` | KuCoin secret |
| `KUCOIN_PASSWORD` | KuCoin passphrase |

KuCoin keys are optional — public OHLCV data works without authentication. Keys only needed if rate limits become an issue.

---

## Running

```bash
cd /home/entitypak/claude/ema-50-200-1h-touch
pip install -r requirements.txt
python bot.py
```

**Deployment:** Heroku worker dyno (`Procfile`: `worker: python bot.py`), Python 3.10.

---

## Design Decisions

**Why vol_ma = median not mean?**
Crash/spike candles inflate the mean. A 3× spike candle in the 20-candle window raises the bar so high that normal candles look flat. Median is stable — up to 9 spike candles can't move it.

**Why check the closed candle (`df.iloc[-2]`) not the live one (`df.iloc[-1]`)?**
The live candle's high/low/close are constantly changing. A wick forming mid-candle might not be the final wick. Only a closed candle gives a definitive record of whether the body or wick touched/passed through the EMA. Using `iloc[-2]` means every alert is based on confirmed, final price data.

**Why 5-minute check interval?**
A 1h candle closes once per hour. The 5-min poll detects the close within 5 min of it happening — fast enough to be actionable. Polling faster would re-check the same closed candle repeatedly without new information. The 4h cooldown prevents duplicate alerts on the same closed candle during the 55 min before the next one closes.

**Why 4-hour cooldown?**
If price consolidates around EMA50 for several hours, without a cooldown every check fires an alert. 4h means you get alerted once per meaningful episode, not per candle.

**Why no MACD / RSI / structure filter?**
This bot's job is pure notification, not signal generation. The user decides whether to act. Adding signal logic here would duplicate claude-trading-bot. Keep it simple: touch + volume = alert.

**Why direction-aware pullback detection (auto-screener) vs any-touch (manual)?**
The screener's entire premise is "coin was trending up → now pulling back to support." Alerting on a touch from below (price bouncing off EMA50 after being below it) would be noise — that's not the setup. Storing `price_state.json` per symbol lets the bot know which direction the price came from.

**Why 1.5× volume for screener entry, 1.3× for manual alerts, and 0.5× for auto-screener pullback alerts?**
Screener entry (1.5×): only genuinely volume-confirmed trending moves get auto-added — strict to avoid filling the list with noise.

Manual alerts (1.3×): any-touch mode needs a volume confirmation that something meaningful is happening.

Auto-screener pullbacks (0.3×): a pullback to EMA50 by definition has *less* volume than the breakout that qualified it — that's what a pullback looks like. During consolidation, volume dries up as the breakout momentum fades and price drifts back to support. Using 1.3× here would block nearly every valid pullback setup. 0.5× filters completely dead/illiquid candles (where a handful of trades produce a meaningless wick) while letting all real pullbacks through.

**Why auto-remove at 3% below EMA200?**
If a coin crashes through both EMAs it's no longer a pullback setup — it's in freefall. Keeping it on the auto-list would generate breakdown alerts indefinitely. 3% gives a small buffer for wicks below EMA200 that recover, without holding dead setups forever.

**Why only scan top 100 pairs by 24h quote volume?**
Scanning all KuCoin USDT pairs (~300+) at 0.3s per pair would take 90+ seconds and hammer the rate limit. Top 100 by volume covers all meaningful liquid coins. Low-volume pairs are unlikely to produce clean EMA setups anyway.

**Why does the screener run at boot (last_scan_ts = 0)?**
On fresh deploy or restart the auto-list may be empty. Running immediately ensures it's populated within seconds rather than waiting up to an hour for the first scheduled scan.
