#!/bin/bash
# Comprehensive backup of all critical state files
# Run: hourly via cron, before restarts, and on-demand
# Keeps last 48 hourly backups + last 7 daily snapshots

STORAGE="/home/opc/crypto-trading-bot/storage"
HOURLY_DIR="$STORAGE/backups/hourly"
DAILY_DIR="$STORAGE/backups/daily"
TS=$(date +%Y%m%d_%H%M%S)
BACKUP="$HOURLY_DIR/$TS"

mkdir -p "$BACKUP" "$DAILY_DIR"

# Critical state files (JSON)
for f in active_signals.json closed_signals.json signal_stats.json \
         real_trading_state.json scanner_weights.json signal_learner.json \
         trade_monitor.json signals_history.json; do
    [ -f "$STORAGE/$f" ] && cp "$STORAGE/$f" "$BACKUP/$f"
done

# Append-only logs (JSONL) — these are the source of truth
for f in ml_live_feedback.jsonl real_trade_feedback.jsonl audit_trail.jsonl; do
    [ -f "$STORAGE/$f" ] && cp "$STORAGE/$f" "$BACKUP/$f"
done

# ML models (only if they exist and are recent)
ML_DIR="$STORAGE/ml_models"
if [ -d "$ML_DIR" ]; then
    mkdir -p "$BACKUP/ml_models"
    for f in "$ML_DIR"/model_*.joblib; do
        [ -f "$f" ] && cp "$f" "$BACKUP/ml_models/"
    done
    for f in "$ML_DIR"/candidate_*.json; do
        [ -f "$f" ] && cp "$f" "$BACKUP/ml_models/"
    done
fi

# Config snapshot
[ -f "/home/opc/crypto-trading-bot/config/settings.yaml" ] && \
    cp "/home/opc/crypto-trading-bot/config/settings.yaml" "$BACKUP/"
[ -f "/home/opc/crypto-trading-bot/.env" ] && \
    cp "/home/opc/crypto-trading-bot/.env" "$BACKUP/.env"

# Count files backed up
FILE_COUNT=$(find "$BACKUP" -type f | wc -l)
TOTAL_SIZE=$(du -sh "$BACKUP" | cut -f1)

# Daily snapshot: copy latest hourly to daily (once per day at midnight)
HOUR=$(date +%H)
if [ "$HOUR" = "00" ]; then
    DAILY="$DAILY_DIR/$(date +%Y%m%d)"
    [ ! -d "$DAILY" ] && cp -r "$BACKUP" "$DAILY"
fi

# Rotate: keep last 48 hourly backups
cd "$HOURLY_DIR" 2>/dev/null
ls -dt */ 2>/dev/null | tail -n +49 | xargs rm -rf 2>/dev/null

# Rotate: keep last 7 daily snapshots
cd "$DAILY_DIR" 2>/dev/null
ls -dt */ 2>/dev/null | tail -n +8 | xargs rm -rf 2>/dev/null

echo "$(date): Backed up $FILE_COUNT files ($TOTAL_SIZE) to $BACKUP"
