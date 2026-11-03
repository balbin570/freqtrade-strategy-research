"""One-shot Binance Vision paper trader for GitHub Actions; never places orders."""
import asyncio
import os
import sys
import time
import psycopg2
import paper_trader as bot

LOCK_KEY = 630521941

async def run():
    if not os.getenv('DATABASE_URL'):
        raise RuntimeError('DATABASE_URL is required')
    # Session-level lock, held until all writes are complete; protects overlapping jobs.
    with psycopg2.connect(os.environ['DATABASE_URL'], sslmode='require', connect_timeout=8) as guard:
        guard.autocommit = True
        with guard.cursor() as cur:
            cur.execute('SELECT pg_try_advisory_lock(%s)', (LOCK_KEY,))
            if not cur.fetchone()[0]:
                print('Another scheduled run is active; skipping.')
                return
        try:
            await asyncio.to_thread(bot.load_state)
            # Existing state is authoritative. Never initialize/replay blindly.
            cursors = {}
            for symbol in bot.SYMBOLS:
                cursor = bot.state['symbols'].get(symbol, {}).get('last_candle_ms')
                if not isinstance(cursor, int) or cursor <= 0:
                    raise RuntimeError(f'Missing persisted cursor for {symbol}; refusing to replay historical trades')
                cursors[symbol] = cursor
            # Fetch last 1000 completed candles (~83 hours); fail closed if the gap exceeds it.
            # Warm up indicators from at least 120 candles before the persisted cursor.
            queued = []
            for symbol in bot.SYMBOLS:
                rows = await asyncio.to_thread(bot.fetch_historical_candles, symbol, 1000)
                cursor = cursors[symbol]
                preceding = [r for r in rows if r['time'] <= cursor]
                upcoming = [r for r in rows if r['time'] > cursor]
                if not preceding or len(preceding) < 120:
                    raise RuntimeError(f'Insufficient historical overlap for {symbol}; manual recovery needed')
                if upcoming and upcoming[0]['time'] - cursor != 300000:
                    raise RuntimeError(f'Gap in {symbol} candles; refusing to skip trades')
                bot.candles[symbol] = preceding[-160:]
                print(f'{symbol}: processing {len(upcoming)} completed candles', flush=True)
                queued.extend((candle['time'], symbol, candle) for candle in upcoming)
            # Merge across symbols in candle-time order to preserve cash/max-position semantics.
            for _, symbol, candle in sorted(queued):
                await bot.process_closed_candle(symbol, candle)
            # Wait for notifications scheduled by original engine to complete.
            current = asyncio.current_task()
            pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
            if pending:
                await asyncio.wait(pending, timeout=25)
            print('Scheduled paper run finished; Supabase saved.', flush=True)
        finally:
            with guard.cursor() as cur:
                cur.execute('SELECT pg_advisory_unlock(%s)', (LOCK_KEY,))

if __name__ == '__main__':
    try:
        asyncio.run(run())
    except Exception as exc:
        print(f'FAILED: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        sys.exit(1)
