#!/bin/bash
# Sync ml_live_feedback.jsonl from VM1 (bot) to VM2 (ML dashboard)
# Run this periodically or via cron: */5 * * * * /path/to/sync_feedback.sh

KEY="$HOME/.ssh/cryptobot_oci"
VM1="opc@150.230.171.48"
VM2="opc@158.101.112.94"
FILE="crypto-trading-bot/storage/ml_live_feedback.jsonl"

ssh -i "$KEY" "$VM1" "cat /home/opc/$FILE" 2>/dev/null | ssh -i "$KEY" "$VM2" "cat > /home/opc/$FILE" 2>/dev/null
