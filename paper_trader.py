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
    await asyncio.to_thread(load_state)

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
        "telegram": state["telegram"], "persistence": state.get("persistence"), "strategies": {}
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
                      "Portfolio state is stored in Supabase PostgreSQL; local /tmp is not authoritative.")
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
