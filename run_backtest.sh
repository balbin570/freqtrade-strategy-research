#!/bin/bash
set -e

freqtrade download-data \
  --config user_data/config.json \
  --timeframes 5m 15m \
  --timerange 20250101-20261001

freqtrade backtesting \
  --config user_data/config.json \
  --strategy-list Strategy001 Strategy002 Strategy003 \
  --timeframe 5m \
  --timerange 20260101-20261001 \
  --export trades
