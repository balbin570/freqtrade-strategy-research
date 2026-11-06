#!/usr/bin/env python3
"""Offline Binance Vision 4h data and causal SuperTrend signal audit. No orders."""
import io, json, sys, urllib.request, zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'hermes_source'
if not (SOURCE / 'hermes_trading' / 'signals.py').exists():
    raise SystemExit('Missing hermes_source/hermes_trading/signals.py')
sys.path.insert(0, str(SOURCE))
from hermes_trading.signals import supertrend

PAIRS = ('BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT')
END = datetime(2026, 11, 1, tzinfo=timezone.utc)
START = END - timedelta(days=180)
WARMUP = START - timedelta(days=35)
OUT = ROOT / 'supertrend_audit_results'
OUT.mkdir(parents=True, exist_ok=True)


def get_data(symbol):
    frames = []
    month = datetime(WARMUP.year, WARMUP.month, 1, tzinfo=timezone.utc)
    while month < END:
        url = (f'https://data.binance.vision/data/spot/monthly/klines/'
               f'{symbol}/4h/{symbol}-4h-{month:%Y-%m}.zip')
        print('DOWNLOAD', url, flush=True)
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0'}), timeout=90) as response:
            raw = response.read()
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            csv_name = next(n for n in archive.namelist() if n.endswith('.csv'))
            with archive.open(csv_name) as stream:
                frames.append(pd.read_csv(stream, header=None, usecols=[0,1,2,3,4,5],
                    names=['time','open','high','low','close','volume']))
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    df = pd.concat(frames, ignore_index=True)
    df['time'] = pd.to_numeric(df['time'], errors='raise')
    df.loc[df.time > 10**14, 'time'] /= 1000  # Binance archives may use microseconds
    for col in ('open','high','low','close','volume'):
        df[col] = pd.to_numeric(df[col], errors='raise')
    df = df.drop_duplicates('time').sort_values('time')
    lo, hi = int(WARMUP.timestamp()*1000), int(END.timestamp()*1000)
    df = df[(df.time >= lo) & (df.time + 4*3600*1000 <= hi)].copy()
    df['dt'] = pd.to_datetime(df.time, unit='ms', utc=True)
    df = df.set_index('dt')
    if df.empty or df.index[0] > WARMUP + timedelta(hours=4):
        raise ValueError(f'{symbol}: missing warmup coverage')
    if df.index[-1] < END - timedelta(hours=4):
        raise ValueError(f'{symbol}: missing end coverage')
    if (df.time.diff().dropna() != 4*3600*1000).any():
        raise ValueError(f'{symbol}: missing/irregular 4h candles')
    return df


def main():
    summaries = []
    for symbol in PAIRS:
        df = get_data(symbol)
        line, direction = supertrend(df, 10, 3.0)
        df['st_line'] = line
        df['st_direction'] = direction
        df['flip_up'] = (direction == 1) & (direction.shift(1) == -1)
        df['flip_down'] = (direction == -1) & (direction.shift(1) == 1)
        sample = df.loc[(df.index >= START) & (df.index < END)].copy()
        if len(sample) != 180 * 6:
            raise ValueError(f'{symbol}: expected 1080 4h candles; got {len(sample)}')
        # Verify indicator values are unchanged when future rows are removed.
        for end_idx in (250, 500, len(df)-1):
            subset = df.iloc[:end_idx].copy()
            _, partial = supertrend(subset, 10, 3.0)
            if not partial.equals(direction.iloc[:end_idx]):
                raise AssertionError(f'{symbol}: SuperTrend future-data dependency detected')
        events = sample.loc[sample.flip_up | sample.flip_down,
            ['open','high','low','close','volume','st_line','st_direction','flip_up','flip_down']].copy()
        events.to_csv(OUT / f'{symbol}_flips.csv')
        summaries.append({'symbol':symbol,'bars_4h':len(sample),
            'flip_up':int(sample.flip_up.sum()),'flip_down':int(sample.flip_down.sum()),
            'first_utc':str(sample.index[0]),'last_utc':str(sample.index[-1]),
            'causal_prefix_test':'PASS'})
        print('PASS', summaries[-1], flush=True)
    (OUT / 'summary.json').write_text(json.dumps({'period_start':START.isoformat(),
        'period_end_exclusive':END.isoformat(),'source':'Binance Vision monthly spot 4h archives',
        'purpose':'data and SuperTrend signal audit only; NOT a PnL backtest',
        'pairs':summaries},indent=2), encoding='utf-8')
    pd.DataFrame(summaries).to_csv(OUT / 'summary.csv',index=False)

if __name__ == '__main__':
    main()
