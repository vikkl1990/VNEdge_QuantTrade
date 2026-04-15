#!/bin/bash
# VN Edge: Sync ML models VM4 → VM1 every 15 min
# Cron: */15 * * * * /home/opc/crypto-trading-bot/scripts/sync_ml_models.sh

set -e
VM4_HOST="opc@10.0.2.4"
SSH_KEY="$HOME/.ssh/cryptobot_oci"
LOCAL_DIR="/home/opc/crypto-trading-bot/storage/ml_models"

mkdir -p "$LOCAL_DIR"

rsync -az --delete -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no -o ConnectTimeout=10" \
    "$VM4_HOST:/home/opc/crypto-trading-bot/storage/ml_models/" \
    "$LOCAL_DIR/" 2>&1 | tail -5

echo "ML model sync OK at $(date)"
