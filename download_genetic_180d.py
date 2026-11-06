#!/usr/bin/env python3
"""Download Binance Vision spot 5m OHLCV, UTC, without API credentials."""
import csv
import io
import json
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

PAIRS = ('BTC', 'ETH', 'SOL', 'BNB', 'XRP')
START = datetime(2026, 5, 1, tzinfo=timezone.utc)  # warmup included
END = datetime(2026, 11, 6, tzinfo=timezone.utc)  # exclusive
OUT = Path('user_data/data/binance')
OUT.mkdir(parents=True, exist_ok=True)

def fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'research-backtest/1.0'})
    with urllib.request.urlopen(req, timeout=90) as response:
        return response.read()

def read_zip(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        filename = next(n for n in z.namelist() if n.endswith('.csv'))
        for row in csv.reader(io.TextIOWrapper(z.open(filename), encoding='utf-8')):
            if len(row) < 6 or not row[0].isdigit():
                continue
            ts = int(row[0]); ts = ts // 1000 if ts > 10**14 else ts
            yield [ts, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])]

for pair in PAIRS:
    symbol = pair + 'USDT'
    rows = {}
    for month in range(5, 11):
        filename = f'{symbol}-5m-2026-{month:02d}.zip'
        url = f'https://data.binance.vision/data/spot/monthly/klines/{symbol}/5m/{filename}'
        print('GET', url, flush=True)
        for row in read_zip(fetch(url)):
            rows[row[0]] = row
    for day in range(1, 6):
        filename = f'{symbol}-5m-2026-11-{day:02d}.zip'
        url = f'https://data.binance.vision/data/spot/daily/klines/{symbol}/5m/{filename}'
        print('GET', url, flush=True)
        for row in read_zip(fetch(url)):
            rows[row[0]] = row
    lower = int(START.timestamp() * 1000)
    upper = int(END.timestamp() * 1000)
    selected = [rows[t] for t in sorted(rows) if lower <= t < upper]
    expected = int((END - START).total_seconds() // 300)
    if len(selected) != expected:
        raise RuntimeError(f'{symbol}: missing candles: expected {expected}, found {len(selected)}')
    for prev, curr in zip(selected, selected[1:]):
        if curr[0] - prev[0] != 300_000:
            raise RuntimeError(f'{symbol}: gap after {prev[0]}')
    path = OUT / f'{pair}_USDT-5m.json'
    path.write_text(json.dumps(selected, separators=(',', ':')))
    print(f'SAVED {path}: {len(selected)} candles', flush=True)
