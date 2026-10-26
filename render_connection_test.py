import asyncio
import json
import os
from datetime import datetime, timezone
from fastapi import FastAPI
import websockets

app = FastAPI(title='Freqtrade Paper Connectivity Check')

@app.get('/')
def root():
    return {'status': 'TEST_ONLY', 'paper_only': True, 'orders': False, 'utc': datetime.now(timezone.utc).isoformat(), 'next': '/check'}

@app.get('/check')
async def check():
    url = 'wss://data-stream.binance.vision/ws/btcusdt@trade'
    try:
        async with websockets.connect(url, open_timeout=10, close_timeout=3) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            event = json.loads(raw)
            return {'status': 'CONNECTED', 'source': 'Binance Vision WebSocket', 'symbol': event.get('s'), 'price': event.get('p'), 'event_time_ms': event.get('E'), 'paper_only': True, 'orders': False}
    except Exception as exc:
        return {'status': 'UNAVAILABLE', 'error_type': type(exc).__name__, 'message': str(exc)[:250], 'paper_only': True, 'orders': False}

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=int(os.getenv('PORT', '10000')))
