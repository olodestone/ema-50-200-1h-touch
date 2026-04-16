"""
Auto-screener: scans top KuCoin USDT pairs and returns those where
1h close is above EMA50 with above-average volume, OR where EMA50 just
crossed above EMA200 (golden cross) in the last 3 closed candles.
"""

import time
import pandas as pd
from datetime import datetime, timezone

AUTO_VOLUME_MULT       = 1.5   # volume must be > vol_ma × this for screener entry
SCREENER_TOP_N         = 100   # only check top N pairs by 24h quote volume
MIN_EMA50_MARGIN       = 0.005 # close must be at least 0.5% above EMA50 to qualify
GOLDEN_CROSS_LOOKBACK  = 3     # candles back for Path B (immediate cross detection)
FRESH_CROSS_WINDOW_H   = 72    # hours — controls ⭐ tag + wider buffer in bot.py
CROSS_INFO_WINDOW_H    = 336   # hours (14 days) — Path A looks this far back for a cross
                                # to include as an informational note; no effect on alert behaviour

# Stablecoins and pegged assets — excluded from screener (price always hugs EMA)
EXCLUDED = {"USDC/USDT", "USDT/USDC", "TUSD/USDT", "BUSD/USDT", "DAI/USDT",
            "FDUSD/USDT", "PYUSD/USDT", "USDP/USDT", "PAXG/USDT", "XAUT/USDT"}


def detect_golden_cross(df: pd.DataFrame, lookback: int = GOLDEN_CROSS_LOOKBACK) -> tuple[bool, object]:
    """
    Check if EMA50 crossed above EMA200 within the last `lookback` closed candles.
    Scans most-recent-first so the freshest cross is returned.

    iloc[-2] is the most recent closed candle; iloc[-1] is the still-forming candle.

    Returns (True, cross_ts) for the most recent cross found,
    or (False, None) if no cross within the window.
    """
    for offset in range(2, lookback + 3):
        if offset + 1 >= len(df):
            break
        curr = df.iloc[-offset]
        prev = df.iloc[-offset - 1]
        if pd.isna(prev["ema50"]) or pd.isna(prev["ema200"]):
            continue
        if prev["ema50"] <= prev["ema200"] and curr["ema50"] > curr["ema200"]:
            # Return as UTC ISO string so callers can store/compare without type guessing
            ts = curr["ts"]
            try:
                iso = ts.isoformat()            # pd.Timestamp → "2026-04-08T14:00:00+00:00"
            except AttributeError:
                iso = datetime.utcfromtimestamp(int(ts) / 1000).isoformat()
            return True, iso
    return False, None


def scan_trending_coins(exchange, top_n: int = SCREENER_TOP_N) -> list[dict]:
    """
    Returns list of dicts for USDT symbols matching either:
      Path A — momentum entry:
        - 1h close > EMA50  (by at least MIN_EMA50_MARGIN)
        - 1h EMA50 > EMA200 (bullish macro alignment)
        - 1h volume > vol_ma × AUTO_VOLUME_MULT

      Path B — golden cross entry (no volume/margin gate):
        - EMA50 just crossed above EMA200 in last GOLDEN_CROSS_LOOKBACK candles

    Each dict: {"symbol": str, "close": float, "ema50": float, "ema200": float,
                "pct": float, "vol_ratio": float,
                "entry_reason": "momentum"|"golden_cross",
                "cross_ts": pd.Timestamp|None}

    Only scans top_n pairs by 24h quote volume to keep scan time reasonable.
    """
    print("  Screener: loading markets & tickers...")
    try:
        exchange.load_markets()
        tickers = exchange.fetch_tickers()
    except Exception as e:
        print(f"  Screener: fetch_tickers error: {e}")
        return []

    # Filter to USDT spot pairs, exclude stablecoins/pegged assets
    usdt_pairs = []
    for sym, ticker in tickers.items():
        if not sym.endswith("/USDT"):
            continue
        if sym in EXCLUDED:
            continue
        market = exchange.markets.get(sym, {})
        if market.get("type") not in (None, "spot"):
            continue
        quote_vol = ticker.get("quoteVolume") or 0
        usdt_pairs.append((sym, quote_vol))

    usdt_pairs.sort(key=lambda x: x[1], reverse=True)
    candidates = [sym for sym, _ in usdt_pairs[:top_n]]
    print(f"  Screener: checking top {len(candidates)} pairs by volume...")

    trending = []
    for sym in candidates:
        try:
            raw = exchange.fetch_ohlcv(sym, "1h", limit=336)  # 336 = 14 days; EMA200 needs 200
            if len(raw) < 60:
                continue

            df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
            df["ts"]     = pd.to_datetime(df["ts"], unit="ms", utc=True)
            df["ema50"]  = df["close"].ewm(span=50,  adjust=False).mean()
            df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
            df["vol_ma"] = df["volume"].rolling(20).median()

            last   = df.iloc[-2]   # last closed candle — confirmed position
            close  = last["close"]
            ema50  = last["ema50"]
            ema200 = last["ema200"]
            vol    = last["volume"]
            vol_ma = last["vol_ma"]

            if pd.isna(ema50) or pd.isna(ema200) or pd.isna(vol_ma) or vol_ma == 0:
                continue

            pct       = (close - ema50) / ema50 * 100
            vol_ratio = round(vol / vol_ma, 2)

            above_margin  = (close - ema50) / ema50 >= MIN_EMA50_MARGIN
            bullish_macro = ema50 > ema200

            # Path A — momentum: trending with volume confirmation
            # Scan back CROSS_INFO_WINDOW_H (14 days) for a recent golden cross.
            # cross_ts is informational only — shown in entry/pullback alerts but
            # does not change whether alerts fire or what thresholds apply.
            if above_margin and bullish_macro and vol > vol_ma * AUTO_VOLUME_MULT:
                _, cross_ts = detect_golden_cross(df, lookback=CROSS_INFO_WINDOW_H)
                trending.append({"symbol": sym, "close": close, "ema50": ema50,
                                 "ema200": ema200, "pct": pct, "vol_ratio": vol_ratio,
                                 "entry_reason": "momentum", "cross_ts": cross_ts})
                cross_note = f" | cross {cross_ts}" if cross_ts is not None else ""
                print(f"    ✓ {sym} — momentum (close {close:.6g} > EMA50 {ema50:.6g} > EMA200 {ema200:.6g}, +{pct:.2f}%{cross_note})")

            # Path B — golden cross: EMA50 just crossed above EMA200 (no vol/margin gate)
            elif bullish_macro:
                just_crossed, cross_ts = detect_golden_cross(df)
                if just_crossed:
                    trending.append({"symbol": sym, "close": close, "ema50": ema50,
                                     "ema200": ema200, "pct": pct, "vol_ratio": vol_ratio,
                                     "entry_reason": "golden_cross",
                                     "cross_ts": cross_ts})
                    print(f"    🌟 {sym} — golden cross (EMA50 {ema50:.6g} crossed above EMA200 {ema200:.6g} @ {cross_ts})")

            time.sleep(0.5)  # rate limiting — 0.3s was hitting KuCoin 429s
        except Exception as e:
            print(f"  Screener {sym}: {e}")
            continue

    print(f"  Screener: {len(trending)} trending pair(s) found.")
    return trending
