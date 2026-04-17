"""
Backtest: would the explosive screener have caught RAVE and ENJ before their moves?

Simulates the scanner running at 00:30 UTC each day (checking the prior closed daily candle).
Reports which signal fires, how many days before the explosion, and remaining upside.
"""

import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import ccxt
import pandas as pd
from explosive_screener import _compute, check_fresh_cross, check_coil, check_reversal, check_pullback

kucoin = ccxt.kucoin({
    "apiKey":   os.getenv("KUCOIN_API_KEY",   ""),
    "secret":   os.getenv("KUCOIN_SECRET",    ""),
    "password": os.getenv("KUCOIN_PASSWORD",  ""),
    "enableRateLimit": True,
})
mexc = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "spot"}})


def fetch_daily(exchange, sym, limit=300):
    # Resolve the actual market symbol (spot or swap)
    actual_sym = sym
    if sym not in exchange.markets:
        base = sym.split("/")[0]
        swap_sym = f"{base}/USDT:USDT"
        if swap_sym in exchange.markets:
            actual_sym = swap_sym
    raw = exchange.fetch_ohlcv(actual_sym, "1d", limit=limit)
    df  = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return _compute(df)


def find_explosion_start(df):
    """
    Find the first explosive candle in the run-up to the most recent peak.
    Strategy: find the highest close in last 30 days, look back 21 days from
    that peak and find the first candle with >10% single-day move AND >2× volume.
    That is the 'day 0' of the explosion.
    """
    recent = df.iloc[-32:-1]  # last 30 closed candles
    if len(recent) < 10:
        return None

    peak_iloc = recent["close"].idxmax()
    peak_pos  = df.index.get_loc(peak_iloc)

    lookback = min(21, peak_pos - 1)
    window   = df.iloc[peak_pos - lookback : peak_pos]

    for i in range(1, len(window)):
        row  = window.iloc[i]
        prev = window.iloc[i - 1]
        pct  = (row["close"] - prev["close"]) / prev["close"] * 100
        vm   = row["volume"] / row["vol_ma"] if not pd.isna(row["vol_ma"]) and row["vol_ma"] > 0 else 0
        if pct >= 10 and vm >= 2.0:
            return df.index.get_loc(window.index[i])

    return None


def run_backtest(sym, exchange, exch_name):
    print(f"\n{'═'*52}")
    print(f"  {sym}  [{exch_name}]")
    print(f"{'═'*52}")

    df = fetch_daily(exchange, sym)

    exp_iloc = find_explosion_start(df)
    if exp_iloc is None:
        print("  Could not identify explosion day in recent data.")
        return

    exp_row   = df.iloc[exp_iloc]
    prev_row  = df.iloc[exp_iloc - 1]
    exp_date  = exp_row["ts"].strftime("%Y-%m-%d")
    exp_pct   = (exp_row["close"] - prev_row["close"]) / prev_row["close"] * 100
    exp_close = exp_row["close"]
    peak_close = df.iloc[exp_iloc: exp_iloc + 14]["close"].max()  # 14-day peak after explosion

    print(f"  Explosion day : {exp_date}  +{exp_pct:.1f}% on the day")
    print(f"  Price before  : {prev_row['close']:.6g}")
    print(f"  14-day peak   : {peak_close:.6g}  (+{(peak_close/prev_row['close']-1)*100:.0f}% total move)")
    print()
    print(f"  Scanning day by day up to 14 days before explosion...")
    print()

    first_signal = None

    # Day-by-day simulation: scanner runs on morning of day T, sees closed candle T-1
    # exp_iloc is the explosion candle. We scan T = exp_iloc-13 .. exp_iloc+7
    # (+7 handles staircase patterns where the main surge comes days after the initial move)
    for days_before in range(13, -8, -1):
        # Scanner runs "the morning after" candle at (exp_iloc - days_before)
        # So the window includes one extra row so iloc[-2] == the candle we want to check
        end = exp_iloc - days_before + 2
        if end < 25 or end > len(df):
            continue

        window     = df.iloc[:end].copy()
        check_date = df.iloc[exp_iloc - days_before]["ts"].strftime("%Y-%m-%d")
        check_close = df.iloc[exp_iloc - days_before]["close"]

        for fn in (check_fresh_cross, check_coil, check_reversal, check_pullback):
            sig = fn(window, sym, exch_name)
            if sig:
                remaining = (peak_close - check_close) / check_close * 100
                timing = (f"{days_before}d BEFORE explosion"
                          if days_before > 0 else
                          "ON explosion day" if days_before == 0 else
                          f"{abs(days_before)}d AFTER explosion")
                print(f"  ✓  {sig['signal']:12s}  fired {check_date}  ({timing})")
                print(f"     Price at signal : {check_close:.6g}")
                print(f"     EMA50           : {sig['ema50']:.6g}   EMA200: {sig['ema200']:.6g}")
                print(f"     Remaining upside: +{remaining:.0f}% to 14d peak")
                print()
                if first_signal is None:
                    first_signal = (days_before, sig["signal"])
                break   # one signal per day

    if first_signal is None:
        print("  ✗  No signal fired in the window. Screener would have MISSED this move.")
    else:
        days, stype = first_signal
        verdict = "BEFORE explosion" if days > 0 else ("same day" if days == 0 else "after")
        print(f"  Verdict: first alert was {stype} — {days}d {verdict}.")


def resolve(sym):
    """Try KuCoin spot first, then MEXC spot, then MEXC swap (futures track spot price)."""
    kucoin.load_markets()
    if sym in kucoin.markets:
        try:
            raw = kucoin.fetch_ohlcv(sym, "1d", limit=60)
            if len(raw) >= 50:
                return kucoin, "KuCoin"
        except Exception:
            pass
    mexc.load_markets()
    if sym in mexc.markets:
        return mexc, "MEXC"
    # Fallback: try MEXC swap symbol (e.g. BASE/USDT:USDT) — swap price ≈ spot
    base = sym.split("/")[0]
    swap_sym = f"{base}/USDT:USDT"
    mexc_swap = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    mexc_swap.load_markets()
    if swap_sym in mexc_swap.markets:
        return mexc_swap, "MEXC-swap"
    return None, None


if __name__ == "__main__":
    for sym in ["ENJ/USDT", "RAVE/USDT", "BIANRENSHENG/USDT"]:
        exch, name = resolve(sym)
        if exch is None:
            print(f"\n{sym}: not found on KuCoin or MEXC — skipping.")
            continue
        try:
            run_backtest(sym, exch, name)
        except Exception as e:
            print(f"\n{sym} backtest error: {e}")
