#!/usr/bin/env bash
# =============================================================================
# Oracle Cloud VM Management Script
# =============================================================================
#
# Manage the CryptoBot VM: status, start, stop, restart, ssh, deploy, destroy
#
# Usage:
#   ./deploy/oracle_vm_manage.sh status
#   ./deploy/oracle_vm_manage.sh ssh
#   ./deploy/oracle_vm_manage.sh stop
#   ./deploy/oracle_vm_manage.sh start
#   ./deploy/oracle_vm_manage.sh restart-bot
#   ./deploy/oracle_vm_manage.sh logs
#   ./deploy/oracle_vm_manage.sh deploy
#   ./deploy/oracle_vm_manage.sh destroy
#
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEPLOY_INFO="$SCRIPT_DIR/.last_deployment.json"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# ---------------------------------------------------------------------------
# Load deployment info
# ---------------------------------------------------------------------------

load_info() {
    if [ ! -f "$DEPLOY_INFO" ]; then
        echo -e "${RED}No deployment found. Run oracle_vm_create.sh first.${NC}"
        exit 1
    fi
    INSTANCE_ID=$(jq -r '.instance_id' "$DEPLOY_INFO")
    PUBLIC_IP=$(jq -r '.public_ip' "$DEPLOY_INFO")
    SSH_USER=$(jq -r '.ssh_user' "$DEPLOY_INFO")
    SSH_KEY=$(jq -r '.ssh_key' "$DEPLOY_INFO")
    VCN_ID=$(jq -r '.vcn_id' "$DEPLOY_INFO")
    SUBNET_ID=$(jq -r '.subnet_id' "$DEPLOY_INFO")
    COMPARTMENT_ID=$(jq -r '.compartment_id' "$DEPLOY_INFO")
    SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

cmd_status() {
    load_info
    echo -e "${CYAN}=== VM Status ===${NC}"
    STATE=$(oci compute instance get --instance-id "$INSTANCE_ID" \
        --query 'data."lifecycle-state"' --raw-output 2>/dev/null || echo "UNKNOWN")
    echo -e "  Instance: $INSTANCE_ID"
    echo -e "  State:    $STATE"
    echo -e "  IP:       $PUBLIC_IP"

    if [ "$STATE" = "RUNNING" ]; then
        echo ""
        echo -e "${CYAN}=== Bot Status ===${NC}"
        ssh $SSH_OPTS -i "$SSH_KEY" "$SSH_USER@$PUBLIC_IP" \
            "sudo systemctl status cryptobot --no-pager 2>/dev/null || echo 'Service not installed'" 2>/dev/null || \
            echo "  Cannot reach VM via SSH"
    fi
}

cmd_ssh() {
    load_info
    echo -e "${GREEN}Connecting to $SSH_USER@$PUBLIC_IP ...${NC}"
    ssh $SSH_OPTS -i "$SSH_KEY" "$SSH_USER@$PUBLIC_IP"
}

cmd_stop_vm() {
    load_info
    echo -e "${YELLOW}Stopping VM instance...${NC}"
    oci compute instance action --instance-id "$INSTANCE_ID" --action SOFTSTOP
    echo -e "${GREEN}Stop command sent. VM will shut down gracefully.${NC}"
}

cmd_start_vm() {
    load_info
    echo -e "${GREEN}Starting VM instance...${NC}"
    oci compute instance action --instance-id "$INSTANCE_ID" --action START
    echo -e "${GREEN}Start command sent. Wait 1-2 minutes for boot.${NC}"
}

cmd_restart_bot() {
    load_info
    echo -e "${YELLOW}Restarting CryptoBot service...${NC}"
    ssh $SSH_OPTS -i "$SSH_KEY" "$SSH_USER@$PUBLIC_IP" \
        "sudo systemctl restart cryptobot && echo 'Bot restarted' && sudo systemctl status cryptobot --no-pager"
}

cmd_stop_bot() {
    load_info
    echo -e "${YELLOW}Stopping CryptoBot service...${NC}"
    ssh $SSH_OPTS -i "$SSH_KEY" "$SSH_USER@$PUBLIC_IP" \
        "sudo systemctl stop cryptobot && echo 'Bot stopped'"
}

cmd_logs() {
    load_info
    LINES="${2:-100}"
    echo -e "${CYAN}=== Last $LINES log lines ===${NC}"
    ssh $SSH_OPTS -i "$SSH_KEY" "$SSH_USER@$PUBLIC_IP" \
        "sudo journalctl -u cryptobot -n $LINES --no-pager"
}

cmd_logs_follow() {
    load_info
    echo -e "${CYAN}=== Following bot logs (Ctrl+C to stop) ===${NC}"
    ssh $SSH_OPTS -i "$SSH_KEY" "$SSH_USER@$PUBLIC_IP" \
        "sudo journalctl -u cryptobot -f"
}

cmd_deploy() {
    load_info
    echo -e "${CYAN}=== Deploying latest code to VM ===${NC}"

    echo "Uploading files..."
    rsync -avz --progress \
        -e "ssh $SSH_OPTS -i $SSH_KEY" \
        --exclude '.git' \
        --exclude '__pycache__' \
        --exclude '*.pyc' \
        --exclude 'data/journal/*' \
        --exclude 'data/state/*' \
        --exclude 'logs/*' \
        --exclude '.env' \
        --exclude 'node_modules' \
        --exclude 'deploy/.last_deployment.json' \
        "$PROJECT_DIR/" \
        "$SSH_USER@$PUBLIC_IP:/tmp/cryptobot-update/"

    echo "Installing on VM..."
    ssh $SSH_OPTS -i "$SSH_KEY" "$SSH_USER@$PUBLIC_IP" bash <<'EOF'
set -e
sudo rsync -av --exclude '.env' /tmp/cryptobot-update/ /opt/cryptobot/
sudo chown -R cryptobot:cryptobot /opt/cryptobot
rm -rf /tmp/cryptobot-update

# Update pip packages if requirements changed
sudo -u cryptobot bash -c 'cd /opt/cryptobot && source venv/bin/activate && pip install -r requirements.txt -q'

# Restart the bot
sudo systemctl restart cryptobot 2>/dev/null && echo "Bot restarted with new code" || echo "Start bot manually"
EOF

    echo -e "${GREEN}Deployment complete!${NC}"
}

cmd_health() {
    load_info
    echo -e "${CYAN}=== System Health ===${NC}"
    ssh $SSH_OPTS -i "$SSH_KEY" "$SSH_USER@$PUBLIC_IP" bash <<'EOF'
echo "--- CPU & Memory ---"
free -h | head -2
echo ""
uptime
echo ""

echo "--- Disk ---"
df -h / | tail -1
echo ""

echo "--- Bot Process ---"
systemctl is-active cryptobot 2>/dev/null || echo "not running"
echo ""

echo "--- Docker ---"
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || echo "Docker not in use"
echo ""

echo "--- Recent Errors ---"
journalctl -u cryptobot --no-pager -p err --since "1 hour ago" 2>/dev/null | tail -5 || echo "No recent errors"
EOF
}

cmd_config() {
    load_info
    echo -e "${CYAN}=== Opening .env config for editing ===${NC}"
    ssh $SSH_OPTS -t -i "$SSH_KEY" "$SSH_USER@$PUBLIC_IP" \
        "sudo -u cryptobot nano /opt/cryptobot/.env"
}

cmd_destroy() {
    load_info
    echo -e "${RED}╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${RED}║  WARNING: This will permanently destroy the VM and all  ║${NC}"
    echo -e "${RED}║  associated resources (VCN, subnet, security list).     ║${NC}"
    echo -e "${RED}╚══════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo "  Instance: $INSTANCE_ID"
    echo "  IP:       $PUBLIC_IP"
    echo ""
    read -rp "Type 'DESTROY' to confirm: " confirm

    if [ "$confirm" != "DESTROY" ]; then
        echo "Aborted."
        exit 0
    fi

    echo ""
    echo "Terminating instance..."
    oci compute instance terminate --instance-id "$INSTANCE_ID" --force \
        --preserve-boot-volume false 2>/dev/null || true

    echo "Waiting for termination..."
    sleep 30

    # Clean up networking
    echo "Removing subnet..."
    oci network subnet delete --subnet-id "$SUBNET_ID" --force 2>/dev/null || true
    sleep 10

    echo "Removing security list..."
    # Get non-default security lists
    SL_IDS=$(oci network security-list list \
        --compartment-id "$COMPARTMENT_ID" \
        --vcn-id "$VCN_ID" \
        --query 'data[?contains("display-name", `cryptobot`)].id' \
        --raw-output 2>/dev/null || echo "")
    for sl in $SL_IDS; do
        [ "$sl" != "null" ] && [ -n "$sl" ] && \
            oci network security-list delete --security-list-id "$sl" --force 2>/dev/null || true
    done

    # Remove internet gateway
    echo "Removing internet gateway..."
    IGW_IDS=$(oci network internet-gateway list \
        --compartment-id "$COMPARTMENT_ID" \
        --vcn-id "$VCN_ID" \
        --query 'data[].id' --raw-output 2>/dev/null || echo "")
    for igw in $IGW_IDS; do
        [ "$igw" != "null" ] && [ -n "$igw" ] && \
            oci network internet-gateway delete --ig-id "$igw" --force 2>/dev/null || true
    done
    sleep 5

    echo "Removing VCN..."
    oci network vcn delete --vcn-id "$VCN_ID" --force 2>/dev/null || true

    rm -f "$DEPLOY_INFO"

    echo ""
    echo -e "${GREEN}All resources destroyed.${NC}"
}

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

usage() {
    echo "Usage: $0 <command>"
    echo ""
    echo "Commands:"
    echo "  status       Show VM and bot status"
    echo "  ssh          SSH into the VM"
    echo "  start        Start the VM"
    echo "  stop         Stop the VM (soft shutdown)"
    echo "  restart-bot  Restart the CryptoBot service"
    echo "  stop-bot     Stop the CryptoBot service"
    echo "  logs [N]     Show last N log lines (default: 100)"
    echo "  follow       Follow bot logs in real-time"
    echo "  deploy       Deploy latest code to VM"
    echo "  health       Show system health summary"
    echo "  config       Edit .env file on VM"
    echo "  destroy      Permanently destroy VM and all resources"
    echo ""
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

case "${1:-help}" in
    status)       cmd_status ;;
    ssh)          cmd_ssh ;;
    start)        cmd_start_vm ;;
    stop)         cmd_stop_vm ;;
    restart-bot)  cmd_restart_bot ;;
    stop-bot)     cmd_stop_bot ;;
    logs)         cmd_logs "$@" ;;
    follow)       cmd_logs_follow ;;
    deploy)       cmd_deploy ;;
    health)       cmd_health ;;
    config)       cmd_config ;;
    destroy)      cmd_destroy ;;
    help|--help|-h) usage ;;
    *)
        echo -e "${RED}Unknown command: $1${NC}"
        usage
        exit 1
        ;;
esac
