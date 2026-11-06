#!/usr/bin/env python3
"""Independent long-only spot research, not a trading bot. Binance Vision 1h, 20 symbols."""
import csv, io, json, time, urllib.request, urllib.error, zipfile
from pathlib import Path
import numpy as np
import pandas as pd

SYMBOLS = ['BTCUSDT','ETHUSDT','BNBUSDT','XRPUSDT','SOLUSDT','ADAUSDT','DOGEUSDT','TRXUSDT','LINKUSDT','AVAXUSDT','DOTUSDT','LTCUSDT','BCHUSDT','ATOMUSDT','UNIUSDT','ETCUSDT','XLMUSDT','NEARUSDT','APTUSDT','FILUSDT']
OUT=Path('research_v1_backtest_results'); OUT.mkdir(exist_ok=True)
BASE='https://data.binance.vision/data/spot/monthly/klines'
# 0.15% fee + 0.05% slippage each side; 0.40% estimated round trip.
SIDE_COST=0.002
SPLIT=pd.Timestamp('2025-11-01',tz='UTC')
END=pd.Timestamp('2026-11-01',tz='UTC')

def fetch(symbol, year, month):
    period=f'{year}-{month:02d}'
    url=f'{BASE}/{symbol}/1h/{symbol}-1h-{period}.zip'
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 research'}),timeout=70) as r: blob=r.read()
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                names=[n for n in z.namelist() if n.endswith('.csv')]
                if len(names)!=1: raise ValueError('Unexpected CSV count')
                df=pd.read_csv(z.open(names[0]),header=None,usecols=[0,1,2,3,4,5],names=['time','open','high','low','close','volume'],dtype=str)
            df=df[pd.to_numeric(df.time,errors='coerce').notna()].copy()
            ts=pd.to_numeric(df.time).astype('int64')
            df['time']=pd.to_datetime(np.where(ts>10**14,ts//1000,ts),unit='ms',utc=True)
            for c in ['open','high','low','close','volume']: df[c]=pd.to_numeric(df[c],errors='raise')
            return df
        except Exception as exc:
            if attempt==3: raise RuntimeError(f'{symbol} {period}: {exc}') from exc
            time.sleep(2*(attempt+1))

def get_data(symbol):
    pieces=[]
    for year in range(2023,2027):
        for month in range(1,13):
            if (2023,11)<=(year,month)<=(2026,10):
                pieces.append(fetch(symbol,year,month))
    d=pd.concat(pieces,ignore_index=True).sort_values('time').reset_index(drop=True)
    expected=pd.date_range('2023-11-01','2026-10-31 23:00',freq='h',tz='UTC')
    if len(d)!=len(expected) or not d.time.equals(pd.Series(expected,name='time')): raise ValueError(f'{symbol}: invalid time continuity')
    if not ((d.high>=d[['open','close','low']].max(axis=1))&(d.low<=d[['open','close','high']].min(axis=1))&(d[['open','high','low','close']]>0).all(axis=1)&(d.volume>=0)).all(): raise ValueError(f'{symbol}: invalid OHLCV')
    return d

def signals(d):
    c=d.close; h=d.high; l=d.low; v=d.volume
    ema50=c.ewm(span=50,adjust=False).mean(); ema200=c.ewm(span=200,adjust=False).mean()
    r=c.pct_change(); gain=r.clip(lower=0).ewm(alpha=1/14,adjust=False).mean(); loss=(-r.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean()
    rsi=100-100/(1+gain/loss.replace(0,np.nan))
    tr=pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    atr=tr.ewm(alpha=1/14,adjust=False).mean(); atrp=atr/c
    highest=h.shift(1).rolling(24).max(); lowest=l.shift(1).rolling(24).min()
    volratio=v/v.shift(1).rolling(24).mean()
    trend=ema50>ema200
    # All signals use only completed current and earlier candles; next candle open is execution.
    candidates={
        'TREND_EMA':trend & (c>ema50) & (c>c.shift(1)),
        'MEAN_REVERSION':(rsi<32) & (c>ema200),
        'BREAKOUT_RETEST':(c>highest) & (l<=highest*1.005) & trend,
        'MOMENTUM_VOLUME':(c/c.shift(24)-1>0.025) & (volratio>1.5) & trend,
        'VOLATILITY_REGIME':trend & (atrp>atrp.shift(1).rolling(168).median()) & (c>highest),
    }
    # Exit rules defined ex ante; no optimization on holdout.
    exits={
        'TREND_EMA':(~trend)|(c<ema50),
        'MEAN_REVERSION':(rsi>55)|(c<ema200),
        'BREAKOUT_RETEST':(c<ema50)|(c<lowest),
        'MOMENTUM_VOLUME':(c<ema50)|(rsi<45),
        'VOLATILITY_REGIME':(c<ema50)|(atrp<atrp.shift(1).rolling(168).median()),
    }
    return candidates,exits

def simulate(d, entry, exit_, start, end):
    # Each segment starts flat, ensuring development positions do not leak into holdout.
    # 1 unit initial equity, full notional allocation, long only, no leverage.
    t=d.time.to_numpy(); op=d.open.to_numpy(dtype=float)
    ent=entry.fillna(False).to_numpy(dtype=bool); ex=exit_.fillna(False).to_numpy(dtype=bool)
    start_i=int(np.searchsorted(t,np.datetime64(start.tz_localize(None)),side='left')) if False else int(d.time.searchsorted(start))
    end_i=int(d.time.searchsorted(end))
    cash=1.0; units=0.0; entry_price=0.0; trades=[]; equity=[]; n_entries=0
    # signal from i-1 executes at open i, never on same bar
    for i in range(start_i,end_i):
        price=op[i]
        if units>0 and ex[i-1]:
            proceeds=units*price*(1-SIDE_COST)
            trades.append({'entry_utc':str(d.time.iloc[entry_idx]),'exit_utc':str(d.time.iloc[i]),'net_pct':100*(proceeds/entry_cash-1)})
            cash=proceeds; units=0.0
        if units==0 and ent[i-1] and i<end_i-1:
            entry_cash=cash; units=cash*(1-SIDE_COST)/price; cash=0.0; entry_idx=i; n_entries+=1
        equity.append(cash+units*price*(1-SIDE_COST))
    if units>0:
        price=op[end_i-1]; proceeds=units*price*(1-SIDE_COST)
        trades.append({'entry_utc':str(d.time.iloc[entry_idx]),'exit_utc':str(d.time.iloc[end_i-1]),'net_pct':100*(proceeds/entry_cash-1)})
        cash=proceeds; equity[-1]=cash
    arr=np.asarray(equity); dd=arr/np.maximum.accumulate(arr)-1
    gains=sum(max(x['net_pct'],0) for x in trades); losses=-sum(min(x['net_pct'],0) for x in trades)
    return {'return_pct':round(100*(cash-1),3),'max_drawdown_pct':round(100*dd.min(),3), 'trades':len(trades),'win_rate_pct':round(100*sum(x['net_pct']>0 for x in trades)/len(trades),2) if trades else None,'profit_factor':round(gains/losses,3) if losses else None,'closed_trades':trades}

def main():
    rows=[]; failures=[]
    for symbol in SYMBOLS:
        print('BACKTEST',symbol,flush=True)
        try:
            d=get_data(symbol); entries,exits=signals(d)
            for strategy in entries:
                for segment,start,end in [('development',pd.Timestamp('2023-11-01',tz='UTC'),SPLIT),('holdout',SPLIT,END)]:
                    result=simulate(d,entries[strategy],exits[strategy],start,end)
                    trades=result.pop('closed_trades')
                    rows.append({'symbol':symbol,'strategy':strategy,'segment':segment,**result})
                    for tr in trades: trade_rows.append({'symbol':symbol,'strategy':strategy,'segment':segment,**tr})
        except Exception as exc:
            failures.append({'symbol':symbol,'error':str(exc)}); print('ERROR',symbol,exc,flush=True)
    pd.DataFrame(rows).to_csv(OUT/'results_by_symbol.csv',index=False)
    pd.DataFrame(trade_rows).to_csv(OUT/'closed_trades.csv',index=False)
    summary=[]
    if rows:
        frame=pd.DataFrame(rows)
        for (strategy,segment),g in frame.groupby(['strategy','segment']):
            summary.append({'strategy':strategy,'segment':segment,'symbols':len(g),'positive_symbols':int((g.return_pct>0).sum()),'median_return_pct':round(float(g.return_pct.median()),3),'mean_return_pct':round(float(g.return_pct.mean()),3),'median_max_drawdown_pct':round(float(g.max_drawdown_pct.median()),3),'total_closed_trades':int(g.trades.sum())})
    pd.DataFrame(summary).to_csv(OUT/'strategy_summary.csv',index=False)
    (OUT/'methodology.json').write_text(json.dumps({'source':'Binance Vision spot monthly 1h','symbols':SYMBOLS,'development':'2023-11-01 to 2025-10-31','holdout':'2025-11-01 to 2026-10-31','entry':'next candle open after completed signal','fee_per_side_pct':0.15,'slippage_per_side_pct':0.05,'portfolio':'independent 100% allocated single-position per symbol and strategy; NOT combined portfolio','no_live_orders':True,'caveats':['No funding, market impact or delistings','Survivorship bias: present-day symbol selection','No portfolio-level risk aggregation','Five predeclared baseline strategies; not optimized','Profit factor uses sum of per-trade percentage returns']},indent=2),encoding='utf-8')
    (OUT/'failures.json').write_text(json.dumps(failures,indent=2),encoding='utf-8')
    print(pd.DataFrame(summary).to_string(index=False),flush=True)
    if failures: raise RuntimeError(f'{len(failures)} symbol(s) failed; see failures.json')

if __name__=='__main__':
    trade_rows=[]
    main()
