"""
Daily pre-explosion setup scanner.
Uses 1h + 1d candles from KuCoin + MEXC (spot and swap) + Binance + Gate.io USDT pairs.
Run multiple times per day. Completely separate from the 1h EMA touch bot.

Signals:

  FRESH_CROSS  — EMA50 just crossed above EMA200 on the 1H chart (within 72h).
                 Uses 1h candles; fires earlier than waiting for daily confirmation.

  COIL         — Coin dormant (tight 14-day range + compressed EMAs ≤ 20% gap),
                 then first big volume surge breaks above EMA50.

  REVERSAL     — Downtrend (EMA50 < EMA200) broken by explosive surge above EMA50.
                 Path A: 4× vol + 15%+ day + 10%+ above EMA50  (ENJ/RAVE)
                 Path B: 1.2× vol + 20%+ day + 20%+ above EMA50 (staircase, BNRENSHENGUSDT)
                 Path C: 10× vol + broke above EMA50            (vol explosion before price)
                 Requires confirmation: next closed candle holds above mid-range of
                 explosive day — filters dead-cat bounces.

  PULLBACK     — Had golden cross + 15%+ run, now pulling back to EMA50.
                 Missed-entry retest.

Multi-signal: all matching signals are returned per coin.
Confluence flag is set when ≥2 signals agree on the same coin.
Parallel: each exchange scanned with ThreadPoolExecutor(max_workers=8).
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd

# Leveraged tokens (e.g. LINK3L, AAVE3S, DOGE2L) — always follow the underlying,
# produce constant false signals, and aren't tradeable setups.
_LEVERAGED_RE = re.compile(r'\d+[LlSs]$')

TOP_N                   = 300   # top pairs by 24h quote volume per exchange
VOL_SURGE_MULT          = 3.0   # COIL: vol > vol_ma × this on breakout day
COIL_RANGE_PCT          = 0.25  # COIL: 14-day (high-low)/avg must be < 25%
COIL_EMA_GAP_PCT        = 0.20  # COIL: EMA50/EMA200 gap must be < 20%
FRESH_CROSS_1H_LOOKBACK = 72    # FRESH_CROSS: look back this many 1h candles (= 3 days)
PULL_PEAK_MIN_PCT       = 0.15  # PULLBACK: prior run above EMA50 must be ≥ 15%
PULL_MAX_DIST_PCT       = 0.05  # PULLBACK: close must be within 5% above EMA50
PULL_LOOKBACK           = 10    # PULLBACK: days to look back for prior peak
PULL_CROSS_WINDOW       = 90    # PULLBACK: golden cross must be within 90 days (was 35 — too short, excluded valid setups after 5-week runs)

REV_VOL_MULT         = 4.0   # REVERSAL path A: explosive volume (RAVE/ENJ)
REV_MIN_DAILY_PCT    = 15.0  # REVERSAL path A: single-day move ≥ 15%
REV_EMA50_BREAK_PCT  = 0.10  # REVERSAL path A: close ≥ 10% above EMA50
REV_B_VOL_MULT       = 1.2   # REVERSAL path B: moderate volume (BNRENSHENGUSDT)
REV_B_MIN_DAILY_PCT  = 20.0  # REVERSAL path B: single-day move ≥ 20%
REV_B_EMA50_BREAK    = 0.20  # REVERSAL path B: close ≥ 20% above EMA50
REV_C_VOL_MULT       = 10.0  # REVERSAL path C: massive volume alone (vol explosion)
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
    """EMA50 crossed above EMA200 within last `lookback` closed candles."""
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


def check_fresh_cross(df_1h: pd.DataFrame, sym: str, exch: str) -> dict | None:
    """
    EMA50 just crossed above EMA200 on the 1H chart within the last 72 candles.
    Uses 1h OHLCV — fires well before the daily candle would confirm a cross.
    """
    if len(df_1h) < 25:
        return None
    last = df_1h.iloc[-2]
    if any(pd.isna(last[c]) for c in ["close", "ema50", "ema200"]):
        return None

    if last["ema50"] <= last["ema200"]:
        return None
    if last["close"] < last["ema50"] * 0.95:
        return None

    crossed, cross_ts = _find_cross(df_1h, FRESH_CROSS_1H_LOOKBACK)
    if not crossed:
        return None

    gap_pct = (last["ema50"] - last["ema200"]) / last["ema200"] * 100
    return {
        "signal":    "FRESH_CROSS",
        "symbol":    sym,
        "exchange":  exch,
        "close":     float(last["close"]),
        "ema50":     float(last["ema50"]),
        "ema200":    float(last["ema200"]),
        "cross_ts":  cross_ts,
        "gap_pct":   round(gap_pct, 2),
        "timeframe": "1h",
    }


def check_coil(df: pd.DataFrame, sym: str, exch: str) -> dict | None:
    """
    Coin was dormant (tight range, compressed EMAs ≤ 20% gap) then erupted with
    3× volume and broke above EMA50 — catches day 1 of the explosive move.
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
    if close < ema50:
        return None
    if vol < vol_ma * VOL_SURGE_MULT:
        return None

    w = df.iloc[-16:-2]
    if len(w) < 10:
        return None
    avg_price   = w["close"].mean()
    price_range = (w["high"].max() - w["low"].min()) / avg_price
    if price_range > COIL_RANGE_PCT:
        return None

    ema_gap = abs(ema50 - ema200) / ema200
    if ema_gap > COIL_EMA_GAP_PCT:
        return None

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
    Downtrend reversal — EMA50 < EMA200, coin erupts above EMA50.

    df.iloc[-3] = explosive day candidate
    df.iloc[-2] = confirmation candle (must close above explosive day's mid-range)

    Three paths:
      A — 4× vol + 15%+ day + 10%+ above EMA50            (ENJ, RAVE)
      B — 1.2× vol + 20%+ day + 20%+ above EMA50          (staircase: BNRENSHENGUSDT)
      C — 10× vol + close > EMA50                          (volume before price)

    Confirmation gate filters dead-cat bounces (D/USDT pattern).
    """
    if len(df) < 26:
        return None

    explosive = df.iloc[-3]
    confirm   = df.iloc[-2]
    prev      = df.iloc[-4]

    close  = explosive["close"]
    hi     = explosive["high"]
    lo     = explosive["low"]
    ema50  = explosive["ema50"]
    ema200 = explosive["ema200"]
    vol    = explosive["volume"]
    vol_ma = explosive["vol_ma"]

    if any(pd.isna(x) for x in [close, hi, lo, ema50, ema200, vol_ma, prev["close"],
                                  confirm["close"]]) or vol_ma == 0:
        return None
    if ema50 >= ema200:
        return None

    daily_pct = (close - prev["close"]) / prev["close"] * 100

    path_a = (
        close >= ema50 * (1 + REV_EMA50_BREAK_PCT) and
        vol >= vol_ma * REV_VOL_MULT and
        daily_pct >= REV_MIN_DAILY_PCT
    )
    path_b = (
        close >= ema50 * (1 + REV_B_EMA50_BREAK) and
        vol >= vol_ma * REV_B_VOL_MULT and
        daily_pct >= REV_B_MIN_DAILY_PCT
    )
    path_c = (
        close > ema50 and
        vol >= vol_ma * REV_C_VOL_MULT
    )
    if not (path_a or path_b or path_c):
        return None

    explosive_mid = (hi + lo) / 2
    if confirm["close"] < explosive_mid:
        return None

    base_window = df.iloc[-(REV_BASE_WINDOW + 3):-3]
    if len(base_window) < REV_BASE_WINDOW:
        return None
    if base_window["close"].mean() > ema50 * 1.05:
        return None

    ema_gap_pct = (ema200 - ema50) / ema200 * 100
    path_label  = "A" if path_a else ("B" if path_b else "C")
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
        "path":        path_label,
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
    if ema50 <= ema200:
        return None

    dist = (close - ema50) / ema50
    if dist > PULL_MAX_DIST_PCT or dist < -0.03:
        return None

    w = df.iloc[-(PULL_LOOKBACK + 2):-2]
    if len(w) < 5:
        return None
    max_above = ((w["close"] - w["ema50"]) / w["ema50"]).max()
    if max_above < PULL_PEAK_MIN_PCT:
        return None

    if len(df) >= PULL_CROSS_WINDOW + 3:
        w_cross = df.iloc[-(PULL_CROSS_WINDOW + 2):-2]
        if not (w_cross["ema50"] <= w_cross["ema200"]).any():
            return None

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
        base = sym.split("/")[0]
        if _LEVERAGED_RE.search(base):   # skip 3L, 3S, 2L, 2S etc. leveraged tokens
            continue
        m = exchange.markets.get(sym, {})
        if m.get("type") not in (None, "spot"):
            continue
        pairs.append((sym, t.get("quoteVolume") or 0))

    pairs.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in pairs[:top_n]]


def _top_swap_pairs(exchange, top_n: int = TOP_N) -> list[str]:
    """Return top MEXC perpetual swap pairs by volume (BASE/USDT:USDT format)."""
    try:
        exchange.load_markets()
        tickers = exchange.fetch_tickers()
    except Exception as e:
        print(f"  [explosive] swap tickers error: {e}")
        return []

    pairs = []
    for sym, t in tickers.items():
        if not sym.endswith("/USDT:USDT"):
            continue
        base = sym.split("/")[0]
        if f"{base}/USDT" in EXCLUDED:
            continue
        m = exchange.markets.get(sym, {})
        if m.get("type") != "swap":
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


def _fetch_1h(exchange, sym: str) -> pd.DataFrame | None:
    try:
        raw = exchange.fetch_ohlcv(sym, "1h", limit=250)
        if len(raw) < 60:
            return None
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return _compute(df)
    except Exception as e:
        print(f"  [explosive] 1h {sym}: {e}")
        return None


def _scan_one(sym: str, exch, exch_name: str, display_sym: str | None = None) -> list[dict]:
    """
    Fetch 1h + daily for one symbol, run ALL signal checks, return every matching signal.
    display_sym overrides sym in output (used for swap pairs: sym=BASE/USDT:USDT, display=BASE/USDT).
    Confluence flag is set on each signal when ≥2 fire for the same coin.
    """
    show_sym = display_sym if display_sym is not None else sym
    df_1h = _fetch_1h(exch, sym)
    time.sleep(0.1)
    df_d = _fetch_daily(exch, sym)
    time.sleep(0.1)

    signals = []
    if df_1h is not None:
        sig = check_fresh_cross(df_1h, show_sym, exch_name)
        if sig:
            signals.append(sig)
    if df_d is not None:
        for fn in (check_coil, check_reversal, check_pullback):
            sig = fn(df_d, show_sym, exch_name)
            if sig:
                signals.append(sig)

    if len(signals) >= 2:
        for s in signals:
            s["confluence"] = True

    return signals


def _run_parallel(tasks: list[tuple], max_workers: int = 8) -> list[dict]:
    """
    Execute _scan_one for each (sym, exch, exch_name, display_sym) task in parallel.
    Returns all signals found across all tasks, printing results as they arrive.
    """
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_scan_one, sym, exch, exch_name, display_sym):
            (sym, display_sym or sym, exch_name)
            for sym, exch, exch_name, display_sym in tasks
        }
        for future in as_completed(futures):
            _sym, show_sym, exch_name = futures[future]
            try:
                sigs = future.result()
                if sigs:
                    results.extend(sigs)
                    labels = "+".join(s["signal"] for s in sigs)
                    confl  = " ⚡CONFLUENCE" if len(sigs) >= 2 else ""
                    print(f"    ✓ {labels} — {show_sym} [{exch_name}]{confl}")
            except Exception as e:
                print(f"    [explosive] {show_sym}: {e}")
    return results


def scan_explosive_setups(
    kucoin, mexc=None, mexc_swap=None, binance=None, gate=None
) -> list[dict]:
    """
    Scan KuCoin + MEXC spot + Binance + Gate.io + MEXC swap for pre-explosion setups.

    FRESH_CROSS uses 1h candles; COIL/REVERSAL/PULLBACK use daily candles.
    All matching signals are returned per coin (multi-signal, no priority suppression).
    Confluence flag set when ≥2 signals agree on the same coin — highest conviction alert.
    MEXC swap pass catches futures-only listings not on any spot exchange.
    Uses ThreadPoolExecutor(max_workers=8) per exchange for parallel fetching.

    Returns list of signal dicts — one or more per qualifying coin.
    """
    results = []
    seen    = set()

    spot_exchanges = [
        ("KuCoin",  kucoin),
        ("MEXC",    mexc),
        ("Binance", binance),
        ("Gate.io", gate),
    ]

    for exch_name, exch in spot_exchanges:
        if exch is None:
            continue
        print(f"  [explosive] {exch_name}: loading pairs...")
        candidates = _top_pairs(exch)
        tasks = []
        for sym in candidates:
            base = sym.split("/")[0]
            if base not in seen:
                seen.add(base)
                tasks.append((sym, exch, exch_name, None))
        print(f"  [explosive] {exch_name}: scanning {len(tasks)} new pair(s)...")
        results.extend(_run_parallel(tasks))

    # MEXC swap pass — catches futures-only coins not listed on any spot exchange
    if mexc_swap is not None:
        print(f"  [explosive] MEXC-swap: loading pairs...")
        swap_candidates = _top_swap_pairs(mexc_swap)
        tasks = []
        for swap_sym in swap_candidates:
            base = swap_sym.split("/")[0]
            if base not in seen:
                seen.add(base)
                display_sym = swap_sym.split(":")[0]
                tasks.append((swap_sym, mexc_swap, "MEXC-swap", display_sym))
        print(f"  [explosive] MEXC-swap: scanning {len(tasks)} new pair(s)...")
        results.extend(_run_parallel(tasks))

    total_coins = len({r["symbol"] for r in results})
    print(f"  [explosive] Done: {len(results)} signal(s) across {total_coins} coin(s).")
    return results
