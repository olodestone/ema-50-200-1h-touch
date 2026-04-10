"""
Auto-screener: scans top KuCoin USDT pairs and returns those where
1h close is above EMA50 with above-average volume.
"""

import time
import pandas as pd

AUTO_VOLUME_MULT = 1.5   # volume must be > vol_ma × this for screener entry
SCREENER_TOP_N   = 100   # only check top N pairs by 24h quote volume


def scan_trending_coins(exchange, top_n: int = SCREENER_TOP_N) -> list[str]:
    """
    Returns list of USDT symbols where:
      - 1h close > EMA50
      - 1h volume > vol_ma × AUTO_VOLUME_MULT

    Only scans top_n pairs by 24h quote volume to keep scan time reasonable.
    """
    print("  Screener: loading markets & tickers...")
    try:
        exchange.load_markets()
        tickers = exchange.fetch_tickers()
    except Exception as e:
        print(f"  Screener: fetch_tickers error: {e}")
        return []

    # Filter to USDT spot pairs, sort by 24h quote volume descending
    usdt_pairs = []
    for sym, ticker in tickers.items():
        if not sym.endswith("/USDT"):
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
            df["vol_ma"] = df["volume"].rolling(20).median()

            last   = df.iloc[-2]   # last closed candle — confirmed position
            close  = last["close"]
            ema50  = last["ema50"]
            vol    = last["volume"]
            vol_ma = last["vol_ma"]

            if pd.isna(ema50) or pd.isna(vol_ma) or vol_ma == 0:
                continue

            if close > ema50 and vol > vol_ma * AUTO_VOLUME_MULT:
                trending.append(sym)
                print(f"    ✓ {sym} — trending (close {close:.6g} > EMA50 {ema50:.6g})")

            time.sleep(0.3)  # gentle rate limiting between requests
        except Exception as e:
            print(f"  Screener {sym}: {e}")
            continue

    print(f"  Screener: {len(trending)} trending pair(s) found.")
    return trending
