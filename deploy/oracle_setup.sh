#!/usr/bin/env bash
# ==============================================================================
# Oracle Cloud VM Setup Script for Crypto Trading Bot
#
# Tested on: Oracle Linux 8/9, Ubuntu 22.04 (aarch64 / x86_64)
#
# Usage:
#   1. SSH into your Oracle Cloud VM
#   2. Upload or clone the project
#   3. Run: sudo bash deploy/oracle_setup.sh
#
# This script will:
#   - Update system packages
#   - Install Docker and Docker Compose
#   - Install Python 3.11 (for non-Docker deployments)
#   - Configure firewall rules (open port 8080)
#   - Create a dedicated bot user
#   - Set up directory structure
#   - Install the systemd service
# ==============================================================================

set -euo pipefail

PROJECT_DIR="/opt/crypto-trading-bot"
BOT_USER="cryptobot"
DASHBOARD_PORT=8080

echo "============================================="
echo " Crypto Trading Bot - Oracle Cloud Setup"
echo "============================================="

# ----------------------------------------------------------
# 1. Detect OS
# ----------------------------------------------------------
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_ID="$ID"
else
    echo "ERROR: Cannot detect OS. Exiting."
    exit 1
fi

echo "[*] Detected OS: $OS_ID"

# ----------------------------------------------------------
# 2. Update system packages
# ----------------------------------------------------------
echo "[*] Updating system packages..."
if [[ "$OS_ID" == "ol" || "$OS_ID" == "centos" || "$OS_ID" == "rhel" ]]; then
    dnf update -y
    dnf install -y git curl wget tar gcc make openssl-devel libffi-devel bzip2-devel
elif [[ "$OS_ID" == "ubuntu" || "$OS_ID" == "debian" ]]; then
    apt-get update && apt-get upgrade -y
    apt-get install -y git curl wget build-essential libssl-dev libffi-dev zlib1g-dev
else
    echo "WARNING: Unsupported OS '$OS_ID'. Attempting apt-get..."
    apt-get update && apt-get upgrade -y
fi

# ----------------------------------------------------------
# 3. Install Docker
# ----------------------------------------------------------
echo "[*] Installing Docker..."
if ! command -v docker &>/dev/null; then
    if [[ "$OS_ID" == "ol" || "$OS_ID" == "centos" || "$OS_ID" == "rhel" ]]; then
        dnf install -y dnf-utils
        dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
        dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
    else
        curl -fsSL https://get.docker.com | bash
        apt-get install -y docker-compose-plugin
    fi
    systemctl enable docker
    systemctl start docker
    echo "[+] Docker installed successfully."
else
    echo "[+] Docker already installed."
fi

# Install Docker Compose standalone (fallback)
if ! command -v docker-compose &>/dev/null && ! docker compose version &>/dev/null 2>&1; then
    echo "[*] Installing Docker Compose standalone..."
    ARCH=$(uname -m)
    curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${ARCH}" \
        -o /usr/local/bin/docker-compose
    chmod +x /usr/local/bin/docker-compose
fi

# ----------------------------------------------------------
# 4. Install Python 3.11 (for non-Docker deployment)
# ----------------------------------------------------------
echo "[*] Installing Python 3.11..."
if ! command -v python3.11 &>/dev/null; then
    if [[ "$OS_ID" == "ol" || "$OS_ID" == "centos" || "$OS_ID" == "rhel" ]]; then
        dnf install -y python3.11 python3.11-pip python3.11-devel
    elif [[ "$OS_ID" == "ubuntu" || "$OS_ID" == "debian" ]]; then
        apt-get install -y software-properties-common
        add-apt-repository -y ppa:deadsnakes/ppa 2>/dev/null || true
        apt-get update
        apt-get install -y python3.11 python3.11-venv python3.11-dev
    fi
    echo "[+] Python 3.11 installed."
else
    echo "[+] Python 3.11 already installed."
fi

# ----------------------------------------------------------
# 5. Setup firewall rules
# ----------------------------------------------------------
echo "[*] Configuring firewall..."
if command -v firewall-cmd &>/dev/null; then
    firewall-cmd --permanent --add-port=${DASHBOARD_PORT}/tcp
    firewall-cmd --reload
    echo "[+] firewalld: port ${DASHBOARD_PORT}/tcp opened."
elif command -v ufw &>/dev/null; then
    ufw allow ${DASHBOARD_PORT}/tcp
    echo "[+] ufw: port ${DASHBOARD_PORT}/tcp opened."
else
    # Fall back to iptables
    iptables -I INPUT -p tcp --dport ${DASHBOARD_PORT} -j ACCEPT
    echo "[+] iptables: port ${DASHBOARD_PORT}/tcp opened."
fi

echo ""
echo "  IMPORTANT: You must also open port ${DASHBOARD_PORT} in the Oracle Cloud"
echo "  Console under Networking > Virtual Cloud Networks > Security Lists."
echo ""

# ----------------------------------------------------------
# 6. Create dedicated bot user
# ----------------------------------------------------------
echo "[*] Creating bot user '${BOT_USER}'..."
if ! id "${BOT_USER}" &>/dev/null; then
    useradd -r -m -d /home/${BOT_USER} -s /bin/bash "${BOT_USER}"
    usermod -aG docker "${BOT_USER}"
    echo "[+] User '${BOT_USER}' created and added to docker group."
else
    echo "[+] User '${BOT_USER}' already exists."
    usermod -aG docker "${BOT_USER}" 2>/dev/null || true
fi

# ----------------------------------------------------------
# 7. Setup directory structure
# ----------------------------------------------------------
echo "[*] Setting up project directory at ${PROJECT_DIR}..."
mkdir -p "${PROJECT_DIR}"/{config,data,logs,deploy}

# Copy project files if running from source directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "${SCRIPT_DIR}/main.py" ]; then
    echo "[*] Copying project files from ${SCRIPT_DIR}..."
    cp -r "${SCRIPT_DIR}"/* "${PROJECT_DIR}"/
    cp -r "${SCRIPT_DIR}"/.env.example "${PROJECT_DIR}"/ 2>/dev/null || true
fi

chown -R "${BOT_USER}:${BOT_USER}" "${PROJECT_DIR}"
chmod 750 "${PROJECT_DIR}"
chmod 640 "${PROJECT_DIR}"/.env 2>/dev/null || true

# ----------------------------------------------------------
# 8. Setup Python virtual environment
# ----------------------------------------------------------
echo "[*] Setting up Python virtual environment..."
if command -v python3.11 &>/dev/null; then
    sudo -u "${BOT_USER}" python3.11 -m venv "${PROJECT_DIR}/venv"
    sudo -u "${BOT_USER}" "${PROJECT_DIR}/venv/bin/pip" install --upgrade pip
    sudo -u "${BOT_USER}" "${PROJECT_DIR}/venv/bin/pip" install -r "${PROJECT_DIR}/requirements.txt"
    echo "[+] Virtual environment created and dependencies installed."
fi

# ----------------------------------------------------------
# 9. Install systemd service
# ----------------------------------------------------------
echo "[*] Installing systemd service..."
cp "${PROJECT_DIR}/deploy/cryptobot.service" /etc/systemd/system/cryptobot.service
systemctl daemon-reload
systemctl enable cryptobot
echo "[+] Systemd service installed and enabled."

# ----------------------------------------------------------
# 10. Final instructions
# ----------------------------------------------------------
echo ""
echo "============================================="
echo " Setup Complete!"
echo "============================================="
echo ""
echo " Next steps:"
echo ""
echo "   1. Copy your .env file:"
echo "      cp .env.example ${PROJECT_DIR}/.env"
echo "      nano ${PROJECT_DIR}/.env"
echo ""
echo "   2. Edit your config:"
echo "      nano ${PROJECT_DIR}/config/settings.yaml"
echo ""
echo "   3a. Start with Docker:"
echo "      cd ${PROJECT_DIR}"
echo "      sudo -u ${BOT_USER} docker compose up -d"
echo ""
echo "   3b. Or start with systemd (Python direct):"
echo "      sudo systemctl start cryptobot"
echo ""
echo "   4. Check status:"
echo "      sudo systemctl status cryptobot"
echo "      docker compose logs -f"
echo ""
echo "   5. Dashboard: http://<your-vm-ip>:${DASHBOARD_PORT}"
echo ""
echo "============================================="
