#!/bin/bash
set -e

echo "BINANCE VISION CONNECTION TEST"

python - <<'PY'
import urllib.request

url = (
    "https://data.binance.vision/data/spot/monthly/klines/"
    "BTCUSDT/5m/BTCUSDT-5m-2026-09.zip"
)

try:
    with urllib.request.urlopen(url, timeout=30) as response:
        print("HTTP STATUS:", response.status)
        print("CONTENT TYPE:", response.headers.get("Content-Type"))
        print("BINANCE VISION ACCESS: OK")
except Exception as exc:
    print("BINANCE VISION ACCESS: FAILED")
    print(str(exc))
    raise
PY
