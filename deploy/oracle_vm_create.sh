#!/usr/bin/env bash
# =============================================================================
# Oracle Cloud VM Auto-Creation Script for Crypto Trading Bot
# =============================================================================
#
# This script automates the entire process of:
#   1. Installing & configuring OCI CLI
#   2. Creating networking (VCN, subnet, security lists)
#   3. Launching a compute instance (ARM free tier by default)
#   4. Configuring firewall rules
#   5. Setting up SSH access
#   6. Deploying the trading bot
#
# Prerequisites:
#   - Oracle Cloud account (free tier works)
#   - OCI CLI installed (script will check/install)
#   - SSH key pair (script will generate if missing)
#
# Usage:
#   chmod +x deploy/oracle_vm_create.sh
#   ./deploy/oracle_vm_create.sh
#
#   # Or with overrides:
#   SHAPE="VM.Standard.A1.Flex" OCPUS=2 MEMORY_GB=12 \
#     ./deploy/oracle_vm_create.sh
#
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — override via environment variables
# ---------------------------------------------------------------------------

# Display name for the VM
VM_NAME="${VM_NAME:-cryptobot-vm}"

# Shape: ARM free tier (4 OCPU / 24 GB free) or AMD
SHAPE="${SHAPE:-VM.Standard.A1.Flex}"
OCPUS="${OCPUS:-2}"
MEMORY_GB="${MEMORY_GB:-12}"

# Boot volume size in GB (min 47 for free tier)
BOOT_VOLUME_GB="${BOOT_VOLUME_GB:-50}"

# OS Image: Oracle Linux 8 ARM (default) — script auto-detects
IMAGE_OS="${IMAGE_OS:-Oracle Linux}"
IMAGE_VERSION="${IMAGE_VERSION:-8}"

# Networking
VCN_NAME="${VCN_NAME:-cryptobot-vcn}"
VCN_CIDR="${VCN_CIDR:-10.0.0.0/16}"
SUBNET_NAME="${SUBNET_NAME:-cryptobot-subnet}"
SUBNET_CIDR="${SUBNET_CIDR:-10.0.1.0/24}"
SECLIST_NAME="${SECLIST_NAME:-cryptobot-seclist}"

# SSH
SSH_KEY_PATH="${SSH_KEY_PATH:-$HOME/.ssh/cryptobot_oci}"
SSH_AUTHORIZED_KEY="${SSH_AUTHORIZED_KEY:-}"

# OCI Config
OCI_PROFILE="${OCI_PROFILE:-DEFAULT}"
COMPARTMENT_ID="${COMPARTMENT_ID:-}"
AVAILABILITY_DOMAIN="${AVAILABILITY_DOMAIN:-}"

# Bot deployment
BOT_PROJECT_DIR="${BOT_PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
DEPLOY_BOT="${DEPLOY_BOT:-true}"
BOT_MODE="${BOT_MODE:-paper}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

log()   { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
step()  { echo -e "\n${CYAN}━━━ Step $1: $2 ━━━${NC}"; }
banner() {
    echo -e "${BLUE}"
    echo "╔══════════════════════════════════════════════════════════╗"
    echo "║     Oracle Cloud VM Auto-Creator — Crypto Trading Bot   ║"
    echo "╚══════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

check_command() {
    command -v "$1" &>/dev/null
}

wait_for_state() {
    local resource_type="$1" id="$2" target_state="$3" timeout="${4:-600}"
    local elapsed=0
    log "Waiting for $resource_type to reach state: $target_state ..."
    while [ $elapsed -lt $timeout ]; do
        local state
        state=$(oci "$resource_type" get --"${resource_type##*.}"-id "$id" \
            --query 'data."lifecycle-state"' --raw-output 2>/dev/null || echo "UNKNOWN")
        if [ "$state" = "$target_state" ]; then
            log "$resource_type is $target_state"
            return 0
        fi
        echo -ne "  State: $state (${elapsed}s / ${timeout}s)\r"
        sleep 10
        elapsed=$((elapsed + 10))
    done
    err "Timeout waiting for $resource_type to reach $target_state"
    return 1
}

# ---------------------------------------------------------------------------
# Step 0: Pre-flight checks
# ---------------------------------------------------------------------------

banner

step 0 "Pre-flight checks"

# Check/install OCI CLI
if ! check_command oci; then
    warn "OCI CLI not found. Installing..."
    if [[ "$OSTYPE" == "darwin"* ]]; then
        if check_command brew; then
            brew install oci-cli
        else
            bash -c "$(curl -L https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh)" -- --accept-all-defaults
        fi
    elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
        bash -c "$(curl -L https://raw.githubusercontent.com/oracle/oci-cli/master/scripts/install/install.sh)" -- --accept-all-defaults
        export PATH="$HOME/bin:$PATH"
    else
        err "Unsupported OS. Install OCI CLI manually: https://docs.oracle.com/en-us/iaas/Content/API/SDKDocs/cliinstall.htm"
        exit 1
    fi
fi

log "OCI CLI version: $(oci --version)"

# Check if OCI is configured
if [ ! -f "$HOME/.oci/config" ]; then
    warn "OCI CLI not configured. Running setup wizard..."
    echo ""
    echo -e "${YELLOW}You will need:${NC}"
    echo "  1. Tenancy OCID    (from OCI Console > Tenancy Details)"
    echo "  2. User OCID       (from OCI Console > User Settings)"
    echo "  3. Region          (e.g., us-ashburn-1, ap-mumbai-1)"
    echo "  4. API Key         (wizard will generate one)"
    echo ""
    echo "After the wizard, upload the PUBLIC key to OCI Console > User > API Keys"
    echo ""
    oci setup config
    echo ""
    echo -e "${YELLOW}IMPORTANT: Upload your API public key to OCI Console before continuing.${NC}"
    echo "  File: $HOME/.oci/oci_api_key_public.pem"
    echo "  Go to: OCI Console > Identity > Users > Your User > API Keys > Add API Key"
    echo ""
    read -rp "Press Enter after uploading the API key to continue..."
fi

# Verify OCI connectivity
log "Verifying OCI connectivity..."
if ! oci iam region list --output table &>/dev/null; then
    err "Cannot connect to OCI. Check your config at $HOME/.oci/config"
    err "Ensure your API key is uploaded to the OCI Console."
    exit 1
fi
log "OCI connection verified"

# Check jq
if ! check_command jq; then
    warn "jq not found. Installing..."
    if [[ "$OSTYPE" == "darwin"* ]]; then
        brew install jq
    else
        sudo apt-get install -y jq 2>/dev/null || sudo yum install -y jq 2>/dev/null
    fi
fi

# ---------------------------------------------------------------------------
# Step 1: Resolve tenancy and compartment
# ---------------------------------------------------------------------------

step 1 "Resolving tenancy and compartment"

TENANCY_ID=$(oci iam compartment list --all --compartment-id-in-subtree true \
    --query 'data[0]."compartment-id"' --raw-output 2>/dev/null || true)

if [ -z "$TENANCY_ID" ]; then
    TENANCY_ID=$(grep -A5 "\[$OCI_PROFILE\]" "$HOME/.oci/config" | grep tenancy | head -1 | cut -d= -f2 | tr -d ' ')
fi

if [ -z "$COMPARTMENT_ID" ]; then
    COMPARTMENT_ID="$TENANCY_ID"
    log "Using root compartment: $COMPARTMENT_ID"
else
    log "Using compartment: $COMPARTMENT_ID"
fi

# ---------------------------------------------------------------------------
# Step 2: Resolve availability domain
# ---------------------------------------------------------------------------

step 2 "Resolving availability domain"

if [ -z "$AVAILABILITY_DOMAIN" ]; then
    AVAILABILITY_DOMAIN=$(oci iam availability-domain list \
        --compartment-id "$COMPARTMENT_ID" \
        --query 'data[0].name' --raw-output)
fi
log "Availability domain: $AVAILABILITY_DOMAIN"

# ---------------------------------------------------------------------------
# Step 3: Generate SSH key pair
# ---------------------------------------------------------------------------

step 3 "Setting up SSH keys"

if [ -z "$SSH_AUTHORIZED_KEY" ]; then
    if [ ! -f "$SSH_KEY_PATH" ]; then
        log "Generating SSH key pair at $SSH_KEY_PATH"
        ssh-keygen -t ed25519 -f "$SSH_KEY_PATH" -N "" -C "cryptobot-oci"
    else
        log "Using existing SSH key: $SSH_KEY_PATH"
    fi
    SSH_AUTHORIZED_KEY=$(cat "${SSH_KEY_PATH}.pub")
fi

log "SSH public key ready"

# ---------------------------------------------------------------------------
# Step 4: Create VCN (Virtual Cloud Network)
# ---------------------------------------------------------------------------

step 4 "Creating Virtual Cloud Network"

# Check if VCN already exists
EXISTING_VCN=$(oci network vcn list \
    --compartment-id "$COMPARTMENT_ID" \
    --display-name "$VCN_NAME" \
    --query 'data[0].id' --raw-output 2>/dev/null || echo "")

if [ -n "$EXISTING_VCN" ] && [ "$EXISTING_VCN" != "null" ] && [ "$EXISTING_VCN" != "" ]; then
    VCN_ID="$EXISTING_VCN"
    log "Using existing VCN: $VCN_ID"
else
    log "Creating VCN: $VCN_NAME ($VCN_CIDR)"
    VCN_ID=$(oci network vcn create \
        --compartment-id "$COMPARTMENT_ID" \
        --display-name "$VCN_NAME" \
        --cidr-blocks "[\"$VCN_CIDR\"]" \
        --dns-label "cryptobot" \
        --query 'data.id' --raw-output)
    log "VCN created: $VCN_ID"
fi

# ---------------------------------------------------------------------------
# Step 5: Create Internet Gateway
# ---------------------------------------------------------------------------

step 5 "Creating Internet Gateway"

EXISTING_IGW=$(oci network internet-gateway list \
    --compartment-id "$COMPARTMENT_ID" \
    --vcn-id "$VCN_ID" \
    --display-name "cryptobot-igw" \
    --query 'data[0].id' --raw-output 2>/dev/null || echo "")

if [ -n "$EXISTING_IGW" ] && [ "$EXISTING_IGW" != "null" ] && [ "$EXISTING_IGW" != "" ]; then
    IGW_ID="$EXISTING_IGW"
    log "Using existing Internet Gateway: $IGW_ID"
else
    IGW_ID=$(oci network internet-gateway create \
        --compartment-id "$COMPARTMENT_ID" \
        --vcn-id "$VCN_ID" \
        --display-name "cryptobot-igw" \
        --is-enabled true \
        --query 'data.id' --raw-output)
    log "Internet Gateway created: $IGW_ID"
fi

# ---------------------------------------------------------------------------
# Step 6: Create Route Table
# ---------------------------------------------------------------------------

step 6 "Configuring Route Table"

# Get default route table
RT_ID=$(oci network vcn get --vcn-id "$VCN_ID" \
    --query 'data."default-route-table-id"' --raw-output)

# Add internet gateway route
oci network route-table update \
    --rt-id "$RT_ID" \
    --route-rules "[{
        \"cidrBlock\": \"0.0.0.0/0\",
        \"networkEntityId\": \"$IGW_ID\"
    }]" \
    --force \
    --query 'data.id' --raw-output >/dev/null
log "Route table updated with internet gateway route"

# ---------------------------------------------------------------------------
# Step 7: Create Security List
# ---------------------------------------------------------------------------

step 7 "Creating Security List"

EXISTING_SL=$(oci network security-list list \
    --compartment-id "$COMPARTMENT_ID" \
    --vcn-id "$VCN_ID" \
    --display-name "$SECLIST_NAME" \
    --query 'data[0].id' --raw-output 2>/dev/null || echo "")

INGRESS_RULES='[
    {
        "source": "0.0.0.0/0",
        "protocol": "6",
        "isStateless": false,
        "tcpOptions": {"destinationPortRange": {"min": 22, "max": 22}}
    },
    {
        "source": "0.0.0.0/0",
        "protocol": "6",
        "isStateless": false,
        "tcpOptions": {"destinationPortRange": {"min": 8080, "max": 8080}}
    },
    {
        "source": "0.0.0.0/0",
        "protocol": "1",
        "isStateless": false,
        "icmpOptions": {"type": 3, "code": 4}
    }
]'

EGRESS_RULES='[
    {
        "destination": "0.0.0.0/0",
        "protocol": "all",
        "isStateless": false
    }
]'

if [ -n "$EXISTING_SL" ] && [ "$EXISTING_SL" != "null" ] && [ "$EXISTING_SL" != "" ]; then
    SL_ID="$EXISTING_SL"
    oci network security-list update \
        --security-list-id "$SL_ID" \
        --ingress-security-rules "$INGRESS_RULES" \
        --egress-security-rules "$EGRESS_RULES" \
        --force >/dev/null
    log "Updated existing security list: $SL_ID"
else
    SL_ID=$(oci network security-list create \
        --compartment-id "$COMPARTMENT_ID" \
        --vcn-id "$VCN_ID" \
        --display-name "$SECLIST_NAME" \
        --ingress-security-rules "$INGRESS_RULES" \
        --egress-security-rules "$EGRESS_RULES" \
        --query 'data.id' --raw-output)
    log "Security list created: $SL_ID"
fi

# ---------------------------------------------------------------------------
# Step 8: Create Subnet
# ---------------------------------------------------------------------------

step 8 "Creating Subnet"

EXISTING_SUBNET=$(oci network subnet list \
    --compartment-id "$COMPARTMENT_ID" \
    --vcn-id "$VCN_ID" \
    --display-name "$SUBNET_NAME" \
    --query 'data[0].id' --raw-output 2>/dev/null || echo "")

if [ -n "$EXISTING_SUBNET" ] && [ "$EXISTING_SUBNET" != "null" ] && [ "$EXISTING_SUBNET" != "" ]; then
    SUBNET_ID="$EXISTING_SUBNET"
    log "Using existing subnet: $SUBNET_ID"
else
    SUBNET_ID=$(oci network subnet create \
        --compartment-id "$COMPARTMENT_ID" \
        --vcn-id "$VCN_ID" \
        --availability-domain "$AVAILABILITY_DOMAIN" \
        --display-name "$SUBNET_NAME" \
        --cidr-block "$SUBNET_CIDR" \
        --route-table-id "$RT_ID" \
        --security-list-ids "[\"$SL_ID\"]" \
        --dns-label "botsubnet" \
        --query 'data.id' --raw-output)
    log "Subnet created: $SUBNET_ID"
fi

# ---------------------------------------------------------------------------
# Step 9: Find OS Image
# ---------------------------------------------------------------------------

step 9 "Finding OS image for $SHAPE"

# Determine architecture
if [[ "$SHAPE" == *"A1"* ]]; then
    ARCH="aarch64"
else
    ARCH="x86_64"
fi

IMAGE_ID=$(oci compute image list \
    --compartment-id "$COMPARTMENT_ID" \
    --operating-system "$IMAGE_OS" \
    --operating-system-version "$IMAGE_VERSION" \
    --shape "$SHAPE" \
    --sort-by TIMECREATED \
    --sort-order DESC \
    --query 'data[0].id' --raw-output 2>/dev/null || echo "")

if [ -z "$IMAGE_ID" ] || [ "$IMAGE_ID" = "null" ]; then
    # Fallback: try Ubuntu
    warn "Oracle Linux image not found. Trying Ubuntu 22.04..."
    IMAGE_OS="Canonical Ubuntu"
    IMAGE_VERSION="22.04"
    IMAGE_ID=$(oci compute image list \
        --compartment-id "$COMPARTMENT_ID" \
        --operating-system "$IMAGE_OS" \
        --operating-system-version "$IMAGE_VERSION" \
        --shape "$SHAPE" \
        --sort-by TIMECREATED \
        --sort-order DESC \
        --query 'data[0].id' --raw-output 2>/dev/null || echo "")
fi

if [ -z "$IMAGE_ID" ] || [ "$IMAGE_ID" = "null" ]; then
    err "Could not find a compatible OS image for shape $SHAPE"
    exit 1
fi

log "Image: $IMAGE_OS $IMAGE_VERSION ($IMAGE_ID)"

# ---------------------------------------------------------------------------
# Step 10: Create cloud-init script
# ---------------------------------------------------------------------------

step 10 "Generating cloud-init script"

CLOUD_INIT=$(cat <<'CLOUD_INIT_EOF'
#!/bin/bash
set -e

LOG="/var/log/cryptobot-setup.log"
exec > >(tee -a "$LOG") 2>&1

echo "=== CryptoBot cloud-init starting at $(date) ==="

# Detect OS
if [ -f /etc/oracle-linux-release ]; then
    OS="oracle"
elif [ -f /etc/lsb-release ]; then
    OS="ubuntu"
else
    OS="unknown"
fi

echo "Detected OS: $OS"

# Install dependencies
if [ "$OS" = "oracle" ]; then
    dnf install -y python3.11 python3.11-pip git htop tmux firewalld
    alternatives --set python3 /usr/bin/python3.11 2>/dev/null || true

    # Firewall
    systemctl enable --now firewalld
    firewall-cmd --permanent --add-port=22/tcp
    firewall-cmd --permanent --add-port=8080/tcp
    firewall-cmd --reload

elif [ "$OS" = "ubuntu" ]; then
    apt-get update
    apt-get install -y python3.11 python3.11-venv python3-pip git htop tmux ufw

    # Firewall
    ufw allow 22/tcp
    ufw allow 8080/tcp
    ufw --force enable
fi

# Install Docker
if ! command -v docker &>/dev/null; then
    echo "Installing Docker..."
    if [ "$OS" = "oracle" ]; then
        dnf install -y dnf-utils
        dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
        dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
    else
        curl -fsSL https://get.docker.com | sh
    fi
    systemctl enable --now docker
fi

# Create bot user
if ! id cryptobot &>/dev/null; then
    useradd -r -m -d /opt/cryptobot -s /bin/bash cryptobot
    usermod -aG docker cryptobot 2>/dev/null || true
fi

# Create directory structure
mkdir -p /opt/cryptobot/{logs,data/state,data/journal}
chown -R cryptobot:cryptobot /opt/cryptobot

echo "=== CryptoBot cloud-init completed at $(date) ==="
echo "VM is ready for bot deployment."
CLOUD_INIT_EOF
)

# Base64 encode for OCI
CLOUD_INIT_B64=$(echo "$CLOUD_INIT" | base64)

log "Cloud-init script prepared"

# ---------------------------------------------------------------------------
# Step 11: Launch the compute instance
# ---------------------------------------------------------------------------

step 11 "Launching compute instance"

log "Configuration:"
echo "  Name:    $VM_NAME"
echo "  Shape:   $SHAPE"
echo "  OCPUs:   $OCPUS"
echo "  Memory:  ${MEMORY_GB} GB"
echo "  Boot:    ${BOOT_VOLUME_GB} GB"
echo "  Image:   $IMAGE_OS $IMAGE_VERSION"
echo ""

# Check if instance already exists
EXISTING_INSTANCE=$(oci compute instance list \
    --compartment-id "$COMPARTMENT_ID" \
    --display-name "$VM_NAME" \
    --lifecycle-state RUNNING \
    --query 'data[0].id' --raw-output 2>/dev/null || echo "")

if [ -n "$EXISTING_INSTANCE" ] && [ "$EXISTING_INSTANCE" != "null" ] && [ "$EXISTING_INSTANCE" != "" ]; then
    INSTANCE_ID="$EXISTING_INSTANCE"
    warn "Instance '$VM_NAME' already exists and is running: $INSTANCE_ID"
else
    # Build shape config for flex shapes
    SHAPE_CONFIG=""
    if [[ "$SHAPE" == *"Flex"* ]]; then
        SHAPE_CONFIG="--shape-config '{\"ocpus\": $OCPUS, \"memoryInGBs\": $MEMORY_GB}'"
    fi

    INSTANCE_ID=$(eval oci compute instance launch \
        --compartment-id "$COMPARTMENT_ID" \
        --availability-domain "$AVAILABILITY_DOMAIN" \
        --display-name "$VM_NAME" \
        --shape "$SHAPE" \
        $SHAPE_CONFIG \
        --image-id "$IMAGE_ID" \
        --subnet-id "$SUBNET_ID" \
        --assign-public-ip true \
        --boot-volume-size-in-gbs "$BOOT_VOLUME_GB" \
        --ssh-authorized-keys-file "${SSH_KEY_PATH}.pub" \
        --user-data-file <(echo "$CLOUD_INIT") \
        --query 'data.id' --raw-output)

    log "Instance launch initiated: $INSTANCE_ID"

    # Wait for instance to be running
    log "Waiting for instance to reach RUNNING state (this may take 2-5 minutes)..."
    ELAPSED=0
    TIMEOUT=600
    while [ $ELAPSED -lt $TIMEOUT ]; do
        STATE=$(oci compute instance get --instance-id "$INSTANCE_ID" \
            --query 'data."lifecycle-state"' --raw-output 2>/dev/null || echo "UNKNOWN")
        if [ "$STATE" = "RUNNING" ]; then
            log "Instance is RUNNING"
            break
        fi
        printf "  State: %-15s (%ds / %ds)\r" "$STATE" "$ELAPSED" "$TIMEOUT"
        sleep 10
        ELAPSED=$((ELAPSED + 10))
    done

    if [ "$STATE" != "RUNNING" ]; then
        err "Instance failed to reach RUNNING state within ${TIMEOUT}s"
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# Step 12: Get public IP
# ---------------------------------------------------------------------------

step 12 "Retrieving public IP address"

# Get VNIC attachment
VNIC_ID=$(oci compute instance list-vnics \
    --instance-id "$INSTANCE_ID" \
    --query 'data[0]."vnic-id"' --raw-output)

PUBLIC_IP=$(oci network vnic get \
    --vnic-id "$VNIC_ID" \
    --query 'data."public-ip"' --raw-output)

log "Public IP: $PUBLIC_IP"

# Determine SSH user
if [[ "$IMAGE_OS" == *"Ubuntu"* ]]; then
    SSH_USER="ubuntu"
else
    SSH_USER="opc"
fi

# ---------------------------------------------------------------------------
# Step 13: Wait for SSH to become available
# ---------------------------------------------------------------------------

step 13 "Waiting for SSH access"

SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o LogLevel=ERROR"
ELAPSED=0
TIMEOUT=300

while [ $ELAPSED -lt $TIMEOUT ]; do
    if ssh $SSH_OPTS -i "$SSH_KEY_PATH" "$SSH_USER@$PUBLIC_IP" "echo ok" &>/dev/null; then
        log "SSH is ready"
        break
    fi
    printf "  Waiting for SSH... (%ds / %ds)\r" "$ELAPSED" "$TIMEOUT"
    sleep 10
    ELAPSED=$((ELAPSED + 10))
done

if [ $ELAPSED -ge $TIMEOUT ]; then
    warn "SSH not available after ${TIMEOUT}s. Cloud-init may still be running."
    warn "Try manually: ssh -i $SSH_KEY_PATH $SSH_USER@$PUBLIC_IP"
fi

# ---------------------------------------------------------------------------
# Step 14: Deploy the trading bot
# ---------------------------------------------------------------------------

if [ "$DEPLOY_BOT" = "true" ]; then
    step 14 "Deploying trading bot to VM"

    log "Waiting 30s for cloud-init to complete..."
    sleep 30

    # Upload the project
    log "Uploading project files..."
    rsync -avz --progress \
        -e "ssh $SSH_OPTS -i $SSH_KEY_PATH" \
        --exclude '.git' \
        --exclude '__pycache__' \
        --exclude '*.pyc' \
        --exclude 'data/journal/*' \
        --exclude 'data/state/*' \
        --exclude 'logs/*' \
        --exclude '.env' \
        --exclude 'node_modules' \
        "$BOT_PROJECT_DIR/" \
        "$SSH_USER@$PUBLIC_IP:/tmp/cryptobot-upload/"

    # Set up the bot on the remote machine
    log "Setting up bot on remote machine..."
    ssh $SSH_OPTS -i "$SSH_KEY_PATH" "$SSH_USER@$PUBLIC_IP" bash <<'REMOTE_SETUP'
set -e

# Wait for cloud-init to finish
while [ ! -f /var/log/cryptobot-setup.log ] || ! grep -q "completed" /var/log/cryptobot-setup.log 2>/dev/null; do
    echo "Waiting for cloud-init to finish..."
    sleep 10
    # Safety timeout: check if at least basic packages are installed
    if command -v python3 &>/dev/null && command -v pip3 &>/dev/null; then
        break
    fi
done

# Copy files to bot directory
sudo cp -r /tmp/cryptobot-upload/* /opt/cryptobot/
sudo chown -R cryptobot:cryptobot /opt/cryptobot
rm -rf /tmp/cryptobot-upload

# Set up Python virtual environment
sudo -u cryptobot bash <<'VENV_SETUP'
cd /opt/cryptobot
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
VENV_SETUP

# Create .env from example if not exists
if [ ! -f /opt/cryptobot/.env ] && [ -f /opt/cryptobot/.env.example ]; then
    sudo -u cryptobot cp /opt/cryptobot/.env.example /opt/cryptobot/.env
    echo "NOTE: Edit /opt/cryptobot/.env with your API keys"
fi

# Create necessary directories
sudo -u cryptobot mkdir -p /opt/cryptobot/{logs,data/state,data/journal}

# Install systemd service
if [ -f /opt/cryptobot/deploy/cryptobot.service ]; then
    sudo cp /opt/cryptobot/deploy/cryptobot.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable cryptobot
    echo "Systemd service installed and enabled"
fi

echo "Bot setup complete!"
REMOTE_SETUP

    log "Bot deployed successfully"
else
    step 14 "Skipping bot deployment (DEPLOY_BOT=false)"
fi

# ---------------------------------------------------------------------------
# Step 15: Summary
# ---------------------------------------------------------------------------

step 15 "Deployment Complete"

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║                  VM CREATION SUCCESSFUL                      ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${CYAN}Instance ID:${NC}   $INSTANCE_ID"
echo -e "  ${CYAN}Public IP:${NC}     $PUBLIC_IP"
echo -e "  ${CYAN}SSH User:${NC}      $SSH_USER"
echo -e "  ${CYAN}Shape:${NC}         $SHAPE ($OCPUS OCPU / ${MEMORY_GB} GB)"
echo -e "  ${CYAN}OS:${NC}            $IMAGE_OS $IMAGE_VERSION"
echo ""
echo -e "  ${YELLOW}SSH Command:${NC}"
echo -e "    ssh -i $SSH_KEY_PATH $SSH_USER@$PUBLIC_IP"
echo ""
echo -e "  ${YELLOW}Dashboard URL:${NC}"
echo -e "    http://$PUBLIC_IP:8080"
echo ""

if [ "$DEPLOY_BOT" = "true" ]; then
    echo -e "  ${YELLOW}Next Steps:${NC}"
    echo "    1. SSH into the VM:"
    echo "       ssh -i $SSH_KEY_PATH $SSH_USER@$PUBLIC_IP"
    echo ""
    echo "    2. Configure API keys:"
    echo "       sudo -u cryptobot nano /opt/cryptobot/.env"
    echo ""
    echo "    3. Start the bot (paper mode):"
    echo "       sudo systemctl start cryptobot"
    echo ""
    echo "    4. Check logs:"
    echo "       sudo journalctl -u cryptobot -f"
    echo ""
    echo "    5. Or run manually:"
    echo "       sudo -u cryptobot bash"
    echo "       cd /opt/cryptobot && source venv/bin/activate"
    echo "       python main.py --mode paper"
fi

echo ""

# Save deployment info
DEPLOY_INFO="$BOT_PROJECT_DIR/deploy/.last_deployment.json"
cat > "$DEPLOY_INFO" <<DEPLOY_JSON
{
    "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "instance_id": "$INSTANCE_ID",
    "public_ip": "$PUBLIC_IP",
    "ssh_user": "$SSH_USER",
    "ssh_key": "$SSH_KEY_PATH",
    "shape": "$SHAPE",
    "ocpus": $OCPUS,
    "memory_gb": $MEMORY_GB,
    "os": "$IMAGE_OS $IMAGE_VERSION",
    "vcn_id": "$VCN_ID",
    "subnet_id": "$SUBNET_ID",
    "compartment_id": "$COMPARTMENT_ID",
    "availability_domain": "$AVAILABILITY_DOMAIN"
}
DEPLOY_JSON

log "Deployment info saved to $DEPLOY_INFO"
echo ""
