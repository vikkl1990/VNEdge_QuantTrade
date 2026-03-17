#!/usr/bin/env bash
# ==============================================================================
# Startup script for Crypto Trading Bot
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "[*] Starting Crypto Trading Bot..."
echo "[*] Project directory: ${PROJECT_DIR}"

# ----------------------------------------------------------
# 1. Check .env file exists
# ----------------------------------------------------------
if [ ! -f "${PROJECT_DIR}/.env" ]; then
    echo "ERROR: .env file not found at ${PROJECT_DIR}/.env"
    echo "       Copy .env.example to .env and configure your API keys."
    echo "       cp ${PROJECT_DIR}/.env.example ${PROJECT_DIR}/.env"
    exit 1
fi
echo "[+] .env file found."

# ----------------------------------------------------------
# 2. Check config exists
# ----------------------------------------------------------
if [ ! -f "${PROJECT_DIR}/config/settings.yaml" ]; then
    echo "ERROR: Configuration file not found at ${PROJECT_DIR}/config/settings.yaml"
    echo "       Please create your configuration file before starting the bot."
    exit 1
fi
echo "[+] Configuration file found."

# ----------------------------------------------------------
# 3. Create log and data directories
# ----------------------------------------------------------
mkdir -p "${PROJECT_DIR}/logs"
mkdir -p "${PROJECT_DIR}/data"
echo "[+] Log and data directories ready."

# ----------------------------------------------------------
# 4. Activate virtual environment if it exists
# ----------------------------------------------------------
if [ -d "${PROJECT_DIR}/venv" ]; then
    echo "[*] Activating virtual environment..."
    source "${PROJECT_DIR}/venv/bin/activate"
    echo "[+] Virtual environment activated."
elif [ -d "${PROJECT_DIR}/.venv" ]; then
    echo "[*] Activating virtual environment (.venv)..."
    source "${PROJECT_DIR}/.venv/bin/activate"
    echo "[+] Virtual environment activated."
else
    echo "[!] No virtual environment found. Using system Python."
fi

# ----------------------------------------------------------
# 5. Start the bot
# ----------------------------------------------------------
cd "${PROJECT_DIR}"

echo "[*] Launching bot..."
echo "============================================="

exec python main.py "$@"
