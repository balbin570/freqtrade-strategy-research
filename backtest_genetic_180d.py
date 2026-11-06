#!/usr/bin/env python3
"""Independent, API-free GeneticEngineV1Causal research simulator.
Uses original strategy's signal methods, but custom OHLCV execution model.
Not an exact Freqtrade backtest. No orders, keys, exchange API, or network.
"""
import json, sys, importlib.util
from pathlib import Path
import numpy as np
import pandas as pd

ROOT=Path('/freqtrade/research') if Path('/freqtrade/research/GeneticEngineV1Causal.py').exists() else Path(__file__).resolve().parent
DATA=ROOT/'user_data/data/binance'
OUT=ROOT/'genetic_180d_results'; OUT.mkdir(parents=True,exist_ok=True)
SYMBOLS=('BTC','ETH','SOL','BNB','XRP')
START=pd.Timestamp('2026-05-10',tz='UTC'); SPLIT=pd.Timestamp('2026-09-07',tz='UTC'); END=pd.Timestamp('2026-11-06',tz='UTC')
FEE=.0015; STAKE=100.; START_CASH=1000.; MAX_OPEN=1

spec=importlib.util.spec_from_file_location('genetic_causal',ROOT/'GeneticEngineV1Causal.py')
mod=importlib.util.module_from_spec(spec); sys.modules[spec.name]=mod; spec.loader.exec_module(mod)
strategy=mod.GeneticEngineV1Causal(config={'stake_currency':'USDT','dry_run':True,'exchange':{'name':'binance'},'runmode':'backtest'})

frames={}
for symbol in SYMBOLS:
    path=DATA/f'{symbol}_USDT-5m.json'
    if not path.exists(): raise FileNotFoundError(path)
    df=pd.DataFrame(json.loads(path.read_text()),columns=['time','open','high','low','close','volume'])
    df['date']=pd.to_datetime(df['time'],unit='ms',utc=True)
    df=df.sort_values('date').reset_index(drop=True)
    if df['date'].duplicated().any() or not df['date'].diff().dropna().eq(pd.Timedelta(minutes=5)).all():
        raise ValueError(f'{symbol}: missing/duplicate 5m candles')
    if df.date.min()>START-pd.Timedelta(days=10) or df.date.max()<END-pd.Timedelta(minutes=5):
        raise ValueError(f'{symbol}: insufficient coverage')
    # Deliberately do not call populate_indicators(): it computes hundreds of
    # unused genes. Entry/exit methods lazily compute the actual selected genes.
    df=strategy.populate_entry_trend(df,{'pair':f'{symbol}/USDT'})
    df=strategy.populate_exit_trend(df,{'pair':f'{symbol}/USDT'})
    df['entry']=df.get('enter_long',pd.Series(0,index=df.index)).fillna(0).eq(1)
    df['exit']=df.get('exit_long',pd.Series(0,index=df.index)).fillna(0).eq(1)
    # Next candle open, never current signal candle's close.
    df['entry_next']=df['entry'].shift(1,fill_value=False)
    df['exit_next']=df['exit'].shift(1,fill_value=False)
    frames[symbol]=df
    print(symbol,'candles',len(df),'signals',int(df.entry.sum()),flush=True)

# Portfolio-level chronological execution, one position at a time.
# Stop-first intrabar assumption; no slippage, no spread or liquidity model.
# Trailing stop begins after +2% observed high; trailing distance 1%.
# A same-bar trailing activation and stop is treated conservatively.
ROI={int(k):float(v) for k,v in strategy.minimal_roi.items()}

def run(lo,hi,label):
    cash=START_CASH; pos=None; trades=[]; equity_curve=[]; peak=START_CASH; dd=0.; last={}
    sub={s:df[(df.date>=lo)&(df.date<hi)].reset_index(drop=True) for s,df in frames.items()}
    events=sorted(((r.date,s,r) for s,df in sub.items() for r in df.itertuples(index=False)),key=lambda z:(z[0],z[1]))
    last_time=None
    for t,s,r in events:
        if last_time is not None and t!=last_time:
            equity=cash+(pos['qty']*last.get(pos['sym'],pos['entry'])*(1-FEE) if pos else 0.)
            peak=max(peak,equity); dd=max(dd,1-equity/peak)
            equity_curve.append({'date':str(last_time),'equity':round(equity,6)})
        last_time=t; last[s]=float(r.close)
        if pos and pos['sym']==s:
            age=int((t-pos['date']).total_seconds()/60)
            roi=ROI[max(k for k in ROI if k<=age)]
            entry=pos['entry']; stop=entry*(1+float(strategy.stoploss))
            if pos['trail_active']:
                stop=max(stop,pos['peak']*(1-float(strategy.trailing_stop_positive)))
            reason=None; price=None
            if r.open<=stop: price=float(r.open);reason='stop_gap'
            elif r.low<=stop: price=stop;reason='stop'
            elif r.open>=entry*(1+roi):price=float(r.open);reason='roi_gap'
            elif r.high>=entry*(1+roi):price=entry*(1+roi);reason='roi'
            elif bool(r.exit_next):price=float(r.open);reason='exit_signal'
            if reason is None:
                if r.high>=entry*(1+float(strategy.trailing_stop_positive_offset)):
                    pos['trail_active']=True
                pos['peak']=max(pos['peak'],float(r.high))
                # If activated in this candle, do not assume intrabar order.
                # If both activation and reversal are possible, conservative exit.
                if pos['trail_active']:
                    newstop=max(entry*(1+float(strategy.stoploss)),pos['peak']*(1-float(strategy.trailing_stop_positive)))
                    if r.low<=newstop:
                        price=min(float(r.open),newstop) if r.open<=newstop else newstop
                        reason='trailing_ambiguous_stop'
            if reason:
                proceeds=pos['qty']*price*(1-FEE);cash+=proceeds
                trades.append({'symbol':s,'entry_time':str(pos['date']),'exit_time':str(t),'entry_price':entry,'exit_price':price,'reason':reason,'net_pnl_usdt':round(proceeds-pos['cost'],6)})
                pos=None
        if pos is None and bool(r.entry_next) and cash>=STAKE*(1+FEE):
            price=float(r.open); cost=STAKE*(1+FEE);cash-=cost
            pos={'sym':s,'date':t,'entry':price,'qty':STAKE/price,'cost':cost,'peak':price,'trail_active':False}
    equity=cash+(pos['qty']*last[pos['sym']]*(1-FEE) if pos else 0.)
    peak=max(peak,equity);dd=max(dd,1-equity/peak)
    pnls=[x['net_pnl_usdt'] for x in trades]; gains=sum(x for x in pnls if x>0);losses=-sum(x for x in pnls if x<0)
    result={'window':label,'start':str(lo),'end_exclusive':str(hi),'initial_cash':START_CASH,'final_equity':round(equity,4),'return_pct':round((equity/START_CASH-1)*100,4),'max_drawdown_pct':round(dd*100,4),'closed_trades':len(trades),'win_rate_pct':round(100*sum(x>0 for x in pnls)/len(pnls),2) if pnls else None,'profit_factor':round(gains/losses,4) if losses else None,'open_position':pos['sym'] if pos else None}
    pd.DataFrame(trades,columns=['symbol','entry_time','exit_time','entry_price','exit_price','reason','net_pnl_usdt']).to_csv(OUT/f'{label}_trades.csv',index=False)
    pd.DataFrame(equity_curve).to_csv(OUT/f'{label}_equity.csv',index=False)
    print(json.dumps(result),flush=True)
    return result

results=[run(START,SPLIT,'development_120d'),run(SPLIT,END,'holdout_59d'),run(START,END,'full_period')]
(OUT/'summary.json').write_text(json.dumps({'results':results,'notes':['Independent simulator, NOT Freqtrade-equivalent','0.15% fee per side; no slippage','Next candle open entries','One position, 100 USDT stake','Trailing intrabar ambiguity handled conservatively','Holdout uses previously selected parameters; historical selection bias not ruled out','Period 2026-05-10 to 2026-11-06 is exactly 180 days']},indent=2))
print('SAVED',OUT,flush=True)
