#!/usr/bin/env bash
# scripts/agent_watchdog.sh
# Phase 6.C / Agent change #4 (2026-04-25)
#
# Catches when an agent itself silently fails (didn't produce its expected
# deliverable on schedule). Runs every 30 min via cron.
#
# Usage: */30 * * * * /home/opc/crypto-trading-bot/scripts/agent_watchdog.sh

set -u
ROOT=/home/opc/crypto-trading-bot
ALERT_LOG=$ROOT/storage/watchdog.log
ROLLBACK_DIR=$ROOT/.rollback
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

mkdir -p "$(dirname "$ALERT_LOG")"

# Each entry: <filename_pattern>|<window_in_minutes>
EXPECTED_AGENTS=(
  "silent_failure_scan|360"     # Agent 5 heavy run — every 6h
  "data_integrity|1560"          # Agent 8 — daily + buffer
  "exec_quality|1560"            # Agent 2 — daily + buffer
  "ml_pipeline|10800"            # Agent 11 — weekly + buffer
)

ALERTS=()

for entry in "${EXPECTED_AGENTS[@]}"; do
    pattern="${entry%|*}"
    window_min="${entry#*|}"

    # Find newest matching file within window
    latest=$(find "$ROLLBACK_DIR" -type f -name "*${pattern}*" -mmin -"$window_min" 2>/dev/null | head -1)

    if [[ -z "$latest" ]]; then
        ALERTS+=("MISSING: '$pattern' has not delivered in last ${window_min}min")
    fi
done

# Risk Monitor logs to a different location
RISK_LOG="$ROLLBACK_DIR/risk_check.log"
if [[ -f "$RISK_LOG" ]]; then
    last_mod=$(stat -c '%Y' "$RISK_LOG" 2>/dev/null || stat -f '%m' "$RISK_LOG" 2>/dev/null || echo 0)
    age_sec=$(( $(date +%s) - last_mod ))
    if [[ $age_sec -gt 900 ]]; then  # 15 min
        ALERTS+=("STALE: Risk Monitor log (Agent 6) hasn't updated in $((age_sec / 60))min")
    fi
else
    ALERTS+=("MISSING: Risk Monitor log (Agent 6) doesn't exist at $RISK_LOG")
fi

# Lightweight SFH alerts file — surface any
SFH_ALERTS="$ROOT/storage/sfh_alerts.log"
if [[ -f "$SFH_ALERTS" ]]; then
    recent_alerts=$(find "$SFH_ALERTS" -mmin -60 -type f 2>/dev/null)
    if [[ -n "$recent_alerts" ]]; then
        # 2026-04-26 fix: grep -c (no match) outputs "0" + nonzero exit
        # so `|| echo 0` appends "\n0" → $n_alerts becomes "0\n0" → bash
        # `[[ -gt ]]` chokes with "syntax error in expression". Take last line.
        n_alerts=$(grep -c "ALERT:" "$SFH_ALERTS" 2>/dev/null | tail -1)
        n_alerts=${n_alerts:-0}
        if [[ "${n_alerts:-0}" -gt 0 ]]; then
            ALERTS+=("INFO: $n_alerts SFH lightweight alerts in last hour — see $SFH_ALERTS")
        fi
    fi
fi

# Bot heartbeat (independent check)
last_heartbeat=$(systemctl status cryptobot 2>/dev/null | grep -oE 'Active:.*' | head -1 || echo "UNKNOWN")
if ! systemctl is-active --quiet cryptobot; then
    ALERTS+=("CRITICAL: cryptobot service is NOT active")
fi

# Write summary
if [[ ${#ALERTS[@]} -gt 0 ]]; then
    {
        echo "[$TS] WATCHDOG — ${#ALERTS[@]} alert(s):"
        for a in "${ALERTS[@]}"; do echo "  - $a"; done
        echo "  bot status: $last_heartbeat"
        echo ""
    } >> "$ALERT_LOG"
fi

# Optional: write status to bot_state for dashboard display (if table exists)
PGPASSWORD=VnEdge2026db psql -h localhost -U vnedge -d vnedge -t -A -c \
    "INSERT INTO bot_state (key, value, updated_at)
     VALUES ('watchdog_last_run', '${#ALERTS[@]}', NOW())
     ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();" \
    2>/dev/null || true  # best-effort; OK if bot_state schema differs

exit 0
