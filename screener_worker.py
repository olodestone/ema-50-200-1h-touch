#!/usr/bin/env python3
"""Subprocess worker: scan_trending_coins. Writes JSON to argv[1] then exits, freeing all memory."""
import sys
import os
import json
import numpy as np
import ccxt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from screener import scan_trending_coins


class _Enc(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer):  return int(obj)
        if isinstance(obj, np.floating): return float(obj)
        if isinstance(obj, np.ndarray):  return obj.tolist()
        try:
            return float(obj)
        except Exception:
            return str(obj)


exchange = ccxt.kucoin({
    "apiKey":   os.getenv("KUCOIN_API_KEY",  ""),
    "secret":   os.getenv("KUCOIN_SECRET",   ""),
    "password": os.getenv("KUCOIN_PASSWORD", ""),
    "enableRateLimit": True,
})

result = scan_trending_coins(exchange)

with open(sys.argv[1], "w") as f:
    json.dump(result, f, cls=_Enc)
