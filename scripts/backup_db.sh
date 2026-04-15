#!/bin/bash
# VN Edge: Nightly PostgreSQL backup
# Cron: 0 2 * * * /home/opc/crypto-trading-bot/scripts/backup_db.sh

set -e
BACKUP_DIR="/home/opc/db_backups"
mkdir -p "$BACKUP_DIR"

DATE=$(date +%Y%m%d_%H%M%S)
FILE="$BACKUP_DIR/vnedge_$DATE.sql.gz"

START=$(date +%s)
sudo -u postgres pg_dump vnedge | gzip > "$FILE"
SIZE=$(stat -c%s "$FILE")
DURATION=$(( $(date +%s) - START ))

# Log to backup_log table
sudo -u postgres psql -d vnedge -c "INSERT INTO backup_log (file_size, duration_s, success, note) VALUES ($SIZE, $DURATION, TRUE, '$FILE')" 2>/dev/null || true

# Keep only last 14 days
find "$BACKUP_DIR" -name "vnedge_*.sql.gz" -mtime +14 -delete

echo "Backup OK: $FILE ($(($SIZE/1024))KB in ${DURATION}s)"
