import urllib.request
import zipfile
import io
import csv
from datetime import datetime, timezone

symbol = "BTCUSDT"
timeframe = "5m"
month = "2026-09"

url = (
    f"https://data.binance.vision/data/spot/monthly/klines/"
    f"{symbol}/{timeframe}/{symbol}-{timeframe}-{month}.zip"
)

print("Downloading:", url)

with urllib.request.urlopen(url, timeout=60) as response:
    data = response.read()

with zipfile.ZipFile(io.BytesIO(data)) as archive:
    filename = archive.namelist()[0]
    with archive.open(filename) as file:
        rows = list(csv.reader(io.TextIOWrapper(file)))

# Binance Vision microsecond timestamps -> milliseconds
for row in rows:
    timestamp = int(row[0])
    if timestamp >= 10**15:
        timestamp //= 1000
    row[0] = timestamp

first_date = datetime.fromtimestamp(rows[0][0] / 1000, tz=timezone.utc)
last_date = datetime.fromtimestamp(rows[-1][0] / 1000, tz=timezone.utc)

print("Candles downloaded:", len(rows))
print("First candle UTC:", first_date.isoformat())
print("Last candle UTC:", last_date.isoformat())
print("TIMESTAMP CONVERSION: OK")
