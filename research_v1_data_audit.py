#!/usr/bin/env python3
"""Paper-only Binance Vision 1h data coverage audit; no trading/API keys."""
import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

SYMBOLS = [
    'BTCUSDT','ETHUSDT','BNBUSDT','XRPUSDT','SOLUSDT','ADAUSDT','DOGEUSDT',
    'TRXUSDT','LINKUSDT','AVAXUSDT','DOTUSDT','LTCUSDT','BCHUSDT','ATOMUSDT',
    'UNIUSDT','ETCUSDT','XLMUSDT','NEARUSDT','APTUSDT','FILUSDT',
]
START = (2023, 11)
END = (2026, 10)  # last completed month; 36 months
OUT = Path('research_v1_results')
BASE = 'https://data.binance.vision/data/spot/monthly/klines'


def months(start, end):
    y, m = start
    while (y, m) <= end:
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def download(url):
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0 research-audit'})
            with urllib.request.urlopen(req, timeout=45) as response:
                return response.read(), None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, 'NOT_LISTED_OR_MISSING'
            last = f'HTTP_{e.code}'
        except Exception as e:
            last = type(e).__name__ + ': ' + str(e)[:120]
        time.sleep(attempt + 1)
    return None, last


def check_zip(blob, year, month):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = [n for n in z.namelist() if n.endswith('.csv') and not n.startswith('__MACOSX')]
        if len(names) != 1:
            raise ValueError('Unexpected CSV count')
        with z.open(names[0]) as f:
            reader = csv.reader(io.TextIOWrapper(f, encoding='utf-8'))
            first = last = previous = None
            count = gaps = duplicate_or_reversed = 0
            for row in reader:
                try:
                    t = int(row[0])
                except (ValueError, IndexError):
                    if count == 0:
                        continue  # optional header
                    raise
                if t > 10**14:  # Binance archives may use microseconds
                    t //= 1000
                if first is None:
                    first = t
                if previous is not None:
                    delta = t - previous
                    if delta > 3_600_000:
                        gaps += (delta // 3_600_000) - 1
                    elif delta <= 0:
                        duplicate_or_reversed += 1
                    elif delta != 3_600_000:
                        duplicate_or_reversed += 1
                previous = last = t
                count += 1
    return dict(candles=count, gaps=gaps, non_hourly_or_duplicate=duplicate_or_reversed,
                first_utc=datetime.fromtimestamp(first/1000,timezone.utc).isoformat() if first else None,
                last_utc=datetime.fromtimestamp(last/1000,timezone.utc).isoformat() if last else None)


def main():
    OUT.mkdir(exist_ok=True)
    rows = []
    periods = list(months(START, END))
    for symbol in SYMBOLS:
        print('AUDIT', symbol, flush=True)
        for y, m in periods:
            period = f'{y}-{m:02d}'
            url = f'{BASE}/{symbol}/1h/{symbol}-1h-{period}.zip'
            blob, err = download(url)
            record = {'symbol':symbol,'month':period,'status':'OK' if blob else 'MISSING', 'error':err or ''}
            if blob:
                try:
                    record.update(check_zip(blob,y,m))
                    if record['gaps'] or record['non_hourly_or_duplicate']:
                        record['status'] = 'DATA_ISSUE'
                except Exception as e:
                    record.update(status='ERROR',error=f'{type(e).__name__}: {e}')
            rows.append(record)
            print(f"  {period}: {record['status']} {record.get('candles',0)}", flush=True)
    with (OUT/'monthly_coverage.csv').open('w',newline='',encoding='utf-8') as f:
        fields = ['symbol','month','status','error','candles','gaps','non_hourly_or_duplicate','first_utc','last_utc']
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)
    summary = []
    for symbol in SYMBOLS:
        r = [x for x in rows if x['symbol']==symbol]
        summary.append({'symbol':symbol,'months_ok':sum(x['status']=='OK' for x in r),
                        'months_missing':sum(x['status']=='MISSING' for x in r),
                        'months_with_issues':sum(x['status'] in ('ERROR','DATA_ISSUE') for x in r),
                        'candles_total':sum(x.get('candles',0) for x in r)})
    (OUT/'summary.json').write_text(json.dumps({'start_month':'2023-11','end_month':'2026-10',
        'timeframe':'1h','source':'Binance Vision spot monthly archive','paper_only':True,
        'symbols':summary},indent=2),encoding='utf-8')
    print('SAVED research_v1_results/summary.json and monthly_coverage.csv',flush=True)
    print(json.dumps(summary,indent=2),flush=True)

if __name__ == '__main__':
    main()
