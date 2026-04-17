#!/bin/bash
# VM1 → VM4 ML data sync (runs every 15 min via cron)
# Syncs append-only JSONL files so VM4 trainer always has fresh data.
# Safe: rsync only appends to existing files, never deletes.

set -euo pipefail

VM4="opc@10.0.2.4"
KEY="/home/opc/.ssh/id_vm4_sync"
SSH_OPTS="-i $KEY -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=10"
SRC="/home/opc/crypto-trading-bot/storage"
DST="/home/opc/crypto-trading-bot/storage"
LOG="/home/opc/crypto-trading-bot/logs/ml_sync.log"

TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

mkdir -p "$(dirname "$LOG")"

{
    echo "=== $TS sync start ==="

    # 1. ml_live_feedback.jsonl (live outcome labels, ~2.5MB, append-only)
    rsync -az -e "ssh $SSH_OPTS" "$SRC/ml_live_feedback.jsonl" "$VM4:$DST/ml_live_feedback.jsonl"
    echo "  feedback: OK ($(wc -l < "$SRC/ml_live_feedback.jsonl") lines)"

    # 2. training_data/trades.jsonl (candidate trainer dataset, ~45MB, append-only)
    rsync -az -e "ssh $SSH_OPTS" "$SRC/training_data/trades.jsonl" "$VM4:$DST/training_data/trades.jsonl"
    echo "  trades:   OK ($(wc -l < "$SRC/training_data/trades.jsonl") lines)"

    # 3. closed_signals.json (full trade archive, ~10MB)
    rsync -az -e "ssh $SSH_OPTS" "$SRC/closed_signals.json" "$VM4:$DST/closed_signals.json"
    echo "  closed:   OK ($(du -h "$SRC/closed_signals.json" | cut -f1))"

    # 4. scanner_weights.json (continuous per-scanner tuning state, ~2KB)
    #    Authoritative on VM1 — signal_tracker updates it as trades close + the
    #    hourly :37 cron recomputes it batch. VM4 Research Center displays it.
    if [ -f "$SRC/scanner_weights.json" ]; then
        rsync -az -e "ssh $SSH_OPTS" "$SRC/scanner_weights.json" "$VM4:$DST/scanner_weights.json"
        echo "  weights:  OK ($(du -h "$SRC/scanner_weights.json" | cut -f1))"
    fi

    echo "=== $TS sync done ==="
} >> "$LOG" 2>&1

# Keep log from growing unbounded (rotate at 1MB)
if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
    mv "$LOG" "${LOG}.1"
fi
