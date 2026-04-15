#!/bin/bash
# VN Edge: Trigger ML model retrain on VM4
# Cron: 0 4 * * 1 (Monday 4am UTC = weekly)

set -e
VM4_HOST="opc@10.0.2.4"
SSH_KEY="$HOME/.ssh/cryptobot_oci"

echo "[$(date)] Triggering ML retrain on VM4..."
ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=15 "$VM4_HOST" \
    "cd /home/opc/crypto-trading-bot && python3 ml_training/run_trainer.py --auto" 2>&1 | tail -20

# After training, sync new models to VM1
sleep 5
$(dirname "$0")/sync_ml_models.sh
echo "[$(date)] Retrain + sync complete"
