import urllib.request
import zipfile
import io
import csv
import json
from pathlib import Path

symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
timeframes = ["5m", "15m"]
months = ["2026-08", "2026-09"]

output_dir = Path("user_data/data/binance")
output_dir.mkdir(parents=True, exist_ok=True)

for symbol in symbols:
    for timeframe in timeframes:
        candles = []

        for month in months:
            url = (
                "https://data.binance.vision/data/spot/monthly/klines/"
                f"{symbol}/{timeframe}/{symbol}-{timeframe}-{month}.zip"
            )

            print("Downloading:", url)

            with urllib.request.urlopen(url, timeout=60) as response:
                archive_bytes = response.read()

            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                with archive.open(archive.namelist()[0]) as file:
                    rows = csv.reader(io.TextIOWrapper(file))

                    for row in rows:
                        timestamp = int(row[0])

                        if timestamp >= 10**15:
                            timestamp //= 1000

                        candles.append([
                            timestamp,
                            float(row[1]),
                            float(row[2]),
                            float(row[3]),
                            float(row[4]),
                            float(row[5])
                        ])

        candles.sort(key=lambda candle: candle[0])

        # Remove duplicate timestamps.
        unique = {candle[0]: candle for candle in candles}
        candles = [unique[key] for key in sorted(unique)]

        filename = (
            output_dir / f"{symbol[:-4]}_USDT-{timeframe}.json"
        )

        with open(filename, "w", encoding="utf-8") as file:
            json.dump(candles, file, separators=(",", ":"))

        print(
            "SAVED:",
            filename,
            "CANDLES:",
            len(candles)
        )

print("ALL DATA CONVERTED")
