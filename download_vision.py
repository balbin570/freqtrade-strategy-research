import urllib.request
import zipfile
import io
import csv

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

print("Candles downloaded:", len(rows))
print("First candle:", rows[0][:6])
print("Last candle:", rows[-1][:6])
print("DATA TEST COMPLETE")
