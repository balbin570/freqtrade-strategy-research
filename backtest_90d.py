import io, zipfile, urllib.request, csv, json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np
import pandas as pd

SYMBOLS=['BTCUSDT','ETHUSDT','SOLUSDT']
STOPS=[.03,.05,.10]
FEE=.001
STAKE=100.
MAX_OPEN=5
OUT=Path('backtest_results'); OUT.mkdir(exist_ok=True)

def fetch(symbol, start, end):
    frames=[]; day=start.date(); last=end.date()
    while day<=last:
        url=f'https://data.binance.vision/data/spot/daily/klines/{symbol}/5m/{symbol}-5m-{day:%Y-%m-%d}.zip'
        try:
            with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'}),timeout=40) as r: raw=r.read()
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                with z.open(next(n for n in z.namelist() if n.endswith('.csv'))) as f:
                    frames.append(pd.read_csv(f,header=None,usecols=[0,1,2,3,4,5],names=['time','open','high','low','close','volume']))
        except Exception as e:
            raise RuntimeError(f'Missing Binance Vision archive {url}: {e}') from e
        day+=timedelta(days=1)
    d=pd.concat(frames,ignore_index=True)
    d['time']=pd.to_numeric(d.time)
    d.loc[d.time>10**14,'time']=d.loc[d.time>10**14,'time']/1000
    for col in ['open','high','low','close','volume']: d[col]=pd.to_numeric(d[col])
    d=d.drop_duplicates('time').sort_values('time')
    d=d[(d.time>=int(start.timestamp()*1000)) & (d.time+300000<=int(end.timestamp()*1000))].reset_index(drop=True)
    if len(d)<150 or (d.time.diff().dropna()!=300000).any(): raise RuntimeError(f'Missing or irregular 5m candles: {symbol}')
    print(symbol,len(d),'candles',flush=True)
    return d

def indicators(d):
    d=d.copy()
    for n in [20,50,100]: d[f'ema{n}']=d.close.ewm(span=n,adjust=False,min_periods=n).mean()
    d['ha_close']=(d.open+d.high+d.low+d.close)/4
    ha=np.empty(len(d)); ha[0]=(d.open.iloc[0]+d.close.iloc[0])/2
    for i in range(1,len(d)): ha[i]=(ha[i-1]+d.ha_close.iloc[i-1])/2
    d['ha_open']=ha
    a,b,c=d.ema20,d.ema50,d.ema100
    d['entry']=(a.shift(1)<=b.shift(1))&(a>b)&(d.ha_close>a)&(d.ha_open<d.ha_close)
    d['exit']=(b.shift(1)<=c.shift(1))&(b>c)&(d.ha_close<a)&(d.ha_open>d.ha_close)
    d.loc[:99,['entry','exit']]=False
    return d

def roi(age):
    return .01 if age>=60 else .03 if age>=30 else .04 if age>=20 else .05

def run(frames, stop):
    cash=1000.; positions=[]; trades=[]; last={}; peak=1000.; drawdown=0.; signals=0; rejected_max=0; rejected_cash=0
    events=sorted(((int(r.time),s,r) for s,d in frames.items() for r in d.itertuples(index=False)),key=lambda x:(x[0],x[1]))
    prev=None
    for t,s,r in events:
        if prev is not None and t!=prev:
            eq=cash+sum(p['qty']*last.get(p['symbol'],p['entry_price'])*(1-FEE) for p in positions)
            peak=max(peak,eq); drawdown=max(drawdown,(peak-eq)/peak)
        prev=t; last[s]=float(r.close)
        for p in list(positions):
            if p['symbol']!=s: continue
            age=max(0,(t-p['time'])/60000)
            sl=p['entry_price']*(1-stop); tp=p['entry_price']*(1+roi(age))
            price=None; reason=None
            if r.open<=sl: price=float(r.open); reason='stop_gap'
            elif r.low<=sl: price=sl; reason='stoploss'
            elif r.open>=tp: price=float(r.open); reason='roi_gap'
            elif r.high>=tp: price=tp; reason='roi'
            elif r.exit and r.close>p['entry_price']: price=float(r.close); reason='exit_signal'
            if reason:
                proceeds=p['qty']*price*(1-FEE); pnl=proceeds-p['cost']; cash+=proceeds
                trades.append(dict(symbol=s,entry_time_utc=pd.to_datetime(p['time'],unit='ms',utc=True).isoformat(),exit_time_utc=pd.to_datetime(t,unit='ms',utc=True).isoformat(),entry_price=p['entry_price'],exit_price=price,reason=reason,net_pnl_usdt=round(pnl,6)))
                positions.remove(p)
        if r.entry:
            signals+=1
            if len(positions)>=MAX_OPEN: rejected_max+=1
            elif cash<STAKE*(1+FEE): rejected_cash+=1
            else:
                price=float(r.close); cost=STAKE*(1+FEE); cash-=cost
                positions.append(dict(symbol=s,time=t,entry_price=price,qty=STAKE/price,cost=cost))
    equity=cash+sum(p['qty']*last[p['symbol']]*(1-FEE) for p in positions)
    peak=max(peak,equity); drawdown=max(drawdown,(peak-equity)/peak)
    pnls=[x['net_pnl_usdt'] for x in trades]; wins=sum(p>0 for p in pnls); gain=sum(p for p in pnls if p>0); loss=-sum(p for p in pnls if p<0)
    return dict(stop_pct=round(stop*100),closed_trades=len(trades),open_positions=len(positions),signals=signals,rejected_max_positions=rejected_max,rejected_insufficient_cash=rejected_cash,win_rate_pct=round(wins/len(pnls)*100,2) if pnls else None,profit_factor=round(gain/loss,4) if loss else None,realized_pnl_usdt=round(sum(pnls),4),final_equity_usdt=round(equity,4),total_return_pct=round((equity/1000-1)*100,4),max_drawdown_pct=round(drawdown*100,4)),trades

def main():
    now=datetime.now(timezone.utc); end=now.replace(minute=now.minute//5*5,second=0,microsecond=0)
    start=end-timedelta(days=90); warmup=start-timedelta(days=7)
    print('Evaluation UTC:',start.isoformat(),'to',end.isoformat(),flush=True)
    frames={}
    for symbol in SYMBOLS:
        d=indicators(fetch(symbol,warmup,end))
        d.loc[d.time<int(start.timestamp()*1000),['entry','exit']]=False
        frames[symbol]=d[d.time>=int(start.timestamp()*1000)].copy()
    results=[]
    for stop in STOPS:
        summary,trades=run(frames,stop); results.append(summary)
        with (OUT/f'trades_stop_{int(stop*100)}.csv').open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=['symbol','entry_time_utc','exit_time_utc','entry_price','exit_price','reason','net_pnl_usdt']);w.writeheader();w.writerows(trades)
    pd.DataFrame(results).to_csv(OUT/'summary.csv',index=False)
    (OUT/'summary.json').write_text(json.dumps(dict(start_utc=start.isoformat(),end_utc=end.isoformat(),results=results),indent=2))
    print(pd.DataFrame(results).to_string(index=False),flush=True)

if __name__=='__main__': main()
