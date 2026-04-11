"""
Auto-screener: scans top KuCoin USDT pairs and returns those where
1h close is above EMA50 with above-average volume.
"""

import time
import pandas as pd

AUTO_VOLUME_MULT  = 1.5    # volume must be > vol_ma × this for screener entry
SCREENER_TOP_N    = 100    # only check top N pairs by 24h quote volume
MIN_EMA50_MARGIN  = 0.005  # close must be at least 0.5% above EMA50 to qualify

# Stablecoins and pegged assets — excluded from screener (price always hugs EMA)
EXCLUDED = {"USDC/USDT", "USDT/USDC", "TUSD/USDT", "BUSD/USDT", "DAI/USDT",
            "FDUSD/USDT", "PYUSD/USDT", "USDP/USDT", "PAXG/USDT", "XAUT/USDT"}


def scan_trending_coins(exchange, top_n: int = SCREENER_TOP_N) -> list[dict]:
    """
    Returns list of dicts for USDT symbols where:
      - 1h close > EMA50  (by at least MIN_EMA50_MARGIN)
      - 1h EMA50 > EMA200 (bullish macro alignment)
      - 1h volume > vol_ma × AUTO_VOLUME_MULT

    Each dict: {"symbol": str, "close": float, "ema50": float, "ema200": float,
                "pct": float, "vol_ratio": float}

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
            raw = exchange.fetch_ohlcv(sym, "1h", limit=220)
            if len(raw) < 60:
                continue

            df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
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

            above_margin  = (close - ema50) / ema50 >= MIN_EMA50_MARGIN
            bullish_macro = ema50 > ema200
            if above_margin and bullish_macro and vol > vol_ma * AUTO_VOLUME_MULT:
                pct       = (close - ema50) / ema50 * 100
                vol_ratio = round(vol / vol_ma, 2)
                trending.append({"symbol": sym, "close": close, "ema50": ema50,
                                 "ema200": ema200, "pct": pct, "vol_ratio": vol_ratio})
                print(f"    ✓ {sym} — trending (close {close:.6g} > EMA50 {ema50:.6g} > EMA200 {ema200:.6g}, +{pct:.2f}%)")

            time.sleep(0.5)  # rate limiting — 0.3s was hitting KuCoin 429s
        except Exception as e:
            print(f"  Screener {sym}: {e}")
            continue

    print(f"  Screener: {len(trending)} trending pair(s) found.")
    return trending
