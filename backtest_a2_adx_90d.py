"""Read-only A2 ADX entry-filter comparison, 90d continuous and 60/30 diagnostics.
Imports the existing backtest's candle downloader and indicator calculations.
"""
import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from backtest_90d import fetch, indicators, FEE, STAKE, MAX_OPEN

OUT = Path('a2_adx_results')
OUT.mkdir(exist_ok=True)
LADDER = ((.01,.50),(.02,.25),(.03,.25))
MODELS = {
    'A2_NO_FILTER': None,
    'A2_ADX_15': 15.0,
    'A2_ADX_20': 20.0,
    'A2_ADX_25': 25.0,
}


def add_adx(d, period=14):
    """Wilder ADX, calculated from historical/current completed candles only."""
    d=d.copy()
    up=d.high.diff()
    down=-d.low.diff()
    plus_dm=up.where((up>down)&(up>0),0.0)
    minus_dm=down.where((down>up)&(down>0),0.0)
    prev_close=d.close.shift(1)
    tr=pd.concat([(d.high-d.low),(d.high-prev_close).abs(),(d.low-prev_close).abs()],axis=1).max(axis=1)
    atr=tr.ewm(alpha=1/period,adjust=False,min_periods=period).mean()
    plus_di=100*plus_dm.ewm(alpha=1/period,adjust=False,min_periods=period).mean()/atr.replace(0,float('nan'))
    minus_di=100*minus_dm.ewm(alpha=1/period,adjust=False,min_periods=period).mean()/atr.replace(0,float('nan'))
    dx=100*(plus_di-minus_di).abs()/(plus_di+minus_di).replace(0,float('nan'))
    d['adx14']=dx.ewm(alpha=1/period,adjust=False,min_periods=period).mean()
    return d

INITIAL_CASH = 1000.0


def simulate(frames, universe, adx_min):
    events = sorted(((int(r.time), s, r) for s in universe for r in frames[s].itertuples(index=False)), key=lambda x: (x[0],x[1]))
    cash, positions, trades, last = INITIAL_CASH, [], [], {}
    peak, max_dd, signals, rejected = INITIAL_CASH, 0.0, 0, 0
    prev = None

    def close_piece(p, fraction, price, reason, t):
        nonlocal cash
        qty = p['qty'] * fraction
        proceeds = qty * price * (1-FEE)
        entry_cost = p['cost'] * fraction
        pnl = proceeds - entry_cost
        cash += proceeds
        p['qty'] -= qty
        p['cost'] -= entry_cost
        trades.append(dict(symbol=p['symbol'],entry_time_utc=pd.to_datetime(p['time'],unit='ms',utc=True).isoformat(),exit_time_utc=pd.to_datetime(t,unit='ms',utc=True).isoformat(),exit_price=round(price,8),reason=reason,net_pnl_usdt=round(pnl,8),original_position_id=p['id']))

    next_id = 0
    for t,s,r in events:
        if prev is not None and t != prev:
            equity = cash + sum(p['qty']*last.get(p['symbol'],p['entry_price'])*(1-FEE) for p in positions)
            peak=max(peak,equity); max_dd=max(max_dd,(peak-equity)/peak)
        prev=t
        last[s]=float(r.close)
        for p in list(positions):
            if p['symbol'] != s: continue
            entry=p['entry_price']
            age=max(0,(t-p['time'])/60000)
            stop_price=max(entry*(1-.10),p['trail_stop'])
            # Gap first, then intrabar stop; conservative when stop and target both touched.
            if r.open<=stop_price or r.low<=stop_price:
                price=float(r.open) if r.open<=stop_price else stop_price
                close_piece(p,1.0,price,'stop_gap' if r.open<=stop_price else 'stop',t)
                positions.remove(p)
                continue
            # Sequential partials: 50% / 25% / 25% of ORIGINAL stake.
            for level,part in LADDER:
                if level in p['done']: continue
                target=entry*(1+level)
                if r.high>=target:
                    fill=max(float(r.open),target) if r.open>=target else target
                    fraction=min(1.0,part/p['remaining_original'])
                    close_piece(p,fraction,fill,f'partial_{int(level*100)}pct',t)
                    p['remaining_original']-=part
                    p['done'].add(level)
                    if p['qty'] < 1e-12 or p['remaining_original'] < 1e-9:
                        positions.remove(p)
                        break
            if p not in positions: continue
            if r.exit and r.close>entry:
                close_piece(p,1.0,float(r.close),'exit_signal',t)
                positions.remove(p)
                continue
        if r.entry:
            signals+=1
            if adx_min is not None and (pd.isna(r.adx14) or r.adx14 < adx_min):
                rejected+=1
                continue
            if len(positions)>=MAX_OPEN or cash<STAKE*(1+FEE):
                rejected+=1
            else:
                price=float(r.close)
                cash-=STAKE*(1+FEE)
                next_id+=1
                positions.append(dict(id=next_id,symbol=s,time=t,entry_price=price,qty=STAKE/price,cost=STAKE*(1+FEE),done=set(),remaining_original=1.0,trail_stop=0.0))
    equity=cash+sum(p['qty']*last[p['symbol']]*(1-FEE) for p in positions)
    peak=max(peak,equity); max_dd=max(max_dd,(peak-equity)/peak)
    pnls=[tr['net_pnl_usdt'] for tr in trades]
    gains=sum(x for x in pnls if x>0); losses=-sum(x for x in pnls if x<0)
    # Partial fills are not independent trades. Report completed POSITION count.
    completed_ids={x['original_position_id'] for x in trades}-{p['id'] for p in positions}
    return dict(signals=signals,rejected_entries=rejected,closed_positions=len(completed_ids),exit_fills=len(trades),open_positions=len(positions),realized_pnl_usdt=round(sum(pnls),4),final_equity_usdt=round(equity,4),total_return_pct=round((equity/INITIAL_CASH-1)*100,4),max_drawdown_pct=round(max_dd*100,4),profit_factor=round(gains/losses,4) if losses else None),trades


def main():
    # Fixed date shared by both windows. Daily archives may lag, so omit last 3 days.
    now=datetime.now(timezone.utc)
    end=(now-timedelta(days=3)).replace(hour=0,minute=0,second=0,microsecond=0)
    start=end-timedelta(days=90)
    split=start+timedelta(days=60)
    warmup=start-timedelta(days=7)
    print(f'90-day range: {start.isoformat()} -> {end.isoformat()}')
    frames={}
    for symbol in ('BTCUSDT','ETHUSDT','SOLUSDT'):
        d=add_adx(indicators(fetch(symbol,warmup,end)))
        frames[symbol]=d[d.time>=int(start.timestamp()*1000)].copy()
    results=[]
    for window,lo,hi in (('full_90d',start,end),('development_60d',start,split),('validation_30d',split,end)):
        sub={s:d[(d.time>=int(lo.timestamp()*1000))&(d.time<int(hi.timestamp()*1000))].copy() for s,d in frames.items()}
        for model,adx_min in MODELS.items():
            universe=('BTCUSDT','ETHUSDT','SOLUSDT')
            summary,trades=simulate(sub,universe,adx_min)
            summary={'window':window,'model':model,'coins':'+'.join(s.replace('USDT','') for s in universe),**summary}
            results.append(summary)
            pd.DataFrame(trades).to_csv(OUT/f'{window}_{model}_trades.csv',index=False)
            print(summary,flush=True)
    pd.DataFrame(results).to_csv(OUT/'summary.csv',index=False)
    (OUT/'summary.json').write_text(json.dumps({'start_utc':start.isoformat(),'split_utc':split.isoformat(),'end_utc':end.isoformat(),'adx_thresholds':MODELS,'stop_pct':10,'fee_per_side':FEE,'initial_cash':INITIAL_CASH,'stake':STAKE,'max_open':MAX_OPEN,'notes':['Full 90-day run is continuous; 60d and 30d are separate diagnostic runs with reset capital','All variants use A2 50/25/25 at +1/+2/+3, 10 percent stop, and differ only by ADX14 minimum on entry','All three models use BTC, ETH, SOL and 0.1 percent fee per side','Conservative stop-first assumption; no slippage, entries at signal candle close','Partial fills are not independent completed trades'],'results':results},indent=2),encoding='utf-8')
    print('\nRESULTS\n',pd.DataFrame(results).to_string(index=False),flush=True)

if __name__=='__main__': main()
