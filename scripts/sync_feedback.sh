#!/bin/bash
# Sync ml_live_feedback.jsonl from VM1 (bot) to VM2 (ML dashboard)
# Run via cron every 10 minutes on Mac

SSH_KEY="$HOME/.ssh/cryptobot_oci"
VM1="opc@150.230.171.48"
VM2="opc@158.101.112.94"
TMP="/tmp/ml_live_feedback.jsonl"
REMOTE_PATH="/home/opc/crypto-trading-bot/storage/ml_live_feedback.jsonl"

# Pull from VM1
scp -i "$SSH_KEY" -o ConnectTimeout=10 -o StrictHostKeyChecking=no "$VM1:$REMOTE_PATH" "$TMP" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "$(date): Failed to pull from VM1" >> /tmp/sync_feedback.log
    exit 1
fi

# Push to VM2
scp -i "$SSH_KEY" -o ConnectTimeout=10 -o StrictHostKeyChecking=no "$TMP" "$VM2:$REMOTE_PATH" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "$(date): Failed to push to VM2" >> /tmp/sync_feedback.log
    exit 1
fi

LINES=$(wc -l < "$TMP")
echo "$(date): Synced $LINES records VM1→VM2" >> /tmp/sync_feedback.log
rm -f "$TMP"
