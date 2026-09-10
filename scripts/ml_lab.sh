#!/usr/bin/env bash
# ML Lab helper — runs the ml_training dashboard/scoring server locally.
#
#   scripts/ml_lab.sh start          # serve models on $ML_LAB_PORT (default 8091)
#   scripts/ml_lab.sh train          # serve + run the full training pipeline
#   scripts/ml_lab.sh stop
#   scripts/ml_lab.sh status
#   scripts/ml_lab.sh logs           # tail the log
#
# The bot reaches it via ML_SERVER_URL in .env (default http://127.0.0.1:8091).
# Models are written to storage/ml_models/ and picked up by the server
# automatically (mtime-based reload) — no restart needed after training.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
PORT="${ML_LAB_PORT:-8091}"
LOG="$ROOT/logs/ml_lab.log"
PIDFILE="$ROOT/.ml_lab.pid"
mkdir -p "$ROOT/logs"

is_running() {
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

start() {
  local extra="${1:-}"
  if is_running; then
    echo "ML Lab already running (pid $(cat "$PIDFILE")) on port $PORT"; return 0
  fi
  # Stop any orphaned dashboard child holding the port.
  pkill -f "run_trainer.py.*--port $PORT" 2>/dev/null || true
  sleep 1
  cd "$ROOT"
  nohup "$PY" ml_training/run_trainer.py --port "$PORT" $extra >> "$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  for _ in $(seq 1 20); do
    curl -s -m 2 -o /dev/null "http://127.0.0.1:$PORT/api/health" && break
    sleep 1
  done
  echo "ML Lab started (pid $(cat "$PIDFILE")) → http://127.0.0.1:$PORT  log: $LOG"
}

stop() {
  if is_running; then kill "$(cat "$PIDFILE")" 2>/dev/null || true; fi
  pkill -f "run_trainer.py.*--port $PORT" 2>/dev/null || true
  rm -f "$PIDFILE"
  echo "ML Lab stopped"
}

status() {
  if is_running; then echo "running (pid $(cat "$PIDFILE"))"; else echo "not running (pidfile)"; fi
  curl -s -m 3 "http://127.0.0.1:$PORT/api/ml/health" \
    | "$PY" -c 'import sys,json; d=json.load(sys.stdin); print("health:", d.get("overall_health"), d.get("summary"))' \
    2>/dev/null || echo "port $PORT not answering"
  ls "$ROOT/storage/ml_models"/model_*.joblib 2>/dev/null | sed 's#.*/##' || true
}

case "${1:-status}" in
  start)  start ;;
  train)  start "--train" ;;
  stop)   stop ;;
  status) status ;;
  logs)   tail -f "$LOG" ;;
  *) echo "usage: $0 {start|train|stop|status|logs}"; exit 1 ;;
esac
