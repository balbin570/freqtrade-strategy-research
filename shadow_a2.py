"""Independent forward-only A2 paper portfolio. Writes ONLY bot_state id=2."""
import json
import uuid
import os
from contextlib import closing
from datetime import datetime, timezone
import psycopg2
from psycopg2.extras import Json
import paper_trader as bot

ROW_ID = 2
LOCK_ID = 630521942
LEVELS = ((0.01, 0.50), (0.02, 0.25), (0.03, 0.25))

def blank(cursors):
    return {'version':'A2_SHADOW_V1', 'paper_only':True, 'started_utc':datetime.now(timezone.utc).isoformat(),
            'cash':1000.0, 'positions':[], 'trades':[], 'realized_pnl':0.0,
            'signals':0, 'rejected_max_positions':0, 'rejected_insufficient_cash':0,
            'cursors':cursors, 'last_prices':{}, 'equity_peak':1000.0, 'max_drawdown_pct':0.0, 'telegram_outbox':[]}

def equity(s):
    return s['cash'] + sum(p['qty'] * s['last_prices'].get(p['symbol'],p['entry_price']) for p in s['positions'])

def queue_message(s, message):
    s.setdefault('telegram_outbox', []).append({
        'id': str(uuid.uuid4()), 'text': message
    })

def deliver_messages():
    # Messages are persisted before sending; failed deliveries can be retried.
    # As with most Telegram integrations, a crash after delivery but before
    # acknowledgement can produce a duplicate; event IDs are included.
    with closing(psycopg2.connect(os.environ['DATABASE_URL'], sslmode='require', connect_timeout=8)) as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID,))
            cur.execute('SELECT state_json FROM paper_trading.bot_state WHERE id=%s FOR UPDATE', (ROW_ID,))
            record = cur.fetchone()
            if not record:
                raise RuntimeError('A2 state missing during notification delivery')
            state = record[0]
            pending = state.get('telegram_outbox', [])
            if not pending:
                return
            remaining = []
            for item in pending:
                ok = bot.send_telegram_sync(item['text'] + "\\nID: " + item['id'][:8])
                if not ok:
                    remaining.append(item)
            state['telegram_outbox'] = remaining
            cur.execute('UPDATE paper_trading.bot_state SET state_json=%s, updated_at=NOW() WHERE id=%s',
                        (Json(state), ROW_ID))
        conn.commit()
    print(f'A2 Telegram: sent {len(pending)-len(remaining)}, pending {len(remaining)}', flush=True)

def sell(s,p,qty,price,reason,candle):
    qty=min(qty,p['qty'])
    if qty<=1e-12:return
    proceeds=qty*price*(1-bot.FEE)
    entry_cost=qty*p['entry_price']*(1+bot.FEE)
    pnl=proceeds-entry_cost
    s['cash']+=proceeds
    s['realized_pnl']+=pnl
    p['qty']-=qty
    s['trades'].append({'symbol':p['symbol'],'entry_time':p['entry_time'],'exit_time':candle['time_iso'],
                        'entry_price':p['entry_price'],'exit_price':price,'qty':qty,
                        'reason':reason,'net_pnl_usdt':round(pnl,8)})
    queue_message(s,
        f"🔴 Strategy001-A2 SANAL SATIŞ\\n"
        f"{p['symbol']} | Neden: {reason}\\n"
        f"Alış: {p['entry_price']:.8g} → Satış: {price:.8g}\\n"
        f"Satılan: {qty:.8g} | Net K/Z: {pnl:+.4f} USDT\\n"
        f"Toplam gerçekleşen: {s['realized_pnl']:+.4f} USDT\\n"
        f"Kalan miktar: {p['qty']:.8g}\\n"
        "SADECE SANAL İŞLEM — gerçek emir yok")
    if p['qty']<=1e-10:
        s['positions'].remove(p)

def process(s,symbol,candle,rows):
    if candle['time']<=s['cursors'][symbol]:return
    if candle['time']-s['cursors'][symbol]!=300000:
        raise RuntimeError(f'A2 candle gap {symbol}; refusing skip')
    df=bot.build_indicators(rows)
    i=len(df)-1
    if i<100:raise RuntimeError('A2 insufficient indicator warmup')
    s['last_prices'][symbol]=candle['close']
    for p in list(s['positions']):
        if p['symbol']!=symbol:continue
        stop=p['entry_price']*.9
        if candle['open']<=stop:
            sell(s,p,p['qty'],candle['open'],'stop_gap',candle)
            continue
        if candle['low']<=stop:
            sell(s,p,p['qty'],stop,'stoploss',candle)
            continue
        # Intrabar order cannot be known: stop is checked first; target levels fill ascending.
        for idx,(level,fraction) in enumerate(LEVELS):
            if idx in p['done']:continue
            target=p['entry_price']*(1+level)
            if candle['high']>=target:
                sell(s,p,p['initial_qty']*fraction,max(target,candle['open']),'tp_'+str(level),candle)
                p['done'].append(idx)
        if p in s['positions'] and bot.exit_signal('Strategy001',df,i) and candle['close']>p['entry_price']:
            sell(s,p,p['qty'],candle['close'],'exit_signal',candle)
    if bot.entry_signal('Strategy001',df,i):
        s['signals']+=1
        if len(s['positions'])>=bot.MAX_OPEN:s['rejected_max_positions']+=1
        elif s['cash']<bot.STAKE*(1+bot.FEE):s['rejected_insufficient_cash']+=1
        else:
            price=candle['close']; qty=bot.STAKE/price
            s['cash']-=bot.STAKE*(1+bot.FEE)
            s['positions'].append({'symbol':symbol,'entry_time':candle['time_iso'],
                                   'entry_time_ms':candle['time'],'entry_price':price,
                                   'qty':qty,'initial_qty':qty,'done':[]})
            queue_message(s,
                f"🟢 Strategy001-A2 SANAL ALIŞ\\n"
                f"{symbol}\\nFiyat: {price:.8g}\\n"
                f"Tutar: {bot.STAKE:.2f} USDT | Açık pozisyon: {len(s['positions'])}/{bot.MAX_OPEN}\\n"
                "Kâr alma: +%1 (%50), +%2 (%25), +%3 (%25)\\n"
                "Stop-loss: -%10\\n"
                "SADECE SANAL İŞLEM — gerçek emir yok")
    s['cursors'][symbol]=candle['time']
    eq=equity(s)
    s['equity_peak']=max(s['equity_peak'],eq)
    s['max_drawdown_pct']=max(s['max_drawdown_pct'],100*(1-eq/s['equity_peak']))

def main():
    if not os.getenv('DATABASE_URL'):raise RuntimeError('DATABASE_URL missing')
    with closing(psycopg2.connect(os.environ['DATABASE_URL'],sslmode='require',connect_timeout=8)) as conn:
        conn.autocommit=False
        with conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(%s)',(LOCK_ID,))
            cur.execute('SELECT state_json FROM paper_trading.bot_state WHERE id=1')
            r=cur.fetchone()
            if not r or not isinstance(r[0],dict):raise RuntimeError('Primary portfolio missing')
            primary=r[0]
            cursors={}
            for symbol in bot.SYMBOLS:
                t=primary.get('symbols',{}).get(symbol,{}).get('last_candle_ms')
                if not isinstance(t,int) or t<=0:raise RuntimeError(f'Missing primary cursor {symbol}')
                cursors[symbol]=t
            cur.execute('SELECT state_json FROM paper_trading.bot_state WHERE id=%s FOR UPDATE',(ROW_ID,))
            saved=cur.fetchone()
            if saved:
                s=saved[0]
                if not isinstance(s,dict) or s.get('version')!='A2_SHADOW_V1':raise RuntimeError('Unexpected shadow state; refusing overwrite')
            else:
                s=blank(cursors)
                cur.execute('INSERT INTO paper_trading.bot_state (id,state_json,updated_at) VALUES (%s,%s,NOW())', (ROW_ID,Json(s)))
                conn.commit()
                print('A2 initialized at primary cursors; no historical trades replayed')
                return
            queued=[]; history={}
            for symbol in bot.SYMBOLS:
                rows=bot.fetch_historical_candles(symbol,1000)
                cursor=s['cursors'][symbol]
                before=[r for r in rows if r['time']<=cursor]
                after=[r for r in rows if r['time']>cursor]
                if len(before)<120:raise RuntimeError(f'A2 history overlap insufficient: {symbol}')
                if after and after[0]['time']-cursor!=300000:raise RuntimeError(f'A2 gap: {symbol}')
                history[symbol]=before[-160:]
                queued.extend((r['time'],symbol,r) for r in after)
            for _,symbol,candle in sorted(queued):
                history[symbol].append(candle)
                history[symbol]=history[symbol][-160:]
                process(s,symbol,candle,history[symbol])
            cur.execute('UPDATE paper_trading.bot_state SET state_json=%s, updated_at=NOW() WHERE id=%s', (Json(s),ROW_ID))
            conn.commit()
            print(f'A2 processed {len(queued)} candles; equity={equity(s):.4f}; positions={len(s["positions"])}')
    deliver_messages()

if __name__=='__main__':main()
