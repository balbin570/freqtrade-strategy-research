
"""A2 Top5 vs completed BTC 1-hour EMA trend filters. Backtest only."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from backtest_90d import fetch, indicators, FEE, STAKE, MAX_OPEN

COINS = ("AAVEUSDT", "UNIUSDT", "ADAUSDT", "NEARUSDT", "XRPUSDT")
MODELS = ("A2_BASE", "A2_BTC_1H_EMA50", "A2_BTC_1H_EMA50_200")
OUT = Path("backtest_results_a2_btc_1h_top5")
OUT.mkdir(exist_ok=True)

INITIAL_CASH = 1000.0
STOP = 0.10
LADDER = ((0.01, 0.50), (0.02, 0.25), (0.03, 0.25))


def simulate(frames, regime, model):
    events = sorted(
        (
            (int(r.time), sym, r)
            for sym, df in frames.items()
            for r in df.itertuples(index=False)
        ),
        key=lambda x: (x[0], x[1]),
    )
    cash, positions, fills, last = INITIAL_CASH, [], [], {}
    peak, max_dd = INITIAL_CASH, 0.0
    signals, filtered, rejected = 0, 0, 0
    next_id, prev = 0, None

    def sell(p, fraction, price, reason, t):
        nonlocal cash
        qty = p["qty"] * fraction
        proceeds = qty * price * (1 - FEE)
        cost = p["cost"] * fraction
        cash += proceeds
        p["qty"] -= qty
        p["cost"] -= cost
        fills.append({
            "symbol": p["symbol"],
            "position_id": p["id"],
            "entry_time_utc": pd.to_datetime(
                p["time"], unit="ms", utc=True
            ).isoformat(),
            "exit_time_utc": pd.to_datetime(
                t, unit="ms", utc=True
            ).isoformat(),
            "reason": reason,
            "exit_price": price,
            "net_pnl_usdt": round(proceeds - cost, 8),
        })

    for t, sym, r in events:
        if prev is not None and t != prev:
            equity = cash + sum(
                p["qty"] * last.get(p["symbol"], p["entry_price"]) * (1 - FEE)
                for p in positions
            )
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak)

        prev = t
        last[sym] = float(r.close)

        for p in list(positions):
            if p["symbol"] != sym:
                continue

            entry = p["entry_price"]
            sl = entry * (1 - STOP)

            if r.open <= sl or r.low <= sl:
                price = float(r.open) if r.open <= sl else sl
                sell(
                    p, 1.0, price,
                    "stop_gap" if r.open <= sl else "stop", t
                )
                positions.remove(p)
                continue

            for target_pct, part in LADDER:
                if target_pct in p["done"]:
                    continue

                target = entry * (1 + target_pct)
                if r.high >= target:
                    price = (
                        max(float(r.open), target)
                        if r.open >= target else target
                    )
                    fraction = min(1.0, part / p["remaining_original"])
                    sell(
                        p, fraction, price,
                        f"partial_{int(target_pct * 100)}pct", t
                    )
                    p["remaining_original"] -= part
                    p["done"].add(target_pct)

                    if p["qty"] < 1e-12 or p["remaining_original"] < 1e-9:
                        positions.remove(p)
                        break

            if p not in positions:
                continue

            if r.exit and r.close > entry:
                sell(p, 1.0, float(r.close), "exit_signal", t)
                positions.remove(p)

        if r.entry:
            signals += 1

            if model != "A2_BASE":
                flags = regime.get(t)
                permitted = flags is not None and (
                    flags[0]
                    if model == "A2_BTC_1H_EMA50"
                    else flags[1]
                )
                if not permitted:
                    filtered += 1
                    continue

            if len(positions) >= MAX_OPEN or cash < STAKE * (1 + FEE):
                rejected += 1
                continue

            price = float(r.close)
            cash -= STAKE * (1 + FEE)
            next_id += 1
            positions.append({
                "id": next_id,
                "symbol": sym,
                "time": t,
                "entry_price": price,
                "qty": STAKE / price,
                "cost": STAKE * (1 + FEE),
                "done": set(),
                "remaining_original": 1.0,
            })

    equity = cash + sum(
        p["qty"] * last[p["symbol"]] * (1 - FEE)
        for p in positions
    )
    peak = max(peak, equity)
    max_dd = max(max_dd, (peak - equity) / peak)

    pnls = [f["net_pnl_usdt"] for f in fills]
    gains = sum(x for x in pnls if x > 0)
    losses = -sum(x for x in pnls if x < 0)
    completed = (
        {f["position_id"] for f in fills}
        - {p["id"] for p in positions}
    )

    return {
        "model": model,
        "signals": signals,
        "filtered_signals": filtered,
        "rejected_entries": rejected,
        "closed_positions": len(completed),
        "exit_fills": len(fills),
        "open_positions": len(positions),
        "realized_pnl_usdt": round(sum(pnls), 4),
        "final_equity_usdt": round(equity, 4),
        "total_return_pct": round(
            (equity / INITIAL_CASH - 1) * 100, 4
        ),
        "max_drawdown_pct": round(max_dd * 100, 4),
        "profit_factor": round(gains / losses, 4) if losses else None,
    }, fills


def build_hourly_regime(btc):
    """Only fully closed BTC 1h candles, made from twelve 5m candles."""
    btc = btc.copy()
    btc["dt"] = pd.to_datetime(btc["time"], unit="ms", utc=True)
    btc = btc.set_index("dt").sort_index()
    hourly = btc["close"].resample("1h", closed="left", label="right").agg(["last", "count"])
    hourly = hourly[hourly["count"] == 12].copy()
    hourly["ema50"] = hourly["last"].ewm(span=50, adjust=False, min_periods=50).mean()
    hourly["ema200"] = hourly["last"].ewm(span=200, adjust=False, min_periods=200).mean()
    hourly["above50"] = hourly["last"] > hourly["ema50"]
    hourly["ema50_gt_ema200"] = hourly["ema50"] > hourly["ema200"]
    hourly = hourly.dropna(subset=["ema200"])
    # Hourly index is the candle's closing time, not its opening time.
    # A candle closing at 12:00 can only be used from 12:00 onward.
    return hourly


def main():
    end = (
        datetime.now(timezone.utc) - timedelta(days=3)
    ).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=90)
    split = start + timedelta(days=60)
    warmup = start - timedelta(days=21)  # >=200 completed hourly BTC candles
    start_ms = int(start.timestamp() * 1000)
    print("UTC:", start.isoformat(), "to", end.isoformat(), flush=True)

    btc = fetch("BTCUSDT", warmup, end)
    hourly = build_hourly_regime(btc)
    print("Completed BTC hourly candles:", len(hourly), flush=True)

    # Align every 5-minute entry with the most recently completed BTC 1h candle.
    # Never use a still-forming hourly candle.
    btc_times = pd.DataFrame({"time": sorted(int(t) for t in btc["time"].unique())})
    btc_times["dt"] = pd.to_datetime(btc_times["time"], unit="ms", utc=True)
    hourly_flags = hourly[["above50", "ema50_gt_ema200"]].reset_index()
    aligned = pd.merge_asof(
        btc_times.sort_values("dt"), hourly_flags.sort_values("dt"),
        on="dt", direction="backward", tolerance=pd.Timedelta("1h")
    )
    regime = {
        int(r.time): (
            bool(r.above50) if pd.notna(r.above50) else False,
            bool(r.ema50_gt_ema200) if pd.notna(r.ema50_gt_ema200) else False,
        )
        for r in aligned.itertuples(index=False)
    }

    frames = {}
    for sym in COINS:
        df = indicators(fetch(sym, warmup, end))
        frames[sym] = df[df.time >= start_ms].copy()

    results = []
    windows = (
        ("full_90d", start, end),
        ("development_60d", start, split),
        ("diagnostic_last_30d", split, end),
    )
    for window, lo, hi in windows:
        lo_ms = int(lo.timestamp() * 1000)
        hi_ms = int(hi.timestamp() * 1000)
        subset = {
            sym: df[(df.time >= lo_ms) & (df.time < hi_ms)].copy()
            for sym, df in frames.items()
        }
        for model in MODELS:
            summary, fills = simulate(subset, regime, model)
            summary = {"window": window, **summary}
            results.append(summary)
            pd.DataFrame(
                fills,
                columns=[
                    "symbol", "position_id", "entry_time_utc", "exit_time_utc",
                    "reason", "exit_price", "net_pnl_usdt",
                ],
            ).to_csv(OUT / f"{window}_{model}_fills.csv", index=False)
            print(summary, flush=True)

    pd.DataFrame(results).to_csv(OUT / "summary.csv", index=False)
    (OUT / "summary.json").write_text(
        json.dumps({
            "start_utc": start.isoformat(),
            "split_utc": split.isoformat(),
            "end_utc": end.isoformat(),
            "coins": COINS,
            "models": MODELS,
            "starting_cash": INITIAL_CASH,
            "stop_pct": 10,
            "fee_per_side": FEE,
            "stake": STAKE,
            "max_open": MAX_OPEN,
            "btc_filter": (
                "Most recently completed BTC 1h candle: close > EMA50; "
                "or EMA50 > EMA200 (separate models)"
            ),
            "notes": [
                "A2: 50% at +1%, 25% at +2%, 25% at +3%",
                "Stop-first assumption; no slippage",
                "No orders or database access",
                "60d and 30d reset capital and positions",
                "Last 30d is diagnostic, not independent validation",
                "Only full 1h BTC candles; 21d indicator warmup",
            ],
            "results": results,
        }, indent=2), encoding="utf-8",
    )
    print("RESULTS SAVED:", OUT, flush=True)


if __name__ == "__main__":
    main()
