#!/usr/bin/env bash
# Run the bot on a Mac without letting the machine idle-sleep underneath it.
#
# Overnight 2026-09-10 the laptop entered sleep, the candle feed and the
# WebSocket died with it, and no signals fired for hours. `caffeinate -i`
# holds an idle-sleep assertion for as long as the bot process lives.
# (Closing the lid still sleeps the machine unless it is plugged in and
# "Prevent sleep when display is off" is enabled in System Settings.)
#
#   scripts/run_bot.sh            # paper mode (default)
#   scripts/run_bot.sh --mode backtest --symbols BTCUSDT

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [ -f .bot.pid ] && kill -0 "$(cat .bot.pid)" 2>/dev/null; then
  echo "Bot already running (pid $(cat .bot.pid)). Stop it first: pkill -TERM -f main.py"; exit 1
fi
if command -v caffeinate >/dev/null 2>&1; then
  exec caffeinate -i "$ROOT/.venv/bin/python" main.py "$@"
else
  exec "$ROOT/.venv/bin/python" main.py "$@"
fi
