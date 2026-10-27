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
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        state["telegram"] = {"status": "NOT_CONFIGURED", "detail": "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID"}
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}, encoding="utf-8").encode("ascii")
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            result = json.loads(response.read().decode())
        if result.get("ok"):
            state["telegram"] = {"status": "SENT", "detail": "Telegram accepted message"}
            return True
        state["telegram"] = {"status": "ERROR", "detail": str(result.get("description", "Telegram rejected message"))}
    except Exception as exc:
        state["telegram"] = {"status": "ERROR", "detail": str(exc)[:300]}
    return False


def ema(series, period):
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.where(loss != 0, 100).fillna(50)


def stoch(df, fast=False):
    low_n = df["low"].rolling(5 if fast else 14).min()
    high_n = df["high"].rolling(5 if fast else 14).max()
    k = 100 * (df["close"] - low_n) / (high_n - low_n).replace(0, np.nan)
    if fast:
        d = k.rolling(3).mean()
        return k, d
    slowk = k.rolling(3).mean()
    slowd = slowk.rolling(3).mean()
    return slowk, slowd


def bollinger_lower(df):
    typical = (df["high"] + df["low"] + df["close"]) / 3
    return typical.rolling(20).mean() - 2 * typical.rolling(20).std(ddof=0)


def mfi(df, period=14):
    typical = (df["high"] + df["low"] + df["close"]) / 3
    raw = typical * df["volume"]
    direction = typical.diff()
    positive = raw.where(direction > 0, 0.0).rolling(period).sum()
    negative = raw.where(direction < 0, 0.0).rolling(period).sum()
    ratio = positive / negative.replace(0, np.nan)
    return (100 - 100 / (1 + ratio)).fillna(50)


def parabolic_sar(df, step=0.02, maximum=0.2):
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    n = len(df)
    out = np.full(n, np.nan)
    if n < 2:
        return pd.Series(out, index=df.index)
    bull = True
    af = step
    ep = high[0]
    out[0] = low[0]
    sar = low[0]
    for i in range(1, n):
        prev_sar = sar
        sar = prev_sar + af * (ep - prev_sar)
        if bull:
            sar = min(sar, low[i - 1], low[i - 2] if i > 1 else low[i - 1])
            if low[i] < sar:
                bull = False
                sar = ep
                ep = low[i]
                af = step
            elif high[i] > ep:
                ep = high[i]
                af = min(maximum, af + step)
        else:
            sar = max(sar, high[i - 1], high[i - 2] if i > 1 else high[i - 1])
            if high[i] > sar:
                bull = True
                sar = ep
                ep = high[i]
                af = step
            elif low[i] < ep:
                ep = low[i]
                af = min(maximum, af + step)
        out[i] = sar
    return pd.Series(out, index=df.index)


def hammer_pattern(df):
    body = (df["close"] - df["open"]).abs()
    lower_shadow = np.minimum(df["open"], df["close"]) - df["low"]
    upper_shadow = df["high"] - np.maximum(df["open"], df["close"])
    prior_body = body.shift(1).rolling(10).mean()
    # Transparent approximation of TA-Lib CDLHAMMER; not byte-for-byte TA-Lib parity.
    return ((lower_shadow >= 2 * body) & (upper_shadow <= body * 0.3) &
            (body <= prior_body) & (df["close"] > df["low"])).astype(int)


def fisher_rsi(rsi_series):
    x = 0.1 * (rsi_series - 50)
    # Numerically stable equivalent of tanh(x).
    return np.tanh(x)


def build_indicators(rows):
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["ema5"] = ema(df["close"], 5)
    df["ema10"] = ema(df["close"], 10)
    df["ema20"] = ema(df["close"], 20)
    df["ema50"] = ema(df["close"], 50)
    df["ema100"] = ema(df["close"], 100)
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    ha_open = pd.Series(index=df.index, dtype=float)
    if len(df):
        ha_open.iloc[0] = (df["open"].iloc[0] + df["close"].iloc[0]) / 2
        for i in range(1, len(df)):
            ha_open.iloc[i] = (ha_open.iloc[i-1] + ha_close.iloc[i-1]) / 2
    df["ha_open"] = ha_open
    df["ha_close"] = ha_close
    df["rsi"] = rsi(df["close"])
    df["fisher_rsi"] = fisher_rsi(df["rsi"])
    df["slowk"], df["slowd"] = stoch(df, fast=False)
    df["fastk"], df["fastd"] = stoch(df, fast=True)
    df["bb_lowerband"] = bollinger_lower(df)
    df["sar"] = parabolic_sar(df)
    df["mfi"] = mfi(df)
    df["sma40"] = df["close"].rolling(40).mean()
    # Hammer is a best-effort approximation, clearly exposed in /status.
    df["hammer"] = hammer_pattern(df)
    return df


def crossed_above(a, b, i):
    return i > 0 and pd.notna(a.iloc[i-1]) and pd.notna(b.iloc[i-1]) and pd.notna(a.iloc[i]) and pd.notna(b.iloc[i]) and a.iloc[i-1] <= b.iloc[i-1] and a.iloc[i] > b.iloc[i]


def entry_signal(strategy, df, i):
    if i < 100:
        return False
    r = df.iloc[i]
    if strategy == "Strategy001":
        return (crossed_above(df["ema20"], df["ema50"], i) and
                r["ha_close"] > r["ema20"] and r["ha_open"] < r["ha_close"])
    if strategy == "Strategy002":
        return (r["rsi"] < 30 and r["slowk"] < 20 and
                r["bb_lowerband"] > r["close"] and r["hammer"] == 1)
    if strategy == "Strategy003":
        return (r["rsi"] < 28 and r["rsi"] > 0 and r["close"] < r["sma40"] and
                r["fisher_rsi"] < -0.94 and r["mfi"] < 16.0 and
                ((r["ema50"] > r["ema100"]) or crossed_above(df["ema5"], df["ema10"], i)) and
                r["fastd"] > r["fastk"] and r["fastd"] > 0)
    return False


def exit_signal(strategy, df, i):
    r = df.iloc[i]
    if strategy == "Strategy001":
        return (crossed_above(df["ema50"], df["ema100"], i) and
                r["ha_close"] < r["ema20"] and r["ha_open"] > r["ha_close"])
    if strategy in ("Strategy002", "Strategy003"):
        return r["sar"] > r["close"] and r["fisher_rsi"] > 0.3
    return False


def roi_target(age_minutes):
    if age_minutes >= 60:
        return 0.01
    if age_minutes >= 30:
        return 0.03
    if age_minutes >= 20:
        return 0.04
    return 0.05


def close_position(strategy, pos, price, reason, candle_time):
    portfolio = state["strategies"][strategy]
    proceeds = pos["qty"] * price * (1 - FEE)
    pnl = proceeds - pos["entry_cost"]
    portfolio["cash"] += proceeds
    portfolio["realized_pnl"] += pnl
    trade = {
        "strategy": strategy, "symbol": pos["symbol"], "entry_time": pos["entry_time"],
        "exit_time": candle_time, "entry_price": pos["entry_price"], "exit_price": price,
        "stake_usdt": STAKE, "reason": reason, "net_pnl_usdt": round(pnl, 6),
        "net_return_pct": round(100 * pnl / STAKE, 4)
    }
    portfolio["trades"].append(trade)
    portfolio["positions"].remove(pos)
    msg = (
        f"\U0001F534 {strategy} SANAL SATI\u015e\n"
        f"{pos['symbol']} | Neden: {reason}\n"
        f"Al\u0131\u015f: {pos['entry_price']:.8g} "
        f"\u2192 Sat\u0131\u015f: {price:.8g}\n"
        f"Net k\u00e2r/zarar: {pnl:+.4f} USDT\n"
        f"Toplam ger\u00e7ekle\u015fen: "
        f"{portfolio['realized_pnl']:+.4f} USDT\n"
        "SADECE SANAL \u0130\u015eLEM \u2014 "
        "ger\u00e7ek emir g\u00f6nderilmez"
    )
    # Non-blocking-ish: run HTTP send in background thread.
    asyncio.create_task(asyncio.to_thread(send_telegram_sync, msg))


async def process_closed_candle(symbol, candle):
    async with lock:
        rows = candles[symbol]
        if rows and candle["time"] <= rows[-1]["time"]:
            return
        rows.append(candle)
        if len(rows) > 1000:
            del rows[:-1000]
        state["closed_candles"] += 1
        state["last_event_utc"] = utc_now()
        state["symbols"].setdefault(symbol, {})
        state["symbols"][symbol] = {
            "closed_candles": len(rows), "last_close": candle["close"],
            "last_candle_ms": candle["time"], "candles_in_memory": len(rows)
        }

        df = build_indicators(rows)
        i = len(df) - 1
        if i < 100:
            save_state()
            return

        for strategy in ("Strategy001", "Strategy002", "Strategy003"):
            portfolio = state["strategies"][strategy]
            # Exit handling first; use candle OHLC. For conflicting intrabar stop/ROI, stop wins.
            for pos in list(portfolio["positions"]):
                if pos["symbol"] != symbol:
                    continue
                age = max(0.0, (candle["time"] - pos["entry_time_ms"]) / 60000.0)
                stop = pos["entry_price"] * (1 - 0.10)
                target = pos["entry_price"] * (1 + roi_target(age))
                if candle["open"] <= stop:
                    close_position(strategy, pos, candle["open"], "stop_gap", candle["time_iso"])
                elif candle["low"] <= stop:
                    close_position(strategy, pos, stop, "stoploss", candle["time_iso"])
                elif candle["open"] >= target:
                    close_position(strategy, pos, candle["open"], "roi_gap", candle["time_iso"])
                elif candle["high"] >= target:
                    close_position(strategy, pos, target, "roi", candle["time_iso"])
                elif exit_signal(strategy, df, i) and (not pos or candle["close"] > pos["entry_price"]):
                    close_position(strategy, pos, candle["close"], "exit_signal", candle["time_iso"])

            # One entry per signal candle per strategy/symbol. Same symbol may have multiple positions.
            if entry_signal(strategy, df, i):
                portfolio["signals"] += 1
                if len(portfolio["positions"]) >= MAX_OPEN:
                    portfolio["rejected_max_positions"] += 1
                elif portfolio["cash"] < STAKE * (1 + FEE):
                    portfolio["rejected_insufficient_cash"] += 1
                else:
                    price = candle["close"]
                    qty = STAKE / price
                    entry_cost = STAKE * (1 + FEE)
                    portfolio["cash"] -= entry_cost
                    pos = {
                        "symbol": symbol, "entry_time": candle["time_iso"],
                        "entry_time_ms": candle["time"], "entry_price": price,
                        "qty": qty, "entry_cost": entry_cost
                    }
                    portfolio["positions"].append(pos)
                    msg = (
                        f"\U0001F7E2 {strategy} SANAL ALI\u015e\n"
                        f"{symbol}\n"
                        f"Fiyat: {price:.8g}\n"
                        f"Tutar: {STAKE:.2f} USDT | "
                        f"A\u00e7\u0131k pozisyon: "
                        f"{len(portfolio['positions'])}/{MAX_OPEN}\n"
                        "SADECE SANAL \u0130\u015eLEM \u2014 "
                        "ger\u00e7ek emir g\u00f6nderilmez"
                    )
                    asyncio.create_task(asyncio.to_thread(send_telegram_sync, msg))
        save_state()




def fetch_historical_candles(symbol, limit=160):
    import urllib.parse
    params = urllib.parse.urlencode({"symbol": symbol, "interval": TIMEFRAME, "limit": limit})
    url = "https://data-api.binance.vision/api/v3/klines?" + params
    request = urllib.request.Request(url, headers={"User-Agent": "paper-research/1.1"})
    with urllib.request.urlopen(request, timeout=18) as response:
        data = json.loads(response.read().decode("utf-8"))
    now_ms = int(time.time() * 1000)
    rows = []
    for k in data:
        close_ms = int(k[6])
        if close_ms >= now_ms:
            continue
        rows.append({
            "time": close_ms,
            "time_iso": datetime.fromtimestamp(close_ms / 1000, tz=timezone.utc).isoformat(),
            "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
            "close": float(k[4]), "volume": float(k[5]),
        })
    return rows


async def initialize_history():
    state["history"]["status"] = "LOADING"
    for symbol in SYMBOLS:
        try:
            rows = await asyncio.to_thread(fetch_historical_candles, symbol)
            if len(rows) < 101:
                raise ValueError(f"Only {len(rows)} completed historical candles returned")
            async with lock:
                candles[symbol] = rows[-1000:]
                state["symbols"][symbol] = {
                    "closed_candles": len(rows), "last_close": rows[-1]["close"],
                    "last_candle_ms": rows[-1]["time"], "candles_in_memory": len(rows),
                }
                state["history"]["symbols"][symbol] = len(rows)
        except Exception as exc:
            state["history"]["errors"][symbol] = str(exc)[:200]
    state["history"]["status"] = ("READY" if len(state["history"]["symbols"]) == len(SYMBOLS)
                                  else "PARTIAL_OR_FAILED")


async def bootstrap_market():
    await initialize_history()
    await market_loop()

async def market_loop():
    while True:
        try:
            state["status"] = "CONNECTING"
            async with websockets.connect(STREAM_URL, open_timeout=20, ping_interval=20, ping_timeout=20) as ws:
                state["connected"] = True
                state["status"] = "RUNNING"
                state["last_error"] = None
                async for raw in ws:
                    state["messages"] += 1
                    packet = json.loads(raw)
                    data = packet.get("data", {})
                    if data.get("e") != "kline":
                        continue
                    k = data.get("k", {})
                    if not k.get("x"):
                        continue
                    symbol = k.get("s")
                    if symbol not in SYMBOLS:
                        continue
                    candle = {
                        "time": int(k["T"]),
                        "time_iso": datetime.fromtimestamp(int(k["T"]) / 1000, tz=timezone.utc).isoformat(),
                        "open": float(k["o"]), "high": float(k["h"]), "low": float(k["l"]),
                        "close": float(k["c"]), "volume": float(k["v"])
                    }
                    await process_closed_candle(symbol, candle)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state["connected"] = False
            state["status"] = "RECONNECTING"
            state["last_error"] = str(exc)[:300]
            state["reconnects"] += 1
            await asyncio.sleep(min(30, 2 + state["reconnects"]))


@app.on_event("startup")
async def startup():
    load_state()

    asyncio.create_task(asyncio.to_thread(
        send_telegram_sync,
        "\u2705 Freqtrade sanal i\u015flem motoru ba\u015flat\u0131ld\u0131.\n"
        "Strategy001/002/003 | BTC/ETH/SOL | 5m\n"
        "Her strateji: 1.000 USDT; i\u015flem tutar\u0131: 100 USDT; en fazla 5 a\u00e7\u0131k pozisyon.\n"
        "Yaln\u0131zca sanal i\u015flem \u2014 ger\u00e7ek emir g\u00f6nderilmez."
    ))

    asyncio.create_task(bootstrap_market())


@app.get("/")
async def root():
    return {"service": "freqtrade-live-paper-v1", "status": state["status"],
            "paper_only": True, "orders": False, "trading": True,
            "status_url": "/status", "note": "Independent paper engine; not official Freqtrade backtesting."}


@app.get("/check")
async def check():
    return {"status": "RUNNING" if state["connected"] else state["status"],
            "source": "Binance Vision WebSocket", "symbols": list(SYMBOLS),
            "paper_only": True, "orders": False}


@app.get("/status")
async def status():
    result = {
        "status": state["status"], "connected": state["connected"],
        "last_event_utc": state["last_event_utc"], "last_error": state["last_error"],
        "messages": state["messages"], "closed_candles": state["closed_candles"],
        "reconnects": state["reconnects"], "paper_only": True, "orders": False,
        "trading": True, "timeframe": TIMEFRAME, "symbols": state["symbols"],
        "history": state["history"],
        "telegram": state["telegram"], "strategies": {}
    }
    for name, p in state["strategies"].items():
        result["strategies"][name] = {
            "starting_wallet_usdt": START_WALLET,
            "cash_usdt": round(p["cash"], 4),
            "realized_pnl_usdt": round(p["realized_pnl"], 4),
            "equity_estimate_usdt": round(p["cash"] + sum(x["qty"] * state["symbols"].get(x["symbol"], {}).get("last_close", x["entry_price"]) for x in p["positions"]), 4),
            "open_positions": len(p["positions"]), "max_open_positions": MAX_OPEN,
            "signals": p["signals"], "closed_trades": len(p["trades"]),
            "wins": sum(1 for t in p["trades"] if t["net_pnl_usdt"] > 0),
            "net_closed_pnl_usdt": round(sum(t["net_pnl_usdt"] for t in p["trades"]), 4),
            "rejected_max_positions": p["rejected_max_positions"],
            "rejected_insufficient_cash": p["rejected_insufficient_cash"],
            "recent_trades": p["trades"][-5:]
        }
    result["note"] = ("Paper simulation only; no Binance orders. Indicators are locally implemented. "
                      "Hammer is an approximation, not exact TA-Lib parity. "
                      "On Render Free, /tmp state is temporary and may be lost on restart.")
    return result


@app.get("/telegram-test")
async def telegram_test():
    ok = await asyncio.to_thread(
        send_telegram_sync,
        "\u2705 Telegram T\u00fcrk\u00e7e karakter testi: \u00e7 \u011f \u0131 \u0130 \u00f6 \u015f \u00fc"
    )
    return {"ok": ok, "telegram": state["telegram"]}


@app.get("/daily")
async def daily():
    lines = [
        "\U0001F4CA G\u00dcNL\u00dcK SANAL \u0130\u015eLEM \u00d6ZET\u0130"
    ]

    for name, p in state["strategies"].items():
        lines.append(
            f"{name}: ger\u00e7ekle\u015fen k\u00e2r/zarar "
            f"{p['realized_pnl']:+.2f} USDT, "
            f"a\u00e7\u0131k {len(p['positions'])}/{MAX_OPEN}, "
            f"kapanan {len(p['trades'])}"
        )

    lines.append(
        "SADECE SANAL \u0130\u015eLEM \u2014 "
        "ger\u00e7ek emir g\u00f6nderilmez"
    )

    ok = await asyncio.to_thread(
        send_telegram_sync,
        "\n".join(lines)
    )

    return {
        "ok": ok,
        "telegram": state["telegram"],
        "summary": lines
    }


@app.get("/trades/{strategy}")
async def trades(strategy: str):
    if strategy not in state["strategies"]:
        return {"error": "Unknown strategy", "allowed": list(state["strategies"])}
    return {"strategy": strategy, "trades": state["strategies"][strategy]["trades"][-100:]}


@app.get("/health")
async def health():
    return {"ok": True, "paper_only": True, "orders": False, "connected": state["connected"]}

@app.get("/db-test")
async def db_test():
    def check_connection():
        conn = psycopg2.connect(
            os.environ["DATABASE_URL"],
            connect_timeout=8,
            sslmode="require"
        )
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT to_regclass('paper_trading.bot_state')"
                )
                return cur.fetchone()[0] is not None
        finally:
            conn.close()

    try:
        table_exists = await asyncio.to_thread(check_connection)
        return {
            "database_connected": True,
            "table_exists": table_exists
        }
    except Exception as exc:
        print("SUPABASE_CONNECTION_ERROR:", repr(exc), flush=True)
        return {
            "database_connected": False,
            "error_type": type(exc).__name__
        }
