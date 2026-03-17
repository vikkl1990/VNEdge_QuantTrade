#!/bin/bash
# OCI A1.Flex VM Auto-Provisioning Script
# Retries creating a better VM until capacity is available
# Usage: ./upgrade_vm.sh [--background]
#
# Free tier A1.Flex: up to 4 OCPU / 24GB RAM (we request 2 OCPU / 4GB)

set -euo pipefail

# ── Configuration ──
COMPARTMENT_ID="ocid1.tenancy.oc1..aaaaaaaahbhugoyewrb3g4f2att5onpbakauseaqgwuhotwmlxy3ylp3eqhq"
SUBNET_ID="ocid1.subnet.oc1.iad.aaaaaaaazo5yibmcg2wef5dq64rpvc6olkgvtvt4r2fhq5g3urykyuxkvcda"
IMAGE_ID="ocid1.image.oc1.iad.aaaaaaaaxfcokbqtyp2of4lk43vb2uhf3ok3idgmvsfnvpotqd5ppg3nuwpa"
SSH_KEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFYd/TN06nPBncuguAilb0zrVRA6F7ftdxSCg0a7aXpl cryptobot-oci"
OLD_INSTANCE_ID="ocid1.instance.oc1.iad.anuwcljtcwcb34qc5hg4u4j3gj6q4spcgpwckwq5u5ey6v2s3hwzsj5ioogq"
OLD_IP="150.136.153.181"
SSH_KEY_FILE="$HOME/.ssh/cryptobot_oci"

DISPLAY_NAME="cryptobot-vm-a1"
SHAPE="VM.Standard.A1.Flex"
OCPUS=2
MEMORY_GB=4

# Availability domains to try (rotate through all 3)
ADS=("Bvlw:US-ASHBURN-AD-1" "Bvlw:US-ASHBURN-AD-2" "Bvlw:US-ASHBURN-AD-3")

# Retry settings
RETRY_INTERVAL=300  # 5 minutes between attempts
MAX_ATTEMPTS=288    # 24 hours of retrying (288 * 5min = 24h)
LOG_FILE="$HOME/vm_upgrade.log"
STATUS_FILE="$HOME/vm_upgrade_status.json"

# ── Functions ──

log() {
    local msg="$(date '+%Y-%m-%d %H:%M:%S') | $1"
    echo "$msg" | tee -a "$LOG_FILE"
}

update_status() {
    local status="$1"
    local detail="${2:-}"
    local new_instance_id="${3:-}"
    local new_ip="${4:-}"

    cat > "$STATUS_FILE" <<STATUSEOF
{
    "status": "$status",
    "detail": "$detail",
    "attempts": $ATTEMPT,
    "max_attempts": $MAX_ATTEMPTS,
    "last_attempt": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')",
    "started_at": "$START_TIME",
    "target_shape": "$SHAPE",
    "target_ocpus": $OCPUS,
    "target_memory_gb": $MEMORY_GB,
    "old_instance_id": "$OLD_INSTANCE_ID",
    "old_ip": "$OLD_IP",
    "new_instance_id": "$new_instance_id",
    "new_ip": "$new_ip",
    "retry_interval_sec": $RETRY_INTERVAL
}
STATUSEOF
}

try_launch() {
    local ad="$1"
    log "Attempting launch in $ad ($OCPUS OCPU / ${MEMORY_GB}GB RAM)..."

    local result
    result=$(oci compute instance launch \
        --compartment-id "$COMPARTMENT_ID" \
        --availability-domain "$ad" \
        --shape "$SHAPE" \
        --shape-config "{\"ocpus\": $OCPUS, \"memoryInGBs\": $MEMORY_GB}" \
        --display-name "$DISPLAY_NAME" \
        --image-id "$IMAGE_ID" \
        --subnet-id "$SUBNET_ID" \
        --assign-public-ip true \
        --metadata "{\"ssh_authorized_keys\": \"$SSH_KEY\"}" \
        --output json 2>&1) || true

    if echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['data']['id'])" 2>/dev/null; then
        return 0
    fi

    if echo "$result" | grep -q "Out of host capacity"; then
        log "  ❌ $ad: Out of capacity"
        return 1
    elif echo "$result" | grep -q "timed out"; then
        log "  ⏱️ $ad: API timeout"
        return 1
    else
        log "  ❌ $ad: $(echo "$result" | grep -o '"message": "[^"]*"' | head -1)"
        return 1
    fi
}

wait_for_running() {
    local instance_id="$1"
    local max_wait=300  # 5 minutes
    local elapsed=0

    log "Waiting for instance $instance_id to reach RUNNING state..."

    while [ $elapsed -lt $max_wait ]; do
        local state
        state=$(oci compute instance get --instance-id "$instance_id" --query 'data."lifecycle-state"' --raw-output 2>/dev/null || echo "UNKNOWN")

        if [ "$state" = "RUNNING" ]; then
            log "  ✅ Instance is RUNNING"
            return 0
        fi

        log "  State: $state (waiting...)"
        sleep 30
        elapsed=$((elapsed + 30))
    done

    log "  ⚠️ Timeout waiting for RUNNING state"
    return 1
}

get_public_ip() {
    local instance_id="$1"

    # Get VNIC attachment
    local vnic_id
    vnic_id=$(oci compute vnic-attachment list \
        --compartment-id "$COMPARTMENT_ID" \
        --instance-id "$instance_id" \
        --output json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['data'][0]['vnic-id'])" 2>/dev/null)

    if [ -z "$vnic_id" ]; then
        return 1
    fi

    # Get public IP from VNIC
    oci network vnic get --vnic-id "$vnic_id" --query 'data."public-ip"' --raw-output 2>/dev/null
}

setup_new_instance() {
    local new_ip="$1"
    log "Setting up new instance at $new_ip..."

    # Wait for SSH to be available
    local ssh_ready=false
    for i in $(seq 1 12); do
        if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 -i "$SSH_KEY_FILE" opc@"$new_ip" "echo ready" 2>/dev/null; then
            ssh_ready=true
            break
        fi
        log "  Waiting for SSH... (attempt $i/12)"
        sleep 10
    done

    if [ "$ssh_ready" = false ]; then
        log "  ❌ SSH not available after 2 minutes"
        return 1
    fi

    log "  ✅ SSH connected"

    # Install Python and dependencies
    ssh -o StrictHostKeyChecking=no -o ConnectTimeout=30 -i "$SSH_KEY_FILE" opc@"$new_ip" bash <<'SETUP'
set -e
echo "Installing Python 3.11 and dependencies..."
sudo dnf install -y python3.11 python3.11-pip git 2>/dev/null || sudo yum install -y python3.11 python3.11-pip git 2>/dev/null

# Create project directory
mkdir -p /home/opc/crypto-trading-bot

# Set up Python alias
echo 'alias python=python3.11' >> ~/.bashrc
echo 'alias pip=pip3.11' >> ~/.bashrc

echo "Base setup complete"
SETUP

    log "  ✅ Base packages installed"

    # Sync bot code from old instance
    log "  Syncing bot code from old instance..."

    # Copy from local machine
    cd "$HOME/Desktop/Claude AI Crypto Bot/crypto-trading-bot"
    rsync -avz --exclude='.env' --exclude='__pycache__' --exclude='.git' --exclude='.DS_Store' \
        --exclude='data/state/' --exclude='data/journal/' --exclude='logs/' \
        -e "ssh -o StrictHostKeyChecking=no -o ConnectTimeout=30 -i $SSH_KEY_FILE" \
        ./ opc@"$new_ip":/home/opc/crypto-trading-bot/ 2>/dev/null

    log "  ✅ Code synced"

    # Copy .env from old instance
    log "  Copying .env from old instance..."
    local env_content
    env_content=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 -i "$SSH_KEY_FILE" opc@"$OLD_IP" \
        "cat /home/opc/crypto-trading-bot/.env" 2>/dev/null) || true

    if [ -n "$env_content" ]; then
        echo "$env_content" | ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 -i "$SSH_KEY_FILE" opc@"$new_ip" \
            "cat > /home/opc/crypto-trading-bot/.env"
        log "  ✅ .env copied"
    else
        log "  ⚠️ Could not copy .env from old instance — copy manually!"
    fi

    # Install pip requirements
    ssh -o StrictHostKeyChecking=no -o ConnectTimeout=60 -i "$SSH_KEY_FILE" opc@"$new_ip" bash <<'PIPREQ'
cd /home/opc/crypto-trading-bot
python3.11 -m pip install --user -r requirements.txt 2>/dev/null || pip3.11 install --user -r requirements.txt
echo "Pip packages installed"
PIPREQ

    log "  ✅ Python packages installed"

    # Set up systemd service
    ssh -o StrictHostKeyChecking=no -o ConnectTimeout=30 -i "$SSH_KEY_FILE" opc@"$new_ip" bash <<'SYSTEMD'
sudo cp /home/opc/crypto-trading-bot/deploy/cryptobot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable cryptobot.service
sudo systemctl start cryptobot.service
sleep 3
sudo systemctl status cryptobot.service --no-pager || true
echo "Service setup complete"
SYSTEMD

    log "  ✅ Systemd service configured and started"

    # Open firewall ports
    ssh -o StrictHostKeyChecking=no -o ConnectTimeout=30 -i "$SSH_KEY_FILE" opc@"$new_ip" bash <<'FIREWALL'
sudo firewall-cmd --permanent --add-port=8080/tcp 2>/dev/null || true
sudo firewall-cmd --reload 2>/dev/null || true
# Also try iptables in case firewalld isn't available
sudo iptables -I INPUT -p tcp --dport 8080 -j ACCEPT 2>/dev/null || true
echo "Firewall configured"
FIREWALL

    log "  ✅ Firewall ports opened"

    return 0
}

# ── Main Loop ──

START_TIME="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
ATTEMPT=0

log "═══════════════════════════════════════════════════════════"
log "OCI VM Upgrade Script Started"
log "Target: $SHAPE ($OCPUS OCPU / ${MEMORY_GB}GB RAM)"
log "Retry interval: ${RETRY_INTERVAL}s | Max attempts: $MAX_ATTEMPTS"
log "═══════════════════════════════════════════════════════════"

update_status "starting" "Beginning VM provisioning attempts"

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
    ATTEMPT=$((ATTEMPT + 1))
    log ""
    log "── Attempt $ATTEMPT/$MAX_ATTEMPTS ──"

    update_status "retrying" "Attempt $ATTEMPT of $MAX_ATTEMPTS"

    # Try each AD
    for ad in "${ADS[@]}"; do
        NEW_INSTANCE_ID=$(try_launch "$ad" 2>/dev/null || echo "")

        if [ -n "$NEW_INSTANCE_ID" ] && [ "$NEW_INSTANCE_ID" != "" ]; then
            log "🎉 Instance created! ID: $NEW_INSTANCE_ID"
            update_status "provisioned" "Instance created in $ad" "$NEW_INSTANCE_ID"

            # Wait for RUNNING
            if wait_for_running "$NEW_INSTANCE_ID"; then
                # Get public IP
                sleep 30  # Give time for IP assignment
                NEW_IP=$(get_public_ip "$NEW_INSTANCE_ID")

                if [ -n "$NEW_IP" ]; then
                    log "✅ New VM IP: $NEW_IP"
                    update_status "setting_up" "Configuring new instance" "$NEW_INSTANCE_ID" "$NEW_IP"

                    if setup_new_instance "$NEW_IP"; then
                        log ""
                        log "═══════════════════════════════════════════════════════════"
                        log "🎉 UPGRADE COMPLETE!"
                        log "  Old VM: $OLD_IP (E2.Micro, 1GB RAM)"
                        log "  New VM: $NEW_IP (A1.Flex, ${MEMORY_GB}GB RAM, $OCPUS OCPU)"
                        log "  Dashboard: http://$NEW_IP:8080"
                        log ""
                        log "  Next steps:"
                        log "  1. Verify bot is running: ssh -i ~/.ssh/cryptobot_oci opc@$NEW_IP"
                        log "  2. Check dashboard: http://$NEW_IP:8080"
                        log "  3. Stop old instance when ready:"
                        log "     oci compute instance action --instance-id $OLD_INSTANCE_ID --action STOP"
                        log "═══════════════════════════════════════════════════════════"

                        update_status "complete" "Upgrade successful!" "$NEW_INSTANCE_ID" "$NEW_IP"
                        exit 0
                    else
                        log "⚠️ Setup failed, but instance is running at $NEW_IP"
                        update_status "setup_failed" "Instance running but setup failed" "$NEW_INSTANCE_ID" "$NEW_IP"
                        exit 1
                    fi
                else
                    log "⚠️ Could not get public IP"
                    update_status "no_ip" "Instance running but no public IP" "$NEW_INSTANCE_ID"
                fi
            fi
        fi
    done

    if [ $ATTEMPT -lt $MAX_ATTEMPTS ]; then
        log "All ADs out of capacity. Retrying in ${RETRY_INTERVAL}s..."
        update_status "waiting" "All ADs out of capacity, waiting ${RETRY_INTERVAL}s"
        sleep $RETRY_INTERVAL
    fi
done

log "❌ Max attempts ($MAX_ATTEMPTS) reached. Could not provision A1.Flex instance."
update_status "failed" "Max attempts reached after 24 hours"
exit 1
