import os
import requests

TOKEN   = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")


def send_telegram(msg: str):
    if not TOKEN or not CHAT_ID:
        print("⚠️ Telegram not configured")
        return
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")


def get_updates(offset: int) -> list:
    if not TOKEN:
        return []
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
        resp = requests.get(url, params={"offset": offset, "timeout": 0}, timeout=10)
        data = resp.json()
        return data.get("result", []) if data.get("ok") else []
    except Exception as e:
        print(f"getUpdates error: {e}")
        return []
