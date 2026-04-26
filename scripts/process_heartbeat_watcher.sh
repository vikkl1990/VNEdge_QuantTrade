#!/usr/bin/env bash
# Agent 16 NEW — Process Heartbeat Watcher (every 10min cron)
# 2026-04-26
#
# Each tracked process publishes a heartbeat (last activity timestamp).
# Alert if any heartbeat is older than 2× normal cadence.
#
# Tracked processes:
#   - cryptobot.service (main loop) — expects log activity within 5 min
#   - bybit-shadow-daemon.service — expects activity within 5 min
#   - bybit-shadow-monitor.service — expects activity within 5 min
#   - Mac demo dispatcher — expects an INSERT to user_trades(exchange='bybit', trade_type='demo')
#                           within 30 min IF there are matching delta_shadow opens
#
# Output: storage/heartbeat/heartbeat_TS.log + alert via stdout (cron mails)
# Cron: */10 * * * * /home/opc/crypto-trading-bot/scripts/process_heartbeat_watcher.sh

set -u
ROOT=/home/opc/crypto-trading-bot
OUT_DIR=$ROOT/storage/heartbeat
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
mkdir -p "$OUT_DIR"
ALERT_LOG=$OUT_DIR/alerts.log
STATUS_FILE=$OUT_DIR/status_latest.txt

ALERTS=()

heartbeat_check() {
    local svc="$1"
    local max_quiet_min="$2"
    local last_log=$(sudo journalctl -u "$svc" --since "${max_quiet_min} minutes ago" --no-pager 2>&1 | grep -v "^-- " | wc -l)
    if ! systemctl is-active --quiet "$svc"; then
        ALERTS+=("🔴 $svc NOT ACTIVE")
    elif [[ "${last_log:-0}" -lt 2 ]]; then
        ALERTS+=("🟡 $svc active but quiet (<2 log lines in ${max_quiet_min}min)")
    fi
}

heartbeat_check cryptobot 5
heartbeat_check bybit-shadow-daemon 5
heartbeat_check bybit-shadow-monitor 5

# ── Mac demo dispatcher health ──
# If we have delta_shadow opens in last 30 min but ZERO bybit_demo mirrors → dead
MAC_HEALTH=$(PGPASSWORD=VnEdge2026db psql -h localhost -U vnedge -d vnedge -A -F '|' -t -c "
WITH d AS (
  SELECT id FROM user_trades
  WHERE exchange='delta_india' AND trade_type='shadow'
    AND opened_at >= NOW() - INTERVAL '30 minutes'
),
bd AS (
  SELECT metadata::jsonb->>'mirror_of_delta_trade_id' AS parent
  FROM user_trades
  WHERE exchange='bybit' AND trade_type='demo'
    AND opened_at >= NOW() - INTERVAL '30 minutes'
)
SELECT COUNT(d.id), COUNT(bd.parent)
FROM d LEFT JOIN bd ON bd.parent = d.id::text;
" 2>/dev/null)

DELTA_OPENS=$(echo "$MAC_HEALTH" | cut -d'|' -f1)
DEMO_MIRRORS=$(echo "$MAC_HEALTH" | cut -d'|' -f2)
DELTA_OPENS=${DELTA_OPENS:-0}
DEMO_MIRRORS=${DEMO_MIRRORS:-0}

if [[ "${DELTA_OPENS:-0}" -gt 0 && "${DEMO_MIRRORS:-0}" -eq 0 ]]; then
    ALERTS+=("🔴 Mac demo dispatcher SILENT: ${DELTA_OPENS} delta opens / 0 demo mirrors in 30min")
fi

# ── In-memory open trades aging ──
STUCK=$(PGPASSWORD=VnEdge2026db psql -h localhost -U vnedge -d vnedge -A -F '|' -t -c "
SELECT COUNT(*) FROM user_trades
WHERE closed_at IS NULL
  AND opened_at < NOW() - INTERVAL '60 minutes';
" 2>/dev/null | tr -d '[:space:]')

STUCK=${STUCK:-0}
if [[ "${STUCK:-0}" -gt 0 ]]; then
    ALERTS+=("🔴 ${STUCK} trade(s) open >60min — exit guards likely broken")
fi

# Write status
{
    echo "[$TS] heartbeat check"
    echo "  cryptobot=$(systemctl is-active cryptobot)"
    echo "  bybit-shadow-daemon=$(systemctl is-active bybit-shadow-daemon)"
    echo "  bybit-shadow-monitor=$(systemctl is-active bybit-shadow-monitor)"
    echo "  delta_opens_30m=$DELTA_OPENS demo_mirrors_30m=$DEMO_MIRRORS"
    echo "  trades_stuck_60m=$STUCK"
    echo "  alerts=${#ALERTS[@]}"
} > "$STATUS_FILE"

if [[ ${#ALERTS[@]} -gt 0 ]]; then
    {
        echo "[$TS] HEARTBEAT alerts:"
        for a in "${ALERTS[@]}"; do echo "  - $a"; done
        echo ""
    } >> "$ALERT_LOG"
    # Also stdout for cron mail / journald
    for a in "${ALERTS[@]}"; do echo "$a"; done
    exit 1
fi

exit 0
