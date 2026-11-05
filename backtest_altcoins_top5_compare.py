"""Read-only, post-selection research: top-5 altcoins, CONTROL vs A2, same fixed 90d window.
Requires existing backtest_90d.py and backtest_ladders_90d.py in repository root.
No database writes, no orders, no Telegram messages.
"""
import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pandas as pd
from backtest_90d import fetch, indicators
from backtest_ladders_90d import simulate

SYMBOLS = ('AAVEUSDT', 'UNIUSDT', 'ADAUSDT', 'NEARUSDT', 'XRPUSDT')
OUT = Path('backtest_results_top5_compare')
OUT.mkdir(exist_ok=True)
# Exactly the same historical window as the previously reported 10-coin backtest.
START = datetime(2026, 8, 4, tzinfo=timezone.utc)
END = datetime(2026, 11, 2, tzinfo=timezone.utc)
SPLIT = START + timedelta(days=60)
A2 = ((.01, .50), (.02, .25), (.03, .25))


def main():
    print(f'Fixed evaluation: {START.isoformat()} to {END.isoformat()}', flush=True)
    frames = {}
    for symbol in SYMBOLS:
        d = indicators(fetch(symbol, START - timedelta(days=7), END))
        d.loc[d.time < int(START.timestamp() * 1000), ['entry', 'exit']] = False
        frames[symbol] = d[(d.time >= int(START.timestamp() * 1000)) & (d.time < int(END.timestamp() * 1000))].copy()
    rows = []
    for window, lo, hi in (('full_90d', START, END), ('development_60d', START, SPLIT), ('validation_30d', SPLIT, END)):
        sub = {s: d[(d.time >= int(lo.timestamp() * 1000)) & (d.time < int(hi.timestamp() * 1000))].copy() for s, d in frames.items()}
        for name, ladder in (('CONTROL', None), ('A2_50_25_25', A2)):
            summary, trades = simulate(sub, SYMBOLS, ladder)
            row = {'window': window, 'model': name, 'coins': ','.join(SYMBOLS), **summary}
            rows.append(row)
            with (OUT / f'{window}_{name}_trades.csv').open('w', newline='', encoding='utf-8') as f:
                fields = ['symbol', 'entry_time_utc', 'exit_time_utc', 'exit_price', 'reason', 'net_pnl_usdt', 'original_position_id']
                writer = csv.DictWriter(f, fieldnames=fields)
                writer.writeheader()
                writer.writerows(trades)
            print(row, flush=True)
    pd.DataFrame(rows).to_csv(OUT / 'summary.csv', index=False)
    (OUT / 'summary.json').write_text(json.dumps({
        'start_utc': START.isoformat(), 'end_utc': END.isoformat(), 'split_utc': SPLIT.isoformat(),
        'symbols': SYMBOLS, 'stop_pct': 10, 'initial_cash_usdt': 1000, 'stake_usdt': 100,
        'max_open_positions': 5, 'fee_per_side': .001,
        'selection_warning': 'These five coins were selected using results from the same 90-day window. Full-period results are in-sample and not independent validation.',
        'window_warning': '60-day and 30-day diagnostic runs reset capital and positions; they do not sum to the continuous 90-day portfolio.',
        'results': rows
    }, indent=2), encoding='utf-8')
    print(pd.DataFrame(rows).to_string(index=False), flush=True)

if __name__ == '__main__':
    main()
