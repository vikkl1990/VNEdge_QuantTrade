# Oracle Cloud Deployment Guide - Crypto Trading Bot

## 1. Create an Oracle Cloud Free Tier VM

### Sign Up
1. Go to https://cloud.oracle.com and create a free tier account.
2. You get an Always Free ARM Ampere A1 instance (up to 4 OCPUs, 24 GB RAM).

### Create the VM
1. Navigate to **Compute > Instances > Create Instance**.
2. Configure:
   - **Name**: `crypto-trading-bot`
   - **Image**: Oracle Linux 8 (or Ubuntu 22.04)
   - **Shape**: VM.Standard.A1.Flex (ARM) -- select 1 OCPU, 6 GB RAM (free tier)
   - **Networking**: Use default VCN or create a new one
   - **Add SSH keys**: Upload your public key (`~/.ssh/id_rsa.pub`)
3. Click **Create** and wait for the instance to start.
4. Note the **Public IP Address** from the instance details.

---

## 2. SSH Setup

### Connect to Your VM
```bash
ssh -i ~/.ssh/id_rsa opc@<PUBLIC_IP>        # Oracle Linux
ssh -i ~/.ssh/id_rsa ubuntu@<PUBLIC_IP>      # Ubuntu
```

### Harden SSH (recommended)
```bash
sudo nano /etc/ssh/sshd_config
# Set: PasswordAuthentication no
# Set: PermitRootLogin no
sudo systemctl restart sshd
```

---

## 3. Security List / Firewall Rules

### Oracle Cloud Console (required)
1. Go to **Networking > Virtual Cloud Networks**.
2. Click your VCN, then click the **subnet**.
3. Click the **Security List** (Default Security List).
4. **Add Ingress Rule**:
   - Source CIDR: `0.0.0.0/0` (or restrict to your IP)
   - Destination Port Range: `8080`
   - Protocol: TCP
   - Description: `Trading Bot Dashboard`

### VM Firewall
The setup script handles this automatically. To do it manually:

**Oracle Linux (firewalld)**:
```bash
sudo firewall-cmd --permanent --add-port=8080/tcp
sudo firewall-cmd --reload
```

**Ubuntu (ufw)**:
```bash
sudo ufw allow 8080/tcp
```

---

## 4. Deploy with Docker (Recommended)

### Run Setup Script
```bash
# Upload or clone your project to the VM
git clone <your-repo-url> /tmp/crypto-trading-bot
cd /tmp/crypto-trading-bot
sudo bash deploy/oracle_setup.sh
```

### Configure
```bash
cd /opt/crypto-trading-bot
sudo -u cryptobot cp .env.example .env
sudo -u cryptobot nano .env
# Fill in your exchange API keys and other settings
```

### Start with Docker Compose
```bash
cd /opt/crypto-trading-bot
sudo -u cryptobot docker compose up -d
```

### Manage
```bash
# View logs
docker compose logs -f

# Restart
docker compose restart

# Stop
docker compose down

# Rebuild after code changes
docker compose up -d --build
```

---

## 5. Deploy with Python Directly (Alternative)

### Run Setup Script
The setup script installs Python 3.11 and creates a virtual environment automatically:
```bash
sudo bash deploy/oracle_setup.sh
```

### Configure
```bash
cd /opt/crypto-trading-bot
sudo -u cryptobot cp .env.example .env
sudo -u cryptobot nano .env
```

### Start with systemd
```bash
sudo systemctl start cryptobot
sudo systemctl status cryptobot
```

### Manage
```bash
# View logs
sudo journalctl -u cryptobot -f

# Restart
sudo systemctl restart cryptobot

# Stop
sudo systemctl stop cryptobot

# Disable auto-start
sudo systemctl disable cryptobot
```

---

## 6. Telegram Bot Setup (Optional)

If your bot supports Telegram notifications:

1. Open Telegram and message **@BotFather**.
2. Send `/newbot` and follow prompts to create a bot.
3. Copy the **Bot Token** provided.
4. Get your Chat ID: message **@userinfobot** and note the `Id` value.
5. Add to `.env`:
   ```
   TELEGRAM_BOT_TOKEN=your_bot_token_here
   TELEGRAM_CHAT_ID=your_chat_id_here
   ```
6. Restart the bot:
   ```bash
   sudo systemctl restart cryptobot
   # or
   docker compose restart
   ```

---

## 7. Monitoring and Maintenance

### Dashboard
Access the web dashboard at:
```
http://<PUBLIC_IP>:8080
```

### Log Rotation
Docker handles log rotation via the `json-file` driver configured in `docker-compose.yml`.

For systemd, logs are managed by journald. To limit disk usage:
```bash
sudo journalctl --vacuum-size=500M
```

### Updates
```bash
cd /opt/crypto-trading-bot

# Pull latest code
git pull origin main

# Docker
docker compose down
docker compose up -d --build

# Systemd
sudo systemctl stop cryptobot
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl start cryptobot
```

### Backups
Back up your data and configuration regularly:
```bash
tar -czf ~/cryptobot-backup-$(date +%Y%m%d).tar.gz \
    /opt/crypto-trading-bot/.env \
    /opt/crypto-trading-bot/config/ \
    /opt/crypto-trading-bot/data/
```

---

## 8. Common Troubleshooting

### Bot won't start
```bash
# Check logs
sudo journalctl -u cryptobot -n 50 --no-pager
docker compose logs --tail 50

# Verify .env exists and has correct keys
cat /opt/crypto-trading-bot/.env

# Check Python can import dependencies
/opt/crypto-trading-bot/venv/bin/python -c "import ccxt; print(ccxt.__version__)"
```

### Cannot access dashboard
1. Confirm the bot is running and listening on port 8080.
2. Check the Oracle Cloud Security List has an ingress rule for port 8080.
3. Check the VM firewall allows port 8080:
   ```bash
   sudo firewall-cmd --list-ports    # Oracle Linux
   sudo ufw status                   # Ubuntu
   ```
4. Test locally on the VM:
   ```bash
   curl http://localhost:8080
   ```

### High memory usage
- Reduce trading pairs in `config/settings.yaml`.
- Lower the number of candles kept in memory.
- Check Docker resource limits in `docker-compose.yml`.

### Exchange API errors
- Verify API keys in `.env` are correct and have trading permissions.
- Check if your IP is whitelisted on the exchange.
- Some exchanges require specific API key permissions (e.g., spot trading enabled).

### Permission denied errors
```bash
sudo chown -R cryptobot:cryptobot /opt/crypto-trading-bot
sudo chmod 750 /opt/crypto-trading-bot
sudo chmod 640 /opt/crypto-trading-bot/.env
```

### systemd service fails repeatedly
```bash
# Check start limit
sudo systemctl reset-failed cryptobot
sudo systemctl start cryptobot

# View full error
sudo journalctl -u cryptobot -e
```
