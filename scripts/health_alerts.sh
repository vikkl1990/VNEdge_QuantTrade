#!/bin/bash
# VN Edge: Health monitoring alerts via Telegram
# Cron: */5 * * * * /home/opc/crypto-trading-bot/scripts/health_alerts.sh

set -e
TG_TOKEN=$(grep TELEGRAM_BOT_TOKEN /home/opc/crypto-trading-bot/.env 2>/dev/null | cut -d= -f2)
TG_CHAT=$(grep TELEGRAM_CHAT_ID /home/opc/crypto-trading-bot/.env 2>/dev/null | cut -d= -f2)
[ -z "$TG_TOKEN" ] && exit 0

send_alert() {
    curl -s "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
        -d "chat_id=${TG_CHAT}" -d "text=🚨 VN Edge Alert: $1" -d "parse_mode=HTML" >/dev/null
}

# 1. Bot service running?
if ! systemctl is-active --quiet cryptobot; then
    send_alert "<b>cryptobot service is DOWN</b>"
    exit 0
fi

# 2. PostgreSQL running?
if ! systemctl is-active --quiet postgresql; then
    send_alert "<b>PostgreSQL is DOWN</b>"
fi

# 3. Disk space < 10%?
DISK=$(df / | tail -1 | awk '{print $5}' | tr -d '%')
if [ "$DISK" -gt 90 ]; then
    send_alert "<b>Disk space critical: ${DISK}% used</b>"
fi

# 4. Recent errors in bot logs (last 5 min)?
ERR_COUNT=$(journalctl -u cryptobot --since '5 min ago' --no-pager | grep -c 'ERROR\|CRITICAL' || true)
if [ "$ERR_COUNT" -gt 20 ]; then
    send_alert "<b>${ERR_COUNT} errors in last 5min</b>"
fi
