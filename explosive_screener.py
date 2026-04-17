"""
Daily pre-explosion setup scanner.
Uses 1d candles from KuCoin + MEXC USDT pairs.
Run once per day. Completely separate from the 1h EMA touch bot.

Three signals checked on df.iloc[-2] (last closed daily candle):

  FRESH_CROSS  — EMA50 just crossed above EMA200 in last 5 daily candles.
                 Fires before the explosive phase. Pattern: BNRENSHENGUSDT, DUSDT.

  COIL         — Coin dormant (tight 14-day range + compressed EMAs), then
                 first big volume surge breaks above EMA50.
                 Catches the explosion on day 1. Pattern: ENJUSDT, BIOUSDT.

  PULLBACK     — Had golden cross + 15%+ run, now pulling back to EMA50.
                 Missed-entry retest. Pattern: BNRENSHENGUSDT 1h.
"""

import time
import pandas as pd

TOP_N                = 150   # top pairs by 24h quote volume per exchange
VOL_SURGE_MULT       = 3.0   # COIL: vol > vol_ma × this on breakout day
COIL_RANGE_PCT       = 0.25  # COIL: 14-day (high-low)/avg must be < 25%
COIL_EMA_GAP_PCT     = 0.12  # COIL: EMA50/EMA200 gap must be < 12%
FRESH_CROSS_LOOKBACK = 5     # FRESH_CROSS: check back this many closed daily candles
PULL_PEAK_MIN_PCT    = 0.15  # PULLBACK: prior run above EMA50 must be ≥ 15%
PULL_MAX_DIST_PCT    = 0.05  # PULLBACK: close must be within 5% above EMA50
PULL_LOOKBACK        = 10    # PULLBACK: days to look back for prior peak
PULL_CROSS_WINDOW    = 35    # PULLBACK: golden cross must be within 35 days

REV_VOL_MULT         = 4.0   # REVERSAL path A: explosive volume (RAVE/ENJ)
REV_MIN_DAILY_PCT    = 15.0  # REVERSAL path A: single-day move ≥ 15%
REV_EMA50_BREAK_PCT  = 0.10  # REVERSAL path A: close ≥ 10% above EMA50
REV_B_VOL_MULT       = 1.2   # REVERSAL path B: moderate volume (BNRENSHENGUSDT)
REV_B_MIN_DAILY_PCT  = 20.0  # REVERSAL path B: single-day move ≥ 20%
REV_B_EMA50_BREAK    = 0.20  # REVERSAL path B: close ≥ 20% above EMA50 (bigger break needed)
REV_BASE_WINDOW      = 5     # REVERSAL: avg of last N closes must be near/below EMA50

EXCLUDED = {
    "USDC/USDT", "TUSD/USDT", "BUSD/USDT", "DAI/USDT",
    "FDUSD/USDT", "PYUSD/USDT", "USDP/USDT", "PAXG/USDT",
    "XAUT/USDT", "USDT/USDC", "USDD/USDT",
}


def _compute(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema50"]  = df["close"].ewm(span=50,  adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    df["vol_ma"] = df["volume"].rolling(20).median()
    return df


def _find_cross(df: pd.DataFrame, lookback: int) -> tuple[bool, str | None]:
    """EMA50 crossed above EMA200 within last `lookback` closed daily candles."""
    for offset in range(2, lookback + 3):
        if offset + 1 >= len(df):
            break
        curr = df.iloc[-offset]
        prev = df.iloc[-offset - 1]
        if pd.isna(prev["ema50"]) or pd.isna(prev["ema200"]):
            continue
        if prev["ema50"] <= prev["ema200"] and curr["ema50"] > curr["ema200"]:
            try:
                iso = curr["ts"].isoformat()
            except Exception:
                iso = str(curr["ts"])
            return True, iso
    return False, None


def check_fresh_cross(df: pd.DataFrame, sym: str, exch: str) -> dict | None:
    """
    EMA50 just crossed above EMA200 on the daily — fires BEFORE the explosive phase.
    """
    if len(df) < 25:
        return None
    last = df.iloc[-2]
    if any(pd.isna(last[c]) for c in ["close", "ema50", "ema200"]):
        return None

    crossed, cross_ts = _find_cross(df, FRESH_CROSS_LOOKBACK)
    if not crossed:
        return None
    if last["close"] < last["ema50"] * 0.95:  # price too far below EMA50 after cross
        return None

    gap_pct = (last["ema50"] - last["ema200"]) / last["ema200"] * 100
    return {
        "signal":   "FRESH_CROSS",
        "symbol":   sym,
        "exchange": exch,
        "close":    float(last["close"]),
        "ema50":    float(last["ema50"]),
        "ema200":   float(last["ema200"]),
        "cross_ts": cross_ts,
        "gap_pct":  round(gap_pct, 2),
    }


def check_coil(df: pd.DataFrame, sym: str, exch: str) -> dict | None:
    """
    Coin was dormant (tight range, compressed EMAs) then erupted with 3× volume
    and broke above EMA50 — catches day 1 of the explosive move.
    """
    if len(df) < 30:
        return None
    last = df.iloc[-2]
    close  = last["close"]
    ema50  = last["ema50"]
    ema200 = last["ema200"]
    vol    = last["volume"]
    vol_ma = last["vol_ma"]

    if any(pd.isna(x) for x in [close, ema50, ema200, vol_ma]) or vol_ma == 0:
        return None
    if close < ema50:  # must have broken above EMA50
        return None
    if vol < vol_ma * VOL_SURGE_MULT:  # must have explosive volume
        return None

    # The 14 days BEFORE the explosion candle must show dormancy
    w = df.iloc[-16:-2]
    if len(w) < 10:
        return None
    avg_price   = w["close"].mean()
    price_range = (w["high"].max() - w["low"].min()) / avg_price
    if price_range > COIL_RANGE_PCT:  # range too wide — not dormant
        return None

    # EMAs must be compressed (close together, not already diverging in a trend)
    ema_gap = abs(ema50 - ema200) / ema200
    if ema_gap > COIL_EMA_GAP_PCT:
        return None

    # Not already in an established uptrend (EMA50 > EMA200 the entire prior 20 days)
    if len(df) >= 25:
        if (df.iloc[-22:-2]["ema50"] > df.iloc[-22:-2]["ema200"]).all():
            return None

    return {
        "signal":    "COIL",
        "symbol":    sym,
        "exchange":  exch,
        "close":     float(close),
        "ema50":     float(ema50),
        "ema200":    float(ema200),
        "vol_ratio": round(vol / vol_ma, 2),
        "range_pct": round(price_range * 100, 1),
        "ema_gap":   round(ema_gap * 100, 1),
    }


def check_reversal(df: pd.DataFrame, sym: str, exch: str) -> dict | None:
    """
    Downtrend reversal — coin was below EMA50 (EMA50 < EMA200), then erupts
    above EMA50 with explosive volume and a large single-day move.
    Catches ENJ/RAVE-style setups that COIL misses because the EMA gap is
    too wide (coin was in a real downtrend, not just dormant).

    Conditions (on last closed daily candle):
      - EMA50 < EMA200  (still in macro downtrend by EMA definition)
      - close ≥ EMA50 × (1 + REV_EMA50_BREAK_PCT)  (broke 10%+ above EMA50)
      - volume > vol_ma × REV_VOL_MULT  (4× surge, high conviction)
      - single-day pct_change ≥ REV_MIN_DAILY_PCT  (≥ 15% daily move)
      - avg of last REV_BASE_WINDOW closes ≤ EMA50 × 1.05  (was near/below EMA50 recently)
    """
    if len(df) < 25:
        return None
    last = df.iloc[-2]
    prev = df.iloc[-3]
    close  = last["close"]
    ema50  = last["ema50"]
    ema200 = last["ema200"]
    vol    = last["volume"]
    vol_ma = last["vol_ma"]

    if any(pd.isna(x) for x in [close, ema50, ema200, vol_ma, prev["close"]]) or vol_ma == 0:
        return None
    if ema50 >= ema200:  # must still be in macro downtrend
        return None

    daily_pct = (close - prev["close"]) / prev["close"] * 100

    # Path A: explosive single-day surge with 4× volume (RAVE/ENJ)
    path_a = (
        close >= ema50 * (1 + REV_EMA50_BREAK_PCT) and
        vol >= vol_ma * REV_VOL_MULT and
        daily_pct >= REV_MIN_DAILY_PCT
    )
    # Path B: strong price breakout with moderate volume — staircase pattern (BNRENSHENGUSDT)
    path_b = (
        close >= ema50 * (1 + REV_B_EMA50_BREAK) and
        vol >= vol_ma * REV_B_VOL_MULT and
        daily_pct >= REV_B_MIN_DAILY_PCT
    )
    if not (path_a or path_b):
        return None

    # Confirm the coin was near or below EMA50 recently (not already running up)
    base_window = df.iloc[-(REV_BASE_WINDOW + 2):-2]
    if len(base_window) < REV_BASE_WINDOW:
        return None
    avg_recent_close = base_window["close"].mean()
    if avg_recent_close > ema50 * 1.05:  # was already well above EMA50 before today
        return None

    ema_gap_pct = (ema200 - ema50) / ema200 * 100  # how far EMA50 is below EMA200
    return {
        "signal":      "REVERSAL",
        "symbol":      sym,
        "exchange":    exch,
        "close":       float(close),
        "ema50":       float(ema50),
        "ema200":      float(ema200),
        "vol_ratio":   round(vol / vol_ma, 2),
        "daily_pct":   round(daily_pct, 1),
        "ema_gap_pct": round(ema_gap_pct, 1),
        "path":        "A" if path_a else "B",
    }


def check_pullback(df: pd.DataFrame, sym: str, exch: str) -> dict | None:
    """
    Coin had a golden cross + 15%+ run, now pulling back to EMA50.
    Missed-entry second chance — fires while EMA50 > EMA200 holds.
    """
    if len(df) < 15:
        return None
    last = df.iloc[-2]
    close  = last["close"]
    ema50  = last["ema50"]
    ema200 = last["ema200"]

    if any(pd.isna(x) for x in [close, ema50, ema200]):
        return None
    if ema50 <= ema200:  # bullish macro required
        return None

    # Price must be near EMA50 from above (pulling back into it)
    dist = (close - ema50) / ema50
    if dist > PULL_MAX_DIST_PCT or dist < -0.03:
        return None

    # Must have run significantly above EMA50 within the lookback window
    w = df.iloc[-(PULL_LOOKBACK + 2):-2]
    if len(w) < 5:
        return None
    max_above = ((w["close"] - w["ema50"]) / w["ema50"]).max()
    if max_above < PULL_PEAK_MIN_PCT:  # no prior meaningful run — not the setup
        return None

    # Confirm golden cross was recent (EMA50 was below EMA200 within ~35 days)
    if len(df) >= PULL_CROSS_WINDOW + 3:
        w_cross = df.iloc[-(PULL_CROSS_WINDOW + 2):-2]
        if not (w_cross["ema50"] <= w_cross["ema200"]).any():
            return None  # long-standing uptrend — different setup, not a pullback post-cross

    return {
        "signal":   "PULLBACK",
        "symbol":   sym,
        "exchange": exch,
        "close":    float(close),
        "ema50":    float(ema50),
        "ema200":   float(ema200),
        "dist_pct": round(dist * 100, 2),
        "peak_pct": round(max_above * 100, 1),
    }


def _top_pairs(exchange, top_n: int = TOP_N) -> list[str]:
    try:
        exchange.load_markets()
        tickers = exchange.fetch_tickers()
    except Exception as e:
        print(f"  [explosive] tickers error: {e}")
        return []

    pairs = []
    for sym, t in tickers.items():
        if not sym.endswith("/USDT") or sym in EXCLUDED:
            continue
        m = exchange.markets.get(sym, {})
        if m.get("type") not in (None, "spot"):
            continue
        pairs.append((sym, t.get("quoteVolume") or 0))

    pairs.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in pairs[:top_n]]


def _fetch_daily(exchange, sym: str) -> pd.DataFrame | None:
    try:
        raw = exchange.fetch_ohlcv(sym, "1d", limit=250)
        if len(raw) < 50:
            return None
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return _compute(df)
    except Exception as e:
        print(f"  [explosive] daily {sym}: {e}")
        return None


def scan_explosive_setups(kucoin, mexc=None) -> list[dict]:
    """
    Scan KuCoin + MEXC for daily pre-explosion setups.
    Returns one signal per coin max (priority: FRESH_CROSS > COIL > PULLBACK).
    Each dict contains: signal, symbol, exchange, close, ema50, ema200, + signal-specific fields.
    """
    results   = []
    seen      = set()   # base symbols already processed
    exchanges = [("KuCoin", kucoin)]
    if mexc is not None:
        exchanges.append(("MEXC", mexc))

    for exch_name, exch in exchanges:
        if exch is None:
            continue
        print(f"  [explosive] {exch_name}: loading pairs...")
        candidates = _top_pairs(exch)
        print(f"  [explosive] {exch_name}: scanning {len(candidates)} pairs...")

        for sym in candidates:
            base = sym.split("/")[0]
            if base in seen:
                continue
            seen.add(base)

            df = _fetch_daily(exch, sym)
            if df is None:
                time.sleep(0.3)
                continue

            for fn in (check_fresh_cross, check_coil, check_reversal, check_pullback):
                sig = fn(df, sym, exch_name)
                if sig:
                    results.append(sig)
                    print(f"    ✓ {sig['signal']} — {sym} [{exch_name}]")
                    break

            time.sleep(0.3)

    print(f"  [explosive] Done: {len(results)} setup(s) found.")
    return results
