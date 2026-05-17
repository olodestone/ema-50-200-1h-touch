#!/usr/bin/env python3
"""Subprocess worker: scan_explosive_setups. Writes JSON to argv[1] then exits, freeing all memory."""
import sys
import os
import json
import numpy as np
import ccxt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from explosive_screener import scan_explosive_setups


class _Enc(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer):  return int(obj)
        if isinstance(obj, np.floating): return float(obj)
        if isinstance(obj, np.ndarray):  return obj.tolist()
        try:
            return float(obj)
        except Exception:
            return str(obj)


kucoin = ccxt.kucoin({
    "apiKey":   os.getenv("KUCOIN_API_KEY",  ""),
    "secret":   os.getenv("KUCOIN_SECRET",   ""),
    "password": os.getenv("KUCOIN_PASSWORD", ""),
    "enableRateLimit": True,
})

try:
    mexc = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "spot"}})
except Exception:
    mexc = None

try:
    mexc_swap = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "swap"}})
except Exception:
    mexc_swap = None

try:
    binance = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}})
except Exception:
    binance = None

try:
    gate = ccxt.gateio({"enableRateLimit": True})
except Exception:
    gate = None

result = scan_explosive_setups(kucoin, mexc, mexc_swap=mexc_swap, binance=binance, gate=gate)

with open(sys.argv[1], "w") as f:
    json.dump(result, f, cls=_Enc)
