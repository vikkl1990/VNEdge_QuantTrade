#!/usr/bin/env bash
# ML Lab helper — the local ML serving dashboard + the auto-retrainer.
#
#   scripts/ml_lab.sh start          # serve models on $ML_LAB_PORT (default 8091) + start auto-retrainer
#   scripts/ml_lab.sh train          # run the full training pipeline now (training-only subprocess)
#   scripts/ml_lab.sh stop
#   scripts/ml_lab.sh status
#   scripts/ml_lab.sh logs           # tail the serving log
#   scripts/ml_lab.sh retrain-logs   # tail the auto-retrainer log
#
# Processes (each with its own pidfile):
#   .ml_lab.pid        ml_training/run_trainer.py --port $PORT      (serving dashboard)
#   .ml_retrain.pid    ml_training/auto_retrainer.py                (polls live calibration,
#                       spawns `run_trainer --train --no-dashboard` when a family DRIFTs)
#
# The bot reaches the server via ML_SERVER_URL in .env (default http://127.0.0.1:8091).
# Models are written to storage/ml_models/ and picked up by the server
# automatically (mtime-based reload) — no restart needed after training.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
PORT="${ML_LAB_PORT:-8091}"
LOG="$ROOT/logs/ml_lab.log"
RLOG="$ROOT/logs/ml_retrain.log"
PIDFILE="$ROOT/.ml_lab.pid"
RPIDFILE="$ROOT/.ml_retrain.pid"
RETRAIN_INTERVAL="${ML_RETRAIN_CHECK_SEC:-3600}"
RETRAIN_MIN_N="${ML_RETRAIN_MIN_N:-50}"
mkdir -p "$ROOT/logs"

alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

port_owner() { lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1 || true; }

kill_port_orphans() {
  # A previous run's dashboard child (multiprocessing spawn) can outlive its
  # parent and keep the port; the next trainer's dashboard then fails to bind
  # and the UI silently talks to the orphan. Kill whatever owns the port.
  local pid
  pid="$(port_owner)"
  if [ -n "$pid" ]; then
    echo "port $PORT held by pid $pid — stopping it"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 10); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -9 "$pid" 2>/dev/null || true
  fi
  pkill -f "run_trainer.py --port $PORT" 2>/dev/null || true
}

start_server() {
  # The port is owned by the trainer's dashboard child, not the pidfile pid,
  # so "running" = pidfile alive AND the port answers.
  if alive "$PIDFILE" && curl -s -m 2 -o /dev/null "http://127.0.0.1:$PORT/api/health"; then
    echo "ML Lab already running (pid $(cat "$PIDFILE"), port owner $(port_owner)) on port $PORT"; return 0
  fi
  # pidfile stale or port owned by someone else: clean slate
  alive "$PIDFILE" && kill "$(cat "$PIDFILE")" 2>/dev/null || true
  rm -f "$PIDFILE"
  kill_port_orphans
  sleep 1
  cd "$ROOT"
  nohup "$PY" ml_training/run_trainer.py --port "$PORT" >> "$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  for _ in $(seq 1 30); do
    curl -s -m 2 -o /dev/null "http://127.0.0.1:$PORT/api/health" && break
    sleep 1
  done
  echo "ML Lab serving (pid $(cat "$PIDFILE")) → http://127.0.0.1:$PORT  log: $LOG"
}

start_retrainer() {
  if alive "$RPIDFILE"; then
    echo "auto-retrainer already running (pid $(cat "$RPIDFILE"))"; return 0
  fi
  cd "$ROOT"
  nohup "$PY" -m ml_training.auto_retrainer \
      --dashboard-url "http://127.0.0.1:$PORT" \
      --check-interval "$RETRAIN_INTERVAL" \
      --min-n "$RETRAIN_MIN_N" >> "$RLOG" 2>&1 &
  echo $! > "$RPIDFILE"
  echo "auto-retrainer started (pid $(cat "$RPIDFILE")), every ${RETRAIN_INTERVAL}s, min_n=$RETRAIN_MIN_N  log: $RLOG"
}

train_now() {
  cd "$ROOT"
  echo "training pipeline started (training-only process, serving dashboard untouched) → $RLOG"
  nohup "$PY" ml_training/run_trainer.py --train --no-dashboard --port "$PORT" \
      --timeframes 5m,15m,1h,4h >> "$RLOG" 2>&1 &
  echo "pid $!"
}

stop() {
  alive "$RPIDFILE" && kill "$(cat "$RPIDFILE")" 2>/dev/null || true
  alive "$PIDFILE" && kill "$(cat "$PIDFILE")" 2>/dev/null || true
  sleep 1
  kill_port_orphans
  rm -f "$PIDFILE" "$RPIDFILE"
  echo "ML Lab stopped"
}

status() {
  if alive "$PIDFILE"; then echo "server: running (pid $(cat "$PIDFILE"), port owner $(port_owner))"; else echo "server: not running"; fi
  if alive "$RPIDFILE"; then echo "auto-retrainer: running (pid $(cat "$RPIDFILE"))"; else echo "auto-retrainer: not running"; fi
  curl -s -m 3 "http://127.0.0.1:$PORT/api/ml/health" \
    | "$PY" -c 'import sys,json; d=json.load(sys.stdin); print("health:", d.get("overall_health"), d.get("summary"))' \
    2>/dev/null || echo "port $PORT not answering"
  ls "$ROOT/storage/ml_models"/model_*.joblib 2>/dev/null | sed 's#.*/##' | head -40 || true
}

case "${1:-status}" in
  start)  start_server; start_retrainer ;;
  train)  start_server; train_now ;;
  stop)   stop ;;
  status) status ;;
  logs)   tail -f "$LOG" ;;
  retrain-logs) tail -f "$RLOG" ;;
  *) echo "usage: $0 {start|train|stop|status|logs|retrain-logs}"; exit 1 ;;
esac
