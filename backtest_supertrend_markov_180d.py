"""Independent spot long-only SuperTrend vs Markov soft-sizing research, paper only."""
import io, zipfile, urllib.request, json, csv, os
from datetime import datetime, timezone
import numpy as np
import pandas as pd

SYMS=['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT']
START=pd.Timestamp('2026-05-05',tz='UTC'); END=pd.Timestamp('2026-11-01',tz='UTC')
SPLIT=START+pd.Timedelta(days=120)
FEE=.0015; INITIAL=1000.0
OUT='supertrend_markov_results'; os.makedirs(OUT,exist_ok=True)
FAV={'up_low_vol','up_high_vol','sideways_low_vol'}; UNFAV={'down_high_vol'}
STATES=['up_low_vol','up_high_vol','sideways_low_vol','sideways_high_vol','down_low_vol','down_high_vol']

def get_data(sym):
    frames=[]
    for month in ['2026-04','2026-05','2026-06','2026-07','2026-08','2026-09','2026-10']:
        url=f'https://data.binance.vision/data/spot/monthly/klines/{sym}/4h/{sym}-4h-{month}.zip'
        print('FETCH',url,flush=True)
        req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'})
        with urllib.request.urlopen(req,timeout=120) as response:
            blob=response.read()
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            with z.open(next(n for n in z.namelist() if n.endswith('.csv'))) as f:
                raw=pd.read_csv(f,header=None)
        raw=raw.iloc[:,:6]; raw.columns=['time','open','high','low','close','volume']
        ts=pd.to_numeric(raw.time,errors='coerce'); raw=raw[ts.notna()].copy(); ts=ts[ts.notna()]
        raw['time']=pd.to_datetime(ts,unit='us' if ts.max()>1e14 else 'ms',utc=True)
        for col in ['open','high','low','close','volume']: raw[col]=pd.to_numeric(raw[col],errors='raise')
        frames.append(raw)
    df=pd.concat(frames).drop_duplicates('time').sort_values('time').set_index('time')
    df=df[(df.index>=pd.Timestamp('2026-04-01',tz='UTC'))&(df.index<END)]
    expected=pd.date_range(df.index[0],df.index[-1],freq='4h')
    if len(df)!=len(expected) or not df.index.equals(expected): raise ValueError(f'{sym}: missing or duplicate 4h bars')
    if len(df[(df.index>=START)&(df.index<END)])!=1080: raise ValueError(f'{sym}: expected 1080 test bars')
    return df

def supertrend(df,period=10,mult=3.0):
    h=df.high; l=df.low; c=df.close
    tr=pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    atr=tr.ewm(alpha=1/period,adjust=False).mean()
    bu=((h+l)/2+mult*atr).to_numpy(); bl=((h+l)/2-mult*atr).to_numpy(); close=c.to_numpy()
    n=len(df); upper=np.full(n,np.nan); lower=np.full(n,np.nan); direction=np.zeros(n,dtype=int)
    upper[0]=bu[0]; lower[0]=bl[0]; direction[0]=1
    for i in range(1,n):
        upper[i]=bu[i] if bu[i]<upper[i-1] or close[i-1]>upper[i-1] else upper[i-1]
        lower[i]=bl[i] if bl[i]>lower[i-1] or close[i-1]<lower[i-1] else lower[i-1]
        direction[i]=1 if close[i]>upper[i-1] else (-1 if close[i]<lower[i-1] else direction[i-1])
    return pd.Series(direction,index=df.index)

def classify(df):
    ret=df.close.pct_change(12); vol=np.log(df.close/df.close.shift()).rolling(14).std()
    dire=np.where(ret>.003,'up',np.where(ret<-.003,'down','sideways'))
    vc=np.where(vol>.008,'high_vol','low_vol')
    return pd.Series([a+'_'+b if pd.notna(r) and pd.notna(v) else None for a,b,r,v in zip(dire,vc,ret,vol)],index=df.index)

def hysteresis(states,n=3):
    out=[]; stable=None; candidate=None; count=0
    for state in states:
        if state is None: out.append(stable);candidate=None;count=0;continue
        if state==candidate:count+=1
        else:candidate=state;count=1
        if stable is None or (state!=stable and count>=n):stable=state
        out.append(stable)
    return pd.Series(out,index=states.index)

def matrix(states):
    counts=pd.DataFrame(1.,index=STATES,columns=STATES)
    a=states.dropna().to_list()
    for x,y in zip(a[:-1],a[1:]):counts.loc[x,y]+=1
    return counts.div(counts.sum(axis=1),axis=0)

def scores(df):
    raw=classify(df); stable=hysteresis(raw)
    # Training only: transitions strictly before holdout. For dev, expanding prior history, never current/future.
    arr=[]; train_counts=pd.DataFrame(1.,index=STATES,columns=STATES)
    last=None
    for t,s in stable.items():
        mat=train_counts.div(train_counts.sum(axis=1),axis=0)
        p=float(mat.loc[s,list(FAV)].sum()) if s in STATES else 0.
        score=1.0 if s is None else max(.25,min(1.,(.30 if s in FAV else 0.)+.70*p))
        if s in UNFAV:score=.25
        arr.append(score)
        # No updates during holdout. Training uses completed bars only.
        if t<SPLIT:
            if last in STATES and s in STATES:train_counts.loc[last,s]+=1.
            last=s
    return pd.Series(arr,index=df.index)

def simulate(df,weight,tag,sym):
    cash=INITIAL; qty=0.; entry_cost=0.; entry_time=None; records=[]; equity=[]
    direction=supertrend(df)
    ema50=df.close.ewm(span=50,adjust=False).mean(); ema200=df.close.ewm(span=200,adjust=False).mean()
    # signal at completed bar i-1, execute at OPEN of i
    for i in range(1,len(df)):
        t=df.index[i]; prev=df.index[i-1]; op=float(df.open.iloc[i]); close=float(df.close.iloc[i]);
        if t<START:continue
        if t>=END:break
        flip_down=direction.iloc[i-1]==-1 and direction.iloc[i-2]==1 if i>=2 else False
        flip_up=direction.iloc[i-1]==1 and direction.iloc[i-2]==-1 if i>=2 else False
        if qty>0 and flip_down:
            proceeds=qty*op*(1-FEE); pnl=proceeds-entry_cost; cash+=proceeds
            records.append({'symbol':sym,'variant':tag,'entry':str(entry_time),'exit':str(t),'pnl_usdt':round(pnl,6),'return_pct':round(100*pnl/entry_cost,5)})
            qty=0.;entry_cost=0.;entry_time=None
        if qty==0 and flip_up and ema50.iloc[i-1]>ema200.iloc[i-1]:
            w=float(weight.iloc[i-1]); allocated=cash*w; qty=allocated/(op*(1+FEE)); entry_cost=allocated;cash-=allocated;entry_time=t
        equity.append((t,cash+qty*close))
    # Mark-to-market at end, no artificial liquidation or exit trade
    eq=pd.Series(dict(equity),dtype=float)
    return records,eq,{'open_position':qty>0,'end_equity':round(float(eq.iloc[-1]),4)}

def metrics(records,eq,start,end):
    e=eq[(eq.index>=start)&(eq.index<end)]
    if e.empty:raise ValueError('No equity data')
    prior=eq[eq.index<start]; base=float(prior.iloc[-1]) if len(prior) else INITIAL
    ret=(float(e.iloc[-1])/base-1)*100
    dd=((e/e.cummax())-1).min()*100
    trades=[r for r in records if start<=pd.Timestamp(r['exit'])<end]
    profits=[r['pnl_usdt'] for r in trades]; pos=sum(x for x in profits if x>0);neg=-sum(x for x in profits if x<0)
    return {'return_pct':round(ret,4),'max_drawdown_pct':round(float(dd),4),'closed_trades':len(trades),'win_rate_pct':round(100*sum(x>0 for x in profits)/len(profits),3) if profits else None,'profit_factor':round(pos/neg,4) if neg else (None if not pos else 'infinite')}

all_trades=[];summary=[]
for sym in SYMS:
    df=get_data(sym); sc=scores(df)
    for name,weights in [('SuperTrend',pd.Series(1.,index=df.index)),('SuperTrend_Markov_soft_sizing',sc)]:
        trades,eq,extra=simulate(df,weights,name,sym);all_trades+=trades
        for segment,a,b in [('development',START,SPLIT),('holdout',SPLIT,END),('full',START,END)]:
            summary.append({'symbol':sym,'variant':name,'segment':segment,**metrics(trades,eq,a,b),**extra})
        eq.rename('equity').to_csv(f'{OUT}/{sym}_{name}_equity.csv')
    print('DONE',sym,flush=True)
with open(f'{OUT}/summary.csv','w',newline='') as f:
    writer=csv.DictWriter(f,fieldnames=summary[0].keys());writer.writeheader();writer.writerows(summary)
with open(f'{OUT}/trades.csv','w',newline='') as f:
    fields=['symbol','variant','entry','exit','pnl_usdt','return_pct'];writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(all_trades)
with open(f'{OUT}/methodology.json','w') as f:
    json.dump({'source':'Binance Vision spot monthly 4h','period':'2026-05-05 through 2026-10-31','split':str(SPLIT),'capital_per_symbol_usdt':INITIAL,'fee_per_side':FEE,'execution':'next candle open','markov':'soft sizing, expanding training transitions before split, frozen in holdout; 4h only','caveats':['five separate 1000 USDT portfolios, not shared capital','no slippage or spread','markov 4h-only; upstream multi-timeframe 1h/4h/1d not reproduced','positions crossing split remain open','end positions marked to market, not force closed','dev returns reflect online learning; holdout matrix frozen']},f,indent=2)
print(pd.DataFrame(summary).to_string(index=False))
