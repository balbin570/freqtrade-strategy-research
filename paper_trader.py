import asyncio
import json
import math
import psycopg2
import os
import time
import urllib.request
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
    "note": "PAPER ONLY. Public market data only. No exchange orders are sent.",
    "history": {"status": "NOT_STARTED", "symbols": {}, "errors": {}}
}
telegram_last_error = None


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def initial_portfolio():
    return {"cash": START_WALLET, "positions": [], "trades": [], "realized_pnl": 0.0,
            "signals": 0, "rejected_max_positions": 0, "rejected_insufficient_cash": 0}


def apply_saved_state(saved):
    if not isinstance(saved, dict) or not isinstance(saved.get("strategies"), dict):
        raise ValueError("Invalid saved portfolio state")
    for name in ("strategies", "symbols", "closed_candles"):
        if name in saved:
            state[name] = saved[name]
    for name in ("Strategy001", "Strategy002", "Strategy003"):
        state["strategies"].setdefault(name, initial_portfolio())


def db_load_sync():
    conn = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=8, sslmode="require")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT state_json FROM paper_trading.bot_state WHERE id = 1")
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def db_save_sync(payload):
    conn = psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=8, sslmode="require")
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO paper_trading.bot_state (id, state_json, updated_at)
                    VALUES (1, %s::jsonb, NOW())
                    ON CONFLICT (id) DO UPDATE
                    SET state_json = EXCLUDED.state_json, updated_at = NOW()
                """, (json.dumps(payload, separators=(",", ":")),))
    finally:
        conn.close()
