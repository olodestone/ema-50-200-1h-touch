"""
EMA 50/200 1H Touch Alert Bot

Two watchlists:
  Manual  — user-added coins, alerts on any EMA50/EMA200 touch with good volume
  Auto    — screener-discovered coins (close > EMA50 + volume), alerts only on
            pullbacks to EMA50 or breakdown touches of EMA200
"""

import os
import json
import time
import ccxt
import pandas as pd
from datetime import datetime, timedelta
from logger import send_telegram, get_updates
from screener import scan_trending_coins
from explosive_screener import scan_explosive_setups

# ─── Config ────────────────────────────────────────────────────────────────
WATCHLIST_FILE         = "watchlist.json"
AUTO_WATCHLIST_FILE    = "auto_watchlist.json"
PRICE_STATE_FILE       = "price_state.json"
ALERT_STATE_FILE       = "alert_state.json"
EXPLOSIVE_STATE_FILE   = "explosive_alerts.json"
HISTORY_FILE           = "alert_history.json"
HISTORY_MAX            = 50   # keep last N alerts across all types

CHECK_INTERVAL        = 300           # seconds between candle checks (5 min)
AUTO_SCAN_INTERVAL    = 3600          # seconds between screener scans (1 hour)
ALERT_COOLDOWN        = timedelta(hours=4)
VOLUME_MULT           = 1.3           # volume gate for manual-watchlist touch alerts
PULLBACK_VOLUME_MULT  = 0.3           # volume gate for auto-screener pullback alerts
                                      # (pullbacks naturally have lower volume than
                                      #  the breakout that qualified the coin for the
                                      #  screener — 0.3× filters dead/illiquid candles
                                      #  without blocking normal consolidation touches)
TOUCH_BUFFER             = 0.001      # 0.1% — close within this of EMA counts as touch
FRESH_CROSS_TOUCH_BUFFER = 0.005      # 0.5% — wider buffer for fresh golden cross coins
                                      # (EMA50/200 are close together post-cross; price
                                      #  may dip slightly below EMA50 before bouncing)
AUTO_REMOVE_THRESH    = 0.97          # auto-remove if close < EMA200 × this (3% below)
FRESH_CROSS_WINDOW    = 72            # hours — ⭐ tag, wider touch buffer, "fresh cross" label
CROSS_INFO_WINDOW     = 336           # hours (14 days) — informational 📌 note on alerts

EXPLOSIVE_COOLDOWNS = {
    "FRESH_CROSS": timedelta(days=7),
    "COIL":        timedelta(days=3),
    "REVERSAL":    timedelta(days=3),
    "PULLBACK":    timedelta(days=2),
}
EXPLOSIVE_SCAN_HOUR = 0   # run after this UTC hour (00:30 UTC daily)
EXPLOSIVE_SCAN_MIN  = 30

TOKEN   = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# ─── Exchanges ──────────────────────────────────────────────────────────────
exchange = ccxt.kucoin({
    "apiKey":    os.getenv("KUCOIN_API_KEY",    ""),
    "secret":    os.getenv("KUCOIN_SECRET",     ""),
    "password":  os.getenv("KUCOIN_PASSWORD",   ""),
    "enableRateLimit": True,
})

try:
    mexc_exchange = ccxt.mexc({
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })
except Exception as _mexc_err:
    print(f"MEXC spot init failed: {_mexc_err}")
    mexc_exchange = None

try:
    mexc_swap_exchange = ccxt.mexc({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })
except Exception as _mexc_swap_err:
    print(f"MEXC swap init failed: {_mexc_swap_err}")
    mexc_swap_exchange = None

# ─── Alert cooldown state (persisted) ───────────────────────────────────────
# key: "SYMBOL|label"  value: datetime of last alert sent
# Persisted so restarts don't re-fire alerts within the 4h cooldown window.
last_alert: dict = {}

# ─── Alert history (persisted) ───────────────────────────────────────────────
# Rolling list of last HISTORY_MAX alert records across all alert types.
alert_history: list = []

# ─── Persistence ─────────────────────────────────────────────────────────────
def load_watchlist() -> list:
    if not os.path.exists(WATCHLIST_FILE):
        return []
    try:
        with open(WATCHLIST_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_watchlist(watchlist: list):
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(sorted(set(watchlist)), f, indent=2)


def load_auto_watchlist() -> list:
    if not os.path.exists(AUTO_WATCHLIST_FILE):
        return []
    try:
        with open(AUTO_WATCHLIST_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_auto_watchlist(watchlist: list):
    with open(AUTO_WATCHLIST_FILE, "w") as f:
        json.dump(sorted(set(watchlist)), f, indent=2)


def load_price_state() -> dict:
    if not os.path.exists(PRICE_STATE_FILE):
        return {}
    try:
        with open(PRICE_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_price_state(state: dict):
    with open(PRICE_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def load_alert_state() -> dict:
    """Load last_alert from disk. Keys are 'SYMBOL|label', values are datetime."""
    if not os.path.exists(ALERT_STATE_FILE):
        return {}
    try:
        with open(ALERT_STATE_FILE) as f:
            raw = json.load(f)
        return {k: datetime.fromisoformat(v) for k, v in raw.items()}
    except Exception:
        return {}


def save_alert_state(state: dict):
    """Persist last_alert to disk. Converts datetime values to ISO strings."""
    with open(ALERT_STATE_FILE, "w") as f:
        json.dump({k: v.isoformat() for k, v in state.items()}, f, indent=2)


def load_explosive_state() -> dict:
    """Load explosive scan state: last_scan_date + per-signal cooldown timestamps."""
    if not os.path.exists(EXPLOSIVE_STATE_FILE):
        return {"last_scan_date": None, "alerts": {}}
    try:
        with open(EXPLOSIVE_STATE_FILE) as f:
            data = json.load(f)
        alerts = {k: datetime.fromisoformat(v) for k, v in data.get("alerts", {}).items()}
        return {"last_scan_date": data.get("last_scan_date"), "alerts": alerts}
    except Exception:
        return {"last_scan_date": None, "alerts": {}}


def save_explosive_state(state: dict):
    with open(EXPLOSIVE_STATE_FILE, "w") as f:
        json.dump({
            "last_scan_date": state.get("last_scan_date"),
            "alerts": {k: v.isoformat() for k, v in state["alerts"].items()},
        }, f, indent=2)


def load_history() -> list:
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def save_history():
    with open(HISTORY_FILE, "w") as f:
        json.dump(alert_history[-HISTORY_MAX:], f, indent=2)


def append_history(record: dict):
    alert_history.append(record)
    if len(alert_history) > HISTORY_MAX:
        alert_history[:] = alert_history[-HISTORY_MAX:]
    save_history()


# ─── Symbol normalisation ────────────────────────────────────────────────────
def normalise_symbol(raw: str) -> str | None:
    """
    Accept inputs like: BTC, btc, BTC/USDT, BTCUSDT, BTC-USDT
    Returns ccxt-format "BTC/USDT" or None if unrecognisable.
    """
    s = raw.upper().strip()
    # Strip perpetual/futures suffixes from charting tools (e.g. BTCUSDT.P, BTC.P)
    for suffix in (".P", "-PERP", "_PERP", ".PERP"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
            break
    if "/" in s:
        base, quote = s.split("/", 1)
        if base and quote:          # guard against "/AVNT" → base="", quote="AVNT"
            return f"{base}/{quote}"
        s = base or quote           # strip the stray slash and continue
    if "-" in s:
        base, quote = s.split("-", 1)
        return f"{base}/{quote}"
    for quote in ("USDT", "USDC", "BTC", "ETH", "BNB"):
        if s.endswith(quote) and len(s) > len(quote):
            base = s[: -len(quote)]
            return f"{base}/{quote}"
    if s.isalpha():
        return f"{s}/USDT"
    return None


def verify_symbol(symbol: str) -> bool:
    try:
        exchange.load_markets()
        return symbol in exchange.markets
    except Exception:
        return False


# ─── Indicator helpers ────────────────────────────────────────────────────────
def fetch_1h_ohlcv(symbol: str) -> pd.DataFrame | None:
    try:
        raw = exchange.fetch_ohlcv(symbol, "1h", limit=220)
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        return df
    except Exception as e:
        print(f"  fetch_ohlcv error {symbol}: {e}")
        return None


def compute_emas(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema50"]  = df["close"].ewm(span=50,  adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    df["vol_ma"] = df["volume"].rolling(20).median()
    return df


# ─── Manual watchlist: any EMA touch ─────────────────────────────────────────
def check_touch(symbol: str) -> list[dict]:
    """
    Returns alert dicts for each EMA touched on the last *closed* 1h candle (any direction).
    Uses iloc[-2] — iloc[-1] is the still-forming candle whose high/low/close are not final.
    """
    df = fetch_1h_ohlcv(symbol)
    if df is None or len(df) < 205:
        return []

    df = compute_emas(df)
    last   = df.iloc[-2]   # last closed candle
    close  = last["close"]
    high   = last["high"]
    low    = last["low"]
    vol    = last["volume"]
    vol_ma = last["vol_ma"]

    if pd.isna(vol_ma) or vol_ma == 0:
        return []

    good_volume = vol > vol_ma * VOLUME_MULT

    alerts = []
    for label, ema_val in [("EMA50", last["ema50"]), ("EMA200", last["ema200"])]:
        if pd.isna(ema_val):
            continue
        candle_touched = low <= ema_val <= high
        close_near     = abs(close - ema_val) / ema_val <= TOUCH_BUFFER
        if (candle_touched or close_near) and good_volume:
            alerts.append({
                "symbol":     symbol,
                "ema_label":  label,
                "ema_value":  round(ema_val, 6),
                "close":      round(close, 6),
                "volume":     round(vol, 2),
                "vol_ma":     round(vol_ma, 2),
                "alert_type": "touch",
            })
    return alerts


# ─── Auto-watchlist: pullback / breakdown touch only ─────────────────────────
def check_pullback(symbol: str, state: dict) -> tuple[list[dict], dict]:
    """
    Direction-aware check — only alerts when price was previously *above*
    the EMA being tested.

    Alert types:
      pullback  — was above EMA50, candle now wicks into / near EMA50
      breakdown — was above EMA50, now below EMA50, candle touches EMA200

    Returns (alerts, new_state).
    new_state includes "auto_remove": True if price is 3%+ below EMA200.
    """
    df = fetch_1h_ohlcv(symbol)
    if df is None or len(df) < 205:
        return [], state

    df = compute_emas(df)
    last   = df.iloc[-2]   # last closed candle — high/low/close are final
    close  = last["close"]
    high   = last["high"]
    low    = last["low"]
    vol    = last["volume"]
    vol_ma = last["vol_ma"]
    ema50  = last["ema50"]
    ema200 = last["ema200"]

    if pd.isna(vol_ma) or vol_ma == 0 or pd.isna(ema50) or pd.isna(ema200):
        return [], state

    good_volume   = vol > vol_ma * PULLBACK_VOLUME_MULT
    now_above_50  = close > ema50
    now_above_200 = close > ema200

    # Default True: coin was above EMA50 when added by screener
    was_above_50  = state.get("above_ema50",  True)

    # Fresh golden cross context — passed through to the alert
    cross_ts_str  = state.get("cross_ts")
    is_fresh_cross = False
    if cross_ts_str:
        try:
            cross_dt = datetime.fromisoformat(cross_ts_str).replace(tzinfo=None)
            is_fresh_cross = (datetime.utcnow() - cross_dt).total_seconds() < FRESH_CROSS_WINDOW * 3600
        except Exception:
            pass

    # Wider touch buffer right after a golden cross — EMA50/200 are close together
    # and price may dip slightly below EMA50 before bouncing
    touch_buf = FRESH_CROSS_TOUCH_BUFFER if is_fresh_cross else TOUCH_BUFFER

    vol_ratio = round(vol / vol_ma, 2) if vol_ma else 0
    ema50_dist = round((close - ema50) / ema50 * 100, 2)
    print(f"    {symbol} | close={close:.6g} low={low:.6g} EMA50={ema50:.6g} dist={ema50_dist:+.2f}% vol={vol_ratio}×avg was_above={was_above_50} good_vol={good_volume} fresh_cross={is_fresh_cross}")

    alerts = []

    # EMA50 pullback: was above, now candle touches EMA50
    if was_above_50:
        candle_touched = low <= ema50 <= high
        close_near     = abs(close - ema50) / ema50 <= touch_buf
        if (candle_touched or close_near) and good_volume:
            alerts.append({
                "symbol":        symbol,
                "ema_label":     "EMA50",
                "ema_value":     round(ema50, 6),
                "close":         round(close, 6),
                "volume":        round(vol, 2),
                "vol_ma":        round(vol_ma, 2),
                "alert_type":    "pullback",
                "is_fresh_cross": is_fresh_cross,
                "cross_ts":      cross_ts_str,
            })

    # EMA200 breakdown touch: was above EMA50, now below EMA50, touching EMA200
    if was_above_50 and not now_above_50:
        candle_touched = low <= ema200 <= high
        close_near     = abs(close - ema200) / ema200 <= TOUCH_BUFFER
        if (candle_touched or close_near) and good_volume:
            alerts.append({
                "symbol":        symbol,
                "ema_label":     "EMA200",
                "ema_value":     round(ema200, 6),
                "close":         round(close, 6),
                "volume":        round(vol, 2),
                "vol_ma":        round(vol_ma, 2),
                "alert_type":    "breakdown",
                "is_fresh_cross": is_fresh_cross,
                "cross_ts":      cross_ts_str,
            })

    # Cast to plain Python bool — numpy.bool_ is not JSON-serialisable
    # Preserve cross_ts / entry_reason so fresh-cross tracking survives restarts
    new_state = {
        "above_ema50":  bool(now_above_50),
        "above_ema200": bool(now_above_200),
        "auto_remove":  bool(close < ema200 * AUTO_REMOVE_THRESH),
    }
    if cross_ts_str:
        new_state["cross_ts"]     = cross_ts_str
        new_state["entry_reason"] = state.get("entry_reason", "golden_cross")
    return alerts, new_state


# ─── Alert formatting ─────────────────────────────────────────────────────────
def _fmt(p: float) -> str:
    if p >= 1000:  return f"{p:,.2f}"
    if p >= 1:     return f"{p:.4f}"
    if p >= 0.01:  return f"{p:.6f}"
    return f"{p:.8f}"


def send_alert(alert: dict):
    symbol    = alert["symbol"]
    label     = alert["ema_label"]
    ema_val   = alert["ema_value"]
    close     = alert["close"]
    volume    = alert["volume"]
    vol_ma    = alert["vol_ma"]
    vol_ratio = round(volume / vol_ma, 2) if vol_ma else 0
    kind      = alert.get("alert_type", "touch")

    is_fresh  = alert.get("is_fresh_cross", False)
    cross_ts  = alert.get("cross_ts")
    cross_line = ""
    if cross_ts:
        try:
            cross_dt  = datetime.fromisoformat(cross_ts).replace(tzinfo=None)
            hours_ago = (datetime.utcnow() - cross_dt).total_seconds() / 3600
            if is_fresh:
                # < 72h: fresh cross — show hours
                cross_line = f"\n🌟 Fresh golden cross ({int(hours_ago)}h ago)"
            elif hours_ago < CROSS_INFO_WINDOW:
                # 72h–336h: older cross — show days, informational only
                days_ago   = int(hours_ago / 24)
                cross_line = f"\n📌 Golden cross {days_ago}d ago"
        except Exception:
            pass

    if kind == "pullback":
        header    = f"{'⭐' if is_fresh else '🔄'} EMA50 PULLBACK — {symbol}"
        direction = "pulling back from above"
        footer    = f"{cross_line}\n[auto-screener]"
    elif kind == "breakdown":
        header    = f"⚠️ EMA200 BREAKDOWN TOUCH — {symbol}"
        direction = "broke below EMA50 → testing EMA200"
        footer    = f"{cross_line}\n[auto-screener]"
    else:
        direction = "touching from above" if close >= ema_val else "touching from below"
        header    = f"📍 {label} TOUCH — {symbol}"
        footer    = ""

    msg = (
        f"{'─' * 22}\n"
        f"{header}\n"
        f"{'─' * 22}\n"
        f"Price   {_fmt(close)}\n"
        f"{label:<7} {_fmt(ema_val)}\n"
        f"({direction})\n"
        f"\n"
        f"Volume  {vol_ratio}× avg\n"
        f"1h candle · {datetime.utcnow().strftime('%H:%M UTC')}"
        f"{footer}"
    )
    print(msg)
    send_telegram(msg)
    append_history({
        "ts":        datetime.utcnow().isoformat(timespec="seconds"),
        "kind":      kind,
        "symbol":    symbol,
        "label":     label,
        "close":     close,
        "ema_val":   ema_val,
        "vol_ratio": vol_ratio,
    })


# ─── Telegram command handler ─────────────────────────────────────────────────
def handle_command(text: str, watchlist: list, auto_watchlist: list) -> str | None:
    """
    Returns a reply string or None.
    Returns the sentinel "SCAN" to trigger a screener run from the main loop.
    Mutates watchlist / auto_watchlist in-place and persists changes.
    """
    text  = text.strip()
    lower = text.lower()

    # /help
    if lower in ("/help", "help"):
        return (
            "EMA Touch Bot — Commands\n"
            "─────────────────────\n"
            "Manual watchlist (any touch):\n"
            "  BTC / ETH / SOL/USDT — add\n"
            "  /unwatch BTC — remove\n"
            "  /list — show list\n"
            "\n"
            "Auto-screener (pullbacks only):\n"
            "  /autolist — show auto-discovered coins\n"
            "  /autounwatch BTC — remove from auto-list\n"
            "  /scan — run screener now\n"
            "\n"
            "Daily explosive setups:\n"
            "  /escan — run now (auto-runs 00:30 UTC daily)\n"
            "\n"
            "Review missed alerts:\n"
            "  /missed — last 10 alerts\n"
            "  /missed 20 — last 20 alerts\n"
            "\n"
            "/help — this message"
        )

    # /list
    if lower in ("/list", "list"):
        if not watchlist:
            return "Manual watchlist is empty. Send a coin name to start watching."
        lines = "\n".join(f"• {s}" for s in sorted(watchlist))
        return f"Manual watchlist ({len(watchlist)} pair(s)):\n{lines}"

    # /autolist
    if lower in ("/autolist", "autolist"):
        if not auto_watchlist:
            return "Auto-screener list is empty. Use /scan to discover trending coins."
        lines = "\n".join(f"• {s}" for s in sorted(auto_watchlist))
        return f"Auto-screener list ({len(auto_watchlist)} pair(s)):\n{lines}"

    # /scan — handled in main loop; return sentinel
    if lower in ("/scan", "scan"):
        return "SCAN"

    # /escan — trigger daily explosive scan manually
    if lower in ("/escan", "escan"):
        return "ESCAN"

    # /missed [N] — show last N alerts (default 10, max 20)
    if lower.startswith("/missed") or lower == "missed":
        parts = text.split()
        n = 10
        if len(parts) > 1:
            try:
                n = max(1, min(20, int(parts[1])))
            except ValueError:
                pass
        if not alert_history:
            return "No alerts recorded yet."
        recent = alert_history[-n:][::-1]  # newest first
        lines  = [f"Last {len(recent)} alert(s) — newest first:"]
        sep    = "─" * 22
        lines.append(sep)
        for r in recent:
            try:
                dt     = datetime.fromisoformat(r["ts"])
                ts_str = dt.strftime("%d %b %H:%M UTC")
            except Exception:
                ts_str = str(r.get("ts", ""))[:16]
            kind = r.get("kind", "")
            sym  = r.get("symbol", "")
            if kind == "explosive":
                signal = r.get("signal", "")
                exch   = r.get("exchange", "")
                em     = {"FRESH_CROSS": "🌟", "COIL": "💥", "REVERSAL": "⚡", "PULLBACK": "🔁"}.get(signal, "📊")
                c      = r.get("close", 0)
                e50    = r.get("ema50", 0)
                extra  = ""
                if signal == "COIL":
                    extra = f"  Vol {r.get('vol_ratio', 0)}×"
                elif signal == "PULLBACK":
                    extra = f"  Peak +{r.get('peak_pct', 0):.0f}%"
                elif signal == "FRESH_CROSS":
                    extra = f"  Gap +{r.get('gap_pct', 0):.1f}%"
                lines.append(f"{em} {signal} · {ts_str}\n{sym} · {exch}\nClose {_efmt(c)}  EMA50 {_efmt(e50)}{extra}")
            else:
                label   = r.get("label", "EMA?")
                em_map  = {"touch": "📍", "pullback": "🔄", "breakdown": "⚠️"}
                em      = em_map.get(kind, "📍")
                c       = r.get("close", 0)
                ema_val = r.get("ema_val", 0)
                vr      = r.get("vol_ratio", 0)
                lines.append(f"{em} {label} {kind.upper()} · {ts_str}\n{sym}\nClose {_efmt(c)}  {label} {_efmt(ema_val)}  Vol {vr}×")
            lines.append(sep)
        return "\n".join(lines)

    # /unwatch SYMBOL
    if lower.startswith("/unwatch ") or lower.startswith("unwatch "):
        raw = text.split(None, 1)[1] if " " in text else ""
        sym = normalise_symbol(raw) if raw else None
        if not sym:
            return "Usage: /unwatch BTC  or  /unwatch BTC/USDT"
        if sym in watchlist:
            watchlist.remove(sym)
            save_watchlist(watchlist)
            return f"Removed {sym} from manual watchlist."
        return f"{sym} is not in the manual watchlist."

    # /autounwatch SYMBOL
    if lower.startswith("/autounwatch ") or lower.startswith("autounwatch "):
        raw = text.split(None, 1)[1] if " " in text else ""
        sym = normalise_symbol(raw) if raw else None
        if not sym:
            return "Usage: /autounwatch BTC"
        if sym in auto_watchlist:
            auto_watchlist.remove(sym)
            save_auto_watchlist(auto_watchlist)
            return f"Removed {sym} from auto-screener list."
        return f"{sym} is not in the auto-screener list."

    # /watch SYMBOL  or  bare symbol
    raw = text.lstrip("/").replace("watch", "").strip() if lower.startswith("/watch") else text
    sym = normalise_symbol(raw)
    if not sym:
        return f"Could not parse '{text}' as a symbol. Try: BTC or SOL/USDT"

    if sym in watchlist:
        return f"{sym} is already in the manual watchlist."

    send_telegram(f"Checking {sym} on KuCoin...")
    if not verify_symbol(sym):
        return f"❌ {sym} not found on KuCoin. Try the full pair, e.g. {sym.split('/')[0]}/USDT"

    watchlist.append(sym)
    save_watchlist(watchlist)
    return (
        f"✅ Added {sym} to manual watchlist.\n"
        f"Will alert on EMA50/EMA200 1h touches with good volume.\n"
        f"Watching {len(watchlist)} pair(s) total."
    )


# ─── Screener run ─────────────────────────────────────────────────────────────
def run_screener(auto_watchlist: list, price_state: dict):
    """Runs the screener, adds new trending coins, notifies via Telegram."""
    print(f"[{datetime.utcnow().strftime('%H:%M')}] Running auto-screener...")
    try:
        found     = scan_trending_coins(exchange)
        new_items = [c for c in found if c["symbol"] not in auto_watchlist]
        for item in new_items:
            s = item["symbol"]
            auto_watchlist.append(s)
            if s not in price_state:
                state_entry = {"above_ema50": True, "above_ema200": True}
                # Store cross_ts for any coin that had a recent golden cross —
                # both Path B (direct cross detection) and Path A (momentum coin
                # where the cross happened up to FRESH_CROSS_WINDOW_H hours ago).
                # cross_ts is already an ISO string from detect_golden_cross().
                cross_ts = item.get("cross_ts")
                if cross_ts is not None:
                    state_entry["cross_ts"]     = cross_ts
                    state_entry["entry_reason"] = item.get("entry_reason", "momentum")
                price_state[s] = state_entry

        if new_items:
            save_auto_watchlist(auto_watchlist)
            save_price_state(price_state)

            # Separate golden cross coins from momentum coins for different alerts
            cross_items    = [i for i in new_items if i.get("entry_reason") == "golden_cross"]
            momentum_items = [i for i in new_items if i.get("entry_reason") != "golden_cross"]

            def _fmt_entry(item):
                sym   = item["symbol"]
                c     = item["close"]
                e50   = item["ema50"]
                e200  = item["ema200"]
                pct   = item["pct"]
                vr    = item["vol_ratio"]
                prec  = max(len(f"{c:.8f}".rstrip("0").split(".")[-1]), 2)
                fmt   = f"{{:.{prec}f}}"
                # Show cross note when a golden cross was found within 14 days
                # but older than 72h (72h+ gets the ⭐ fresh-cross treatment instead)
                cross_note = ""
                cross_ts = item.get("cross_ts")
                if cross_ts:
                    try:
                        cross_dt  = datetime.fromisoformat(cross_ts).replace(tzinfo=None)
                        hours_ago = (datetime.utcnow() - cross_dt).total_seconds() / 3600
                        if hours_ago >= FRESH_CROSS_WINDOW:
                            days_ago = int(hours_ago / 24)
                            cross_note = f"\n  📌 Golden cross {days_ago}d ago"
                    except Exception:
                        pass
                return (f"• {sym}\n"
                        f"  Price {fmt.format(c)}  EMA50 {fmt.format(e50)}"
                        f"  EMA200 {fmt.format(e200)}"
                        f"  (+{pct:.1f}%)  vol {vr}×avg"
                        f"{cross_note}")

            def _fmt_cross_entry(item):
                sym     = item["symbol"]
                c       = item["close"]
                e50     = item["ema50"]
                e200    = item["ema200"]
                prec    = max(len(f"{c:.8f}".rstrip("0").split(".")[-1]), 2)
                fmt     = f"{{:.{prec}f}}"
                gap_pct = round((e50 - e200) / e200 * 100, 2)
                # Show actual cross candle time, not current time
                cross_ts = item.get("cross_ts")
                try:
                    cross_dt  = datetime.fromisoformat(cross_ts)
                    hours_ago = int((datetime.utcnow() - cross_dt.replace(tzinfo=None)).total_seconds() / 3600)
                    time_str  = f"crossed {cross_dt.strftime('%d %b %H:%M UTC')}"
                    if hours_ago > 0:
                        time_str += f" ({hours_ago}h ago)"
                except Exception:
                    time_str = f"detected {datetime.utcnow().strftime('%H:%M UTC')}"
                return (f"{'─' * 22}\n"
                        f"🌟 GOLDEN CROSS — {sym}\n"
                        f"{'─' * 22}\n"
                        f"EMA50  crossed above EMA200\n"
                        f"EMA50   {fmt.format(e50)}\n"
                        f"EMA200  {fmt.format(e200)}  (+{gap_pct}%)\n"
                        f"Price   {fmt.format(c)}\n"
                        f"\nWatch for pullbacks to EMA50\n"
                        f"{time_str}\n"
                        f"[auto-added to watchlist]")

            # Fire individual golden cross alerts — one per coin
            for item in sorted(cross_items, key=lambda x: x["symbol"]):
                msg = _fmt_cross_entry(item)
                print(msg)
                send_telegram(msg)

            # Batch momentum coins into a single summary
            if momentum_items:
                send_telegram(
                    f"🔍 Screener found {len(momentum_items)} new trending coin(s):\n"
                    + "\n".join(_fmt_entry(item) for item in sorted(momentum_items, key=lambda x: x["symbol"]))
                    + f"\nAuto-list total: {len(auto_watchlist)}"
                )
        else:
            print("  Screener: no new trending coins.")
            send_telegram(
                f"🔍 Screener complete — {len(auto_watchlist)} pair(s) in auto-list, no new additions."
            )
    except Exception as e:
        print(f"Screener error: {e}")


# ─── Explosive daily alert formatting ────────────────────────────────────────
def _efmt(p: float) -> str:
    if p >= 1000:  return f"{p:,.2f}"
    if p >= 1:     return f"{p:.4f}"
    if p >= 0.01:  return f"{p:.6f}"
    return f"{p:.8f}"


def send_explosive_alert(sig: dict):
    sym    = sig["symbol"]
    exch   = sig["exchange"]
    signal = sig["signal"]
    close  = sig["close"]
    ema50  = sig["ema50"]
    ema200 = sig["ema200"]
    date_str = datetime.utcnow().strftime("%d %b %Y")

    if signal == "FRESH_CROSS":
        gap_pct  = sig.get("gap_pct", 0)
        cross_ts = sig.get("cross_ts", "")
        cross_line = ""
        if cross_ts:
            try:
                dt = pd.Timestamp(cross_ts)
                cross_line = f"\nCrossed {dt.strftime('%d %b %H:%M')} UTC (1h)"
            except Exception:
                pass
        header = f"🌟 1H FRESH CROSS — {sym}"
        body = (
            f"EMA50  crossed above EMA200 on 1h\n"
            f"EMA50   {_efmt(ema50)}\n"
            f"EMA200  {_efmt(ema200)}  (+{gap_pct:.1f}%)\n"
            f"Close   {_efmt(close)}"
            f"{cross_line}\n"
            f"\nExplosive move likely ahead"
        )

    elif signal == "COIL":
        vol_ratio = sig.get("vol_ratio", 0)
        range_pct = sig.get("range_pct", 0)
        ema_gap   = sig.get("ema_gap", 0)
        header = f"💥 DAILY COIL BREAKOUT — {sym}"
        body = (
            f"Dormant {range_pct:.0f}% range (14d)\n"
            f"EMAs compressed ({ema_gap:.1f}% gap)\n"
            f"Volume  {vol_ratio}× avg  ← surge\n"
            f"Close   {_efmt(close)}\n"
            f"EMA50   {_efmt(ema50)}\n"
            f"EMA200  {_efmt(ema200)}"
        )

    elif signal == "REVERSAL":
        vol_ratio  = sig.get("vol_ratio", 0)
        daily_pct  = sig.get("daily_pct", 0)
        ema_gap    = sig.get("ema_gap_pct", 0)
        header = f"⚡ DAILY REVERSAL — {sym}"
        body = (
            f"Downtrend broken — first explosive surge\n"
            f"+{daily_pct:.0f}% on the day  Vol {vol_ratio}× avg\n"
            f"Close broke {((close/ema50 - 1)*100):.0f}% above EMA50\n"
            f"EMA50   {_efmt(ema50)}  (EMA200 {ema_gap:.0f}% above)\n"
            f"Close   {_efmt(close)}"
        )

    else:  # PULLBACK
        dist_pct = sig.get("dist_pct", 0)
        peak_pct = sig.get("peak_pct", 0)
        header = f"🔁 DAILY PULLBACK ENTRY — {sym}"
        body = (
            f"After +{peak_pct:.0f}% run above EMA50\n"
            f"Now {dist_pct:+.1f}% from EMA50\n"
            f"EMA50   {_efmt(ema50)}\n"
            f"EMA200  {_efmt(ema200)}\n"
            f"Close   {_efmt(close)}"
        )

    msg = (
        f"{'═' * 22}\n"
        f"{header}\n"
        f"[{exch}]\n"
        f"{'═' * 22}\n"
        f"{body}\n"
        f"\nDaily candle · {date_str}"
    )
    print(msg)
    send_telegram(msg)
    record = {
        "ts":       datetime.utcnow().isoformat(timespec="seconds"),
        "kind":     "explosive",
        "signal":   signal,
        "symbol":   sym,
        "exchange": exch,
        "close":    close,
        "ema50":    ema50,
        "ema200":   ema200,
    }
    if signal == "FRESH_CROSS":
        record["gap_pct"] = sig.get("gap_pct", 0)
    elif signal == "COIL":
        record["vol_ratio"] = sig.get("vol_ratio", 0)
        record["range_pct"] = sig.get("range_pct", 0)
    elif signal == "PULLBACK":
        record["dist_pct"] = sig.get("dist_pct", 0)
        record["peak_pct"] = sig.get("peak_pct", 0)
    append_history(record)


def run_explosive_scan(expl_state: dict):
    """Run the daily explosive setup scan and fire Telegram alerts."""
    print(f"[{datetime.utcnow().strftime('%H:%M')}] Running daily explosive scan...")
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        setups = scan_explosive_setups(exchange, mexc_exchange, mexc_swap=mexc_swap_exchange)
        expl_state["last_scan_date"] = today_str
        if not setups:
            print("  [explosive] No setups found today.")
            save_explosive_state(expl_state)
            return
        fired = 0
        for sig in setups:
            key  = f"{sig['symbol']}|{sig['signal']}"
            cd   = EXPLOSIVE_COOLDOWNS.get(sig["signal"], timedelta(days=2))
            prev = expl_state["alerts"].get(key)
            if prev is None or (datetime.utcnow() - prev) >= cd:
                send_explosive_alert(sig)
                expl_state["alerts"][key] = datetime.utcnow()
                fired += 1
            else:
                remaining = cd - (datetime.utcnow() - prev)
                print(f"  {sig['symbol']} {sig['signal']} — cooldown ({int(remaining.total_seconds()/3600)}h left)")
        save_explosive_state(expl_state)
        print(f"  [explosive] {fired} alert(s) fired.")
    except Exception as e:
        print(f"[explosive] Scan error: {e}")
        expl_state["last_scan_date"] = today_str
        save_explosive_state(expl_state)


# ─── Main loop ────────────────────────────────────────────────────────────────
def run():
    print("EMA 50/200 Touch Bot started.")
    send_telegram("EMA Touch Bot online. Send a coin name to watch it, or /help for all commands.")

    watchlist      = load_watchlist()
    auto_watchlist = load_auto_watchlist()
    price_state    = load_price_state()
    last_alert.update(load_alert_state())
    alert_history[:] = load_history()

    if watchlist:
        send_telegram(f"Resumed manual watchlist: {', '.join(watchlist)}")
    if auto_watchlist:
        send_telegram(f"Resumed auto-screener list: {len(auto_watchlist)} pair(s)")

    tg_offset     = 0
    last_check_ts = 0.0
    last_scan_ts  = 0.0  # 0 so screener runs immediately on first boot

    expl_state              = load_explosive_state()
    last_explosive_scan_date = expl_state.get("last_scan_date")  # None → run immediately

    while True:
        # ── Poll Telegram for commands ──────────────────────────────────────
        try:
            updates = get_updates(tg_offset)
            for upd in updates:
                tg_offset = upd["update_id"] + 1
                msg  = upd.get("message", {})
                text = msg.get("text", "").strip()
                if not text:
                    continue
                print(f"← TG: {text!r}")
                reply = handle_command(text, watchlist, auto_watchlist)
                if reply == "SCAN":
                    send_telegram("Running screener scan... this may take a minute.")
                    run_screener(auto_watchlist, price_state)
                    last_scan_ts = time.time()
                elif reply == "ESCAN":
                    send_telegram("Running daily explosive setup scan... this may take a few minutes.")
                    run_explosive_scan(expl_state)
                    last_explosive_scan_date = datetime.utcnow().strftime("%Y-%m-%d")
                elif reply:
                    send_telegram(reply)
        except Exception as e:
            print(f"TG poll error: {e}")

        now     = time.time()
        now_utc = datetime.utcnow()

        # ── Auto-screener: hourly (or on /scan) ────────────────────────────
        if now - last_scan_ts >= AUTO_SCAN_INTERVAL:
            last_scan_ts = now
            run_screener(auto_watchlist, price_state)

        # ── Daily explosive scan: once per day at 00:30 UTC (or on first boot) ──
        today_str = now_utc.strftime("%Y-%m-%d")
        after_cutoff = now_utc.hour > EXPLOSIVE_SCAN_HOUR or (
            now_utc.hour == EXPLOSIVE_SCAN_HOUR and now_utc.minute >= EXPLOSIVE_SCAN_MIN
        )
        if today_str != last_explosive_scan_date and (last_explosive_scan_date is None or after_cutoff):
            last_explosive_scan_date = today_str
            run_explosive_scan(expl_state)

        # ── EMA checks every CHECK_INTERVAL ────────────────────────────────
        if now - last_check_ts >= CHECK_INTERVAL:
            last_check_ts = now
            ts_str = datetime.utcnow().strftime("%H:%M")

            if not watchlist and not auto_watchlist:
                print(f"[{ts_str}] Both watchlists empty — nothing to check.")

            # Manual watchlist — alert on any touch
            if watchlist:
                print(f"[{ts_str}] Checking {len(watchlist)} manual pair(s)...")
                for symbol in list(watchlist):
                    try:
                        alerts = check_touch(symbol)
                        for alert in alerts:
                            key  = f"{symbol}|{alert['ema_label']}"
                            prev = last_alert.get(key)
                            if prev is None or (datetime.utcnow() - prev) >= ALERT_COOLDOWN:
                                send_alert(alert)
                                last_alert[key] = datetime.utcnow()
                                save_alert_state(last_alert)
                            else:
                                remaining = ALERT_COOLDOWN - (datetime.utcnow() - prev)
                                print(f"  {symbol} {alert['ema_label']} — cooldown ({int(remaining.total_seconds()/60)}m left)")
                        if not alerts:
                            print(f"  {symbol} — no touch")
                    except Exception as e:
                        print(f"  {symbol} check error: {e}")
                    time.sleep(1.0)

            # Auto-watchlist — pullback / breakdown alerts only
            if auto_watchlist:
                print(f"[{ts_str}] Checking {len(auto_watchlist)} auto-screener pair(s)...")
                to_remove = []
                for symbol in list(auto_watchlist):
                    try:
                        state             = price_state.get(symbol, {"above_ema50": True, "above_ema200": True})
                        alerts, new_state = check_pullback(symbol, state)
                        price_state[symbol] = new_state

                        for alert in alerts:
                            # "_auto" suffix keeps cooldown independent from manual watchlist
                            key  = f"{symbol}|{alert['ema_label']}_auto"
                            prev = last_alert.get(key)
                            if prev is None or (datetime.utcnow() - prev) >= ALERT_COOLDOWN:
                                send_alert(alert)
                                last_alert[key] = datetime.utcnow()
                                save_alert_state(last_alert)
                            else:
                                remaining = ALERT_COOLDOWN - (datetime.utcnow() - prev)
                                print(f"  {symbol} {alert['ema_label']} auto — cooldown ({int(remaining.total_seconds()/60)}m left)")

                        if new_state.get("auto_remove"):
                            print(f"  {symbol} — queued for auto-removal (>3% below EMA200)")
                            to_remove.append(symbol)
                        elif not alerts:
                            print(f"  {symbol} — no pullback")
                    except Exception as e:
                        print(f"  {symbol} auto-check error: {e}")
                    time.sleep(1.0)

                if to_remove:
                    for s in to_remove:
                        auto_watchlist.remove(s)
                        price_state.pop(s, None)
                    save_auto_watchlist(auto_watchlist)
                    save_price_state(price_state)
                    send_telegram(
                        f"🗑 Auto-removed {len(to_remove)} coin(s) from screener (price >3% below EMA200):\n"
                        + "\n".join(f"• {s}" for s in to_remove)
                    )
                else:
                    save_price_state(price_state)

        time.sleep(10)


if __name__ == "__main__":
    run()
