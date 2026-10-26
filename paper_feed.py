"""Binance Vision 5m closed-candle feed; PAPER ONLY, no order functionality.
Deploy with: uvicorn paper_feed:app --host 0.0.0.0 --port $PORT
"""
import asyncio
import json
import os
import urllib.request
import urllib.error
from collections import deque
from datetime import datetime, timezone

import websockets
from fastapi import FastAPI

app = FastAPI(title="Freqtrade Paper Feed", version="0.1")
SYMBOLS = ("btcusdt", "ethusdt", "solusdt")
STREAM_URL = "wss://data-stream.binance.vision/stream?streams=" + "/".join(f"{s}@kline_5m" for s in SYMBOLS)
CANDLES = {s.upper(): deque(maxlen=300) for s in SYMBOLS}
STATE = {"status": "STARTING", "connected": False, "last_event_utc": None, "last_error": None, "messages": 0, "closed_candles": 0, "reconnects": 0}
_task = None
TELEGRAM = {"status": "NOT_TESTED", "detail": None}

def telegram_send_sync():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return "MISSING_CONFIG", "Environment variables missing"
    payload = json.dumps({"chat_id": chat_id, "text": "✅ Freqtrade Paper Bot: Render Telegram bağlantısı başarılı. PAPER ONLY — gerçek emir yok."}).encode("utf-8")
    request = urllib.request.Request(
        "https://api.telegram.org/bot" + token + "/sendMessage",
        data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
            return ("SENT", "Telegram accepted message") if body.get("ok") else ("ERROR", "Telegram rejected message")
    except urllib.error.HTTPError as exc:
        return "ERROR", "Telegram HTTP " + str(exc.code)
    except Exception as exc:
        return "ERROR", type(exc).__name__

async def telegram_test():
    result, detail = await asyncio.to_thread(telegram_send_sync)
    TELEGRAM.update(status=result, detail=detail)


async def collector():
    delay = 2
    while True:
        try:
            STATE.update(status="CONNECTING", connected=False)
            async with websockets.connect(STREAM_URL, open_timeout=25, ping_interval=20, ping_timeout=25, max_size=2**20) as ws:
                STATE.update(status="RUNNING", connected=True, last_error=None)
                delay = 2
                async for raw in ws:
                    obj = json.loads(raw)
                    k = obj.get("data", {}).get("k", {})
                    if not k:
                        continue
                    STATE["messages"] += 1
                    STATE["last_event_utc"] = datetime.now(timezone.utc).isoformat()
                    if not k.get("x"):
                        continue  # Ignore incomplete candles; no lookahead.
                    symbol = k["s"]
                    candle = {"open_time_ms": int(k["t"]), "close_time_ms": int(k["T"]),
                              "open": float(k["o"]), "high": float(k["h"]),
                              "low": float(k["l"]), "close": float(k["c"]),
                              "volume": float(k["v"])}
                    series = CANDLES.get(symbol)
                    if series is None:
                        continue
                    if series and series[-1]["open_time_ms"] == candle["open_time_ms"]:
                        series[-1] = candle
                    elif not series or candle["open_time_ms"] > series[-1]["open_time_ms"]:
                        series.append(candle)
                        STATE["closed_candles"] += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            STATE["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
            STATE["status"] = "RECONNECTING"
        finally:
            STATE["connected"] = False
            STATE["reconnects"] += 1
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)

@app.on_event("startup")
async def startup():
    global _task
    _task = asyncio.create_task(collector())
    asyncio.create_task(telegram_test())

@app.on_event("shutdown")
async def shutdown():
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass

@app.get("/")
def home():
    return {"service": "FREQTRADE-LIVE-PAPER-V1", "phase": "DATA_FEED_ONLY", "paper_only": True,
            "orders": False, "trading": False, "status": STATE["status"]}

@app.get("/status")
def status():
    return {**STATE, "paper_only": True, "orders": False, "trading": False,
            "timeframe": "5m", "symbols": {s: {"closed_candles": len(v),
            "last_close": v[-1]["close"] if v else None,
            "last_candle_ms": v[-1]["close_time_ms"] if v else None} for s, v in CANDLES.items()},
            "telegram": TELEGRAM,
            "note": "This phase only collects closed candles; no strategies or virtual positions yet."
            }
