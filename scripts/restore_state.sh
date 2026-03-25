#!/bin/bash
# Restore state from a backup
# Usage: ./restore_state.sh [backup_dir]
# If no dir specified, shows available backups

STORAGE="/home/opc/crypto-trading-bot/storage"
HOURLY_DIR="$STORAGE/backups/hourly"
DAILY_DIR="$STORAGE/backups/daily"

if [ -z "$1" ]; then
    echo "=== AVAILABLE BACKUPS ==="
    echo
    echo "HOURLY (last 48):"
    ls -dt "$HOURLY_DIR"/*/ 2>/dev/null | head -10 | while read d; do
        count=$(find "$d" -type f | wc -l)
        size=$(du -sh "$d" | cut -f1)
        echo "  $(basename $d)  ($count files, $size)"
    done
    echo
    echo "DAILY (last 7):"
    ls -dt "$DAILY_DIR"/*/ 2>/dev/null | while read d; do
        count=$(find "$d" -type f | wc -l)
        size=$(du -sh "$d" | cut -f1)
        echo "  $(basename $d)  ($count files, $size)"
    done
    echo
    echo "Usage: $0 <backup_dir_name>"
    echo "Example: $0 hourly/20260325_140000"
    exit 0
fi

RESTORE="$STORAGE/backups/$1"
if [ ! -d "$RESTORE" ]; then
    echo "ERROR: Backup directory not found: $RESTORE"
    exit 1
fi

# Safety: backup current state before restoring
SAFETY="$STORAGE/backups/pre_restore_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$SAFETY"
for f in "$STORAGE"/*.json "$STORAGE"/*.jsonl; do
    [ -f "$f" ] && cp "$f" "$SAFETY/"
done
echo "Current state backed up to: $SAFETY"

# Stop bot
echo "Stopping bot..."
pkill -f main.py 2>/dev/null
sleep 3

# Restore state files
for f in "$RESTORE"/*.json "$RESTORE"/*.jsonl "$RESTORE"/*.yaml; do
    [ -f "$f" ] && cp "$f" "$STORAGE/$(basename $f)"
done

# Restore ML models if present
if [ -d "$RESTORE/ml_models" ]; then
    for f in "$RESTORE/ml_models"/*; do
        [ -f "$f" ] && cp "$f" "$STORAGE/ml_models/$(basename $f)"
    done
    echo "ML models restored"
fi

echo "Restored from: $RESTORE"
echo "Files restored: $(find $RESTORE -type f | wc -l)"
echo
echo "Start bot with: cd /home/opc/crypto-trading-bot && python3 main.py"
