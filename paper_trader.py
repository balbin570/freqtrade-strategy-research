import asyncio
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import websockets
from fastapi import FastAPI

# PAPER ONLY: public Binance market data; no exchange order endpoints or API keys.
SYMBOLS = {"BTCUSDT": "BTC/USDT", "ETHUSDT": "ETH/USDT", "SOLUSDT": "SOL/USDT"}
START_WALLET = 1000.0
STAKE = 100.0
MAX_OPEN = 5
FEE = 0.001
TIMEFRAME = "5m"
STATE_PATH = os.getenv("STATE_PATH", "/var/data/paper_state.json")
STREAM_URL = (
    "wss://data-stream.binance.vision/stream?streams="
    "btcusdt@kline_5m/ethusdt@kline_5m/solusdt@kline_5m"
)

app = FastAPI(title="Freqtrade Strategy Paper Research", version="1.0.0")
lock = asyncio.Lock()
candles = {s: [] for s in SYMBOLS}
state = {
    "status": "STARTING",
    "connected": False,
    "last_event_utc": None,
    "last_error": None,
    "messages": 0,
    "closed_candles": 0,
    "reconnects": 0,
    "paper_only": True,
    "orders": False,
    "trading": True,
    "timeframe": TIMEFRAME,
    "symbols": {},
    "strategies": {},
    "telegram": {"status": "NOT_TESTED", "detail": ""},
    "note": "PAPER ONLY. Public market data only. No exchange orders are sent."
}
telegram_last_error = None


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def initial_portfolio():
    return {"cash": START_WALLET, "positions": [], "trades": [], "realized_pnl": 0.0,
            "signals": 0, "rejected_max_positions": 0, "rejected_insufficient_cash": 0}


def load_state():
    global state
    try:
        path = Path(STATE_PATH)
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            for name in ("strategies", "symbols", "closed_candles"):
                if name in saved:
                    state[name] = saved[name]
            for strategy in ("Strategy001", "Strategy002", "Strategy003"):
                if strategy not in state["strategies"]:
                    state["strategies"][strategy] = initial_portfolio()
    except Exception as exc:
        state["last_error"] = f"state load: {exc}"
    for strategy in ("Strategy001", "Strategy002", "Strategy003"):
        state["strategies"].setdefault(strategy, initial_portfolio())


def save_state():
    try:
        path = Path(STATE_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "strategies": state["strategies"],
            "symbols": state["symbols"],
            "closed_candles": state["closed_candles"],
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        state["last_error"] = f"state save: {exc}"


def send_telegram_sync(text):
    import urllib.parse
    import urllib.request
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
