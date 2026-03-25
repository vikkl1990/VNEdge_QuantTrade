#!/bin/bash
# Auto-retrain ML models daily — HYBRID SAFE
# 1. Sync feedback from VM1 to VM2
# 2. Run training pipeline on VM2
# 3. DO NOT auto-deploy — training results saved for comparison
# 4. Deploy only if new AUC > current hybrid AUC (manual review)

SSH_KEY="$HOME/.ssh/cryptobot_oci"
VM1="opc@150.230.171.48"
VM2="opc@158.101.112.94"
TMP="/tmp/ml_live_feedback.jsonl"
REMOTE_PATH="/home/opc/crypto-trading-bot/storage/ml_live_feedback.jsonl"
LOG="/tmp/auto_retrain.log"

echo "$(date): Starting auto-retrain (hybrid-safe)" >> "$LOG"

# Step 1: Sync feedback VM1 → VM2
scp -i "$SSH_KEY" -o ConnectTimeout=10 -o StrictHostKeyChecking=no "$VM1:$REMOTE_PATH" "$TMP" 2>/dev/null
if [ $? -ne 0 ]; then
    echo "$(date): Failed to pull feedback from VM1" >> "$LOG"
    exit 1
fi
scp -i "$SSH_KEY" -o ConnectTimeout=10 -o StrictHostKeyChecking=no "$TMP" "$VM2:$REMOTE_PATH" 2>/dev/null
LINES=$(wc -l < "$TMP")
echo "$(date): Synced $LINES feedback records" >> "$LOG"
rm -f "$TMP"

# Step 2: Kill any existing training
ssh -i "$SSH_KEY" -o ConnectTimeout=10 -o StrictHostKeyChecking=no "$VM2" "pkill -f run_trainer" 2>/dev/null
sleep 5

# Step 3: Start training (results saved to storage/ml_models/ but NOT auto-deployed to VM1)
ssh -i "$SSH_KEY" -o ConnectTimeout=10 -o StrictHostKeyChecking=no "$VM2" "cd /home/opc/crypto-trading-bot && python3 -m ml_training.run_trainer --train --symbols BTC/USDT,ETH/USDT,SOL/USDT --timeframes 1m,5m,15m --port 8082 >ml_training.log 2>&1 &"

echo "$(date): Training started on VM2 (hybrid-safe: no auto-deploy)" >> "$LOG"
